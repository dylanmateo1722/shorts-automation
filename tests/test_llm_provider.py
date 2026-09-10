"""Proveedor LLM: interfaz, proveedor falso y cliente compatible con OpenAI.

El cliente HTTP se prueba sustituyendo ``urlopen`` por respuestas y errores
reales de ``urllib``; lo que se valida es nuestro mapeo de códigos a errores
tipados, no que ``urllib`` funcione.
"""

from __future__ import annotations

import io
import json
import socket
import urllib.error

import pytest

from app.adapters.llm import ProveedorFalso, construir_proveedor
from app.adapters.llm.base import ProveedorLLM
from app.adapters.llm.openai_compatible import ProveedorOpenAICompatible
from app.config.settings import Settings
from app.core.errors import (
    ConfiguracionInvalida,
    ErrorTransitorio,
    LimiteDeTasa,
    RespuestaInvalida,
    TiempoAgotado,
)

# --- interpretación de la respuesta ----------------------------------------


def test_json_limpio_se_interpreta():
    assert ProveedorLLM.interpretar_json('{"a": 1}') == {"a": 1}


def test_json_envuelto_en_bloque_de_codigo_se_acepta():
    """Algunos modelos añaden la valla pese a pedirles que no lo hagan."""
    assert ProveedorLLM.interpretar_json('```json\n{"a": 1}\n```') == {"a": 1}


@pytest.mark.parametrize("bruto", ["", "   ", "no soy json", "{roto", "null"])
def test_respuestas_no_utilizables_se_rechazan(bruto):
    with pytest.raises(RespuestaInvalida):
        ProveedorLLM.interpretar_json(bruto)


def test_un_json_que_no_es_objeto_se_rechaza():
    with pytest.raises(RespuestaInvalida, match="objeto"):
        ProveedorLLM.interpretar_json('["a", "b"]')


# --- proveedor falso -------------------------------------------------------


def test_el_falso_devuelve_las_respuestas_en_orden():
    proveedor = ProveedorFalso([{"n": 1}, {"n": 2}])
    assert proveedor.generar_json("p1") == {"n": 1}
    assert proveedor.generar_json("p2") == {"n": 2}
    assert proveedor.llamadas == 2
    assert proveedor.prompts_recibidos == ["p1", "p2"]


def test_el_falso_sin_respuestas_lo_dice_claramente():
    with pytest.raises(ConfiguracionInvalida, match="LLM_FAKE_RESPONSES"):
        ProveedorFalso().generar_json("p")


def test_el_falso_admite_una_funcion():
    proveedor = ProveedorFalso(lambda prompt: {"eco": len(prompt)})
    assert proveedor.generar_json("hola") == {"eco": 4}


def test_el_falso_carga_respuestas_de_un_archivo(tmp_path):
    ruta = tmp_path / "r.json"
    ruta.write_text(json.dumps([{"text": "hola"}]), encoding="utf-8")
    assert ProveedorFalso.desde_archivo(ruta).generar_json("p") == {"text": "hola"}


def test_archivo_de_respuestas_mal_formado(tmp_path):
    ruta = tmp_path / "r.json"
    ruta.write_text('{"no": "es una lista"}', encoding="utf-8")
    with pytest.raises(ConfiguracionInvalida):
        ProveedorFalso.desde_archivo(ruta)


# --- fábrica ---------------------------------------------------------------


def test_sin_proveedor_configurado_falla_pronto():
    with pytest.raises(ConfiguracionInvalida, match="LLM_PROVIDER"):
        construir_proveedor(Settings())


def test_proveedor_desconocido_lista_los_admitidos():
    with pytest.raises(ConfiguracionInvalida, match="Admitidos"):
        construir_proveedor(Settings(llm_provider="inventado"))


def test_la_fabrica_construye_el_falso(monkeypatch):
    monkeypatch.delenv("LLM_FAKE_RESPONSES", raising=False)
    proveedor = construir_proveedor(Settings(llm_provider="fake", llm_model="m"))
    assert isinstance(proveedor, ProveedorFalso)
    assert proveedor.nombre == "fake" and proveedor.modelo == "m"


def test_la_fabrica_construye_el_compatible(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "clave-de-prueba-larga")
    proveedor = construir_proveedor(
        Settings(llm_provider="moonshot", llm_model="k2", llm_base_url="https://x/v1")
    )
    assert isinstance(proveedor, ProveedorOpenAICompatible)
    assert proveedor.nombre == "moonshot"


