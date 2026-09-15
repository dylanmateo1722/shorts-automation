"""Seguridad: ningún secreto puede llegar a los logs ni al manifest.

El repositorio es público y los logs de sus workflows también, así que un
secreto filtrado queda expuesto al mundo. Estos tests son la comprobación de
esa garantía, no un adorno.
"""

from __future__ import annotations

import io
import json
import logging
import uuid

import pytest

from app.adapters.mpt import MPTAdapter
from app.config.settings import Settings
from app.contracts.models import RenderJob
from app.core.logging import configurar_logging, log_evento, obtener_logger
from app.core.manifest import Manifest
from app.core.redaction import contiene_secreto, redactar

SECRETO = "sk-test-supersecreto-1234567890"


@pytest.fixture
def entorno_con_secretos(monkeypatch):
    monkeypatch.setenv("DEMO_API_KEY", SECRETO)
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "1//refreshtoken-larguisimo-abcdef")
    monkeypatch.setenv("LLM_API_KEY", "clave-de-modelo-muy-larga-999")
    return SECRETO


@pytest.fixture
def captura_de_log():
    """Captura la salida ya formateada del logger de la aplicación."""
    configurar_logging()
    logger = obtener_logger()
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    yield buffer
    logger.removeHandler(handler)
    logger.propagate = True


def test_redactar_oculta_valores_del_entorno(entorno_con_secretos):
    salida = redactar(f"config: api_key={SECRETO}")
    assert SECRETO not in salida
    assert "***" in salida


def test_redactar_oculta_formas_de_credencial_no_declaradas():
    """Defensa en profundidad: tokens que no están en el entorno."""
    for token in ("ghp_abcdefghijklmnopqrst", "ya29.AbCdEfGhIjKlMnOp",
                  "Bearer abcdefghijklmnopqrstuvwx"):
        assert token not in redactar(f"cabecera: {token}")


def test_valores_cortos_no_se_enmascaran(monkeypatch):
    """Enmascarar valores triviales llenaría los logs de falsos positivos."""
    monkeypatch.setenv("X_TOKEN", "es")
    assert redactar("idioma es") == "idioma es"


def test_el_secreto_no_llega_al_log(entorno_con_secretos, captura_de_log):
    log_evento(uuid.uuid4(), "render", "start", detalle=f"key={SECRETO}")
    contenido = captura_de_log.getvalue()

    assert SECRETO not in contenido
    assert not contiene_secreto(contenido)
    assert "***" in contenido


def test_el_secreto_no_llega_al_log_por_excepcion(entorno_con_secretos, captura_de_log):
    obtener_logger().error("fallo al autenticar con %s", SECRETO)
    assert SECRETO not in captura_de_log.getvalue()


def test_la_configuracion_publica_no_expone_secretos(entorno_con_secretos, tmp_path):
    """publico() es una lista cerrada de campos, no un volcado del entorno."""
    publico = Settings(raiz_proyecto=tmp_path).publico()
    serializado = json.dumps(publico)

    assert not contiene_secreto(serializado)
    assert set(publico) == {
        "runs_dir", "mpt_dir", "mpt_timeout_s", "log_level",
        "llm_provider", "llm_model", "target_language", "default_wpm",
        "translation_prompt_version", "adaptation_prompt_version",
        "tts_provider", "tts_voice", "tts_rate", "tts_pitch",
        "subtitle_margin_v", "subtitle_margin_h",
        "subtitle_font", "subtitle_font_size",
    }
    assert "llm_api_key" not in publico
    # El TTS primario no usa credencial y el fallback no existe, pero la
    # variable del fallback está documentada en .env.example: que no aparezca
    # aquí es parte de la lista cerrada.
    assert "elevenlabs_api_key" not in publico


def test_el_manifest_no_contiene_secretos(entorno_con_secretos, tmp_path):
    settings = Settings(raiz_proyecto=tmp_path)
    manifest = Manifest(
        run_id=uuid.uuid4(), config=settings.publico(), versions={"app": "0.1.0"}
    )
    ruta = manifest.guardar(tmp_path)
    contenido = ruta.read_text(encoding="utf-8")

    assert not contiene_secreto(contenido)
    assert SECRETO not in contenido


def test_la_credencial_del_llm_solo_vive_en_el_entorno(monkeypatch, tmp_path):
    """No es un campo del dataclass, así que no puede colarse en un repr."""
    monkeypatch.setenv("LLM_API_KEY", SECRETO)
    settings = Settings(raiz_proyecto=tmp_path, llm_provider="fake")

    assert settings.llm_api_key == SECRETO
    assert SECRETO not in repr(settings)
    assert not contiene_secreto(repr(settings))


def test_la_credencial_del_llm_no_llega_al_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", SECRETO)
    settings = Settings(
        raiz_proyecto=tmp_path, llm_provider="moonshot", llm_model="k2"
    )
    manifest = Manifest(run_id=uuid.uuid4(), config=settings.publico())
    contenido = manifest.guardar(tmp_path).read_text(encoding="utf-8")

    assert SECRETO not in contenido
    assert not contiene_secreto(contenido)
    # El proveedor y el modelo sí se registran: no son secretos y hacen falta
    # para saber con qué se produjo un artefacto.
    assert "moonshot" in contenido and "k2" in contenido


def test_un_error_del_proveedor_no_filtra_la_credencial(monkeypatch, captura_de_log):
    """El cuerpo de un 401 puede repetir la clave enviada."""
    import urllib.error

    from app.adapters.llm.openai_compatible import ProveedorOpenAICompatible
    from app.core.errors import RespuestaInvalida

    monkeypatch.setenv("LLM_API_KEY", SECRETO)
    cliente = ProveedorOpenAICompatible(
        base_url="https://api.ejemplo/v1", api_key=SECRETO, modelo="m", timeout_s=1
    )

    def falla(peticion, timeout=None):
        raise urllib.error.HTTPError("u", 401, f"clave {SECRETO} inválida", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", falla)
    with pytest.raises(RespuestaInvalida) as exc:
        cliente.generar_json("p")

    obtener_logger().error("fallo del proveedor: %s", exc.value.mensaje)
    assert SECRETO not in str(exc.value)
    assert SECRETO not in captura_de_log.getvalue()


def test_el_log_del_motor_se_redacta(
    entorno_con_secretos, settings_falsos, workspace, entrada_controlada, monkeypatch
):
    """Aunque el motor filtre un secreto por stderr, no queda en disco en claro."""
    monkeypatch.setenv("FAKE_MPT_MODE", "secreto")
    job = RenderJob(
        run_id=workspace.run_id, task_id=workspace.run_id, script="x",
        audio_path=workspace.relativa(entrada_controlada["audio"]),
        materials=[workspace.relativa(entrada_controlada["material"])],
    )
    MPTAdapter(settings_falsos, workspace).render(job)

    log = workspace.ruta("render/mpt.log").read_text(encoding="utf-8")
    assert SECRETO not in log
    assert "***" in log
