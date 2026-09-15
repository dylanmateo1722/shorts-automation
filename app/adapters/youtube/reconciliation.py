"""Resolución de la incertidumbre. El requisito central de Gate 7.2.

Cuando una subida deja de tener un desenlace conocido, aquí se decide qué se
sabe realmente. La regla que gobierna el módulo entero es una:

    **Una pérdida de respuesta no es una subida fallida.**

El proveedor pudo haber aceptado los bytes. Dar eso por fallido y volver a subir
crearía un duplicado sin que nadie se enterase, así que la única salida
autorizada ante la duda es ``UNKNOWN``, y ``UNKNOWN`` obliga a revisión humana.

Qué se puede averiguar de verdad
--------------------------------

El protocolo resumable permite consultar el estado de una sesión —``PUT`` al URL
de sesión con ``Content-Range: bytes */<total>``—, y eso es **todo** lo que la
documentación respalda. La consulta exige el ``session_url``.

No existe ninguna llamada que responda «¿se completó la sesión X?» sin ese URL, y
este módulo **no la sustituye por heurísticas**:

* ``videos.list`` no admite ``mine=true``; listar los vídeos propios exigiría
  ``search.list?forMine=true`` o la playlist ``uploads`` del canal, ambas con
  latencia de propagación y con alcances más amplios que ``youtube.upload``,
  cuya suficiencia el propio proyecto documenta como incierta.
* Aunque apareciera un vídeo, **no habría forma de correlacionarlo con nuestro
  intento**: el ``publication_fingerprint`` es interno y el proveedor no lo
  conoce. Emparejar por título es ambiguo, y esa ambigüedad es justo lo que debe
  producir ``UNKNOWN``.

De ahí la consecuencia que el diseño asume y declara: **si el proceso se
reinició, el ``session_url`` se perdió con él y el desenlace es siempre
``UNKNOWN``**, salvo que conste que no se envió ningún byte.
"""

from __future__ import annotations

from app.contracts.models import (
    ConfianzaReconciliacion,
    DesenlaceReconciliacion,
    MotivoReconciliacion,
    ReconciliationRequest,
    ReconciliationResult,
)
from app.core.errors import ErrorPipeline
from app.adapters.youtube.upload import (
    TIMEOUT_POR_DEFECTO_S,
    SesionDesconocida,
    Transporte,
    consultar_progreso,
)

#: Motivos en los que el proveedor pudo haber aceptado el contenido. Ante
#: cualquiera de ellos, la ausencia de evidencia es ``UNKNOWN``, nunca «no se
#: subió».
MOTIVOS_CON_RIESGO_DE_DUPLICADO = frozenset(
    {
        MotivoReconciliacion.respuesta_perdida,
        MotivoReconciliacion.tiempo_agotado,
        MotivoReconciliacion.conexion_reiniciada,
        MotivoReconciliacion.resultado_desconocido,
        MotivoReconciliacion.proceso_interrumpido,
    }
)


#: Formato de ``observed_state`` cuando informa progreso. Lo escriben y lo leen
#: ``formatear_progreso`` y ``leer_progreso``, que existen para que el
#: acoplamiento entre este módulo y el publisher sea explícito: sin ellas,
#: cambiar el texto de un lado rompería silenciosamente al otro.
PLANTILLA_PROGRESO = "{confirmados}/{total} bytes recibidos"


def formatear_progreso(confirmados: int, total: int) -> str:
    """El ``observed_state`` de un progreso parcial."""
    return PLANTILLA_PROGRESO.format(confirmados=confirmados, total=total)


def leer_progreso(observed_state: str) -> int | None:
    """Los bytes que anunciaba un ``observed_state``, si el texto los trae.

    **Solo para representación.** Ninguna decisión de reanudación pasa por aquí:
    el offset con el que se continúa una sesión es
    ``ReconciliationResult.bytes_confirmed``, que es un entero del contrato.
    Recuperar un offset de un texto libre era exactamente el acoplamiento que
    esta separación elimina, así que esta función existe para leer y comparar
    lo que se muestra, no para decidir por dónde sigue una subida.
    """
    cabeza, separador, _ = observed_state.partition("/")
    if not separador:
        return None
    try:
        valor = int(cabeza.strip())
    except ValueError:
        return None
    return valor if valor >= 0 else None


def _resultado(
    peticion: ReconciliationRequest,
    desenlace: DesenlaceReconciliacion,
    confianza: ConfianzaReconciliacion,
    observado: str,
    evidencia: list[str],
    *,
    video_id: str | None = None,
    bytes_confirmed: int | None = None,
) -> ReconciliationResult:
    return ReconciliationResult(
        run_id=peticion.run_id,
        attempt=peticion.attempt,
        outcome=desenlace,
        video_id=video_id,
        bytes_confirmed=bytes_confirmed,
        observed_state=observado,
        confidence=confianza,
        evidence=evidencia,
    )