# --- cliente compatible con OpenAI -----------------------------------------


def _cliente(**extra) -> ProveedorOpenAICompatible:
    base = dict(base_url="https://api.ejemplo/v1", api_key="clave-larga-de-prueba",
                modelo="modelo-x", timeout_s=5)
    base.update(extra)
    return ProveedorOpenAICompatible(**base)


@pytest.mark.parametrize(
    "falta, patron",
    [("base_url", "LLM_BASE_URL"), ("modelo", "LLM_MODEL"), ("api_key", "LLM_API_KEY")],
)
def test_configuracion_incompleta_se_detecta_al_construir(falta, patron):
    with pytest.raises(ConfiguracionInvalida, match=patron):
        _cliente(**{falta: ""})


def _respuesta(contenido: str):
    cuerpo = json.dumps({"choices": [{"message": {"content": contenido}}]})

    class _Falsa(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Falsa(cuerpo.encode("utf-8"))


def test_llamada_correcta_devuelve_json(monkeypatch):
    capturado = {}

    def fake_urlopen(peticion, timeout=None):
        capturado["url"] = peticion.full_url
        capturado["cuerpo"] = json.loads(peticion.data.decode("utf-8"))
        capturado["auth"] = peticion.get_header("Authorization")
        return _respuesta('{"text": "hola"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert _cliente().generar_json("mi prompt") == {"text": "hola"}

    assert capturado["url"] == "https://api.ejemplo/v1/chat/completions"
    assert capturado["cuerpo"]["model"] == "modelo-x"
    assert capturado["cuerpo"]["messages"][0]["content"] == "mi prompt"
    assert capturado["cuerpo"]["response_format"] == {"type": "json_object"}
    assert capturado["auth"] == "Bearer clave-larga-de-prueba"


def test_modo_json_desactivable(monkeypatch):
    capturado = {}

    def fake_urlopen(peticion, timeout=None):
        capturado["cuerpo"] = json.loads(peticion.data.decode("utf-8"))
        return _respuesta('{"text": "hola"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _cliente(modo_json=False).generar_json("p")
    assert "response_format" not in capturado["cuerpo"]


def _falla_con(error):
    def fake_urlopen(peticion, timeout=None):
        raise error

    return fake_urlopen


def test_429_es_limite_de_tasa(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _falla_con(
        urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)))
    with pytest.raises(LimiteDeTasa) as exc:
        _cliente().generar_json("p")
    assert exc.value.retryable is True


@pytest.mark.parametrize("codigo", [500, 502, 503, 504, 408])
def test_errores_de_servidor_son_transitorios(monkeypatch, codigo):
    monkeypatch.setattr("urllib.request.urlopen", _falla_con(
        urllib.error.HTTPError("u", codigo, "err", {}, None)))
    with pytest.raises(ErrorTransitorio) as exc:
        _cliente().generar_json("p")
    assert exc.value.retryable is True


@pytest.mark.parametrize("codigo", [400, 401, 403, 404, 422])
def test_errores_del_cliente_no_son_reintentables(monkeypatch, codigo):
    """Reintentar una petición mal formada repite el fallo y cuesta dinero."""
    monkeypatch.setattr("urllib.request.urlopen", _falla_con(
        urllib.error.HTTPError("u", codigo, "err", {}, None)))
    with pytest.raises(RespuestaInvalida) as exc:
        _cliente().generar_json("p")
    assert exc.value.retryable is False


def test_timeout_es_transitorio(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _falla_con(socket.timeout()))
    with pytest.raises(TiempoAgotado):
        _cliente().generar_json("p")


def test_fallo_de_conexion_es_transitorio(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _falla_con(
        urllib.error.URLError("sin red")))
    with pytest.raises(ErrorTransitorio):
        _cliente().generar_json("p")


def test_envoltura_inesperada_se_rechaza(monkeypatch):
    def fake_urlopen(peticion, timeout=None):
        class _F(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _F(json.dumps({"sin": "choices"}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RespuestaInvalida, match="envoltura"):
        _cliente().generar_json("p")


def test_el_error_http_no_propaga_la_credencial(monkeypatch):
    """El cuerpo de un error puede repetir la clave enviada."""
    monkeypatch.setattr("urllib.request.urlopen", _falla_con(
        urllib.error.HTTPError("u", 401, "clave-larga-de-prueba inválida", {}, None)))
    with pytest.raises(RespuestaInvalida) as exc:
        _cliente().generar_json("p")
    assert "clave-larga-de-prueba" not in str(exc.value)
