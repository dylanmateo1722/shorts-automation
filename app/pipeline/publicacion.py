"""La puerta que hay que cruzar antes de contactar con YouTube.

Ninguna llamada al proveedor ocurre sin pasar por aquí. Si algo no se cumple, el
trabajo se queda en ``NOT_READY`` y **YouTube no se contacta en absoluto**: no se
abre sesión, no se pide token y no se envía un byte. Un fallo de precondición no
es un fallo de subida, y confundirlos haría creer que hubo trato con el proveedor
cuando no lo hubo.

Qué se comprueba, y por qué cada cosa:

* **Existe un ``RenderResult``** y declara éxito. Sin vídeo no hay nada que subir.
* **La QA técnica pasó.** ``fail`` bloquea. ``pass_with_warnings`` también pasa:
  los avisos existen para ser leídos, no para bloquear.
* **El MP4 existe en disco y su ruta es válida.** Que el artefacto declare una
  ruta no es evidencia de que haya un archivo en ella.
* **La procedencia autoriza todo lo que entró al vídeo.** Se comprueban los
  recursos que declara ``RenderJob.assets_de_render`` —narración, material
  visual, subtítulos y rótulo—, que son las **entradas** del render. Un recurso
  ``reference_only`` queda fuera aunque el archivo esté ahí y la QA haya pasado.
* **La integridad cuadra.** El SHA-256 del archivo en disco tiene que coincidir
  con el que el ``RenderResult`` registró. Si no coincide, el vídeo que hay no es
  el que se validó.

Sobre qué autoriza el ledger, que es donde es fácil equivocarse
--------------------------------------------------------------

``ProvenanceLedger`` clasifica **entradas al render**: de dónde viene cada cosa
que acaba dentro del vídeo y qué permite hacer con ella. El MP4 final es el
**producto** de esa cadena, no un eslabón: lo genera el pipeline a partir de
recursos ya autorizados, y no tiene —ni debe tener— una ``LicenseDecision``
propia.

Preguntarle al ledger por ``final/short.mp4`` devuelve «sin decisión», que es la
respuesta correcta a una pregunta equivocada. Lo que hay que preguntar es si
``RenderJob.assets_de_render`` está autorizado; el producto se comprueba de otra
manera, por existencia, tamaño e integridad contra la huella que registró la
composición.

Añadir el MP4 final al ledger para que la pregunta encajara sería justamente el
apaño que rompería la separación: convertiría un producto en una fuente y haría
que el registro dejara de significar lo que dice.


Lo que este módulo **no** hace: juzgar si la pieza es publicable en sentido
editorial o legal. Eso vive en ``QAResult.editorial_legal_assessment``, no se
automatiza, y su estado por defecto es ``NOT_ASSESSED``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from app.core.errors import EntradaInvalida, ProcedenciaInvalida
from app.contracts.models import (
    EstadoQA,
    EstadoRender,
    ProvenanceLedger,
    PublishMetadata,
    QAResult,
    RenderJob,
    RenderResult,
)
from app.core.workspace import Workspace
# La puerta de procedencia del render es la autoridad, y se reutiliza en vez de
# reescribirse: dos implementaciones de la misma regla acaban divergiendo, y la
# que se quede atrás será la que deje pasar algo.
from app.pipeline.render import verificar_procedencia

#: Tamaño de lectura al calcular la huella. Un MP4 no cabe en memoria de golpe.
BLOQUE_HASH = 1024 * 1024


def sha256_de_archivo(ruta: Path, *, bloque: int = BLOQUE_HASH) -> str:
    """SHA-256 del contenido del archivo, leído por bloques."""
    digest = hashlib.sha256()
    with ruta.open("rb") as archivo:
        for trozo in iter(lambda: archivo.read(bloque), b""):
            digest.update(trozo)
    return digest.hexdigest()


@dataclass(frozen=True)
class ResultadoGate:
    """Si se puede publicar, y si no, por qué no.

    ``motivos`` se acumula: se comprueban todas las condiciones y se informan
    todas las que fallan. Devolver solo la primera obligaría a arreglarlas de una
    en una, con una corrida por cada.
    """

    listo: bool
    motivos: list[str] = field(default_factory=list)
    video_path: Path | None = None
    video_sha256: str | None = None
    total_bytes: int = 0

    def __post_init__(self) -> None:
        """Rechaza los gates que afirman lo que no pueden respaldar.

        Un ``listo=True`` sin vídeo, sin huella o sin tamaño es un estado
        imposible: dice «se puede publicar» y no trae con qué. Se valida en la
        construcción, así que un gate inválido **no llega a existir** y el
        publisher no tiene que defenderse de él con una comprobación que las
        optimizaciones de Python puedan borrar.

        Un ``listo=False`` sin motivos es el mismo error por el otro lado: diría
        que no se puede publicar sin decir por qué.
        """
        if self.listo:
            if self.motivos:
                raise EntradaInvalida(
                    "un gate satisfecho no puede traer motivos de rechazo: "
                    f"{'; '.join(self.motivos)}"
                )
            if self.video_path is None:
                raise EntradaInvalida(
                    "el gate declara que se puede publicar y no trae la ruta del "
                    "vídeo"
                )
            if not (self.video_sha256 or "").strip():
                raise EntradaInvalida(
                    "el gate declara que se puede publicar y no trae el SHA-256 "
                    "del vídeo: sin él no hay integridad que comprobar"
                )
            if self.total_bytes <= 0:
                raise EntradaInvalida(
                    f"el gate declara que se puede publicar y el vídeo mide "
                    f"{self.total_bytes} bytes"
                )
        elif not self.motivos:
            raise EntradaInvalida(
                "un gate no satisfecho tiene que decir por qué no lo está"
            )

    @property
    def resumen(self) -> str:
        if self.listo:
            return "precondiciones satisfechas"
        return "; ".join(self.motivos)


def evaluar_precondiciones(
    *,
    render_result: RenderResult | None,
    render_job: RenderJob | None,
    qa_result: QAResult | None,
    ledger: ProvenanceLedger | None,
    metadata: PublishMetadata | None,
    directorio_corrida: Path,
) -> ResultadoGate:
    """Decide si esta corrida puede llegar a YouTube.

    Devuelve siempre un ``ResultadoGate``; no levanta excepciones por una
    precondición incumplida, porque no cumplirlas es un desenlace previsto y no
    un error del programa.

    ``render_job`` es obligatorio y no tiene valor por defecto: dice qué entró
    de verdad al vídeo, y sin él no hay nada cuya procedencia comprobar.
    Dejarlo opcional permitiría que un llamador lo omitiera y se saltara la
    puerta sin enterarse, que es exactamente la clase de omisión silenciosa que
    esta puerta existe para impedir.
    """
    motivos: list[str] = []

    # --- El vídeo ----------------------------------------------------------
    if render_result is None:
        motivos.append("no hay RenderResult: no existe vídeo que publicar")
    elif render_result.status is not EstadoRender.exito:
        motivos.append(
            f"el RenderResult declara {render_result.status.value!r}: no se "
            f"publica un render que no tuvo éxito"
        )

    # --- La QA técnica ------------------------------------------------------
    if qa_result is None:
        motivos.append("no hay QAResult: no consta que el vídeo se comprobara")
    else:
        if qa_result.status is EstadoQA.reprobado or not qa_result.technical_qa_ok:
            motivos.append(
                f"la QA técnica está en {qa_result.status.value!r}: no se publica "
                f"un vídeo que no la pasó"
            )

    # --- La divulgación de IA ----------------------------------------------
    if metadata is None:
        motivos.append("no hay PublishMetadata: no consta qué se quiere publicar")
    elif not metadata.permite_publicacion_automatica:
        motivos.append(
            "la divulgación de IA está en 'requires_current_verification': la "
            "publicación automática queda bloqueada hasta que una persona "
            "verifique la situación"
        )

    # --- El archivo ---------------------------------------------------------
    ruta_relativa = None
    if render_result is not None:
        # Lo que se publica es ``output_path``, y **solo** ``output_path``.
        #
        # En el artefacto de la etapa de composición —``final_video.json``, el
        # que interesa aquí— los dos campos significan esto:
        #
        #   output_path    final/short.mp4   el vídeo compuesto, con subtítulos
        #                                    y rótulo. Es el que se publica.
        #   combined_path  render/final.mp4  el intermedio crudo del motor, que
        #                                    queda referenciado para diagnosticar.
        #
        # ``sha256`` es la huella de ``output_path``, medida sobre el archivo
        # compuesto. Preferir ``combined_path`` publicaría el vídeo sin
        # subtítulos ni rótulo, y además su huella no cuadraría con la
        # registrada. No hay caso en que el intermedio sea lo publicable, así
        # que no hay respaldo: si no hay ``output_path``, no hay nada que subir.
        ruta_relativa = render_result.output_path
    if render_result is not None and not ruta_relativa:
        motivos.append("el RenderResult no declara la ruta del vídeo")

    video_path: Path | None = None
    video_sha256: str | None = None
    total_bytes = 0

    if ruta_relativa:
        video_path = directorio_corrida / ruta_relativa
        if not video_path.is_file():
            motivos.append(
                f"el vídeo declarado no existe en disco: {ruta_relativa!r}"
            )
            video_path = None
        else:
            total_bytes = video_path.stat().st_size
            if total_bytes == 0:
                motivos.append(f"el vídeo {ruta_relativa!r} está vacío")

    # --- La procedencia de lo que ENTRÓ al vídeo ----------------------------
    #
    # Se pregunta por las entradas del render, no por el MP4 final. El ledger
    # clasifica de dónde viene cada cosa que acaba dentro del vídeo; el vídeo en
    # sí es el producto de esa cadena y no lleva decisión propia.
    if ledger is None:
        motivos.append(
            "no hay ProvenanceLedger: no consta la procedencia de lo que entró "
            "al vídeo"
        )
    if render_job is None:
        motivos.append(
            "no hay RenderJob: no consta qué recursos entraron al vídeo, así que "
            "no hay nada cuya procedencia se pueda comprobar"
        )
    elif ledger is not None:
        rutas = render_job.assets_de_render
        if not rutas:
            motivos.append(
                "el RenderJob no declara ningún recurso: un vídeo sin entradas "
                "no es comprobable"
            )
        else:
            # Misma autoridad que la puerta previa al motor: declarado, decidido,
            # 'render_allowed', el archivo existe y su huella sigue siendo la que
            # se autorizó. Reutilizarla evita que dos copias de la regla
            # diverjan; convierte su excepción en un motivo porque aquí las
            # precondiciones se acumulan en vez de interrumpir.
            try:
                verificar_procedencia(
                    Workspace.en_directorio(directorio_corrida, render_job.run_id),
                    ledger,
                    rutas,
                    etapa="publication_gate",
                )
            except ProcedenciaInvalida as exc:
                motivos.append(str(exc))

    # --- La integridad ------------------------------------------------------
    if video_path is not None and render_result is not None:
        if not render_result.sha256:
            motivos.append(
                "el RenderResult no registra el SHA-256 del vídeo: no hay contra "
                "qué comprobar la integridad"
            )
        else:
            video_sha256 = sha256_de_archivo(video_path)
            if video_sha256 != render_result.sha256:
                motivos.append(
                    "INTEGRITY_MISMATCH: el SHA-256 del archivo en disco no "
                    "coincide con el que registró el RenderResult; el vídeo que "
                    "hay no es el que se validó"
                )

    listo = not motivos
    return ResultadoGate(
        listo=listo,
        motivos=motivos,
        video_path=video_path if listo else None,
        video_sha256=video_sha256 if listo else None,
        total_bytes=total_bytes if listo else 0,
    )
