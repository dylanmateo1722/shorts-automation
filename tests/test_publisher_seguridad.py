"""Que nada de lo que no debe salir, salga.

Tres cosas distintas que este Gate añade a la superficie de fuga: el
``session_url``, el access token y la cabecera ``Authorization``. Los tests
recorren registros, salida estándar, artefactos serializados y el ``repr`` de
cada objeto que los toca.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from uuid import uuid4

import pytest

from app.contracts.models import (
    EstadoPublicacion,
    MotivoReconciliacion,
    PublishJob,
    PublishResult,
    ReconciliationRequest,
    ReconciliationResult,
    UploadSession,
    clave_idempotencia,
    publication_fingerprint,
)
from app.core.redaction import contiene_secreto, redactar
from app.core.workspace import Workspace
from app.adapters.youtube.publisher import publicar
from app.pipeline.publicacion import ResultadoGate

from conftest_g72 import (
    ACCESS_TOKEN,
    SESSION_URL,
    TransporteFalso,
    completado,
    error,
    jitter_fijo,
    metadata,
    respuesta_perdida,
    sesion_abierta,
    sin_dormir,
    token,
    video_remoto,
)

SECRETO = "SESION-SECRETA-123"
CONTENIDO = b"MP4-de-prueba-" * 64


def _gate(tmp_path: Path) -> ResultadoGate:
    destino = tmp_path / "final_video.mp4"
    destino.write_bytes(CONTENIDO)
    return ResultadoGate(
        listo=True,
        video_path=destino,
        video_sha256=hashlib.sha256(CONTENIDO).hexdigest(),
        total_bytes=len(CONTENIDO),
    )


def _sesion(run_id=None) -> UploadSession:
    return UploadSession(
        session_url=SESSION_URL,
        run_id=run_id or uuid4(),
        attempt=1,
        video_sha256="a" * 64,
        metadata_sha256="b" * 64,
        total_bytes=1000,
        bytes_confirmed=250,
    )


# ---------------------------------------------------------------------------
# 24. El session_url
# ---------------------------------------------------------------------------


def test_el_session_url_no_esta_en_el_repr():
    sesion = _sesion()
    assert SECRETO not in repr(sesion)
    assert SECRETO not in str(sesion)


def test_el_session_url_no_esta_en_el_volcado():
    sesion = _sesion()
    assert SECRETO not in sesion.model_dump_json()
    assert "session_url" not in sesion.model_dump()


def test_una_sesion_no_puede_reconstruirse_desde_su_volcado():
    """Es el mecanismo que hace estructural el «tras un reinicio, UNKNOWN»."""
    volcado = _sesion().model_dump_json()
    with pytest.raises(Exception):
        UploadSession.model_validate_json(volcado)


def test_el_session_url_no_llega_al_artefacto_persistido(tmp_path):
    """La prueba de fuego: lo que ``Workspace`` escribe en ``runs/``."""
    run_id = uuid4()
    ws = Workspace.en_directorio(tmp_path, run_id)
    ws.crear()
    sesion = _sesion(run_id)
    trabajo = PublishJob(
        run_id=run_id,
        state=EstadoPublicacion.no_listo,
        idempotency_key=clave_idempotencia(run_id),
        upload_session_ref=sesion.referencia,
    )
    ruta = ws.escribir_artefacto("publish_job", trabajo)
    escrito = ruta.read_text(encoding="utf-8")
    assert SECRETO not in escrito
    assert SESSION_URL not in escrito
    assert sesion.referencia in escrito, "la huella sí debe quedar"


def test_la_peticion_de_reconciliacion_no_filtra_el_url(tmp_path):
    """Aunque la sesión viaje dentro de la petición, el URL no se serializa."""
    run_id = uuid4()
    peticion = ReconciliationRequest(
        run_id=run_id,
        attempt=1,
        reason=MotivoReconciliacion.respuesta_perdida,
        publication_fingerprint=publication_fingerprint(run_id, "a" * 64, "b" * 64),
        upload_session=_sesion(run_id),
        last_known_bytes=250,
    )
    volcado = peticion.model_dump_json()
    assert SECRETO not in volcado
    assert SESSION_URL not in volcado


def test_la_referencia_no_revela_el_url():
    sesion = _sesion()
    assert sesion.referencia == hashlib.sha256(SESSION_URL.encode()).hexdigest()
    assert SECRETO not in sesion.referencia


# ---------------------------------------------------------------------------
# 23 y 25. El access token
# ---------------------------------------------------------------------------


def test_el_access_token_no_aparece_en_los_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    transporte = TransporteFalso(
        guion=[sesion_abierta(), completado(), video_remoto()]
    )
    run_id = uuid4()
    publicar(
        run_id=run_id,
        metadata=metadata(run_id),
        gate=_gate(tmp_path),
        token=token(),
        transporte=transporte,
        dormir=sin_dormir,
        aleatorio=jitter_fijo,
    )
    texto = caplog.text
    assert ACCESS_TOKEN not in texto
    assert SECRETO not in texto


def test_el_access_token_no_aparece_en_los_artefactos(tmp_path):
    transporte = TransporteFalso(
        guion=[sesion_abierta(), completado(), video_remoto()]
    )
    run_id = uuid4()
    salida = publicar(
        run_id=run_id,
        metadata=metadata(run_id),
        gate=_gate(tmp_path),
        token=token(),
        transporte=transporte,
        dormir=sin_dormir,
        aleatorio=jitter_fijo,
    )
    for artefacto in (salida.job, salida.result):
        if artefacto is None:
            continue
        volcado = artefacto.model_dump_json()
        assert ACCESS_TOKEN not in volcado
        assert SECRETO not in volcado
        assert "Bearer" not in volcado


def test_ningun_contrato_de_g72_tiene_campos_de_credencial():
    """El mismo criterio que Gate 7.0 aplicó a sus cuatro contratos."""
    prohibidos = (
        "token", "access_token", "refresh_token", "secret", "client_secret",
        "api_key", "apikey", "password", "credential", "cookie", "bearer", "auth",
    )
    for modelo in (ReconciliationRequest, ReconciliationResult, PublishJob, PublishResult):
        for campo in modelo.model_fields:
            assert not any(p in campo.lower() for p in prohibidos), (
                f"{modelo.__name__}.{campo}"
            )


def test_el_mensaje_de_error_no_arrastra_el_url_de_sesion(tmp_path):
    """Un fallo sobre la sesión no puede delatarla al explicarse."""
    transporte = TransporteFalso(
        guion=[sesion_abierta(), respuesta_perdida(final=True), error(404)]
    )
    run_id = uuid4()
    salida = publicar(
        run_id=run_id,
        metadata=metadata(run_id),
        gate=_gate(tmp_path),
        token=token(),
        transporte=transporte,
        dormir=sin_dormir,
        aleatorio=jitter_fijo,
    )
    motivo = salida.job.reason or ""
    assert SECRETO not in motivo
    assert salida.result is not None
    evidencia = " ".join(salida.result.reconciliation.evidence)
    assert SECRETO not in evidencia
    assert SESSION_URL not in evidencia


def test_la_autorizacion_viaja_en_la_cabecera_y_no_en_la_url(tmp_path):
    """Un token en el query string acabaría en cualquier registro de acceso."""
    transporte = TransporteFalso(
        guion=[sesion_abierta(), completado(), video_remoto()]
    )
    run_id = uuid4()
    publicar(
        run_id=run_id,
        metadata=metadata(run_id),
        gate=_gate(tmp_path),
        token=token(),
        transporte=transporte,
        dormir=sin_dormir,
        aleatorio=jitter_fijo,
    )
    for llamada in transporte.llamadas:
        assert ACCESS_TOKEN not in llamada.url


def test_el_redactor_del_proyecto_cubre_el_token_de_acceso():
    entorno = {"YOUTUBE_ACCESS_TOKEN": ACCESS_TOKEN}
    texto = f"la petición llevaba {ACCESS_TOKEN}"
    assert contiene_secreto(texto, entorno)
    assert ACCESS_TOKEN not in redactar(texto, entorno)
