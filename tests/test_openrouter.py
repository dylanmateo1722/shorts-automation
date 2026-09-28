"""OpenRouter sobre el adaptador compatible con OpenAI.

No hay proveedor nuevo: OpenRouter habla Chat Completions, así que se configura
el que ya existe. Lo que sí es propio de OpenRouter son tres cosas, y son las
que se prueban aquí.

El caso que da sentido al archivo es
``test_json_object_viene_desactivado_por_defecto``. OpenRouter documenta que, si
el modelo elegido no soporta salidas estructuradas, **la petición falla** en vez
de ignorarse el parámetro. Sus modelos gratuitos a menudo no las soportan, así
que enviarlo por defecto convertiría la primera llamada en un error.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.adapters.llm import BASE_URL_POR_PROVEEDOR, construir_proveedor
from app.adapters.llm.base import ProveedorLLM
from app.config.settings import Settings
from app.core.errors import ConfiguracionInvalida, RespuestaInvalida

VARIABLES = (
    "LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL", "LLM_JSON_MODE",
    "LLM_MAX_TOKENS", "OPENROUTER_API_KEY", "OPENROUTER_SITE_URL",
    "OPENROUTER_APP_NAME",
)


@pytest.fixture
def entorno(monkeypatch):
    """Entorno limpio: ninguna de estas variables filtra de un test a otro."""
    for nombre in VARIABLES:
        monkeypatch.delenv(nombre, raising=False)
    return monkeypatch


def _openrouter(entorno, **extra) -> Settings:
    entorno.setenv("LLM_PROVIDER", "openrouter")
    entorno.setenv("LLM_MODEL", "google/gemma-4-31b-it:free")
    entorno.setenv("OPENROUTER_API_KEY", "sk-or-v1-clave-de-prueba-no-real")
    for clave, valor in extra.items():
        entorno.setenv(clave, valor)
    return Settings.desde_entorno()


# ---------------------------------------------------------------------------
# 1. Configuración mínima
# ---------------------------------------------------------------------------


def test_basta_la_clave_y_el_modelo(entorno):
    """Sin LLM_BASE_URL: la de OpenRouter se conoce y no hay que teclearla."""
    proveedor = construir_proveedor(_openrouter(entorno))

    assert proveedor.nombre == "openrouter"
    assert proveedor.base_url == "https://openrouter.ai/api/v1"
    assert proveedor.modelo == "google/gemma-4-31b-it:free"


def test_la_base_url_indicada_manda_sobre_la_conocida(entorno):
    """Un proveedor puede servirse desde otro sitio; adivinárselo sería peor."""
    ajustes = _openrouter(entorno, LLM_BASE_URL="https://proxy.interno/v1")
    assert construir_proveedor(ajustes).base_url == "https://proxy.interno/v1"


def test_openrouter_api_key_sirve_como_credencial(entorno):
    """Es el nombre con el que OpenRouter la entrega."""
    proveedor = construir_proveedor(_openrouter(entorno))
    assert proveedor._api_key == "sk-or-v1-clave-de-prueba-no-real"


def test_llm_api_key_manda_si_estan_las_dos(entorno):
    ajustes = _openrouter(entorno, LLM_API_KEY="explicita")
    assert construir_proveedor(ajustes)._api_key == "explicita"


def test_sin_clave_el_error_nombra_la_variable_de_openrouter(entorno):
    """Mandar a LLM_API_KEY a quien tiene OPENROUTER_API_KEY es mandarle a
    buscar lo que ya tiene."""
    entorno.setenv("LLM_PROVIDER", "openrouter")
    entorno.setenv("LLM_MODEL", "x/y:free")

    with pytest.raises(ConfiguracionInvalida) as exc:
        construir_proveedor(Settings.desde_entorno())

    assert "OPENROUTER_API_KEY" in str(exc.value)


def test_otros_proveedores_siguen_nombrando_llm_api_key(entorno):
    entorno.setenv("LLM_PROVIDER", "openai")
    entorno.setenv("LLM_MODEL", "gpt-4o-mini")

    with pytest.raises(ConfiguracionInvalida) as exc:
        construir_proveedor(Settings.desde_entorno())

    assert "LLM_API_KEY" in str(exc.value)
    assert "OPENROUTER" not in str(exc.value)


def test_openrouter_api_key_no_se_usa_con_otro_proveedor(entorno):
    """La clave de OpenRouter no vale en OpenAI; usarla daría un 401 confuso."""
    entorno.setenv("LLM_PROVIDER", "openai")
    entorno.setenv("LLM_MODEL", "gpt-4o-mini")
    entorno.setenv("OPENROUTER_API_KEY", "sk-or-v1-no-deberia-usarse")

    with pytest.raises(ConfiguracionInvalida):
        construir_proveedor(Settings.desde_entorno())


# ---------------------------------------------------------------------------
# 2. La incompatibilidad real: response_format
# ---------------------------------------------------------------------------


def test_json_object_viene_desactivado_por_defecto(entorno):
    """OpenRouter **falla** la petición si el modelo no soporta el parámetro.

    Los modelos gratuitos a menudo no lo soportan, así que enviarlo por defecto
    rompería la primera llamada. Los prompts ya piden JSON y el intérprete sabe
    desenvolverlo, así que no se pierde nada por no pedirlo.
    """
    assert construir_proveedor(_openrouter(entorno)).modo_json is False


def test_se_puede_activar_a_mano_para_un_modelo_que_lo_soporte(entorno):
    ajustes = _openrouter(entorno, LLM_JSON_MODE="1")
    assert construir_proveedor(ajustes).modo_json is True


def test_los_demas_proveedores_lo_siguen_pidiendo(entorno):
    entorno.setenv("LLM_PROVIDER", "openai")
    entorno.setenv("LLM_MODEL", "gpt-4o-mini")
    entorno.setenv("LLM_API_KEY", "k")
    assert construir_proveedor(Settings.desde_entorno()).modo_json is True


# ---------------------------------------------------------------------------
# 3. Atribución: opcional, y solo si se pide
# ---------------------------------------------------------------------------


def test_sin_configurar_no_se_envian_cabeceras_de_atribucion(entorno):
    """Mandar por defecto la URL de un proyecto ajeno le atribuiría un uso que
    no hizo."""
    assert construir_proveedor(_openrouter(entorno)).cabeceras_extra == {}


def test_la_atribucion_se_envia_si_se_configura(entorno):
    ajustes = _openrouter(
        entorno,
        OPENROUTER_SITE_URL="https://ejemplo.org",
        OPENROUTER_APP_NAME="Shorts Foundry",
    )
    assert construir_proveedor(ajustes).cabeceras_extra == {
        "HTTP-Referer": "https://ejemplo.org",
        "X-Title": "Shorts Foundry",
    }


def test_la_atribucion_no_se_aplica_a_otros_proveedores(entorno):
    entorno.setenv("LLM_PROVIDER", "openai")
    entorno.setenv("LLM_MODEL", "m")
    entorno.setenv("LLM_API_KEY", "k")
    entorno.setenv("OPENROUTER_SITE_URL", "https://ejemplo.org")
    assert construir_proveedor(Settings.desde_entorno()).cabeceras_extra == {}


# ---------------------------------------------------------------------------
# 4. Respuestas de modelos gratuitos, que hablan de más
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bruto, descripcion",
    [
        ('{"text": "ok"}', "json limpio"),
        ('```json\n{"text": "ok"}\n```', "entre vallas"),
        ('Aquí tienes el JSON:\n```json\n{"text": "ok"}\n```\n¡Espero que sirva!',
         "prosa antes y después de la valla"),
        ('Claro. {"text": "ok"} Eso es todo.', "prosa sin valla"),
    ],
)
def test_el_json_se_encuentra_aunque_el_modelo_hable_de_mas(bruto, descripcion):
    """Sin ``response_format`` el modelo adorna, y adornar no es equivocarse.

    Descartar una respuesta correcta por venir con un «aquí tienes» delante
    gastaría una llamada y una corrida por un problema de formato.
    """
    assert ProveedorLLM.interpretar_json(bruto) == {"text": "ok"}, descripcion


def test_una_llave_dentro_de_una_cadena_no_confunde_al_extractor():
    """En un guion en español una llave dentro del texto es posible, y contar
    llaves a secas cortaría el objeto por donde no toca."""
    bruto = 'Respuesta: {"text": "una } llave suelta", "n": 1}'
    assert ProveedorLLM.interpretar_json(bruto) == {
        "text": "una } llave suelta",
        "n": 1,
    }


@pytest.mark.parametrize(
    "bruto", ["", "   ", "lo siento, no puedo ayudarte con eso", "[1, 2, 3]"]
)
def test_lo_que_no_es_un_objeto_json_se_sigue_rechazando(bruto):
    """Tolerar el adorno no es tolerar cualquier cosa."""
    with pytest.raises(RespuestaInvalida):
        ProveedorLLM.interpretar_json(bruto)


# ---------------------------------------------------------------------------
# 5. La petición que sale por el cable, contra un servidor HTTP real
# ---------------------------------------------------------------------------


@pytest.fixture
def servidor():
    recibido: dict = {}

    class Manejador(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - lo fija BaseHTTPRequestHandler
            largo = int(self.headers.get("Content-Length", 0))
            recibido["cuerpo"] = json.loads(self.rfile.read(largo).decode("utf-8"))
            # En minúsculas a propósito. Los nombres de cabecera son
            # insensibles a la caja (RFC 9110 §5.1) y ``urllib`` reescribe los
            # nuestros con ``capitalize()``: «HTTP-Referer» sale del proceso
            # como «Http-referer». Comparar con la caja que escribimos haría
            # que una comprobación negativa pasara sin comprobar nada.
            recibido["cabeceras"] = {
                clave.lower(): valor for clave, valor in self.headers.items()
            }
            cuerpo = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": 'Aquí tienes:\n```json\n{"text": "vale"}\n```',
                            },
                            "finish_reason": "stop",
                        }
                    ]
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def log_message(self, *_a):
            """Silencio."""

    httpd = HTTPServer(("127.0.0.1", 0), Manejador)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    recibido["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    yield recibido
    httpd.shutdown()
    httpd.server_close()


def test_la_peticion_real_no_lleva_response_format(entorno, servidor):
    """La comprobación de extremo a extremo de la incompatibilidad.

    No basta con que el objeto diga ``modo_json=False``: lo que importa es lo
    que sale por el cable, porque es lo que OpenRouter rechazaría.
    """
    ajustes = _openrouter(entorno, LLM_BASE_URL=servidor["url"])
    proveedor = construir_proveedor(ajustes)

    assert proveedor.generar_json("dame json") == {"text": "vale"}

    assert "response_format" not in servidor["cuerpo"]
    assert servidor["cuerpo"]["model"] == "google/gemma-4-31b-it:free"
    cabecera = servidor["cabeceras"]["authorization"]
    assert cabecera == "Bearer sk-or-v1-clave-de-prueba-no-real"
    # La atribución no se configuró, así que no viaja.
    assert "http-referer" not in servidor["cabeceras"]
    assert "x-title" not in servidor["cabeceras"]


def test_la_atribucion_configurada_viaja_en_la_peticion(entorno, servidor):
    ajustes = _openrouter(
        entorno,
        LLM_BASE_URL=servidor["url"],
        OPENROUTER_SITE_URL="https://ejemplo.org",
        OPENROUTER_APP_NAME="Shorts Foundry",
    )
    construir_proveedor(ajustes).generar_json("x")

    assert servidor["cabeceras"]["http-referer"] == "https://ejemplo.org"
    assert servidor["cabeceras"]["x-title"] == "Shorts Foundry"


# ---------------------------------------------------------------------------
# 6. Rechazos: cada código tiene un remedio distinto
# ---------------------------------------------------------------------------


@pytest.fixture
def servidor_que_rechaza():
    """Un servidor que responde el código que se le pida en ``codigo``."""
    estado: dict = {"codigo": 401, "intentos": 0}

    class Manejador(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - lo fija BaseHTTPRequestHandler
            estado["intentos"] += 1
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(estado["codigo"])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_a):
            """Silencio."""

    httpd = HTTPServer(("127.0.0.1", 0), Manejador)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    estado["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    yield estado
    httpd.shutdown()
    httpd.server_close()


def test_un_401_dice_que_la_credencial_no_vale_y_no_se_reintenta(
    entorno, servidor_que_rechaza
):
    """Decir solo «401» manda a revisar también el modelo y la base_url, que no
    tienen nada que ver. Y reintentarlo gasta cuatro llamadas para nada: una
    clave inválida sigue siéndolo."""
    servidor_que_rechaza["codigo"] = 401
    ajustes = _openrouter(entorno, LLM_BASE_URL=servidor_que_rechaza["url"])

    with pytest.raises(RespuestaInvalida) as exc:
        construir_proveedor(ajustes).generar_json("x")

    assert "OPENROUTER_API_KEY" in str(exc.value)
    assert servidor_que_rechaza["intentos"] == 1


@pytest.mark.parametrize(
    "codigo, esperado",
    [
        (402, "saldo"),
        (403, "moderó"),
        (404, "LLM_MODEL"),
    ],
)
def test_los_demas_rechazos_dicen_lo_que_documenta_openrouter(
    entorno, servidor_que_rechaza, codigo, esperado
):
    """Los cuatro rechazos llegaban como «rechazó la petición con 4xx», y los
    cuatro tienen remedios incompatibles: cambiar la clave, pagar, cambiar de
    modelo o corregir el identificador."""
    servidor_que_rechaza["codigo"] = codigo
    ajustes = _openrouter(entorno, LLM_BASE_URL=servidor_que_rechaza["url"])

    with pytest.raises(RespuestaInvalida) as exc:
        construir_proveedor(ajustes).generar_json("x")

    assert esperado in str(exc.value)
    assert servidor_que_rechaza["intentos"] == 1


def test_la_credencial_no_aparece_en_el_mensaje_de_un_401(
    entorno, servidor_que_rechaza
):
    """El cuerpo de un error puede repetir la clave; el mensaje nunca la lleva."""
    servidor_que_rechaza["codigo"] = 401
    ajustes = _openrouter(entorno, LLM_BASE_URL=servidor_que_rechaza["url"])

    with pytest.raises(RespuestaInvalida) as exc:
        construir_proveedor(ajustes).generar_json("x")

    assert "sk-or-v1" not in str(exc.value)


def test_la_base_url_conocida_apunta_donde_documenta_openrouter():
    """Si esto cambia, la configuración mínima deja de funcionar en silencio."""
    assert BASE_URL_POR_PROVEEDOR["openrouter"] == "https://openrouter.ai/api/v1"


def test_la_credencial_no_aparece_en_el_cuerpo(entorno, servidor):
    ajustes = _openrouter(entorno, LLM_BASE_URL=servidor["url"])
    construir_proveedor(ajustes).generar_json("x")

    assert "sk-or-v1" not in json.dumps(servidor["cuerpo"])


def test_el_entorno_queda_limpio_entre_pruebas():
    """Centinela: si una variable filtrara, los tests de arriba mentirían."""
    assert not os.environ.get("OPENROUTER_API_KEY")
