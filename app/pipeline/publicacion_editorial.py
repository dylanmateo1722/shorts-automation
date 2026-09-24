"""Gate 7.4-B: publicación privada de contenido editorial real.

Qué añade este módulo sobre lo que ya existía
---------------------------------------------

Gate 7.2 dejó el publisher, el resumable upload y la reconciliación. Gate 7.3
los ejercitó contra YouTube de verdad. Gate 7.4-A hizo que material visual real,
declarado y elegido a mano, llegara al vídeo pasando por la procedencia. Lo que
faltaba no era publicar: era **decir qué se publica y con qué respaldo**.

Este módulo es esa capa de entrada, y solo eso:

* **La metadata editorial se declara en un fichero JSON** y se valida contra
  ``MetadataEditorial`` antes de tocar la red. No hay metadata escrita a mano en
  el código de ningún script, y ``made_for_kids``, ``ai_disclosure`` y
  ``contains_synthetic_media`` no se derivan de nada: se declaran o no se publica.
* **La aprobación editorial y legal humana se declara en otro fichero**, aparte,
  y se valida contra ``AprobacionEditorial``. Nadie edita a mano un artefacto que
  produjo la QA.
* **La puerta se compone, no se reescribe.** ``evaluar_precondiciones_editoriales``
  llama a la de Gate 7.2 —misma autoridad, mismas comprobaciones de vídeo, QA
  técnica, procedencia e integridad— y le añade las de Gate 7.4-B. La puerta
  original no se toca, así que lo que ya estaba verificado sigue estándolo.
* **Los artefactos pertenecen a la corrida**, bajo
  ``runs/<run_id>/publication/``.

Lo que este módulo **no** hace, y por qué
-----------------------------------------

**No convierte la publicación en una etapa del ``StageRunner``.** La idempotencia
del runner es «si el artefacto sigue siendo válido y vigente, se omite; si no, se
re-ejecuta», y re-ejecutar una publicación es subir otra vez. Bajo esa semántica
``--force publication`` sería un botón de «súbelo de nuevo», y no existe ninguna
forma de escribirlo que no lo sea. Así que publicar es una acción explícita, con
su propio subcomando y su propia confirmación, fuera del mecanismo de rerun. Un
``run --force`` no puede alcanzar este código: no hay ninguna etapa registrada que
lo invoque.

**No implementa la salida de ``NEEDS_REVIEW``.** La transición
``NEEDS_REVIEW → VERIFYING`` existe en el contrato y sigue sin tener camino
programático, a propósito: ``UploadSession.session_url`` es memoria-only, no hay
reanudación entre procesos, y un automatismo que se equivoque al decidir que no
se subió nada produce un duplicado. Sale de ``NEEDS_REVIEW`` una persona que mira
el canal. Queda como trabajo posterior, y **no** se resuelve con reruns.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from app.adapters.youtube.auth import TokenAcceso
from app.adapters.youtube.publisher import ResultadoPublicacion, publicar
from app.adapters.youtube.upload import (
    TAMANO_FRAGMENTO_BYTES,
    TIMEOUT_POR_DEFECTO_S,
    Transporte,
)
from app.contracts.models import (
    AprobacionEditorial,
    EstadoEvaluacion,
    EstadoPublicacion,
    MetadataEditorial,
    ProvenanceLedger,
    PublishJob,
    PublishResult,
    QAResult,
    RenderJob,
    RenderResult,
    clave_idempotencia,
)
from app.core.errors import EntradaInvalida
from app.core.workspace import Workspace
from app.pipeline.publicacion import ResultadoGate, evaluar_precondiciones

#: Subdirectorio de la corrida donde vive todo lo de publicación. Los artefactos
#: pertenecen al run: sin esto, saber qué se publicó de una corrida obligaría a
#: buscar en un directorio suelto que nada ata a ella.
DIRECTORIO_PUBLICACION = "publication"

#: Nombres de artefacto, ya con el subdirectorio. ``Workspace.escribir_artefacto``
#: crea los padres que falten, así que no hace falta preparar el directorio.
ARTEFACTO_METADATA = f"{DIRECTORIO_PUBLICACION}/publish_metadata"
ARTEFACTO_APROBACION = f"{DIRECTORIO_PUBLICACION}/editorial_approval"
ARTEFACTO_JOB = f"{DIRECTORIO_PUBLICACION}/publish_job"
ARTEFACTO_RESULTADO = f"{DIRECTORIO_PUBLICACION}/publish_result"

#: Artefactos de la corrida que hacen falta para decidir si se puede publicar.
ARTEFACTO_RENDER_RESULT = "final_video"
ARTEFACTO_RENDER_JOB = "render_job"
ARTEFACTO_QA = "final_video_qa"
ARTEFACTO_PROCEDENCIA = "provenance_ledger"


# ---------------------------------------------------------------------------
# Las dos declaraciones
# ---------------------------------------------------------------------------


def _leer_declaracion(ruta: Path, modelo, que: str):
    """Lee un fichero declarativo y lo valida contra su contrato.

    Se separa de ``Workspace.leer_artefacto`` a propósito: esto **no** es un
    artefacto de la corrida sino un documento que escribe una persona, puede
    estar en cualquier sitio, y el error tiene que decirle dónde se equivocó.
    """
    ruta = Path(ruta)
    if not ruta.is_file():
        raise EntradaInvalida(f"no existe el fichero de {que}: {ruta}")
    try:
        texto = ruta.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EntradaInvalida(f"no se pudo leer {ruta}: {exc}") from exc
    try:
        return modelo.model_validate_json(texto)
    except ValidationError as exc:
        # Pydantic también informa el JSON mal formado como ``ValidationError``,
        # así que no hace falta cazar ``JSONDecodeError`` aparte.
        raise EntradaInvalida(
            f"el fichero de {que} ({ruta}) no cumple su contrato: {exc}"
        ) from exc


def cargar_metadata_editorial(ruta: Path, *, run_id: UUID) -> MetadataEditorial:
    """La metadata declarada, validada y atada a esta corrida.

    ``MetadataEditorial`` ya rechaza por contrato una privacidad distinta de
    ``private`` y una declaración de medios sintéticos ausente, así que una
    solicitud inválida **no llega a existir como objeto** y no hay camino desde
    aquí hasta abrir una sesión de subida.

    Lo que se comprueba además es el ``run_id``: una metadata que declare otra
    corrida no es la de esta, y publicar con ella ataría el resultado a un vídeo
    que nadie describió.
    """
    metadata = _leer_declaracion(ruta, MetadataEditorial, "metadata editorial")
    if metadata.run_id != run_id:
        raise EntradaInvalida(
            f"la metadata declara la corrida {metadata.run_id} y se está "
            f"publicando la {run_id}"
        )
    return metadata


def cargar_aprobacion_editorial(ruta: Path, *, run_id: UUID) -> AprobacionEditorial:
    """La aprobación editorial y legal declarada, atada a esta corrida.

    No se comprueba aquí si aprueba: eso lo decide la puerta, junto con el resto
    de precondiciones, para que un rechazo se informe con todos los motivos y no
    de uno en uno.
    """
    aprobacion = _leer_declaracion(
        ruta, AprobacionEditorial, "aprobación editorial"
    )
    if aprobacion.run_id != run_id:
        raise EntradaInvalida(
            f"la aprobación editorial declara la corrida {aprobacion.run_id} y se "
            f"está publicando la {run_id}"
        )
    return aprobacion


# ---------------------------------------------------------------------------
# La puerta editorial
# ---------------------------------------------------------------------------


def motivos_editoriales(
    aprobacion: AprobacionEditorial | None,
    *,
    render_result: RenderResult | None,
    render_job: RenderJob | None,
    qa_result: QAResult | None,
) -> list[str]:
    """Lo que Gate 7.4-B exige además de la puerta de Gate 7.2.

    Devuelve la lista de motivos por los que **no** se puede publicar contenido
    editorial. Vacía significa que la parte editorial está en orden; no dice nada
    del resto de precondiciones.
    """
    motivos: list[str] = []

    if aprobacion is None:
        motivos.append(
            "no hay aprobación editorial: sin una decisión humana declarada no se "
            "publica contenido editorial real"
        )
    elif not aprobacion.aprobado:
        motivos.append(
            f"la aprobación editorial está en {aprobacion.status.value!r} y solo "
            f"{EstadoEvaluacion.aprobado_por_humano.value!r} autoriza a publicar"
        )

    # El vídeo exacto que se aprobó. Se compara contra la huella que registró el
    # RenderResult, y la puerta de Gate 7.2 ya comprueba que el archivo en disco
    # sigue siendo el de esa huella. Las dos juntas atan la aprobación a los bytes
    # que se van a subir: si se vuelve a renderizar, la huella cambia y esta
    # aprobación deja de valer, que es lo correcto —nadie aprobó el vídeo nuevo—.
    if aprobacion is not None and render_result is not None:
        if aprobacion.video_sha256 != render_result.sha256:
            motivos.append(
                "la aprobación editorial es de otro vídeo: aprueba la huella "
                f"{aprobacion.video_sha256[:12]}… y el RenderResult registra "
                f"{(render_result.sha256 or '')[:12]}…"
            )

    # El material exacto que entró. Se comparan las listas en orden porque el orden
    # del material determina el vídeo: aprobar los mismos archivos en otra
    # secuencia es aprobar otra pieza.
    if aprobacion is not None and render_job is not None:
        if list(aprobacion.materials) != list(render_job.materials):
            motivos.append(
                "la aprobación editorial es de otro material: aprueba "
                f"{list(aprobacion.materials)!r} y el RenderJob declara "
                f"{list(render_job.materials)!r}"
            )

    # Un rechazo registrado en la QA gana sobre cualquier aprobación declarada
    # aparte. El pipeline nunca escribe este valor —solo pone NOT_ASSESSED—, así
    # que si está ahí lo puso una persona, y una persona que rechazó no queda
    # anulada por otro documento.
    if (
        qa_result is not None
        and qa_result.editorial_legal_assessment.status
        is EstadoEvaluacion.rechazado_por_humano
    ):
        motivos.append(
            "la QA registra un rechazo editorial humano "
            f"({EstadoEvaluacion.rechazado_por_humano.value!r}): un rechazo "
            "registrado no lo levanta una aprobación declarada aparte"
        )

    return motivos


def evaluar_precondiciones_editoriales(
    *,
    render_result: RenderResult | None,
    render_job: RenderJob | None,
    qa_result: QAResult | None,
    ledger: ProvenanceLedger | None,
    metadata: MetadataEditorial | None,
    aprobacion: AprobacionEditorial | None,
    directorio_corrida: Path,
) -> ResultadoGate:
    """La puerta de Gate 7.2 más lo que exige Gate 7.4-B.

    No reimplementa nada de la puerta original: la llama. Si algún día cambia una
    de sus reglas, cambia también para este flujo, que es justo lo que se quiere;
    dos copias de la misma regla acaban divergiendo y la que se quede atrás será
    la que deje pasar algo.

    Los motivos se acumulan igual que en la puerta base: se informan todos los que
    fallan, no el primero.
    """
    base = evaluar_precondiciones(
        render_result=render_result,
        render_job=render_job,
        qa_result=qa_result,
        ledger=ledger,
        metadata=metadata,
        directorio_corrida=directorio_corrida,
    )
    editoriales = motivos_editoriales(
        aprobacion,
        render_result=render_result,
        render_job=render_job,
        qa_result=qa_result,
    )
    if not editoriales:
        return base

    # Hay motivos editoriales, así que el gate no está satisfecho pase lo que pase
    # en la parte técnica. Se reconstruye en vez de mutarse porque ``ResultadoGate``
    # es inmutable y valida en la construcción: un gate inconsistente no llega a
    # existir.
    return ResultadoGate(listo=False, motivos=base.motivos + editoriales)


# ---------------------------------------------------------------------------
# Preparar y publicar
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preparacion:
    """Todo lo que hace falta para publicar, ya leído, validado y juzgado.

    Se separa de la publicación a propósito: preparar **no toca la red**, ni
    siquiera para pedir un token. Así una solicitud inválida se para aquí, y el
    hecho de que no haya habido trato con el proveedor no depende de que alguien
    se acordara de comprobar el gate antes de llamar.
    """

    workspace: Workspace
    gate: ResultadoGate
    metadata: MetadataEditorial
    aprobacion: AprobacionEditorial
    job: PublishJob
    render_job: RenderJob
    render_result: RenderResult


def preparar(
    *,
    run_id: UUID,
    directorio_corrida: Path,
    ruta_metadata: Path,
    ruta_aprobacion: Path,
) -> Preparacion:
    """Lee la corrida y las dos declaraciones, y evalúa la puerta. Sin red.

    Persiste la metadata y la aprobación validadas, y el ``PublishJob`` inicial,
    bajo ``runs/<run_id>/publication/``. Se persisten también cuando la puerta
    rechaza: que un intento no llegara a publicar no lo hace menos digno de
    quedar registrado, y sin ese registro nadie sabría después qué se propuso.

    Raises:
        ArtefactoCorrupto: si falta un artefacto de la corrida o no cumple su
            contrato.
        EntradaInvalida: si una declaración falta, no valida o es de otra corrida.
    """
    directorio = Path(directorio_corrida)
    if not directorio.is_dir():
        raise EntradaInvalida(f"no existe el directorio de la corrida: {directorio}")
    workspace = Workspace.en_directorio(directorio, run_id)

    render_result = workspace.leer_artefacto(ARTEFACTO_RENDER_RESULT, RenderResult)
    # El job dice qué entró al vídeo. Es lo que necesita la procedencia, y también
    # a lo que se ata la aprobación editorial.
    render_job = workspace.leer_artefacto(ARTEFACTO_RENDER_JOB, RenderJob)
    qa_result = workspace.leer_artefacto(ARTEFACTO_QA, QAResult)
    ledger = workspace.leer_artefacto(ARTEFACTO_PROCEDENCIA, ProvenanceLedger)

    metadata = cargar_metadata_editorial(ruta_metadata, run_id=run_id)
    aprobacion = cargar_aprobacion_editorial(ruta_aprobacion, run_id=run_id)

    gate = evaluar_precondiciones_editoriales(
        render_result=render_result,
        render_job=render_job,
        qa_result=qa_result,
        ledger=ledger,
        metadata=metadata,
        aprobacion=aprobacion,
        directorio_corrida=directorio,
    )

    job = PublishJob(
        run_id=run_id,
        state=EstadoPublicacion.no_listo,
        idempotency_key=clave_idempotencia(run_id),
    )

    workspace.escribir_artefacto(ARTEFACTO_METADATA, metadata)
    workspace.escribir_artefacto(ARTEFACTO_APROBACION, aprobacion)
    workspace.escribir_artefacto(ARTEFACTO_JOB, job)

    return Preparacion(
        workspace=workspace,
        gate=gate,
        metadata=metadata,
        aprobacion=aprobacion,
        job=job,
        render_job=render_job,
        render_result=render_result,
    )


def persistir_desenlace(
    workspace: Workspace, job: PublishJob, resultado: PublishResult | None
) -> None:
    """Escribe el desenlace de la publicación en la corrida.

    ``publish_result.json`` solo se escribe si existe: no hay resultado cuando no
    se llegó a hablar con el proveedor, y un fichero vacío o con valores
    inventados diría que sí lo hubo.

    Ninguno de los dos lleva la URL de la sesión de subida:
    ``PublishJob.upload_session_ref`` es un SHA-256 de esa URL y
    ``UploadSession.session_url`` está marcada ``exclude=True``, así que no hay
    volcado del que se pueda reconstruir.
    """
    workspace.escribir_artefacto(ARTEFACTO_JOB, job)
    if resultado is not None:
        workspace.escribir_artefacto(ARTEFACTO_RESULTADO, resultado)


def publicar_preparada(
    preparacion: Preparacion,
    *,
    token: TokenAcceso,
    transporte: Transporte,
    timeout_s: int = TIMEOUT_POR_DEFECTO_S,
    tamano_fragmento: int = TAMANO_FRAGMENTO_BYTES,
) -> ResultadoPublicacion:
    """Publica una preparación cuya puerta está satisfecha, y persiste el desenlace.

    Exige el gate satisfecho con un error del dominio, no con un ``assert``: los
    asserts desaparecen bajo ``python -O`` y esta es la última puerta antes de que
    ``publicar`` contacte con YouTube.

    No reimplementa nada del publisher: le pasa el ``PublishJob`` que preparó
    ``preparar``, para que el intento quede contado sobre el mismo trabajo que ya
    consta en la corrida.
    """
    if not preparacion.gate.listo:
        raise EntradaInvalida(
            "la puerta de publicación no está satisfecha y no se contacta con el "
            f"proveedor: {preparacion.gate.resumen}"
        )

    salida = publicar(
        run_id=preparacion.job.run_id,
        metadata=preparacion.metadata,
        gate=preparacion.gate,
        token=token,
        transporte=transporte,
        job=preparacion.job,
        timeout_s=timeout_s,
        tamano_fragmento=tamano_fragmento,
    )
    persistir_desenlace(preparacion.workspace, salida.job, salida.result)
    return salida
