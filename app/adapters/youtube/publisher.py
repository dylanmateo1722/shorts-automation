"""Orquestación de la publicación. Ni protocolo ni autenticación.

Este módulo decide **qué hacer**; ``upload.py`` sabe hablar el protocolo y
``auth.py`` sabe conseguir un token. La separación es del Gate: ``auth`` tiene
que poder comprobarse sin que exista la capacidad de publicar, y el publisher no
debe saber cómo se canjea un refresh token.

El recorrido completo:

    gate previo → sesión → fragmentos → (reconciliación) → verificación → resultado

Las dos reglas que gobiernan el módulo
--------------------------------------

**1. Ante la duda, no se sube.** Si una subida pierde su desenlace, se reconcilia.
Si la reconciliación no puede determinarlo, el trabajo acaba en ``NEEDS_REVIEW``.
``UNKNOWN`` no autoriza una sesión nueva por ningún camino: la condición está en
``ReconciliationResult.permite_nueva_sesion``, que solo es cierta para
``CONFIRMED_NOT_UPLOADED``.

**2. Que el HTTP terminara bien no es que esté publicado.** ``COMPLETED`` exige
haber vuelto a preguntar por el vídeo y que lo observado coincida con lo pedido.

Sobre ``attempt``
-----------------

Hay dos contadores y no son el mismo:

* ``PublishJob.attempt`` cuenta las **entradas en UPLOADING**, y solo lo mueve la
  máquina de estados de Gate 7.0.
* ``UploadSession.attempt`` cuenta las **sesiones abiertas dentro de ese intento**.

Cuando la reconciliación confirma que no se subió nada, se abre otra sesión sin
salir de ``UPLOADING``: la tabla de transiciones de Gate 7.0 no contempla volver
a ``READY``, y no hacía falta tocarla.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar
from uuid import UUID

from app.contracts.models import (
    DesenlaceReconciliacion,
    EstadoPublicacion,
    MotivoReconciliacion,
    Privacidad,
    PublishJob,
    PublishMetadata,
    PublishResult,
    ReconciliationRequest,
    ReconciliationResult,
    UploadSession,
    clave_idempotencia,
    publication_fingerprint,
)
from app.core.errors import (
    EntradaInvalida,
    ErrorPermanente,
    ErrorPipeline,
    ErrorTransitorio,
)
from app.adapters.youtube.auth import AutorizacionInvalida, TokenAcceso
from app.adapters.youtube.reconciliation import reconciliar
from app.adapters.youtube.upload import (
    TAMANO_FRAGMENTO_BYTES,
    TIMEOUT_POR_DEFECTO_S,
    RespuestaPerdida,
    SesionDesconocida,
    Transporte,
    consultar_progreso,
    huella_metadata,
    iniciar_sesion,
    leer_video,
    subir_fragmento,
    url_publica,
)
from app.pipeline.publicacion import ResultadoGate, sha256_de_archivo

_T = TypeVar("_T")

#: Intentos máximos ante un fallo transitorio. Tres, como el resto del proyecto.
MAX_INTENTOS = 3

#: Base del backoff exponencial, en segundos.
BACKOFF_BASE_S = 2.0

#: Sesiones máximas dentro de un mismo intento. Cada una exige que la
#: reconciliación haya confirmado que no se subió nada, así que el tope solo
#: protege contra un bucle patológico; no es una política de reintento.
MAX_SESIONES = 3


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


class _Progreso:
    """Deja ver la sesión viva aunque la subida falle a mitad.

    Sin esto, una excepción dentro de ``_subir`` se llevaría por delante la
    sesión que acababa de crearse, y la reconciliación se quedaría sin el
    ``session_url`` —es decir, sin nada que preguntar— justo en el caso que más
    importa. El portador es la diferencia entre poder reconciliar y no poder.
    """

    __slots__ = ("sesion", "bytes_vistos")

    def __init__(self, sesion: UploadSession | None = None) -> None:
        self.sesion = sesion
        self.bytes_vistos = sesion.bytes_confirmed if sesion else 0

    def registrar(self, sesion: UploadSession) -> None:
        self.sesion = sesion
        self.bytes_vistos = max(self.bytes_vistos, sesion.bytes_confirmed)


@dataclass(frozen=True)
class ResultadoPublicacion:
    """Lo que devuelve el publisher: el trabajo y, si lo hubo, el resultado.

    Los dos van juntos porque describen cosas distintas. ``job`` dice por dónde
    acabó la ejecución; ``result`` dice qué contestó el proveedor, y no existe si
    nunca se llegó a hablar con él.
    """

    job: PublishJob
    result: PublishResult | None = None

    @property
    def publicado(self) -> bool:
        return self.job.state is EstadoPublicacion.completado


def _espera(intento: int, aleatorio: Callable[[], float]) -> float:
    """Backoff exponencial con jitter.

    El jitter se inyecta para que los tests sean deterministas: sin él, dos
    ejecuciones de la misma prueba dormirían distinto.
    """
    return (BACKOFF_BASE_S ** intento) * (0.5 + aleatorio())


def _con_reintentos(
    accion: Callable[[], _T],
    *,
    dormir: Callable[[float], None],
    aleatorio: Callable[[], float],
    max_intentos: int = MAX_INTENTOS,
) -> _T:
    """Ejecuta una acción reintentando solo lo que merece reintentarse.

    Se reintenta lo transitorio: timeouts, 429 y 5xx. **No** se reintenta
    ``RespuestaPerdida``, aunque su categoría sea transitoria: reintentar una
    petición cuya respuesta se perdió es exactamente cómo se crea un duplicado.
    Esa excepción sube intacta para que la resuelva la reconciliación.
    """
    for intento in range(1, max_intentos + 1):
        try:
            return accion()
        except RespuestaPerdida:
            raise
        except ErrorTransitorio as exc:
            if intento == max_intentos:
                raise
            dormir(_espera(intento, aleatorio))
    raise ErrorPipeline(  # pragma: no cover - max_intentos siempre es >= 1
        "la política de reintentos terminó sin ejecutar ninguna acción"
    )


def _motivo_desde(exc: Exception) -> MotivoReconciliacion:
    """Qué motivo de reconciliación corresponde a lo que falló."""
    if isinstance(exc, RespuestaPerdida):
        return MotivoReconciliacion.respuesta_perdida
    if isinstance(exc, SesionDesconocida):
        return MotivoReconciliacion.resultado_desconocido
    return MotivoReconciliacion.tiempo_agotado


def _resultado_desde(
    job: PublishJob,
    metadata: PublishMetadata,
    gate: ResultadoGate,
    *,
    video_id: str | None = None,
    url: str | None = None,
    privacy: Privacidad | None = None,
    uploaded_at: datetime | None = None,
    completed_at: datetime | None = None,
    metadata_sha256: str | None = None,
    reconciliation: ReconciliationResult | None = None,
) -> PublishResult:
    return PublishResult(
        run_id=job.run_id,
        provider="youtube",
        video_id=video_id,
        url=url,
        status=job.state,
        privacy_status=privacy,
        upload_attempts=max(job.attempt, 1),
        uploaded_at=uploaded_at,
        completed_at=completed_at,
        metadata_sha256=metadata_sha256,
        video_sha256=gate.video_sha256,
        reconciliation=reconciliation,
    )


# ---------------------------------------------------------------------------
# Verificación
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verificacion:
    """Comparación entre lo solicitado y lo observado."""

    coincide: bool
    discrepancias: list[str]
    privacy_observada: Privacidad | None = None
    upload_status: str = ""


def verificar(
    token: TokenAcceso,
    video_id: str,
    metadata: PublishMetadata,
    *,
    transporte: Transporte,
    timeout_s: int = TIMEOUT_POR_DEFECTO_S,
) -> Verificacion:
    """Vuelve a preguntar por el vídeo y compara con lo que se pidió.

    Que la subida HTTP terminara bien no es evidencia de que el vídeo esté como
    se quería: el proveedor puede recortar un título, y el estado de privacidad
    es justo lo que no puede darse por supuesto.
    """
    remoto = leer_video(token, video_id, transporte=transporte, timeout_s=timeout_s)
    discrepancias: list[str] = []

    if remoto.video_id != video_id:
        discrepancias.append(
            f"el proveedor devolvió otro identificador del solicitado"
        )
    if remoto.title != metadata.title:
        discrepancias.append("el título observado no coincide con el solicitado")
    if remoto.description != metadata.description:
        discrepancias.append("la descripción observada no coincide con la solicitada")

    privacy_observada: Privacidad | None = None
    try:
        privacy_observada = Privacidad(remoto.privacy_status)
    except ValueError:
        discrepancias.append(
            f"el estado de privacidad observado no es reconocible"
        )
    else:
        if privacy_observada is not metadata.privacy_status:
            discrepancias.append(
                f"la privacidad observada ({privacy_observada.value}) no es la "
                f"solicitada ({metadata.privacy_status.value})"
            )

    # El procesamiento sigue su curso después de la subida; que no haya
    # terminado no es una discrepancia, pero se registra.
    if remoto.upload_status and remoto.upload_status not in ("uploaded", "processed"):
        discrepancias.append(
            f"el proveedor informa uploadStatus {remoto.upload_status!r}"
        )

    return Verificacion(
        coincide=not discrepancias,
        discrepancias=discrepancias,
        privacy_observada=privacy_observada,
        upload_status=remoto.upload_status,
    )


# ---------------------------------------------------------------------------
# El publisher
# ---------------------------------------------------------------------------


def publicar(
    *,
    run_id: UUID,
    metadata: PublishMetadata,
    gate: ResultadoGate,
    token: TokenAcceso,
    transporte: Transporte,
    job: PublishJob | None = None,
    timeout_s: int = TIMEOUT_POR_DEFECTO_S,
    tamano_fragmento: int = TAMANO_FRAGMENTO_BYTES,
    dormir: Callable[[float], None] = time.sleep,
    aleatorio: Callable[[], float] = random.random,
) -> ResultadoPublicacion:
    """Publica el vídeo de una corrida, o explica por qué no pudo.

    ``gate`` tiene que venir ya satisfecho: este módulo no vuelve a evaluar las
    precondiciones, las exige. Un gate no satisfecho produce ``NOT_READY`` sin
    tocar la red.
    """
    trabajo = job or PublishJob(
        run_id=run_id,
        state=EstadoPublicacion.no_listo,
        idempotency_key=clave_idempotencia(run_id),
    )

    # --- La puerta previa ---------------------------------------------------
    if not gate.listo:
        return ResultadoPublicacion(
            job=trabajo.transicionar(
                EstadoPublicacion.revision_pendiente, reason=gate.resumen
            )
            if trabajo.state is not EstadoPublicacion.no_listo
            else trabajo,
        )

    # ``ResultadoGate`` ya rechaza en su construcción un ``listo=True`` sin
    # vídeo, pero la comprobación se repite aquí y con un error del dominio, no
    # con un ``assert``: los asserts desaparecen bajo ``python -O`` y esta es la
    # última puerta antes de hablar con YouTube.
    if gate.video_path is None or not (gate.video_sha256 or "").strip():
        raise EntradaInvalida(
            "el gate dice que se puede publicar pero no trae el vídeo ni su "
            "huella; no se contacta con el proveedor sin ambas cosas"
        )

    if trabajo.state is EstadoPublicacion.no_listo:
        trabajo = trabajo.transicionar(EstadoPublicacion.listo)

    huella_meta = huella_metadata(metadata)
    fingerprint = publication_fingerprint(run_id, gate.video_sha256, huella_meta)

    # Entrar en UPLOADING valida la divulgación de IA: la máquina de estados
    # rechaza la transición si exige verificación humana.
    try:
        trabajo = trabajo.transicionar(
            EstadoPublicacion.subiendo, metadata=metadata
        )
    except ValueError as exc:
        trabajo = trabajo.transicionar(
            EstadoPublicacion.revision_pendiente, reason=str(exc)
        )
        return ResultadoPublicacion(job=trabajo)

    sesion: UploadSession | None = None
    ultima_reconciliacion: ReconciliationResult | None = None

    for numero_sesion in range(1, MAX_SESIONES + 1):
        progreso = _Progreso(sesion)
        try:
            video_id = _subir(
                token=token,
                metadata=metadata,
                gate=gate,
                run_id=run_id,
                numero_sesion=numero_sesion,
                progreso=progreso,
                transporte=transporte,
                timeout_s=timeout_s,
                tamano_fragmento=tamano_fragmento,
                dormir=dormir,
                aleatorio=aleatorio,
            )
        except AutorizacionInvalida as exc:
            # Nunca se reintenta: reintentar una autorización inválida la deja
            # igual de inválida.
            return _fallar(trabajo, metadata, gate, huella_meta, str(exc))
        except ErrorPermanente as exc:
            # Incluye INTEGRITY_MISMATCH y las respuestas 4xx definitivas.
            # ``SesionDesconocida`` también es permanente, pero se trata abajo
            # porque sí deja el desenlace en duda.
            if not isinstance(exc, SesionDesconocida):
                return _fallar(trabajo, metadata, gate, huella_meta, str(exc))
            sesion = progreso.sesion
            ultima_reconciliacion = _preguntar(
                run_id=run_id,
                trabajo=trabajo,
                fingerprint=fingerprint,
                sesion=sesion,
                bytes_vistos=progreso.bytes_vistos,
                exc=exc,
                transporte=transporte,
                timeout_s=timeout_s,
            )
        except (RespuestaPerdida, ErrorTransitorio) as exc:
            sesion = progreso.sesion
            if sesion is None and progreso.bytes_vistos == 0:
                # No llegó a abrirse sesión y no se envió un solo byte: no hay
                # ambigüedad ninguna, y llamarlo «incierto» sería exagerar.
                return _fallar(trabajo, metadata, gate, huella_meta, str(exc))
            ultima_reconciliacion = _preguntar(
                run_id=run_id,
                trabajo=trabajo,
                fingerprint=fingerprint,
                sesion=sesion,
                bytes_vistos=max(
                    progreso.bytes_vistos,
                    getattr(exc, "bytes_enviados", 0),
                ),
                exc=exc,
                transporte=transporte,
                timeout_s=timeout_s,
            )
        else:
            sesion = progreso.sesion
            trabajo = _con_referencia(trabajo, sesion)
            return _tras_la_subida(
                trabajo=trabajo,
                metadata=metadata,
                gate=gate,
                token=token,
                transporte=transporte,
                video_id=video_id,
                huella_meta=huella_meta,
                reconciliation=ultima_reconciliacion,
                timeout_s=timeout_s,
            )

        # --- Se preguntó. Qué se hace con la respuesta ----------------------
        trabajo = _con_referencia(trabajo, sesion)

        if ultima_reconciliacion.outcome is DesenlaceReconciliacion.confirmado_subido:
            return _tras_la_subida(
                trabajo=trabajo,
                metadata=metadata,
                gate=gate,
                token=token,
                transporte=transporte,
                video_id=ultima_reconciliacion.video_id or "",
                huella_meta=huella_meta,
                reconciliation=ultima_reconciliacion,
                timeout_s=timeout_s,
            )

        if ultima_reconciliacion.outcome is DesenlaceReconciliacion.subida_en_curso:
            # Continuar la MISMA sesión. No se abre otra.
            if sesion is None:
                # No es representable —``UPLOAD_IN_PROGRESS`` solo sale de haber
                # consultado una sesión—, pero si llegara a ocurrir, abrir otra
                # sería justo lo prohibido. Se manda a revisión.
                raise ErrorPermanente(
                    "el proveedor informa una subida en curso y no consta la "
                    "sesión con la que continuarla"
                )
            sesion = _sincronizar(sesion, ultima_reconciliacion)
            continue

        if ultima_reconciliacion.permite_nueva_sesion:
            # Único camino hacia una sesión nueva, y exige que el proveedor haya
            # confirmado que no se subió nada.
            sesion = None
            continue

        # UNKNOWN. No se sube nada más.
        trabajo = trabajo.transicionar(
            EstadoPublicacion.revision_pendiente,
            reason=(
                "no se pudo determinar si la subida llegó a completarse; no se "
                "inicia otra para no arriesgar un duplicado"
            ),
        )
        trabajo = _con_reconciliacion(trabajo, ultima_reconciliacion)
        return ResultadoPublicacion(
            job=trabajo,
            result=_resultado_desde(
                trabajo,
                metadata,
                gate,
                metadata_sha256=huella_meta,
                reconciliation=ultima_reconciliacion,
            ),
        )

    # Se agotaron las sesiones. Si consta que no se subió nada, es un fallo
    # limpio; si no consta, lo mira una persona.
    if ultima_reconciliacion is not None and ultima_reconciliacion.permite_nueva_sesion:
        return _fallar(
            trabajo,
            metadata,
            gate,
            huella_meta,
            f"no se pudo completar la subida en {MAX_SESIONES} sesiones; consta "
            f"que no quedó contenido en el proveedor",
            reconciliation=ultima_reconciliacion,
        )
    trabajo = trabajo.transicionar(
        EstadoPublicacion.revision_pendiente,
        reason=f"se agotaron las {MAX_SESIONES} sesiones sin un desenlace claro",
    )
    trabajo = _con_reconciliacion(trabajo, ultima_reconciliacion)
    return ResultadoPublicacion(
        job=trabajo,
        result=_resultado_desde(
            trabajo,
            metadata,
            gate,
            metadata_sha256=huella_meta,
            reconciliation=ultima_reconciliacion,
        ),
    )


def _fallar(
    trabajo: PublishJob,
    metadata: PublishMetadata,
    gate: ResultadoGate,
    huella_meta: str,
    motivo: str,
    *,
    reconciliation: ReconciliationResult | None = None,
) -> ResultadoPublicacion:
    """Cierra el trabajo como fallido. Solo cuando no hay ambigüedad."""
    trabajo = trabajo.transicionar(EstadoPublicacion.fallido, reason=motivo)
    if reconciliation is not None:
        trabajo = _con_reconciliacion(trabajo, reconciliation)
    return ResultadoPublicacion(
        job=trabajo,
        result=_resultado_desde(
            trabajo,
            metadata,
            gate,
            metadata_sha256=huella_meta,
            reconciliation=reconciliation,
        ),
    )


def _preguntar(
    *,
    run_id: UUID,
    trabajo: PublishJob,
    fingerprint: str,
    sesion: UploadSession | None,
    bytes_vistos: int,
    exc: Exception,
    transporte: Transporte,
    timeout_s: int,
) -> ReconciliationResult:
    """Construye la petición de reconciliación y la resuelve."""
    peticion = ReconciliationRequest(
        run_id=run_id,
        attempt=trabajo.attempt,
        reason=_motivo_desde(exc),
        publication_fingerprint=fingerprint,
        upload_session=sesion,
        last_known_bytes=max(bytes_vistos, sesion.bytes_confirmed if sesion else 0),
    )
    return reconciliar(peticion, transporte=transporte, timeout_s=timeout_s)


def _con_referencia(job: PublishJob, sesion: UploadSession | None) -> PublishJob:
    """Anota la huella no secreta de la sesión en el trabajo."""
    if sesion is None:
        return job
    datos = job.model_dump()
    datos["upload_session_ref"] = sesion.referencia
    datos["updated_at"] = _ahora()
    return PublishJob.model_validate(datos)


def _con_reconciliacion(
    job: PublishJob, resultado: ReconciliationResult | None
) -> PublishJob:
    if resultado is None:
        return job
    datos = job.model_dump()
    datos["reconciliation"] = resultado.model_dump()
    datos["updated_at"] = _ahora()
    return PublishJob.model_validate(datos)


def _sincronizar(
    sesion: UploadSession, resultado: ReconciliationResult
) -> UploadSession:
    """Pone la sesión al día con los bytes que el proveedor dio por recibidos.

    El offset sale de ``resultado.bytes_confirmed``, que es un entero del
    contrato y viene garantizado en ``UPLOAD_IN_PROGRESS`` —el único desenlace
    que llega hasta aquí—. ``observed_state`` no se mira: es texto descriptivo, y
    sacar de él un número que decide desde dónde se reanuda sería reintroducir
    el acoplamiento que el contrato eliminó.
    """
    anunciados = resultado.bytes_confirmed
    # La defensa se mantiene por si el desenlace cambiara de forma: conservar lo
    # que la sesión ya daba por confirmado es lo único seguro, porque retroceder
    # el offset reenviaría bytes y adelantarlo dejaría un hueco.
    confirmados = sesion.bytes_confirmed if anunciados is None else anunciados
    datos = sesion.model_dump()
    datos["session_url"] = sesion.session_url
    datos["bytes_confirmed"] = max(confirmados, 0)
    datos["updated_at"] = _ahora()
    return UploadSession.model_validate(datos)


def _subir(
    *,
    token: TokenAcceso,
    metadata: PublishMetadata,
    gate: ResultadoGate,
    run_id: UUID,
    numero_sesion: int,
    progreso: _Progreso,
    transporte: Transporte,
    timeout_s: int,
    tamano_fragmento: int,
    dormir: Callable[[float], None],
    aleatorio: Callable[[], float],
) -> str:
    """Sube el archivo entero por fragmentos y devuelve el ``video_id``.

    Todo avance se comunica por ``progreso`` **en cuanto ocurre**, no al final:
    si esto falla a mitad, el llamador necesita la sesión para poder preguntar
    por ella, y una sesión que solo se devolviera al terminar no serviría de
    nada justo cuando hace falta.

    Si ``progreso`` ya trae una sesión, se continúa **esa misma**; solo se abre
    una nueva cuando viene vacío, y eso únicamente ocurre tras un
    ``CONFIRMED_NOT_UPLOADED``.
    """
    if gate.video_path is None or not (gate.video_sha256 or "").strip():
        raise EntradaInvalida(
            "no se sube un gate sin vídeo ni huella"
        )
    ruta: Path = gate.video_path

    if progreso.sesion is None:
        sesion = _con_reintentos(
            lambda: iniciar_sesion(
                token,
                metadata,
                run_id=run_id,
                attempt=numero_sesion,
                total_bytes=gate.total_bytes,
                video_sha256=gate.video_sha256,
                transporte=transporte,
                timeout_s=timeout_s,
            ),
            dormir=dormir,
            aleatorio=aleatorio,
        )
        progreso.registrar(sesion)
    else:
        # Reanudar exige comprobar que el archivo sigue siendo el mismo.
        _exigir_integridad(ruta, progreso.sesion)

    with ruta.open("rb") as archivo:
        while True:
            sesion = progreso.sesion
            if sesion is None or sesion.bytes_confirmed >= sesion.total_bytes:
                break
            offset = sesion.bytes_confirmed
            archivo.seek(offset)
            restante = sesion.total_bytes - offset
            trozo = archivo.read(min(tamano_fragmento, restante))
            if not trozo:  # pragma: no cover - defensivo
                raise ErrorPermanente(
                    "el archivo se acabó antes de completar la subida"
                )
            resultado = _con_reintentos(
                lambda: subir_fragmento(
                    sesion,
                    trozo,
                    offset=offset,
                    transporte=transporte,
                    timeout_s=timeout_s,
                ),
                dormir=dormir,
                aleatorio=aleatorio,
            )
            nueva, estado = resultado
            progreso.registrar(nueva)
            if estado.confirmado:
                return estado.video_id or ""

    # Todos los bytes enviados y sin identificador: se pregunta explícitamente.
    sesion = progreso.sesion
    if sesion is None:  # pragma: no cover - el bloque anterior siempre la deja
        raise ErrorPermanente("la subida terminó sin dejar constancia de la sesión")
    estado = consultar_progreso(sesion, transporte=transporte, timeout_s=timeout_s)
    if estado.confirmado:
        return estado.video_id or ""
    raise SesionDesconocida(
        "se enviaron todos los bytes y el proveedor no devolvió el id del vídeo"
    )


def _exigir_integridad(ruta: Path, sesion: UploadSession) -> None:
    """Rechaza continuar una sesión con un archivo que ya no es el mismo.

    Es permanente y no se reintenta: la sesión se abrió para un contenido
    concreto, y seguir enviándole otro produciría un vídeo que nadie validó.
    """
    actual = sha256_de_archivo(ruta)
    if actual != sesion.video_sha256:
        raise ErrorPermanente(
            "INTEGRITY_MISMATCH: el archivo cambió desde que se abrió la sesión "
            "de subida; continuarla enviaría un contenido distinto del que se "
            "validó"
        )


def _tras_la_subida(
    *,
    trabajo: PublishJob,
    metadata: PublishMetadata,
    gate: ResultadoGate,
    token: TokenAcceso,
    transporte: Transporte,
    video_id: str,
    huella_meta: str,
    reconciliation: ReconciliationResult | None,
    timeout_s: int,
) -> ResultadoPublicacion:
    """De ``UPLOADED`` a ``COMPLETED``, pasando obligatoriamente por verificar."""
    momento_subida = _ahora()
    trabajo = trabajo.transicionar(EstadoPublicacion.subido)
    if reconciliation is not None:
        trabajo = _con_reconciliacion(trabajo, reconciliation)
    trabajo = trabajo.transicionar(EstadoPublicacion.verificando)

    try:
        comprobacion = verificar(
            token, video_id, metadata, transporte=transporte, timeout_s=timeout_s
        )
    except ErrorPipeline as exc:
        # No se pudo verificar. El vídeo existe, así que no es un fallo: es algo
        # que una persona tiene que mirar.
        trabajo = trabajo.transicionar(
            EstadoPublicacion.revision_pendiente,
            reason=f"el vídeo se subió pero no se pudo verificar: {exc}",
        )
        return ResultadoPublicacion(
            job=trabajo,
            result=_resultado_desde(
                trabajo,
                metadata,
                gate,
                video_id=video_id,
                url=url_publica(video_id),
                uploaded_at=momento_subida,
                metadata_sha256=huella_meta,
                reconciliation=reconciliation,
            ),
        )

    if not comprobacion.coincide:
        trabajo = trabajo.transicionar(
            EstadoPublicacion.revision_pendiente,
            reason="; ".join(comprobacion.discrepancias),
        )
        return ResultadoPublicacion(
            job=trabajo,
            result=_resultado_desde(
                trabajo,
                metadata,
                gate,
                video_id=video_id,
                url=url_publica(video_id),
                privacy=comprobacion.privacy_observada,
                uploaded_at=momento_subida,
                metadata_sha256=huella_meta,
                reconciliation=reconciliation,
            ),
        )

    completado = _ahora()
    trabajo = trabajo.transicionar(EstadoPublicacion.completado)
    return ResultadoPublicacion(
        job=trabajo,
        result=_resultado_desde(
            trabajo,
            metadata,
            gate,
            video_id=video_id,
            url=url_publica(video_id),
            privacy=comprobacion.privacy_observada,
            uploaded_at=momento_subida,
            completed_at=completado,
            metadata_sha256=huella_meta,
            reconciliation=reconciliation,
        ),
    )