def reconciliar(
    peticion: ReconciliationRequest,
    *,
    transporte: Transporte,
    timeout_s: int = TIMEOUT_POR_DEFECTO_S,
) -> ReconciliationResult:
    """Averigua qué pasó con una subida cuyo resultado no consta.

    Nunca sube nada, nunca abre una sesión y nunca reintenta: solo pregunta y
    clasifica. Los cuatro desenlaces posibles y cuándo se alcanzan:

    * ``CONFIRMED_UPLOADED`` — el proveedor devolvió el recurso con su id.
    * ``UPLOAD_IN_PROGRESS`` — el proveedor informó progreso parcial sobre una
      sesión viva; se puede continuar **esa misma** sesión.
    * ``CONFIRMED_NOT_UPLOADED`` — consta que no se transmitió un solo byte, así
      que no hay vídeo que pueda existir.
    * ``UNKNOWN`` — todo lo demás. Obliga a revisión humana.
    """
    # --- Caso 1: no hay con qué preguntar -----------------------------------
    sesion = peticion.upload_session
    if sesion is None:
        # Sin bytes enviados no puede haber vídeo: el proveedor no recibió
        # contenido que pudiera crear uno. Es el único caso en que la ausencia
        # de sesión permite afirmar algo.
        if peticion.last_known_bytes == 0:
            return _resultado(
                peticion,
                DesenlaceReconciliacion.confirmado_no_subido,
                ConfianzaReconciliacion.provider_confirmado,
                observado="no se transmitió ningún byte",
                evidencia=[
                    "last_known_bytes = 0: no se envió contenido, así que no "
                    "pudo crearse ningún vídeo",
                ],
            )
        # Con bytes enviados y sin URL, no hay forma documentada de preguntar.
        return _resultado(
            peticion,
            DesenlaceReconciliacion.desconocido,
            ConfianzaReconciliacion.sin_evidencia,
            observado="sin sesión consultable",
            evidencia=[
                f"motivo {peticion.reason.value}: no se dispone del URL de "
                f"sesión, que es el único mecanismo documentado para consultar "
                f"el estado de una subida resumable",
                f"constan {peticion.last_known_bytes} bytes enviados, así que el "
                f"proveedor pudo haber aceptado el contenido",
                "no se consulta el catálogo del canal: no permitiría "
                "correlacionar un vídeo con este intento",
            ],
        )

    # --- Caso 2: hay sesión; se pregunta ------------------------------------
    try:
        estado = consultar_progreso(sesion, transporte=transporte, timeout_s=timeout_s)
    except SesionDesconocida as exc:
        # La sesión ya no existe. Eso **no** dice si el vídeo se creó: una sesión
        # puede desaparecer después de completarse.
        return _resultado(
            peticion,
            DesenlaceReconciliacion.desconocido,
            ConfianzaReconciliacion.sin_evidencia,
            observado="el proveedor no reconoce la sesión",
            evidencia=[
                str(exc),
                "una sesión desconocida no distingue entre nunca aceptada y ya "
                "completada",
            ],
        )
    except ErrorPipeline as exc:
        # Cualquier otro fallo al preguntar deja la duda intacta.
        return _resultado(
            peticion,
            DesenlaceReconciliacion.desconocido,
            ConfianzaReconciliacion.sin_evidencia,
            observado="no se pudo consultar la sesión",
            evidencia=[str(exc)],
        )

    if estado.confirmado:
        return _resultado(
            peticion,
            DesenlaceReconciliacion.confirmado_subido,
            ConfianzaReconciliacion.provider_confirmado,
            observado="el proveedor devolvió el recurso del vídeo",
            evidencia=[
                "la consulta de la sesión respondió con el vídeo ya creado",
            ],
            video_id=estado.video_id,
        )

    if estado.bytes_confirmed >= sesion.total_bytes:
        # Dice que están todos los bytes pero no da identificador. No se puede
        # afirmar que exista el vídeo ni que no exista.
        return _resultado(
            peticion,
            DesenlaceReconciliacion.desconocido,
            ConfianzaReconciliacion.sin_evidencia,
            observado="bytes completos sin identificador",
            evidencia=[
                "el proveedor da por recibidos todos los bytes pero no devolvió "
                "el id del vídeo",
            ],
        )

    if estado.bytes_confirmed == 0 and peticion.last_known_bytes == 0:
        return _resultado(
            peticion,
            DesenlaceReconciliacion.confirmado_no_subido,
            ConfianzaReconciliacion.provider_confirmado,
            observado="la sesión no ha recibido ningún byte",
            evidencia=[
                "el proveedor informa 0 bytes recibidos sobre una sesión viva",
            ],
        )

    return _resultado(
        peticion,
        DesenlaceReconciliacion.subida_en_curso,
        ConfianzaReconciliacion.provider_parcial,
        observado=formatear_progreso(estado.bytes_confirmed, sesion.total_bytes),
        evidencia=[
            "el proveedor informa progreso parcial sobre una sesión viva; se "
            "puede continuar esa misma sesión",
        ],
        bytes_confirmed=estado.bytes_confirmed,
    )
