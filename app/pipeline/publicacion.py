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
* **La procedencia autoriza el render de ese archivo.** Se consulta el ledger, que
  es la autoridad: ``permite_render`` solo devuelve cierto para
  ``render_allowed``. Un recurso ``reference_only`` queda fuera aunque el archivo
  esté ahí y la QA haya pasado.
* **La integridad cuadra.** El SHA-256 del archivo en disco tiene que coincidir
  con el que el ``RenderResult`` registró. Si no coincide, el vídeo que hay no es
  el que se validó.

Lo que este módulo **no** hace: juzgar si la pieza es publicable en sentido
editorial o legal. Eso vive en ``QAResult.editorial_legal_assessment``, no se
automatiza, y su estado por defecto es ``NOT_ASSESSED``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from app.contracts.models import (
    EstadoQA,
    EstadoRender,
    ProvenanceLedger,
    PublishMetadata,
    QAResult,
    RenderResult,
)

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

    @property
    def resumen(self) -> str:
        if self.listo:
            return "precondiciones satisfechas"
        return "; ".join(self.motivos)


def evaluar_precondiciones(
    *,
    render_result: RenderResult | None,
    qa_result: QAResult | None,
    ledger: ProvenanceLedger | None,
    metadata: PublishMetadata | None,
    directorio_corrida: Path,
) -> ResultadoGate:
    """Decide si esta corrida puede llegar a YouTube.

    Devuelve siempre un ``ResultadoGate``; no levanta excepciones por una
    precondición incumplida, porque no cumplirlas es un desenlace previsto y no
    un error del programa.
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
        # El vídeo final compuesto es el que se publica; el intermedio del motor
        # no. Si no hay compuesto, se cae al de salida y se dice cuál se usó.
        ruta_relativa = render_result.combined_path or render_result.output_path
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

    # --- La procedencia -----------------------------------------------------
    if ledger is None:
        motivos.append("no hay ProvenanceLedger: no consta la procedencia del vídeo")
    elif ruta_relativa:
        if not ledger.permite_render(ruta_relativa):
            clase = ledger.clase(ruta_relativa)
            detalle = clase.value if clase is not None else "sin decisión"
            motivos.append(
                f"la procedencia de {ruta_relativa!r} no autoriza el render "
                f"({detalle}); solo 'render_allowed' puede publicarse"
            )

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
