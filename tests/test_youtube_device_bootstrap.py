"""Alta OAuth por flujo de dispositivo: códigos, sondeo y superficies seguras.

**Ningún test de este archivo habla con Google ni abre un navegador.** El
transporte se simula sustituyendo ``urlopen``; el reloj y la espera se inyectan,
así que el bucle de sondeo se recorre entero sin que nadie duerma de verdad.

Los cuatro casos que dan sentido al resto:

* ``test_el_device_code_no_aparece_en_ninguna_superficie``: es la credencial de
  este flujo. Quien la tenga junto al ``client_secret`` puede reclamar la
  autorización, así que no puede estar en stdout, ni en stderr, ni en un log, ni
  en un ``repr``.
* ``test_el_user_code_si_se_muestra``: el reverso exacto del anterior. Ocultarlo
  «por prudencia» rompería el flujo, porque la persona tiene que teclearlo.
* ``test_el_sondeo_termina_cuando_se_acaba_el_plazo``: sin eso, un alta que
  nadie completa deja el proceso girando para siempre.
* ``test_el_refresh_token_no_aparece_en_ninguna_superficie``: igual que en el
  alta de escritorio, y además comprobando que el redactor lo enmascara ahora
  que se registra en él.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from app.__main__ import main
from app.adapters.youtube import device_bootstrap as dispositivo
from app.adapters.youtube.auth import (
    ENDPOINT_CANALES,
    ENDPOINT_TOKEN,
    SCOPE_GESTION,
    SCOPE_SUBIDA,
    ResultadoAuth,
)
from app.adapters.youtube.device_bootstrap import (
    ENDPOINT_CODIGO_DISPOSITIVO,
    EXPIRACION_POR_DEFECTO_S,
    GRANT_TYPE_DISPOSITIVO,
    INCREMENTO_SLOW_DOWN_S,
    CodigoDeDispositivo,
    ResultadoDispositivo,
    cargar_cliente_dispositivo,
    ejecutar_alta_dispositivo,
    esperar_autorizacion,
    instrucciones_dispositivo,
    solicitar_codigo,
)
from app.adapters.youtube.oauth_bootstrap import CredencialesCliente
from app.config.settings import Settings
from app.core.errors import (
    AutorizacionInvalida,
    EntradaInvalida,
    RespuestaInvalida,
    TiempoAgotado,
)
from app.core.redaction import olvidar_secretos, redactar

CLIENT_ID_FALSO = "fake-device-id.apps.googleusercontent.invalid"
SECRETO_FALSO = "fake-device-secret-no-sirve"
DEVICE_CODE_FALSO = "AH-fake-device-code-0000000000000001"
USER_CODE_FALSO = "GQVQ-JKEC"
URL_VERIFICACION = "https://www.google.com/device"
ACCESS_FALSO = "fake-device-access-token-11"
REFRESH_FALSO = "1//fake-device-refresh-token-2222222222"
CANAL = "UCfakeDeviceChannel0001"


@pytest.fixture(autouse=True)
def _sin_secretos_registrados():
    """El registro del redactor es global: se limpia antes y después.

    Sin esto, un secreto de prueba sobreviviría al test y el redactor lo
    enmascararía en los de después, que es justo el tipo de contaminación que
    hace que una suite mienta.
    """
    olvidar_secretos()
    yield
    olvidar_secretos()


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


class _Respuesta(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def _ok(cuerpo: dict):
    return _Respuesta(json.dumps(cuerpo).encode("utf-8"))


def _http_error(codigo: int, cuerpo: dict | str = ""):
    datos = json.dumps(cuerpo) if isinstance(cuerpo, dict) else cuerpo
    return urllib.error.HTTPError(
        url="https://example.invalid",
        code=codigo,
        msg="error simulado",
        hdrs=None,
        fp=io.BytesIO(datos.encode("utf-8")),
    )


def _error_oauth(http: int, codigo: str):
    """Un error del endpoint de tokens con su código corto, como lo manda Google."""
    return _http_error(http, {"error": codigo, "error_description": "simulado"})


class _Llamadas(list):
    def __init__(self) -> None:
        super().__init__()
        self.respuestas: dict[str, object] = {}

    def urls(self) -> list[str]:
        return [c["url"] for c in self]


@pytest.fixture
def llamadas(monkeypatch) -> _Llamadas:
    registro = _Llamadas()

    def fake_urlopen(peticion, timeout=None):
        registro.append(
            {
                "url": peticion.full_url,
                "method": peticion.get_method(),
                "body": peticion.data.decode("utf-8") if peticion.data else "",
            }
        )
        for prefijo, respuesta in registro.respuestas.items():
            if peticion.full_url.startswith(prefijo):
                if isinstance(respuesta, Exception):
                    raise respuesta
                return respuesta() if callable(respuesta) else respuesta
        raise AssertionError(f"nadie preparó una respuesta para {peticion.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return registro


class _Guion:
    """Devuelve una respuesta distinta en cada llamada, en orden.

    Es lo que hace falta para probar un bucle de sondeo: la gracia está en que la
    primera vez diga «todavía no» y la tercera entregue los tokens. Agotado el
    guion, se repite la última respuesta indefinidamente, que es lo que hace un
    alta que nadie completa.

    Cada entrada es una **fábrica**, no un objeto ya construido, y eso no es
    ceremonia: un ``HTTPError`` lleva su cuerpo en un fichero que se consume al
    leerlo. Reutilizando el mismo objeto, el segundo sondeo vería un cuerpo
    vacío, el código corto se perdería y el bucle tomaría una rama distinta de la
    que el test cree estar probando.
    """

    def __init__(self, *fabricas) -> None:
        self._fabricas = list(fabricas)
        self.consumidas = 0

    def __call__(self):
        fabrica = self._fabricas[min(self.consumidas, len(self._fabricas) - 1)]
        self.consumidas += 1
        valor = fabrica() if callable(fabrica) else fabrica
        if isinstance(valor, Exception):
            raise valor
        return valor


class _Reloj:
    """Un reloj que solo avanza cuando alguien duerme.

    Así el bucle de sondeo se recorre entero y el plazo se agota de verdad, sin
    que la suite tarde media hora en comprobarlo.
    """

    def __init__(self) -> None:
        self.t = 0.0
        self.siestas: list[float] = []

    def ahora(self) -> float:
        return self.t

    def dormir(self, segundos: float) -> None:
        self.siestas.append(segundos)
        self.t += segundos


def _codigo(**extra) -> CodigoDeDispositivo:
    datos = {
        "device_code": DEVICE_CODE_FALSO,
        "user_code": USER_CODE_FALSO,
        "verification_url": URL_VERIFICACION,
        "expires_in_s": 1800,
        "intervalo_s": 5,
    }
    datos.update(extra)
    return CodigoDeDispositivo(**datos)


def _cliente() -> CredencialesCliente:
    return CredencialesCliente(
        client_id=CLIENT_ID_FALSO, client_secret=SECRETO_FALSO, origen="falso.json"
    )


def _tokens_ok() -> dict:
    return {
        "access_token": ACCESS_FALSO,
        "refresh_token": REFRESH_FALSO,
        "expires_in": 3599,
        "scope": SCOPE_GESTION,
        "token_type": "Bearer",
    }


def _preparar(llamadas, *, sondeo=None, canal=None, codigo=None):
    llamadas.respuestas[ENDPOINT_CODIGO_DISPOSITIVO] = lambda: _ok(
        codigo
        if codigo is not None
        else {
            "device_code": DEVICE_CODE_FALSO,
            "user_code": USER_CODE_FALSO,
            "verification_url": URL_VERIFICACION,
            "expires_in": 1800,
            "interval": 5,
        }
    )
    llamadas.respuestas[ENDPOINT_TOKEN] = sondeo or (lambda: _ok(_tokens_ok()))
    llamadas.respuestas[ENDPOINT_CANALES] = lambda: _ok(
        canal if canal is not None else {"items": [{"id": CANAL}]}
    )


@pytest.fixture
def credenciales_json(tmp_path) -> Path:
    """El JSON que descarga Google, con valores de mentira."""
    archivo = tmp_path / "client_secret_device_falso.json"
    archivo.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": CLIENT_ID_FALSO,
                    "project_id": "shorts-automation-falso",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_secret": SECRETO_FALSO,
                }
            }
        ),
        encoding="utf-8",
    )
    return archivo


@pytest.fixture
def entorno(monkeypatch) -> Settings:
    """Entorno de alta: todavía **sin** refresh token, que es lo que se busca."""
    for variable in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "EXPECTED_YOUTUBE_CHANNEL_ID",
        "YOUTUBE_SCOPE",
        "YOUTUBE_DEVICE_SCOPE",
    ):
        monkeypatch.delenv(variable, raising=False)
    return Settings.desde_entorno()


def _alta(entorno, credenciales_json, **extra):
    """Ejecuta el alta completa con reloj y navegador falsos."""
    reloj = _Reloj()
    abierto = {}

    def navegador_falso(url):
        abierto["url"] = url
        return True

    resultado = ejecutar_alta_dispositivo(
        entorno,
        credenciales_json,
        abrir_navegador=navegador_falso,
        anunciar=lambda *a, **k: None,
        dormir=reloj.dormir,
        reloj=reloj.ahora,
        **extra,
    )
    return resultado, abierto, reloj


# ---------------------------------------------------------------------------
# 1. La solicitud del código de dispositivo
# ---------------------------------------------------------------------------


def test_la_solicitud_usa_el_endpoint_y_los_campos_documentados(llamadas):
    _preparar(llamadas)
    codigo = solicitar_codigo(_cliente(), scope=SCOPE_GESTION)

    assert llamadas.urls() == [ENDPOINT_CODIGO_DISPOSITIVO]
    cuerpo = urllib.parse.parse_qs(llamadas[0]["body"])
    assert cuerpo == {"client_id": [CLIENT_ID_FALSO], "scope": [SCOPE_GESTION]}
    assert llamadas[0]["method"] == "POST"
    assert codigo.device_code == DEVICE_CODE_FALSO
    assert codigo.user_code == USER_CODE_FALSO
    assert codigo.verification_url == URL_VERIFICACION
    assert codigo.expires_in_s == 1800
    assert codigo.intervalo_s == 5


def test_la_solicitud_no_manda_el_client_secret(llamadas):
    """La documentación solo pide client_id y scope en este paso."""
    _preparar(llamadas)
    solicitar_codigo(_cliente(), scope=SCOPE_GESTION)

    assert SECRETO_FALSO not in llamadas[0]["body"]


def test_un_cliente_del_tipo_equivocado_se_reporta_como_tal(llamadas):
    llamadas.respuestas[ENDPOINT_CODIGO_DISPOSITIVO] = _error_oauth(401, "invalid_client")

    with pytest.raises(AutorizacionInvalida) as exc:
        solicitar_codigo(_cliente(), scope=SCOPE_GESTION)

    assert "TVs and Limited Input devices" in exc.value.mensaje
    assert exc.value.retryable is False


def test_una_respuesta_sin_codigos_no_se_da_por_buena(llamadas):
    _preparar(llamadas, codigo={"expires_in": 1800, "interval": 5})

    with pytest.raises(RespuestaInvalida):
        solicitar_codigo(_cliente(), scope=SCOPE_GESTION)


def test_una_respuesta_sin_direccion_de_verificacion_no_sirve(llamadas):
    """Sin dirección no hay dónde teclear el código: no es un alta utilizable."""
    _preparar(
        llamadas,
        codigo={
            "device_code": DEVICE_CODE_FALSO,
            "user_code": USER_CODE_FALSO,
            "expires_in": 1800,
        },
    )

    with pytest.raises(RespuestaInvalida) as exc:
        solicitar_codigo(_cliente(), scope=SCOPE_GESTION)

    assert "verificación" in exc.value.mensaje


def test_se_acepta_verification_uri_ademas_de_verification_url(llamadas):
    """Google documenta ``verification_url``; el RFC 8628 la llama ``_uri``."""
    _preparar(
        llamadas,
        codigo={
            "device_code": DEVICE_CODE_FALSO,
            "user_code": USER_CODE_FALSO,
            "verification_uri": URL_VERIFICACION,
            "expires_in": 1800,
        },
    )
    assert solicitar_codigo(_cliente(), scope=SCOPE_GESTION).verification_url == (
        URL_VERIFICACION
    )


def test_sin_expires_in_se_usa_un_plazo_y_nunca_ninguno(llamadas):
    _preparar(
        llamadas,
        codigo={
            "device_code": DEVICE_CODE_FALSO,
            "user_code": USER_CODE_FALSO,
            "verification_url": URL_VERIFICACION,
        },
    )
    codigo = solicitar_codigo(_cliente(), scope=SCOPE_GESTION)

    assert codigo.expires_in_s == EXPIRACION_POR_DEFECTO_S
    assert codigo.expires_in_s > 0


# ---------------------------------------------------------------------------
# 2. El sondeo: los códigos que documenta Google
# ---------------------------------------------------------------------------


def test_el_sondeo_usa_el_grant_type_y_los_campos_documentados(llamadas):
    _preparar(llamadas)
    reloj = _Reloj()
    esperar_autorizacion(
        _cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora
    )

    cuerpo = urllib.parse.parse_qs(llamadas[0]["body"])
    assert cuerpo["grant_type"] == [GRANT_TYPE_DISPOSITIVO]
    assert cuerpo["client_id"] == [CLIENT_ID_FALSO]
    assert cuerpo["client_secret"] == [SECRETO_FALSO]
    assert cuerpo["device_code"] == [DEVICE_CODE_FALSO]


def test_authorization_pending_sigue_esperando(llamadas):
    guion = _Guion(
        lambda: _error_oauth(428, "authorization_pending"),
        lambda: _error_oauth(428, "authorization_pending"),
        lambda: _ok(_tokens_ok()),
    )
    _preparar(llamadas, sondeo=guion)
    reloj = _Reloj()

    tokens = esperar_autorizacion(
        _cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora
    )

    assert tokens.refresh_token == REFRESH_FALSO
    assert guion.consumidas == 3
    assert reloj.siestas == [5, 5], "esperó el intervalo entre sondeos"


def test_slow_down_alarga_el_intervalo(llamadas):
    guion = _Guion(
        lambda: _error_oauth(403, "slow_down"),
        lambda: _error_oauth(403, "slow_down"),
        lambda: _ok(_tokens_ok()),
    )
    _preparar(llamadas, sondeo=guion)
    reloj = _Reloj()

    esperar_autorizacion(_cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora)

    assert reloj.siestas == [
        5 + INCREMENTO_SLOW_DOWN_S,
        5 + 2 * INCREMENTO_SLOW_DOWN_S,
    ], "cada slow_down alarga la espera, y el aumento se acumula"


def test_un_429_tambien_alarga_el_intervalo(llamadas):
    guion = _Guion(lambda: _http_error(429, ""), lambda: _ok(_tokens_ok()))
    _preparar(llamadas, sondeo=guion)
    reloj = _Reloj()

    esperar_autorizacion(_cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora)

    assert reloj.siestas == [5 + INCREMENTO_SLOW_DOWN_S]


def test_access_denied_no_se_reintenta(llamadas):
    guion = _Guion(lambda: _error_oauth(403, "access_denied"))
    _preparar(llamadas, sondeo=guion)
    reloj = _Reloj()

    with pytest.raises(AutorizacionInvalida) as exc:
        esperar_autorizacion(
            _cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora
        )

    assert "denegada" in exc.value.mensaje
    assert exc.value.retryable is False
    assert guion.consumidas == 1, "una negativa no se reintenta ni una vez"


def test_expired_token_pide_empezar_de_nuevo(llamadas):
    """No está en la tabla de Google, sí en el RFC 8628. Se cubre igual."""
    _preparar(llamadas, sondeo=_Guion(lambda: _error_oauth(400, "expired_token")))
    reloj = _Reloj()

    with pytest.raises(AutorizacionInvalida) as exc:
        esperar_autorizacion(
            _cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora
        )

    assert "Vuelve a ejecutar el alta" in exc.value.mensaje


def test_invalid_grant_pide_empezar_de_nuevo(llamadas):
    """Es lo que Google **sí** documenta para un código caducado o ya usado."""
    _preparar(llamadas, sondeo=_Guion(lambda: _error_oauth(400, "invalid_grant")))
    reloj = _Reloj()

    with pytest.raises(AutorizacionInvalida) as exc:
        esperar_autorizacion(
            _cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora
        )

    assert "ya no sirve" in exc.value.mensaje


def test_invalid_client_no_se_reintenta(llamadas):
    _preparar(llamadas, sondeo=_Guion(lambda: _error_oauth(401, "invalid_client")))
    reloj = _Reloj()

    with pytest.raises(AutorizacionInvalida) as exc:
        esperar_autorizacion(
            _cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora
        )

    assert exc.value.retryable is False


@pytest.mark.parametrize(
    "http,codigo",
    [(400, "admin_policy_enforced"), (403, "org_internal"), (401, "unauthorized_client")],
)
def test_los_demas_errores_de_cliente_tampoco_se_reintentan(llamadas, http, codigo):
    _preparar(llamadas, sondeo=_Guion(lambda: _error_oauth(http, codigo)))
    reloj = _Reloj()

    with pytest.raises(AutorizacionInvalida):
        esperar_autorizacion(
            _cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora
        )


def test_un_codigo_desconocido_para_en_vez_de_sondear_en_bucle(llamadas):
    """Si Google añade un código nuevo, pararse es mejor que girar sin entenderlo."""
    _preparar(llamadas, sondeo=_Guion(lambda: _error_oauth(400, "algo_que_no_conocemos")))
    reloj = _Reloj()

    with pytest.raises(RespuestaInvalida):
        esperar_autorizacion(
            _cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora
        )


# ---------------------------------------------------------------------------
# 3. El sondeo: plazo y baches
# ---------------------------------------------------------------------------


def test_el_sondeo_termina_cuando_se_acaba_el_plazo(llamadas):
    """El caso que impide un timeout infinito."""
    _preparar(llamadas, sondeo=_Guion(lambda: _error_oauth(428, "authorization_pending")))
    reloj = _Reloj()

    with pytest.raises(TiempoAgotado) as exc:
        esperar_autorizacion(
            _cliente(),
            _codigo(expires_in_s=30),
            dormir=reloj.dormir,
            reloj=reloj.ahora,
        )

    assert "30s" in exc.value.mensaje
    assert reloj.t >= 30, "el reloj llegó al plazo en vez de girar sin fin"


def test_el_plazo_lo_fija_expires_in_y_no_una_constante_nuestra(llamadas):
    _preparar(llamadas, sondeo=_Guion(lambda: _error_oauth(428, "authorization_pending")))
    reloj = _Reloj()

    with pytest.raises(TiempoAgotado):
        esperar_autorizacion(
            _cliente(),
            _codigo(expires_in_s=12),
            dormir=reloj.dormir,
            reloj=reloj.ahora,
        )

    assert reloj.t < EXPIRACION_POR_DEFECTO_S


def test_un_5xx_no_aborta_el_consentimiento(llamadas):
    """La persona está autorizando en otra pantalla: un bache no tira el proceso."""
    guion = _Guion(lambda: _http_error(503, ""), lambda: _ok(_tokens_ok()))
    _preparar(llamadas, sondeo=guion)
    reloj = _Reloj()

    tokens = esperar_autorizacion(
        _cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora
    )

    assert tokens.refresh_token == REFRESH_FALSO
    assert guion.consumidas == 2


def test_un_corte_de_red_tampoco_aborta_el_consentimiento(llamadas):
    guion = _Guion(lambda: urllib.error.URLError("red caída"), lambda: _ok(_tokens_ok()))
    _preparar(llamadas, sondeo=guion)
    reloj = _Reloj()

    tokens = esperar_autorizacion(
        _cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora
    )

    assert tokens.refresh_token == REFRESH_FALSO


def test_un_bache_indefinido_sigue_acotado_por_el_plazo(llamadas):
    """Tolerar baches no puede convertirse en esperar para siempre."""
    _preparar(llamadas, sondeo=_Guion(lambda: _http_error(503, "")))
    reloj = _Reloj()

    with pytest.raises(TiempoAgotado):
        esperar_autorizacion(
            _cliente(),
            _codigo(expires_in_s=20),
            dormir=reloj.dormir,
            reloj=reloj.ahora,
        )


# ---------------------------------------------------------------------------
# 4. Los tokens
# ---------------------------------------------------------------------------


def test_el_sondeo_devuelve_el_refresh_token(llamadas):
    _preparar(llamadas)
    reloj = _Reloj()
    tokens = esperar_autorizacion(
        _cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora
    )

    assert tokens.refresh_token == REFRESH_FALSO
    assert tokens.access_token == ACCESS_FALSO
    assert tokens.scope == SCOPE_GESTION


def test_una_respuesta_sin_refresh_token_no_se_da_por_buena(llamadas):
    """Sin refresh token este alta no tiene producto."""
    _preparar(
        llamadas,
        sondeo=_Guion(lambda: _ok({"access_token": ACCESS_FALSO, "expires_in": 3599})),
    )
    reloj = _Reloj()

    with pytest.raises(RespuestaInvalida) as exc:
        esperar_autorizacion(
            _cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora
        )

    assert "refresh_token" in exc.value.mensaje


def test_una_respuesta_sin_access_token_no_se_da_por_buena(llamadas):
    _preparar(llamadas, sondeo=_Guion(lambda: _ok({"refresh_token": REFRESH_FALSO})))
    reloj = _Reloj()

    with pytest.raises(RespuestaInvalida):
        esperar_autorizacion(
            _cliente(), _codigo(), dormir=reloj.dormir, reloj=reloj.ahora
        )


# ---------------------------------------------------------------------------
# 5. El alcance: la excepción aprobada de D17
# ---------------------------------------------------------------------------


def test_el_alta_pide_el_alcance_del_flujo_de_dispositivo(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    resultado, _, _ = _alta(entorno, credenciales_json)

    cuerpo = urllib.parse.parse_qs(llamadas[0]["body"])
    assert cuerpo["scope"] == [SCOPE_GESTION]
    assert resultado.scope_solicitado == SCOPE_GESTION


def test_el_alta_no_pide_youtube_upload_porque_este_flujo_no_lo_admite(
    entorno, credenciales_json, llamadas
):
    """La lista de alcances del flujo de dispositivo no incluye youtube.upload."""
    _preparar(llamadas)
    _alta(entorno, credenciales_json)

    cuerpo = urllib.parse.parse_qs(llamadas[0]["body"])
    assert cuerpo["scope"] != [SCOPE_SUBIDA]
    assert "youtube.upload" not in llamadas[0]["body"]


def test_el_alcance_de_d17_no_se_toca(entorno):
    """La excepción es de este flujo; el resto de la arquitectura sigue igual."""
    assert entorno.youtube_scope == SCOPE_SUBIDA
    assert entorno.youtube_device_scope == SCOPE_GESTION
    assert entorno.youtube_scope != entorno.youtube_device_scope


def test_cambiar_youtube_scope_no_cambia_el_del_dispositivo(monkeypatch):
    monkeypatch.setenv("YOUTUBE_SCOPE", "https://www.googleapis.com/auth/youtube.readonly")
    monkeypatch.delenv("YOUTUBE_DEVICE_SCOPE", raising=False)
    settings = Settings.desde_entorno()

    assert settings.youtube_device_scope == SCOPE_GESTION


def test_el_alcance_del_dispositivo_es_configurable(monkeypatch, entorno):
    monkeypatch.setenv(
        "YOUTUBE_DEVICE_SCOPE", "https://www.googleapis.com/auth/youtube.readonly"
    )
    settings = Settings.desde_entorno()

    assert settings.youtube_device_scope == (
        "https://www.googleapis.com/auth/youtube.readonly"
    )


def test_el_resultado_declara_el_alcance_que_realmente_se_pidio(
    entorno, credenciales_json, llamadas
):
    """Sin esto el documento diría el de D17, que no es el que se pidió."""
    _preparar(llamadas)
    resultado, _, _ = _alta(entorno, credenciales_json)

    assert resultado.a_dict()["scope_requested"] == SCOPE_GESTION
    assert resultado.comprobacion.scope_solicitado == SCOPE_GESTION


# ---------------------------------------------------------------------------
# 6. El alta completa y el AUTH CHECK reutilizado
# ---------------------------------------------------------------------------


def test_el_alta_completa_obtiene_el_token_y_comprueba_el_canal(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    resultado, abierto, _ = _alta(entorno, credenciales_json)

    assert isinstance(resultado, ResultadoDispositivo), (
        "cada alta devuelve su propio resultado: el JSON de una no debe poder "
        "confundirse con el de la otra"
    )
    assert resultado.refresh_token == REFRESH_FALSO
    assert resultado.comprobacion.resultado is ResultadoAuth.autenticado
    assert resultado.comprobacion.channel_id == CANAL
    assert abierto["url"] == URL_VERIFICACION


def test_el_alta_llama_a_los_tres_endpoints_y_a_ninguno_de_subida(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    _alta(entorno, credenciales_json)

    urls = llamadas.urls()
    assert urls[0] == ENDPOINT_CODIGO_DISPOSITIVO
    assert urls[1] == ENDPOINT_TOKEN
    assert urls[2].startswith(ENDPOINT_CANALES)
    assert "mine=true" in urls[2]
    assert len(urls) == 3
    for url in urls:
        assert "/videos" not in url
        assert "uploadType" not in url


def test_sin_canal_esperado_el_alta_no_compara_y_lo_dice(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    resultado, _, _ = _alta(entorno, credenciales_json)

    assert resultado.comprobacion.resultado is ResultadoAuth.autenticado
    assert resultado.comprobacion.expected_channel_id is None
    assert "no se comparó" in resultado.comprobacion.detalle


def test_un_canal_distinto_del_esperado_da_wrong_channel(
    entorno, credenciales_json, llamadas, monkeypatch
):
    monkeypatch.setenv("EXPECTED_YOUTUBE_CHANNEL_ID", "UCotroCanalDistinto00001")
    _preparar(llamadas)
    resultado, _, _ = _alta(Settings.desde_entorno(), credenciales_json)

    assert resultado.comprobacion.resultado is ResultadoAuth.canal_incorrecto
    assert resultado.comprobacion.autenticado is True
    assert resultado.comprobacion.canal_correcto is False


def test_un_alcance_insuficiente_se_reporta_explicitamente(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    llamadas.respuestas[ENDPOINT_CANALES] = _http_error(
        403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}
    )
    resultado, _, _ = _alta(entorno, credenciales_json)

    assert resultado.comprobacion.resultado is ResultadoAuth.alcance_insuficiente


def test_un_fallo_transitorio_del_auth_check_se_reporta_como_tal(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    llamadas.respuestas[ENDPOINT_CANALES] = _http_error(503, "")
    resultado, _, _ = _alta(entorno, credenciales_json)

    assert resultado.comprobacion.resultado is ResultadoAuth.error_transitorio
    assert resultado.comprobacion.reintentable is True


# ---------------------------------------------------------------------------
# 7. El navegador es opcional, nunca un requisito
# ---------------------------------------------------------------------------


def test_el_alta_funciona_aunque_no_haya_navegador(
    entorno, credenciales_json, llamadas
):
    """Es el caso normal en la máquina para la que existe este flujo."""
    _preparar(llamadas)
    reloj = _Reloj()

    def sin_navegador(url):
        raise RuntimeError("aquí no hay navegador")

    resultado = ejecutar_alta_dispositivo(
        entorno,
        credenciales_json,
        abrir_navegador=sin_navegador,
        anunciar=lambda *a, **k: None,
        dormir=reloj.dormir,
        reloj=reloj.ahora,
    )

    assert resultado.comprobacion.resultado is ResultadoAuth.autenticado


def test_el_anuncio_llega_antes_de_intentar_abrir_el_navegador(
    entorno, credenciales_json, llamadas
):
    """Si abrir el navegador falla, la dirección ya tiene que estar anunciada."""
    _preparar(llamadas)
    reloj = _Reloj()
    orden = []

    ejecutar_alta_dispositivo(
        entorno,
        credenciales_json,
        abrir_navegador=lambda url: orden.append("navegador"),
        anunciar=lambda mensaje: orden.append("anuncio"),
        dormir=reloj.dormir,
        reloj=reloj.ahora,
    )

    assert orden == ["anuncio", "navegador"]


# ---------------------------------------------------------------------------
# 8. El device_code: nunca en ninguna superficie
# ---------------------------------------------------------------------------


def test_el_device_code_no_aparece_en_ninguna_superficie(
    entorno, credenciales_json, llamadas, caplog
):
    """El caso que define la seguridad de este flujo."""
    _preparar(llamadas)
    salida, error = io.StringIO(), io.StringIO()
    reloj = _Reloj()

    with caplog.at_level(logging.DEBUG):
        with redirect_stdout(salida), redirect_stderr(error):
            resultado = ejecutar_alta_dispositivo(
                entorno,
                credenciales_json,
                abrir_navegador=lambda url: True,
                dormir=reloj.dormir,
                reloj=reloj.ahora,
            )

    assert DEVICE_CODE_FALSO not in salida.getvalue()
    assert DEVICE_CODE_FALSO not in error.getvalue()
    assert DEVICE_CODE_FALSO not in caplog.text
    assert DEVICE_CODE_FALSO not in repr(resultado)
    assert DEVICE_CODE_FALSO not in json.dumps(resultado.a_dict())


def test_el_device_code_queda_fuera_del_repr_del_codigo():
    codigo = _codigo()

    assert DEVICE_CODE_FALSO not in repr(codigo)
    assert DEVICE_CODE_FALSO not in str(codigo)
    assert DEVICE_CODE_FALSO not in json.dumps(codigo.a_dict())


def test_el_device_code_queda_registrado_en_el_redactor(llamadas):
    """Es opaco: ni tiene nombre de variable ni forma reconocible.

    Por eso el registro explícito es la **única** defensa que puede cubrirlo, y
    por eso el test comprueba las dos mitades: que antes no se conoce —si no, no
    estaría probando nada— y que después sí.
    """
    traza = f"traza con {DEVICE_CODE_FALSO} dentro"
    assert DEVICE_CODE_FALSO in redactar(traza), (
        "sin registrarlo el redactor no puede conocerlo; si esto pasa, el test "
        "de después no demuestra nada"
    )

    _preparar(llamadas)
    solicitar_codigo(_cliente(), scope=SCOPE_GESTION)

    assert DEVICE_CODE_FALSO not in redactar(traza)


def test_el_resultado_del_alta_no_arrastra_el_device_code(
    entorno, credenciales_json, llamadas
):
    """Ya cumplió su función; no hay motivo para que sobreviva al proceso."""
    _preparar(llamadas)
    resultado, _, _ = _alta(entorno, credenciales_json)

    assert not hasattr(resultado, "device_code")


# ---------------------------------------------------------------------------
# 9. El user_code: justo lo contrario
# ---------------------------------------------------------------------------


def test_el_user_code_si_se_muestra(entorno, credenciales_json, llamadas):
    """Google lo define como el código que la persona teclea. Ocultarlo rompería
    el flujo."""
    _preparar(llamadas)
    anuncios = []
    reloj = _Reloj()

    ejecutar_alta_dispositivo(
        entorno,
        credenciales_json,
        abrir_navegador=lambda url: True,
        anunciar=anuncios.append,
        dormir=reloj.dormir,
        reloj=reloj.ahora,
    )

    texto = "\n".join(anuncios)
    assert USER_CODE_FALSO in texto
    assert URL_VERIFICACION in texto
    assert DEVICE_CODE_FALSO not in texto


def test_el_user_code_sobrevive_al_redactor(llamadas):
    """Si lo enmascarara, la persona no tendría qué teclear."""
    _preparar(llamadas)
    solicitar_codigo(_cliente(), scope=SCOPE_GESTION)

    assert USER_CODE_FALSO in redactar(f"escribe {USER_CODE_FALSO}")


def test_el_anuncio_llega_por_stderr_y_no_por_stdout(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    salida, error = io.StringIO(), io.StringIO()
    reloj = _Reloj()

    with redirect_stdout(salida), redirect_stderr(error):
        ejecutar_alta_dispositivo(
            entorno,
            credenciales_json,
            abrir_navegador=lambda url: True,
            dormir=reloj.dormir,
            reloj=reloj.ahora,
        )

    assert USER_CODE_FALSO in error.getvalue()
    assert salida.getvalue() == ""


# ---------------------------------------------------------------------------
# 10. El refresh token
# ---------------------------------------------------------------------------


def test_el_refresh_token_no_aparece_en_ninguna_superficie(
    entorno, credenciales_json, llamadas, caplog
):
    _preparar(llamadas)
    salida = io.StringIO()
    reloj = _Reloj()

    with caplog.at_level(logging.DEBUG):
        with redirect_stdout(salida):
            resultado = ejecutar_alta_dispositivo(
                entorno,
                credenciales_json,
                abrir_navegador=lambda url: True,
                anunciar=lambda *a, **k: None,
                dormir=reloj.dormir,
                reloj=reloj.ahora,
            )

    assert REFRESH_FALSO not in salida.getvalue()
    assert REFRESH_FALSO not in caplog.text
    assert REFRESH_FALSO not in repr(resultado)
    assert REFRESH_FALSO not in json.dumps(resultado.a_dict())
    assert REFRESH_FALSO not in instrucciones_dispositivo(resultado)


def test_el_redactor_conoce_la_forma_de_un_refresh_token_de_google():
    """Defensa en profundidad: sin depender de haberlo registrado."""
    olvidar_secretos()
    texto = f"algo salió mal con {REFRESH_FALSO} aquí"

    assert REFRESH_FALSO not in redactar(texto)


def test_el_dict_imprimible_solo_lleva_una_pista_del_token(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    resultado, _, _ = _alta(entorno, credenciales_json)

    datos = resultado.a_dict()
    assert datos["refresh_token_hint"] != REFRESH_FALSO
    assert REFRESH_FALSO not in json.dumps(datos)
    assert datos["flow"] == "device"


def test_el_refresh_token_no_se_escribe_en_ningun_archivo(
    entorno, credenciales_json, llamadas, tmp_path, monkeypatch
):
    """Se comprueba mirando el disco, no leyendo el código."""
    trabajo = tmp_path / "trabajo"
    trabajo.mkdir()
    monkeypatch.chdir(trabajo)

    _preparar(llamadas)
    _alta(entorno, credenciales_json)

    assert list(trabajo.iterdir()) == [], "el alta dejó archivos"
    vecinos = {p.name for p in credenciales_json.parent.iterdir()}
    assert vecinos == {credenciales_json.name, "trabajo"}


def test_ningun_archivo_del_repositorio_contiene_el_token_de_prueba():
    raiz = Path(__file__).resolve().parent.parent
    for ruta in list(raiz.glob("app/**/*.py")) + list(raiz.glob("*.md")):
        texto = ruta.read_text(encoding="utf-8")
        assert REFRESH_FALSO not in texto, ruta


def test_las_instrucciones_avisan_de_que_el_alcance_es_mas_amplio(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    resultado, _, _ = _alta(entorno, credenciales_json)

    texto = instrucciones_dispositivo(resultado)
    assert "MÁS AMPLIO" in texto
    assert SCOPE_GESTION in texto
    for variable in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "EXPECTED_YOUTUBE_CHANNEL_ID",
    ):
        assert variable in texto
    assert CANAL in texto, "el canal descubierto sí se muestra: es público"


# ---------------------------------------------------------------------------
# 11. El JSON del cliente
# ---------------------------------------------------------------------------


def test_se_lee_el_cliente_del_json_descargado(credenciales_json):
    cliente = cargar_cliente_dispositivo(credenciales_json)

    assert cliente.client_id == CLIENT_ID_FALSO
    assert cliente.client_secret == SECRETO_FALSO
    assert SECRETO_FALSO not in repr(cliente)


def test_se_acepta_el_cliente_sin_contenedor(tmp_path):
    """Google no publica la forma del JSON de un cliente de dispositivo."""
    archivo = tmp_path / "plano.json"
    archivo.write_text(
        json.dumps({"client_id": CLIENT_ID_FALSO, "client_secret": SECRETO_FALSO}),
        encoding="utf-8",
    )
    assert cargar_cliente_dispositivo(archivo).client_id == CLIENT_ID_FALSO


def test_un_archivo_que_no_existe_lo_dice_sin_reventar(tmp_path):
    with pytest.raises(EntradaInvalida) as exc:
        cargar_cliente_dispositivo(tmp_path / "no_existe.json")

    assert "TVs and Limited Input devices" in exc.value.mensaje


def test_un_json_sin_client_secret_se_rechaza_nombrando_lo_que_falta(tmp_path):
    archivo = tmp_path / "incompleto.json"
    archivo.write_text(
        json.dumps({"installed": {"client_id": CLIENT_ID_FALSO}}), encoding="utf-8"
    )

    with pytest.raises(EntradaInvalida) as exc:
        cargar_cliente_dispositivo(archivo)

    assert "client_secret" in exc.value.mensaje


def test_el_mensaje_de_error_nunca_lleva_el_contenido_del_archivo(tmp_path):
    archivo = tmp_path / "roto.json"
    archivo.write_text(f"{{no es json {SECRETO_FALSO}", encoding="utf-8")

    with pytest.raises(EntradaInvalida) as exc:
        cargar_cliente_dispositivo(archivo)

    assert SECRETO_FALSO not in exc.value.mensaje


# ---------------------------------------------------------------------------
# 12. Separación del publisher
# ---------------------------------------------------------------------------


def test_el_modulo_no_depende_de_ningun_publisher():
    import ast
    import inspect

    arbol = ast.parse(inspect.getsource(dispositivo))
    modulos = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            modulos.add(nodo.module)
        elif isinstance(nodo, ast.Import):
            modulos.update(a.name for a in nodo.names)

    for modulo in modulos:
        assert "publish" not in modulo.lower()
        assert "publicacion" not in modulo.lower()


def test_el_modulo_no_contiene_ninguna_url_de_subida():
    """Se enumeran las URL del código, sin contar las de los docstrings."""
    import ast
    import inspect

    arbol = ast.parse(inspect.getsource(dispositivo))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            cuerpo = nodo.body
            if (
                cuerpo
                and isinstance(cuerpo[0], ast.Expr)
                and isinstance(cuerpo[0].value, ast.Constant)
                and isinstance(cuerpo[0].value.value, str)
            ):
                cuerpo.pop(0)

    urls = {
        n.value
        for n in ast.walk(arbol)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value.startswith("http")
    }
    assert urls == {ENDPOINT_CODIGO_DISPOSITIVO}, (
        "el módulo solo define una URL propia; las demás las importa de auth.py"
    )
    for url in urls:
        assert "/videos" not in url
        assert "uploadType" not in url
        assert not url.startswith("https://www.googleapis.com/upload/")


def test_el_alta_de_escritorio_sigue_intacta():
    """Este flujo se añade al lado del otro, no lo sustituye."""
    from app.adapters.youtube import oauth_bootstrap

    assert oauth_bootstrap.HOST_CALLBACK == "127.0.0.1"
    assert hasattr(oauth_bootstrap, "generar_pkce")
    assert hasattr(oauth_bootstrap, "generar_state")


def test_el_flujo_de_dispositivo_no_finge_tener_pkce_ni_state():
    """Añadirlos de adorno sugeriría una protección que el protocolo no aplica.

    Se miran los campos que se envían de verdad, no el texto del módulo: el
    docstring explica por qué no hay PKCE y nombrarlo ahí es correcto.
    """
    import ast
    import inspect

    enviados = set()
    for funcion in (dispositivo.solicitar_codigo, dispositivo.esperar_autorizacion):
        arbol = ast.parse(inspect.getsource(funcion).lstrip())
        enviados.update(
            n.value
            for n in ast.walk(arbol)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        )

    for prohibido in ("code_challenge", "code_challenge_method", "code_verifier", "state"):
        assert prohibido not in enviados


# ---------------------------------------------------------------------------
# 13. El comando
# ---------------------------------------------------------------------------


def _ejecutar_cli(credenciales_json, monkeypatch):
    """Ejecuta el subcomando capturando sus dos salidas por separado."""
    reloj = _Reloj()
    # Sin ``raising=False``: si alguien renombra estos nombres, este test tiene
    # que romperse en vez de empezar a dormir de verdad en la suite.
    monkeypatch.setattr(dispositivo, "sleep", reloj.dormir)
    monkeypatch.setattr(dispositivo, "monotonic", reloj.ahora)
    monkeypatch.setattr("webbrowser.open", lambda url: True)

    salida, error = io.StringIO(), io.StringIO()
    with redirect_stdout(salida), redirect_stderr(error):
        codigo = main(["youtube-auth-device", "--credentials", str(credenciales_json)])
    return codigo, salida.getvalue(), error.getvalue()


def test_el_comando_sale_con_cero_cuando_el_canal_es_el_esperado(
    entorno, credenciales_json, llamadas, monkeypatch
):
    monkeypatch.setenv("EXPECTED_YOUTUBE_CHANNEL_ID", CANAL)
    _preparar(llamadas)
    codigo, salida, _ = _ejecutar_cli(credenciales_json, monkeypatch)

    datos = json.loads(salida)
    assert datos["result"] == "AUTHENTICATED"
    assert datos["correct_channel"] is True
    assert codigo == 0


def test_el_comando_sale_con_cero_sin_canal_esperado_configurado(
    entorno, credenciales_json, llamadas, monkeypatch
):
    _preparar(llamadas)
    codigo, salida, _ = _ejecutar_cli(credenciales_json, monkeypatch)

    datos = json.loads(salida)
    assert datos["result"] == "AUTHENTICATED"
    assert datos["expected_channel_id"] is None
    assert codigo == 0


def test_el_comando_no_sale_con_cero_si_el_canal_no_coincide(
    entorno, credenciales_json, llamadas, monkeypatch
):
    """La credencial funciona y aun así el comando tiene que fallar."""
    monkeypatch.setenv("EXPECTED_YOUTUBE_CHANNEL_ID", "UCotroCanalDistinto00001")
    _preparar(llamadas)
    codigo, salida, _ = _ejecutar_cli(credenciales_json, monkeypatch)

    datos = json.loads(salida)
    assert datos["result"] == "WRONG_CHANNEL"
    assert datos["authenticated"] is True, "la credencial sí sirvió"
    assert codigo != 0


def test_el_comando_no_sale_con_cero_con_alcance_insuficiente(
    entorno, credenciales_json, llamadas, monkeypatch
):
    _preparar(llamadas)
    llamadas.respuestas[ENDPOINT_CANALES] = _http_error(
        403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}
    )
    codigo, salida, _ = _ejecutar_cli(credenciales_json, monkeypatch)

    assert json.loads(salida)["result"] == "INSUFFICIENT_SCOPE"
    assert codigo != 0


def test_el_comando_no_sale_con_cero_con_un_fallo_transitorio(
    entorno, credenciales_json, llamadas, monkeypatch
):
    _preparar(llamadas)
    llamadas.respuestas[ENDPOINT_CANALES] = _http_error(503, "")
    codigo, salida, _ = _ejecutar_cli(credenciales_json, monkeypatch)

    assert json.loads(salida)["result"] == "TRANSIENT_ERROR"
    assert codigo != 0


def test_ningun_desenlace_salvo_authenticated_sale_con_cero(
    entorno, credenciales_json, llamadas, monkeypatch
):
    """La regla, recorrida sobre la enumeración entera y contra el CLI de verdad.

    Se fuerza cada desenlace desde el transporte en vez de afirmar la regla sobre
    sí misma: un test que repite la condición que quiere comprobar pasa siempre,
    incluso con el comando roto.
    """
    casos = {
        ResultadoAuth.autenticado: (None, {"items": [{"id": CANAL}]}),
        ResultadoAuth.canal_incorrecto: ("UCotro00000000000000001", {"items": [{"id": CANAL}]}),
        ResultadoAuth.alcance_insuficiente: (
            None,
            _http_error(403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}),
        ),
        ResultadoAuth.autorizacion_invalida: (None, _http_error(401, "")),
        ResultadoAuth.error_transitorio: (None, _http_error(503, "")),
        ResultadoAuth.error_api: (None, {"items": []}),
    }

    vistos = set()
    for esperado, (canal_esperado, respuesta) in casos.items():
        if canal_esperado:
            monkeypatch.setenv("EXPECTED_YOUTUBE_CHANNEL_ID", canal_esperado)
        else:
            monkeypatch.delenv("EXPECTED_YOUTUBE_CHANNEL_ID", raising=False)
        llamadas.clear()
        _preparar(llamadas)
        llamadas.respuestas[ENDPOINT_CANALES] = (
            respuesta if isinstance(respuesta, Exception) else (lambda r=respuesta: _ok(r))
        )

        codigo, salida, _ = _ejecutar_cli(credenciales_json, monkeypatch)
        datos = json.loads(salida)

        assert datos["result"] == esperado.value
        assert (codigo == 0) is (esperado is ResultadoAuth.autenticado), (
            f"{esperado.value} salió con {codigo}"
        )
        vistos.add(esperado)

    # CREDENTIALS_MISSING no es alcanzable desde este flujo: el alta trae su
    # propio token y no exige canal esperado. Se deja constancia en vez de
    # fingir que se probó.
    assert set(ResultadoAuth) - vistos == {ResultadoAuth.credenciales_ausentes}


def test_el_stdout_del_comando_es_json_puro(
    entorno, credenciales_json, llamadas, monkeypatch
):
    _preparar(llamadas)
    _, salida, error = _ejecutar_cli(credenciales_json, monkeypatch)

    datos = json.loads(salida)
    assert datos["flow"] == "device"
    assert USER_CODE_FALSO in error, "el código humano va por stderr"
    assert USER_CODE_FALSO not in salida


def test_el_stdout_sigue_limpio_cuando_el_comando_falla(
    entorno, credenciales_json, llamadas, monkeypatch
):
    _preparar(llamadas)
    llamadas.respuestas[ENDPOINT_CANALES] = _http_error(503, "")
    codigo, salida, _ = _ejecutar_cli(credenciales_json, monkeypatch)

    assert codigo != 0
    json.loads(salida)


def test_el_comando_no_pone_el_device_code_en_ninguna_salida(
    entorno, credenciales_json, llamadas, monkeypatch
):
    _preparar(llamadas)
    _, salida, error = _ejecutar_cli(credenciales_json, monkeypatch)

    assert DEVICE_CODE_FALSO not in salida
    assert DEVICE_CODE_FALSO not in error


def test_el_comando_no_pone_el_refresh_token_en_stdout(
    entorno, credenciales_json, llamadas, monkeypatch
):
    _preparar(llamadas)
    _, salida, error = _ejecutar_cli(credenciales_json, monkeypatch)

    assert REFRESH_FALSO not in salida
    assert REFRESH_FALSO in error, "se entrega una sola vez, por stderr y a mano"


def test_un_error_del_alta_no_ensucia_el_stdout(
    entorno, credenciales_json, llamadas, monkeypatch
):
    """Un fallo permanente sale por el manejador del CLI, no por stdout."""
    llamadas.respuestas[ENDPOINT_CODIGO_DISPOSITIVO] = _error_oauth(
        401, "invalid_client"
    )
    codigo, salida, error = _ejecutar_cli(credenciales_json, monkeypatch)

    assert codigo == 2
    assert salida == ""
    assert "AutorizacionInvalida" in error


# ---------------------------------------------------------------------------
# 14. Nada se persiste
# ---------------------------------------------------------------------------


def test_el_alta_no_escribe_nada_en_el_disco(
    entorno, credenciales_json, llamadas, tmp_path, monkeypatch
):
    trabajo = tmp_path / "vacio"
    trabajo.mkdir()
    monkeypatch.chdir(trabajo)
    _preparar(llamadas)

    antes = {p for p in trabajo.rglob("*")}
    _alta(entorno, credenciales_json)
    despues = {p for p in trabajo.rglob("*")}

    assert antes == despues == set()


def test_el_json_del_cliente_no_se_copia(entorno, credenciales_json, llamadas):
    _preparar(llamadas)
    antes = credenciales_json.read_text(encoding="utf-8")
    _alta(entorno, credenciales_json)

    assert credenciales_json.read_text(encoding="utf-8") == antes
    hermanos = list(credenciales_json.parent.iterdir())
    assert hermanos == [credenciales_json]


def test_las_trazas_del_alta_no_llevan_credenciales(
    entorno, credenciales_json, llamadas, caplog
):
    """Sin ``configurar_logging`` a propósito: usa ``force=True`` y se llevaría
    por delante el handler de ``caplog``, dejando el test sin nada que mirar. El
    filtro de redacción lo pone ``obtener_logger`` en cada evento."""
    _preparar(llamadas)

    with caplog.at_level(logging.DEBUG):
        _alta(entorno, credenciales_json)

    for secreto in (DEVICE_CODE_FALSO, REFRESH_FALSO, ACCESS_FALSO, SECRETO_FALSO):
        assert secreto not in caplog.text
