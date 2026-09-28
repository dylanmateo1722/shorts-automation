"""El proveedor real de Chat Completions, contra un servidor HTTP de verdad.

No es un mock del cliente: es el cliente real —su ``urlopen``, sus cabeceras, su
JSON, su reintento— hablando con un servidor HTTP que responde como responde un
proveedor. Lo único que no es real es el modelo al otro lado.

Se hace así porque el cliente nunca se había ejercitado contra nada. Un mock del
propio ``generar_json`` habría probado el mock; esto prueba el POST.

Los dos casos que dan sentido al archivo:

* ``test_un_429_se_reintenta_y_la_corrida_no_se_pierde``: sin reintento, un solo
  límite de tasa tiraba la corrida entera, y para cuando se llama al modelo ya
  se ingirió la fuente.
* ``test_una_respuesta_truncada_lo_dice_con_esas_palabras``: el error genérico
  decía «no es JSON válido» y mandaba a buscar el problema al sitio equivocado.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.adapters.llm.openai_compatible import ProveedorOpenAICompatible
from app.core.errors import (
    ConfiguracionInvalida,
    ErrorTransitorio,
    LimiteDeTasa,
    RespuestaInvalida,
)


class _Guion:
    """Lo que el servidor va a responder, y lo que recibió."""

    def __init__(self, respuestas: list[tuple[int, dict | str]]) -> None:
        self.respuestas = respuestas
        self.peticiones: list[dict] = []
        self.cabeceras: list[dict] = []

    def siguiente(self) -> tuple[int, dict | str]:
        indice = min(len(self.peticiones) - 1, len(self.respuestas) - 1)
        return self.respuestas[indice]


def _envoltura(contenido: str, finish: str = "stop") -> dict:
    """La forma real que devuelve Chat Completions."""
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": "modelo-de-prueba",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": contenido},
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.fixture
def servidor():
    """Un servidor HTTP real, en un puerto libre, durante el test."""
    guion: _Guion | None = None

    class Manejador(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - lo fija BaseHTTPRequestHandler
            largo = int(self.headers.get("Content-Length", 0))
            cuerpo = self.rfile.read(largo).decode("utf-8")
            guion.peticiones.append(json.loads(cuerpo))
            guion.cabeceras.append(dict(self.headers))

            codigo, carga = guion.siguiente()
            datos = carga if isinstance(carga, str) else json.dumps(carga)
            bruto = datos.encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(bruto)))
            if codigo == 429:
                self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(bruto)

        def log_message(self, *_a):
            """Silencio: el servidor no tiene que ensuciar la salida del test."""

    httpd = HTTPServer(("127.0.0.1", 0), Manejador)
    hilo = threading.Thread(target=httpd.serve_forever, daemon=True)
    hilo.start()

    def montar(respuestas):
        nonlocal guion
        guion = _Guion(respuestas)
        return guion

    montar.url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    yield montar
    httpd.shutdown()
    httpd.server_close()


def _proveedor(servidor, **extra) -> ProveedorOpenAICompatible:
    campos = dict(
        base_url=servidor.url,
        api_key="clave-de-prueba-no-real",
        modelo="modelo-de-prueba",
        nombre="openai_compatible",
        dormir=lambda _s: None,
    )
    campos.update(extra)
    return ProveedorOpenAICompatible(**campos)


# ---------------------------------------------------------------------------
# 1. El camino feliz, sobre HTTP real
# ---------------------------------------------------------------------------


def test_el_proveedor_real_habla_con_un_servidor_real(servidor):
    guion = servidor([(200, _envoltura('{"text": "hola", "segments": []}'))])

    datos = _proveedor(servidor).generar_json("dame json")

    assert datos == {"text": "hola", "segments": []}
    assert len(guion.peticiones) == 1


def test_la_peticion_lleva_lo_que_la_api_espera(servidor):
    guion = servidor([(200, _envoltura('{"ok": true}'))])

    _proveedor(servidor, max_tokens=1234).generar_json("el prompt")

    enviado = guion.peticiones[0]
    assert enviado["model"] == "modelo-de-prueba"
    assert enviado["messages"] == [{"role": "user", "content": "el prompt"}]
    assert enviado["max_tokens"] == 1234
    assert enviado["response_format"] == {"type": "json_object"}


def test_la_credencial_viaja_en_la_cabecera_y_no_en_el_cuerpo(servidor):
    guion = servidor([(200, _envoltura('{"ok": true}'))])

    _proveedor(servidor).generar_json("x")

    assert guion.cabeceras[0]["Authorization"] == "Bearer clave-de-prueba-no-real"
    assert "clave-de-prueba" not in json.dumps(guion.peticiones[0])


def test_un_json_entre_vallas_se_interpreta(servidor):
    """Los modelos envuelven en ```json pese a pedirles que no lo hagan."""
    servidor([(200, _envoltura('```json\n{"text": "vallado"}\n```'))])
    assert _proveedor(servidor).generar_json("x") == {"text": "vallado"}


# ---------------------------------------------------------------------------
# 2. Límite de tasa: el fallo que tiraba la corrida entera
# ---------------------------------------------------------------------------


def test_un_429_se_reintenta_y_la_corrida_no_se_pierde(servidor):
    """Sin esto, un solo límite de tasa tira todo el trabajo ya hecho.

    Para cuando se llama al modelo la fuente ya se ingirió. Rendirse ante un
    429 —que contra una API real es rutina, no anomalía— significa tirarlo.
    """
    guion = servidor(
        [
            (429, {"error": {"message": "slow down"}}),
            (429, {"error": {"message": "slow down"}}),
            (200, _envoltura('{"text": "al tercer intento"}')),
        ]
    )

    datos = _proveedor(servidor).generar_json("x")

    assert datos == {"text": "al tercer intento"}
    assert len(guion.peticiones) == 3


def test_un_429_persistente_acaba_fallando_como_limite_de_tasa(servidor):
    guion = servidor([(429, {"error": {}})])

    with pytest.raises(LimiteDeTasa):
        _proveedor(servidor, max_intentos=3).generar_json("x")

    assert len(guion.peticiones) == 3


def test_un_503_tambien_se_reintenta(servidor):
    guion = servidor(
        [(503, {"error": {}}), (200, _envoltura('{"text": "recuperado"}'))]
    )
    assert _proveedor(servidor).generar_json("x") == {"text": "recuperado"}
    assert len(guion.peticiones) == 2


def test_un_400_no_se_reintenta(servidor):
    """Repetir una petición mal formada la deja igual de mal formada."""
    guion = servidor([(400, {"error": {"message": "bad request"}})])

    with pytest.raises(RespuestaInvalida):
        _proveedor(servidor).generar_json("x")

    assert len(guion.peticiones) == 1


def test_un_401_no_se_reintenta(servidor):
    """Una credencial inválida no mejora insistiendo, y gasta cuota."""
    guion = servidor([(401, {"error": {"message": "invalid api key"}})])

    with pytest.raises(RespuestaInvalida):
        _proveedor(servidor).generar_json("x")

    assert len(guion.peticiones) == 1


def test_el_error_del_proveedor_no_repite_la_credencial(servidor):
    """El cuerpo de un 401 a veces devuelve la clave enviada."""
    servidor([(401, {"error": {"message": "invalid key clave-de-prueba-no-real"}})])

    with pytest.raises(RespuestaInvalida) as exc:
        _proveedor(servidor).generar_json("x")

    assert "clave-de-prueba-no-real" not in str(exc.value)


# ---------------------------------------------------------------------------
# 3. Respuestas que el modelo devuelve mal
# ---------------------------------------------------------------------------


def test_una_respuesta_truncada_lo_dice_con_esas_palabras(servidor):
    """El JSON no está mal escrito: está incompleto, y el remedio es otro.

    Decir «no es JSON válido» manda a revisar el prompt cuando lo que hay que
    subir es el presupuesto de tokens.
    """
    servidor([(200, _envoltura('{"text": "se corta a mitad', finish="length"))])

    with pytest.raises(RespuestaInvalida) as exc:
        _proveedor(servidor).generar_json("x")

    mensaje = str(exc.value)
    assert "cortada" in mensaje
    assert "LLM_MAX_TOKENS" in mensaje


def test_una_respuesta_que_no_es_json_se_rechaza(servidor):
    servidor([(200, _envoltura("lo siento, no puedo ayudarte con eso"))])
    with pytest.raises(RespuestaInvalida):
        _proveedor(servidor).generar_json("x")


def test_una_lista_no_es_un_objeto(servidor):
    servidor([(200, _envoltura('["no", "es", "un", "objeto"]'))])
    with pytest.raises(RespuestaInvalida):
        _proveedor(servidor).generar_json("x")


def test_una_envoltura_sin_choices_se_rechaza(servidor):
    servidor([(200, {"id": "x", "object": "chat.completion"})])
    with pytest.raises(RespuestaInvalida):
        _proveedor(servidor).generar_json("x")


def test_un_cuerpo_que_no_es_json_se_rechaza(servidor):
    servidor([(200, "<html>502 Bad Gateway</html>")])
    with pytest.raises(RespuestaInvalida):
        _proveedor(servidor).generar_json("x")


# ---------------------------------------------------------------------------
# 4. Configuración: el error nombra la variable que falta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "falta, variable",
    [
        ("base_url", "LLM_BASE_URL"),
        ("modelo", "LLM_MODEL"),
        ("api_key", "LLM_API_KEY"),
    ],
)
def test_la_configuracion_incompleta_nombra_la_variable(falta, variable):
    campos = {
        "base_url": "https://api.ejemplo.com/v1",
        "modelo": "m",
        "api_key": "k",
        falta: "",
    }
    with pytest.raises(ConfiguracionInvalida) as exc:
        ProveedorOpenAICompatible(**campos)
    assert variable in str(exc.value)


def test_el_presupuesto_de_tokens_sale_de_la_configuracion():
    """Las etapas llaman sin indicarlo; si quedara fijo en el código no habría
    forma de subirlo cuando una fuente larga lo agota."""
    import os

    from app.adapters.llm import construir_proveedor
    from app.config.settings import Settings

    previo = dict(os.environ)
    try:
        os.environ.update(
            LLM_PROVIDER="openai",
            LLM_BASE_URL="https://api.ejemplo.com/v1",
            LLM_MODEL="m",
            LLM_API_KEY="k",
            LLM_MAX_TOKENS="9001",
        )
        proveedor = construir_proveedor(Settings.desde_entorno())
        assert proveedor.max_tokens == 9001
    finally:
        os.environ.clear()
        os.environ.update(previo)
