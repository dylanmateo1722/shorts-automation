"""Reconciliación: qué se sabe de verdad cuando el desenlace no consta.

Es el requisito central de Gate 7.2. La regla que estos tests defienden es una:
una pérdida de respuesta **no** es una subida fallida, y ante la duda no se
vuelve a subir.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.contracts.models import (
    ConfianzaReconciliacion,
    DesenlaceReconciliacion,
    MotivoReconciliacion,
    ReconciliationRequest,
    ReconciliationResult,
    UploadSession,
    publication_fingerprint,
)
from app.adapters.youtube.reconciliation import reconciliar
from app.adapters.youtube.upload import MULTIPLO_FRAGMENTO

from conftest_g72 import (
    SESSION_URL,
    VIDEO_ID,
    TransporteFalso,
    completado,
    error,
    incompleto,
)

TOTAL = 3 * MULTIPLO_FRAGMENTO
FINGERPRINT = publication_fingerprint(uuid4(), "a" * 64, "b" * 64)


def _sesion(run_id, confirmados: int = 0) -> UploadSession:
    return UploadSession(
        session_url=SESSION_URL,
        run_id=run_id,
        attempt=1,
        video_sha256="a" * 64,
        metadata_sha256="b" * 64,
        total_bytes=TOTAL,
        bytes_confirmed=confirmados,
    )


def _peticion(
    *,
    run_id=None,
    sesion=None,
    reason=MotivoReconciliacion.respuesta_perdida,
    last_known_bytes=0,
) -> ReconciliationRequest:
    run_id = run_id or uuid4()
    return ReconciliationRequest(
        run_id=run_id,
        attempt=1,
        reason=reason,
        publication_fingerprint=FINGERPRINT,
        upload_session=sesion,
        last_known_bytes=last_known_bytes,
    )


# ---------------------------------------------------------------------------
# CASO A: el proveedor confirma el vídeo
# ---------------------------------------------------------------------------


def test_el_proveedor_confirma_el_video_y_no_se_sube_nada_mas():
    run_id = uuid4()
    transporte = TransporteFalso(guion=[completado()])
    resultado = reconciliar(
        _peticion(run_id=run_id, sesion=_sesion(run_id, TOTAL), last_known_bytes=TOTAL),
        transporte=transporte,
    )
    assert resultado.outcome is DesenlaceReconciliacion.confirmado_subido
    assert resultado.video_id == VIDEO_ID
    assert resultado.confidence is ConfianzaReconciliacion.provider_confirmado
    assert not resultado.permite_nueva_sesion
    assert transporte.sesiones_iniciadas == 0


# ---------------------------------------------------------------------------
# CASO B: la sesión sigue viva con progreso parcial
# ---------------------------------------------------------------------------


def test_progreso_parcial_permite_continuar_la_misma_sesion():
    run_id = uuid4()
    resultado = reconciliar(
        _peticion(
            run_id=run_id,
            sesion=_sesion(run_id, MULTIPLO_FRAGMENTO),
            last_known_bytes=MULTIPLO_FRAGMENTO,
        ),
        transporte=TransporteFalso(guion=[incompleto(MULTIPLO_FRAGMENTO)]),
    )
    assert resultado.outcome is DesenlaceReconciliacion.subida_en_curso
    assert resultado.confidence is ConfianzaReconciliacion.provider_parcial
    assert not resultado.permite_nueva_sesion
    assert str(MULTIPLO_FRAGMENTO) in resultado.observed_state


# ---------------------------------------------------------------------------
# CASO C: respuesta perdida
# ---------------------------------------------------------------------------


def test_respuesta_perdida_tras_fragmento_intermedio_reconcilia_sin_duplicar():
    run_id = uuid4()
    transporte = TransporteFalso(guion=[incompleto(MULTIPLO_FRAGMENTO)])
    resultado = reconciliar(
        _peticion(
            run_id=run_id,
            sesion=_sesion(run_id, MULTIPLO_FRAGMENTO),
            last_known_bytes=MULTIPLO_FRAGMENTO,
        ),
        transporte=transporte,
    )
    assert resultado.outcome is DesenlaceReconciliacion.subida_en_curso
    assert transporte.sesiones_iniciadas == 0


def test_respuesta_perdida_tras_el_ultimo_fragmento_y_el_proveedor_confirma():
    """El caso exacto del enunciado: YouTube aceptó y el cliente no se enteró."""
    run_id = uuid4()
    resultado = reconciliar(
        _peticion(
            run_id=run_id, sesion=_sesion(run_id, TOTAL), last_known_bytes=TOTAL
        ),
        transporte=TransporteFalso(guion=[completado()]),
    )
    assert resultado.outcome is DesenlaceReconciliacion.confirmado_subido
    assert resultado.video_id == VIDEO_ID


def test_bytes_completos_sin_identificador_es_desconocido():
    """No se puede afirmar que exista ni que no exista."""
    run_id = uuid4()
    resultado = reconciliar(
        _peticion(run_id=run_id, sesion=_sesion(run_id, TOTAL), last_known_bytes=TOTAL),
        transporte=TransporteFalso(guion=[incompleto(TOTAL)]),
    )
    assert resultado.outcome is DesenlaceReconciliacion.desconocido
    assert not resultado.permite_nueva_sesion
    assert resultado.exige_revision


def test_una_sesion_desconocida_no_permite_concluir_nada():
    run_id = uuid4()
    resultado = reconciliar(
        _peticion(
            run_id=run_id, sesion=_sesion(run_id, TOTAL), last_known_bytes=TOTAL
        ),
        transporte=TransporteFalso(guion=[error(404)]),
    )
    assert resultado.outcome is DesenlaceReconciliacion.desconocido
    assert resultado.confidence is ConfianzaReconciliacion.sin_evidencia
    assert not resultado.permite_nueva_sesion


def test_si_no_se_puede_preguntar_el_desenlace_es_desconocido():
    run_id = uuid4()
    resultado = reconciliar(
        _peticion(
            run_id=run_id, sesion=_sesion(run_id, TOTAL), last_known_bytes=TOTAL
        ),
        transporte=TransporteFalso(guion=[error(503)]),
    )
    assert resultado.outcome is DesenlaceReconciliacion.desconocido


# ---------------------------------------------------------------------------
# CASO D: consta que no se subió
# ---------------------------------------------------------------------------


def test_sesion_viva_sin_un_solo_byte_permite_una_sesion_nueva():
    run_id = uuid4()
    resultado = reconciliar(
        _peticion(run_id=run_id, sesion=_sesion(run_id, 0), last_known_bytes=0),
        transporte=TransporteFalso(guion=[incompleto(0)]),
    )
    assert resultado.outcome is DesenlaceReconciliacion.confirmado_no_subido
    assert resultado.permite_nueva_sesion


def test_sin_sesion_y_sin_bytes_enviados_consta_que_no_se_subio():
    """Sin contenido transmitido no puede haberse creado un vídeo."""
    resultado = reconciliar(
        _peticion(sesion=None, last_known_bytes=0),
        transporte=TransporteFalso(guion=[incompleto(0)]),
    )
    assert resultado.outcome is DesenlaceReconciliacion.confirmado_no_subido
    assert resultado.permite_nueva_sesion


# ---------------------------------------------------------------------------
# Proceso reiniciado: la limitación real, representada como tal
# ---------------------------------------------------------------------------


def test_proceso_reiniciado_con_bytes_enviados_es_desconocido():
    """El ``session_url`` se perdió y no hay forma documentada de preguntar."""
    transporte = TransporteFalso(guion=[completado()])
    resultado = reconciliar(
        _peticion(
            sesion=None,
            reason=MotivoReconciliacion.proceso_interrumpido,
            last_known_bytes=MULTIPLO_FRAGMENTO,
        ),
        transporte=transporte,
    )
    assert resultado.outcome is DesenlaceReconciliacion.desconocido
    assert resultado.confidence is ConfianzaReconciliacion.sin_evidencia
    assert resultado.exige_revision
    assert not resultado.permite_nueva_sesion
    assert transporte.llamadas == [], "no hay nada a lo que llamar sin session_url"


def test_el_desconocido_por_reinicio_explica_la_limitacion():
    """La evidencia dice por qué no se sabe, no se limita a decir que no se sabe."""
    resultado = reconciliar(
        _peticion(
            sesion=None,
            reason=MotivoReconciliacion.proceso_interrumpido,
            last_known_bytes=MULTIPLO_FRAGMENTO,
        ),
        transporte=TransporteFalso(guion=[incompleto(0)]),
    )
    texto = " ".join(resultado.evidence)
    assert "URL de sesión" in texto
    assert "catálogo del canal" in texto


def test_una_peticion_por_reinicio_no_puede_traer_sesion_viva():
    """El contrato impide representar un reinicio que conservara el URL."""
    run_id = uuid4()
    with pytest.raises(ValueError, match="PROCESS_INTERRUPTED"):
        ReconciliationRequest(
            run_id=run_id,
            attempt=1,
            reason=MotivoReconciliacion.proceso_interrumpido,
            publication_fingerprint=FINGERPRINT,
            upload_session=_sesion(run_id, 0),
        )


# ---------------------------------------------------------------------------
# Invariantes del contrato de resultado
# ---------------------------------------------------------------------------


def test_no_hay_confirmado_subido_sin_video_id():
    with pytest.raises(ValueError, match="sin video_id"):
        ReconciliationResult(
            run_id=uuid4(),
            attempt=1,
            outcome=DesenlaceReconciliacion.confirmado_subido,
            confidence=ConfianzaReconciliacion.provider_confirmado,
        )


def test_no_hay_confirmado_subido_sin_confirmacion_del_proveedor():
    with pytest.raises(ValueError, match="confirmación del proveedor"):
        ReconciliationResult(
            run_id=uuid4(),
            attempt=1,
            outcome=DesenlaceReconciliacion.confirmado_subido,
            video_id=VIDEO_ID,
            confidence=ConfianzaReconciliacion.provider_parcial,
        )


def test_un_desconocido_no_puede_traer_video_id():
    with pytest.raises(ValueError, match="hay video_id"):
        ReconciliationResult(
            run_id=uuid4(),
            attempt=1,
            outcome=DesenlaceReconciliacion.desconocido,
            video_id=VIDEO_ID,
            confidence=ConfianzaReconciliacion.sin_evidencia,
        )


def test_un_desconocido_no_puede_presumir_de_evidencia():
    with pytest.raises(ValueError, match="contradicción"):
        ReconciliationResult(
            run_id=uuid4(),
            attempt=1,
            outcome=DesenlaceReconciliacion.desconocido,
            confidence=ConfianzaReconciliacion.provider_confirmado,
        )


def test_solo_confirmado_no_subido_autoriza_una_sesion_nueva():
    """La regla absoluta, escrita en el contrato."""
    for desenlace, confianza in (
        (DesenlaceReconciliacion.confirmado_subido, ConfianzaReconciliacion.provider_confirmado),
        (DesenlaceReconciliacion.subida_en_curso, ConfianzaReconciliacion.provider_parcial),
        (DesenlaceReconciliacion.desconocido, ConfianzaReconciliacion.sin_evidencia),
    ):
        resultado = ReconciliationResult(
            run_id=uuid4(),
            attempt=1,
            outcome=desenlace,
            video_id=VIDEO_ID if desenlace is DesenlaceReconciliacion.confirmado_subido else None,
            confidence=confianza,
        )
        assert not resultado.permite_nueva_sesion, desenlace

    permitido = ReconciliationResult(
        run_id=uuid4(),
        attempt=1,
        outcome=DesenlaceReconciliacion.confirmado_no_subido,
        confidence=ConfianzaReconciliacion.provider_confirmado,
    )
    assert permitido.permite_nueva_sesion


def test_el_fingerprint_es_determinista_y_cambia_con_el_contenido():
    run_id = uuid4()
    a = publication_fingerprint(run_id, "a" * 64, "b" * 64)
    assert a == publication_fingerprint(run_id, "a" * 64, "b" * 64)
    assert a != publication_fingerprint(run_id, "c" * 64, "b" * 64)
    assert a != publication_fingerprint(run_id, "a" * 64, "c" * 64)
    assert a != publication_fingerprint(uuid4(), "a" * 64, "b" * 64)


def test_el_fingerprint_no_se_confunde_con_la_clave_de_idempotencia():
    from app.contracts.models import clave_idempotencia

    run_id = uuid4()
    assert publication_fingerprint(run_id, "a" * 64, "b" * 64) != clave_idempotencia(run_id)
