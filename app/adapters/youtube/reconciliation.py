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


def _resultado(
    peticion: ReconciliationRequest,
    desenlace: DesenlaceReconciliacion,
    confianza: ConfianzaReconciliacion,
    observado: str,
    evidencia: list[str],
    *,
    video_id: str | None = None,
) -> ReconciliationResult:
    return ReconciliationResult(
        run_id=peticion.run_id,
        attempt=peticion.attempt,
        outcome=desenlace,
        video_id=video_id,
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
    if not peticion.sesion_consultable:
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
    sesion = peticion.upload_session
    assert sesion is not None  # lo garantiza sesion_consultable
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
        observado=f"{estado.bytes_confirmed}/{sesion.total_bytes} bytes recibidos",
        evidencia=[
            "el proveedor informa progreso parcial sobre una sesión viva; se "
            "puede continuar esa misma sesión",
        ],
    )
