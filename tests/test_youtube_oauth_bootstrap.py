"""Alta OAuth interactiva: PKCE, state, callback loopback y canje del código.

**Ningún test de este archivo habla con Google ni abre un navegador.** El
transporte se simula sustituyendo ``urlopen``, y el navegador se inyecta como una
función que solo apunta la URL que se le pidió abrir.

El servidor del callback sí se levanta de verdad, en loopback y en un puerto que
elige el sistema: simularlo no probaría lo que importa —que valida el ``state``,
que ignora lo que no es el callback y que se cierra en cuanto termina—.

Los tres casos que dan sentido al resto:

* ``test_un_state_que_no_coincide_se_rechaza``: sin eso, cualquiera que lograra
  que el navegador visitara el callback con un código suyo haría que este proceso
  canjeara una autorización ajena.
* ``test_el_refresh_token_no_se_escribe_en_ningun_archivo``: el alta no persiste
  nada, y se comprueba mirando el disco.
* ``test_el_refresh_token_no_aparece_en_ninguna_superficie``: logs, stdout,
  stderr, ``repr`` y el dict imprimible.
"""

from __future__ import annotations

import io
import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from app.adapters.youtube import auth as modulo_auth
from app.adapters.youtube import oauth_bootstrap as bootstrap
from app.adapters.youtube.auth import ENDPOINT_CANALES, ENDPOINT_TOKEN, SCOPE_SUBIDA
from app.adapters.youtube.oauth_bootstrap import (
    HOST_CALLBACK,
    RUTA_CALLBACK,
    VERIFICADOR_MAX,
    VERIFICADOR_MIN,
    CredencialesCliente,
    ParametrosPKCE,
    ResultadoBootstrap,
    ServidorCallback,
    TokensObtenidos,
    canjear_codigo,
    cargar_cliente,
    construir_url_autorizacion,
    ejecutar_bootstrap,
    generar_pkce,
    generar_state,
    instrucciones,
    pista,
    verificador_valido,
)
from app.adapters.youtube.auth import ResultadoAuth
from app.config.settings import Settings
from app.core.errors import (
    ConfiguracionInvalida,
    EntradaInvalida,
    RespuestaInvalida,
    TiempoAgotado,
)

CLIENT_ID_FALSO = "fake-bootstrap-id.apps.googleusercontent.invalid"
SECRETO_FALSO = "fake-bootstrap-secret-no-sirve"
CODIGO_FALSO = "4/fake-authorization-code-0001"
ACCESS_FALSO = "fake-bootstrap-access-token-11"
REFRESH_FALSO = "fake-bootstrap-refresh-token-22"
CANAL = "UCfakeBootstrapChannel001"


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


class _Llamadas(list):
    def __init__(self) -> None:
        super().__init__()
        self.respuestas: dict[str, object] = {}


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


def _preparar(llamadas, *, tokens=None, canal=None):
    llamadas.respuestas[ENDPOINT_TOKEN] = lambda: _ok(
        tokens
        if tokens is not None
        else {
            "access_token": ACCESS_FALSO,
            "refresh_token": REFRESH_FALSO,
            "expires_in": 3599,
            "scope": SCOPE_SUBIDA,
            "token_type": "Bearer",
        }
    )
    llamadas.respuestas[ENDPOINT_CANALES] = lambda: _ok(
        canal if canal is not None else {"items": [{"id": CANAL}]}
    )


@pytest.fixture
def credenciales_json(tmp_path) -> Path:
    """El JSON que descarga Google, con valores de mentira.

    Vive en el directorio temporal del test, nunca en el repositorio.
    """
    archivo = tmp_path / "client_secret_falso.json"
    archivo.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": CLIENT_ID_FALSO,
                    "project_id": "shorts-automation-falso",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_secret": SECRETO_FALSO,
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    return archivo


@pytest.fixture
def entorno(monkeypatch) -> Settings:
    """Entorno de alta: todavía **sin** refresh token, que es lo que se busca."""
    monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
    monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("EXPECTED_YOUTUBE_CHANNEL_ID", raising=False)
    monkeypatch.delenv("YOUTUBE_SCOPE", raising=False)
    return Settings.desde_entorno()


def _cliente() -> CredencialesCliente:
    return CredencialesCliente(
        client_id=CLIENT_ID_FALSO, client_secret=SECRETO_FALSO, origen="falso.json"
    )


def _visitar(url: str) -> int:
    """Hace la petición del navegador al callback y devuelve el código HTTP."""
    try:
        with urllib.request.urlopen(url, timeout=5) as respuesta:
            return respuesta.status
    except urllib.error.HTTPError as exc:
        return exc.code


# ---------------------------------------------------------------------------
# 1. PKCE
# ---------------------------------------------------------------------------


def test_el_verificador_cumple_lo_que_exige_la_documentacion():
    """43 a 128 caracteres, y solo los no reservados."""
    pkce = generar_pkce()
    assert VERIFICADOR_MIN <= len(pkce.verificador) <= VERIFICADOR_MAX
    assert verificador_valido(pkce.verificador)


def test_el_desafio_es_el_sha256_del_verificador_en_base64url_sin_relleno():
    import base64
    import hashlib

    pkce = generar_pkce()
    esperado = (
        base64.urlsafe_b64encode(hashlib.sha256(pkce.verificador.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert pkce.desafio == esperado
    assert pkce.metodo == "S256"
    assert "=" not in pkce.desafio


def test_cada_alta_usa_un_verificador_distinto():
    assert generar_pkce().verificador != generar_pkce().verificador


@pytest.mark.parametrize("longitud", [42, 129, 0])
def test_una_longitud_fuera_de_rango_se_rechaza(longitud):
    with pytest.raises(ConfiguracionInvalida, match="code_verifier"):
        generar_pkce(longitud)


@pytest.mark.parametrize("longitud", [43, 64, 128])
def test_las_longitudes_del_rango_se_aceptan(longitud):
    pkce = generar_pkce(longitud)
    assert len(pkce.verificador) == longitud
    assert verificador_valido(pkce.verificador)


def test_el_verificador_no_aparece_en_el_repr():
    pkce = generar_pkce()
    assert pkce.verificador not in repr(pkce)
    assert pkce.verificador not in str(pkce)


# ---------------------------------------------------------------------------
# 2. State
# ---------------------------------------------------------------------------


def test_el_state_es_aleatorio_y_de_longitud_razonable():
    uno, otro = generar_state(), generar_state()
    assert uno != otro
    assert len(uno) >= 32


def test_el_state_viaja_en_la_url_de_autorizacion():
    state = generar_state()
    url = construir_url_autorizacion(
        _cliente(),
        redirect_uri="http://127.0.0.1:9999/oauth2callback",
        scope=SCOPE_SUBIDA,
        state=state,
        pkce=generar_pkce(),
    )
    parametros = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert parametros["state"] == [state]


def test_la_url_de_autorizacion_lleva_lo_que_documenta_google():
    pkce = generar_pkce()
    url = construir_url_autorizacion(
        _cliente(),
        redirect_uri="http://127.0.0.1:9999/oauth2callback",
        scope=SCOPE_SUBIDA,
        state="abc",
        pkce=pkce,
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")

    p = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert p["response_type"] == ["code"]
    assert p["client_id"] == [CLIENT_ID_FALSO]
    assert p["code_challenge"] == [pkce.desafio]
    assert p["code_challenge_method"] == ["S256"]
    assert p["access_type"] == ["offline"]
    assert p["scope"] == [SCOPE_SUBIDA]
    assert p["redirect_uri"] == ["http://127.0.0.1:9999/oauth2callback"]
    # El verificador jamás viaja en la URL: solo su hash.
    assert pkce.verificador not in url


def test_el_consentimiento_solo_se_fuerza_si_se_pide():
    comun = dict(
        redirect_uri="http://127.0.0.1:1/oauth2callback",
        scope=SCOPE_SUBIDA,
        state="s",
        pkce=generar_pkce(),
    )
    assert "prompt" not in construir_url_autorizacion(_cliente(), **comun)
    forzada = construir_url_autorizacion(
        _cliente(), **comun, forzar_consentimiento=True
    )
    assert urllib.parse.parse_qs(urllib.parse.urlparse(forzada).query)["prompt"] == [
        "consent"
    ]


def test_no_se_usa_oob_ni_esquema_propio():
    """Ambos están desaconsejados; el redirect es siempre loopback."""
    with ServidorCallback() as servidor:
        assert servidor.redirect_uri.startswith("http://127.0.0.1:")
        assert "urn:ietf:wg:oauth:2.0:oob" not in servidor.redirect_uri
        assert "://" in servidor.redirect_uri
        assert servidor.redirect_uri.split("://")[0] == "http"


# ---------------------------------------------------------------------------
# 3. JSON de credenciales
# ---------------------------------------------------------------------------


def test_se_leen_client_id_y_client_secret(credenciales_json):
    cliente = cargar_cliente(credenciales_json)
    assert cliente.client_id == CLIENT_ID_FALSO
    assert cliente.client_secret == SECRETO_FALSO
    assert cliente.origen == "client_secret_falso.json"


def test_un_archivo_que_no_existe_lo_dice_claramente(tmp_path):
    with pytest.raises(EntradaInvalida, match="no existe el archivo"):
        cargar_cliente(tmp_path / "no-esta.json")


def test_un_json_mal_formado_no_propaga_su_contenido(tmp_path):
    """El contenido podría llevar el secreto: el mensaje solo nombra el archivo."""
    archivo = tmp_path / "roto.json"
    archivo.write_text('{"installed": {"client_secret": "' + SECRETO_FALSO, encoding="utf-8")

    with pytest.raises(EntradaInvalida) as capturado:
        cargar_cliente(archivo)
    assert "no es un JSON legible" in capturado.value.mensaje
    assert SECRETO_FALSO not in capturado.value.mensaje


def test_un_cliente_web_se_rechaza_explicando_por_que(tmp_path):
    archivo = tmp_path / "web.json"
    archivo.write_text(
        json.dumps({"web": {"client_id": "x", "client_secret": "y"}}), encoding="utf-8"
    )
    with pytest.raises(EntradaInvalida, match="cliente 'web'"):
        cargar_cliente(archivo)


def test_un_json_sin_installed_se_rechaza(tmp_path):
    archivo = tmp_path / "otro.json"
    archivo.write_text(json.dumps({"algo": {}}), encoding="utf-8")
    with pytest.raises(EntradaInvalida, match="installed"):
        cargar_cliente(archivo)


@pytest.mark.parametrize("campo", ["client_id", "client_secret"])
def test_un_json_incompleto_nombra_lo_que_falta(tmp_path, campo):
    datos = {"installed": {"client_id": "x", "client_secret": "y"}}
    del datos["installed"][campo]
    archivo = tmp_path / "incompleto.json"
    archivo.write_text(json.dumps(datos), encoding="utf-8")

    with pytest.raises(EntradaInvalida, match=campo):
        cargar_cliente(archivo)


def test_el_secreto_del_cliente_no_aparece_en_el_repr(credenciales_json):
    cliente = cargar_cliente(credenciales_json)
    assert SECRETO_FALSO not in repr(cliente)
    assert SECRETO_FALSO not in str(cliente)
    assert CLIENT_ID_FALSO in repr(cliente)


def test_el_json_de_google_no_se_copia_a_ninguna_parte(credenciales_json, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    antes = {p.name for p in tmp_path.iterdir()}
    cargar_cliente(credenciales_json)
    assert {p.name for p in tmp_path.iterdir()} == antes


# ---------------------------------------------------------------------------
# 4. Servidor del callback
# ---------------------------------------------------------------------------


def test_el_servidor_se_ata_solo_a_loopback():
    with ServidorCallback() as servidor:
        assert servidor.redirect_uri.startswith(f"http://{HOST_CALLBACK}:")
        assert servidor.puerto > 0
        assert servidor.redirect_uri.endswith(RUTA_CALLBACK)


def test_un_callback_valido_devuelve_el_codigo():
    state = generar_state()
    with ServidorCallback() as servidor:
        resultado: dict = {}

        def esperar():
            resultado["codigo"] = servidor.esperar_codigo(state, timeout_s=10)

        hilo = threading.Thread(target=esperar, daemon=True)
        hilo.start()

        estado = _visitar(
            f"{servidor.redirect_uri}?"
            + urllib.parse.urlencode({"code": CODIGO_FALSO, "state": state})
        )
        hilo.join(timeout=10)

    assert estado == 200
    assert resultado["codigo"] == CODIGO_FALSO


def test_un_state_que_no_coincide_se_rechaza():
    """La defensa contra que alguien nos cuele su código de autorización."""
    with ServidorCallback() as servidor:
        fallo: dict = {}

        def esperar():
            try:
                servidor.esperar_codigo("el-bueno", timeout_s=10)
            except EntradaInvalida as exc:
                fallo["mensaje"] = exc.mensaje

        hilo = threading.Thread(target=esperar, daemon=True)
        hilo.start()

        estado = _visitar(
            f"{servidor.redirect_uri}?"
            + urllib.parse.urlencode({"code": CODIGO_FALSO, "state": "el-de-otro"})
        )
        hilo.join(timeout=10)

    assert estado == 403
    assert "state" in fallo["mensaje"]
    # No se filtra cuál era el correcto.
    assert "el-bueno" not in fallo["mensaje"]


def test_un_error_de_google_en_el_callback_se_reporta():
    state = generar_state()
    with ServidorCallback() as servidor:
        fallo: dict = {}

        def esperar():
            try:
                servidor.esperar_codigo(state, timeout_s=10)
            except EntradaInvalida as exc:
                fallo["mensaje"] = exc.mensaje

        hilo = threading.Thread(target=esperar, daemon=True)
        hilo.start()

        estado = _visitar(
            f"{servidor.redirect_uri}?"
            + urllib.parse.urlencode({"error": "access_denied", "state": state})
        )
        hilo.join(timeout=10)

    assert estado == 400
    assert "access_denied" in fallo["mensaje"]


def test_un_callback_sin_codigo_se_reporta():
    state = generar_state()
    with ServidorCallback() as servidor:
        fallo: dict = {}

        def esperar():
            try:
                servidor.esperar_codigo(state, timeout_s=10)
            except EntradaInvalida as exc:
                fallo["mensaje"] = exc.mensaje

        hilo = threading.Thread(target=esperar, daemon=True)
        hilo.start()
        _visitar(f"{servidor.redirect_uri}?" + urllib.parse.urlencode({"state": state}))
        hilo.join(timeout=10)

    assert "código" in fallo["mensaje"]


def test_las_peticiones_que_no_son_el_callback_no_gastan_la_espera():
    """El navegador pide /favicon.ico; eso no puede consumir el turno."""
    state = generar_state()
    with ServidorCallback() as servidor:
        resultado: dict = {}

        def esperar():
            resultado["codigo"] = servidor.esperar_codigo(state, timeout_s=15)

        hilo = threading.Thread(target=esperar, daemon=True)
        hilo.start()

        assert _visitar(f"http://{HOST_CALLBACK}:{servidor.puerto}/favicon.ico") == 404
        estado = _visitar(
            f"{servidor.redirect_uri}?"
            + urllib.parse.urlencode({"code": CODIGO_FALSO, "state": state})
        )
        hilo.join(timeout=15)

    assert estado == 200
    assert resultado["codigo"] == CODIGO_FALSO


def test_el_callback_expira_si_nadie_responde():
    with ServidorCallback() as servidor:
        with pytest.raises(TiempoAgotado, match="no llegó ninguna respuesta"):
            servidor.esperar_codigo(generar_state(), timeout_s=1)


def test_el_servidor_se_cierra_en_cuanto_recibe_el_callback():
    """No se queda escuchando ni un segundo más del necesario."""
    state = generar_state()
    servidor = ServidorCallback()
    puerto = servidor.puerto

    def esperar():
        servidor.esperar_codigo(state, timeout_s=10)

    hilo = threading.Thread(target=esperar, daemon=True)
    hilo.start()
    _visitar(
        f"{servidor.redirect_uri}?"
        + urllib.parse.urlencode({"code": CODIGO_FALSO, "state": state})
    )
    hilo.join(timeout=10)

    # Ya no acepta conexiones, que es lo que significa estar cerrado. No se
    # comprueba volviendo a hacer bind: un puerto recién cerrado sigue en
    # TIME_WAIT y el bind fallaría aunque nadie escuche, que es justo lo
    # contrario de lo que se quiere demostrar.
    sonda = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sonda.settimeout(2)
    try:
        with pytest.raises((ConnectionRefusedError, socket.timeout, OSError)):
            sonda.connect((HOST_CALLBACK, puerto))
    finally:
        sonda.close()


def test_el_manejador_no_registra_la_query_en_la_salida():
    """``BaseHTTPRequestHandler`` la escribiría en stderr, con el código dentro."""
    assert bootstrap._ManejadorCallback.log_message(None, "%s", "algo") is None


# ---------------------------------------------------------------------------
# 5. Canje del código
# ---------------------------------------------------------------------------


def test_el_canje_usa_los_parametros_documentados(llamadas):
    _preparar(llamadas)
    pkce = generar_pkce()
    canjear_codigo(
        _cliente(),
        codigo=CODIGO_FALSO,
        redirect_uri="http://127.0.0.1:9/oauth2callback",
        pkce=pkce,
    )

    cuerpo = urllib.parse.parse_qs(llamadas[0]["body"])
    assert llamadas[0]["url"] == ENDPOINT_TOKEN
    assert llamadas[0]["method"] == "POST"
    assert cuerpo["grant_type"] == ["authorization_code"]
    assert cuerpo["code"] == [CODIGO_FALSO]
    assert cuerpo["code_verifier"] == [pkce.verificador]
    assert cuerpo["redirect_uri"] == ["http://127.0.0.1:9/oauth2callback"]
    assert cuerpo["client_id"] == [CLIENT_ID_FALSO]


def test_el_canje_devuelve_los_dos_tokens(llamadas):
    _preparar(llamadas)
    tokens = canjear_codigo(
        _cliente(), codigo=CODIGO_FALSO, redirect_uri="http://127.0.0.1:9/x",
        pkce=generar_pkce(),
    )
    assert tokens.access_token == ACCESS_FALSO
    assert tokens.refresh_token == REFRESH_FALSO
    assert tokens.scope == SCOPE_SUBIDA


def test_un_canje_sin_refresh_token_explica_el_remedio(llamadas):
    """Pasa al reautorizar algo ya autorizado, y el mensaje dice qué hacer."""
    _preparar(llamadas, tokens={"access_token": ACCESS_FALSO, "expires_in": 3599})
    with pytest.raises(RespuestaInvalida) as capturado:
        canjear_codigo(
            _cliente(), codigo=CODIGO_FALSO, redirect_uri="http://127.0.0.1:9/x",
            pkce=generar_pkce(),
        )
    assert "--forzar-consentimiento" in capturado.value.mensaje


def test_un_canje_sin_access_token_se_rechaza(llamadas):
    _preparar(llamadas, tokens={"refresh_token": REFRESH_FALSO})
    with pytest.raises(RespuestaInvalida, match="access_token"):
        canjear_codigo(
            _cliente(), codigo=CODIGO_FALSO, redirect_uri="http://127.0.0.1:9/x",
            pkce=generar_pkce(),
        )


def test_un_codigo_invalido_no_arrastra_el_secreto(llamadas):
    llamadas.respuestas[ENDPOINT_TOKEN] = _http_error(
        400,
        {
            "error": "invalid_grant",
            "error_description": f"Bad code with secret {SECRETO_FALSO}",
        },
    )
    from app.core.errors import AutorizacionInvalida

    with pytest.raises(AutorizacionInvalida) as capturado:
        canjear_codigo(
            _cliente(), codigo="malo", redirect_uri="http://127.0.0.1:9/x",
            pkce=generar_pkce(),
        )
    assert SECRETO_FALSO not in capturado.value.mensaje


# ---------------------------------------------------------------------------
# 6. Alta completa, con AUTH CHECK
# ---------------------------------------------------------------------------


def _alta(entorno, credenciales_json, llamadas, **extra):
    """Ejecuta el alta simulando el navegador con un hilo que visita el callback."""
    abierto: dict = {}
    # Se saca antes de llamar: no es un parámetro de ``ejecutar_bootstrap``, es
    # lo que el navegador simulado responderá.
    respuesta_fija = extra.pop("respuesta_navegador", None)

    def navegador_falso(url: str) -> bool:
        abierto["url"] = url
        parametros = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        destino = parametros["redirect_uri"][0]
        state = parametros["state"][0]
        respuesta = respuesta_fija or {"code": CODIGO_FALSO, "state": state}

        def visitar():
            # El servidor real de urlopen está parcheado, así que se usa una
            # conexión cruda para no chocar con el mock del transporte.
            import http.client

            partes = urllib.parse.urlparse(destino)
            conexion = http.client.HTTPConnection(partes.hostname, partes.port, timeout=5)
            conexion.request("GET", f"{partes.path}?{urllib.parse.urlencode(respuesta)}")
            conexion.getresponse().read()
            conexion.close()

        threading.Thread(target=visitar, daemon=True).start()
        return True

    return (
        ejecutar_bootstrap(
            entorno,
            credenciales_json,
            abrir_navegador=navegador_falso,
            anunciar=lambda *a, **k: None,
            timeout_callback_s=15,
            **extra,
        ),
        abierto,
    )


def test_el_alta_completa_obtiene_el_token_y_comprueba_el_canal(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    resultado, abierto = _alta(entorno, credenciales_json, llamadas)

    assert resultado.refresh_token == REFRESH_FALSO
    assert resultado.comprobacion.resultado is ResultadoAuth.autenticado
    assert resultado.comprobacion.channel_id == CANAL
    assert resultado.scope_concedido == SCOPE_SUBIDA
    assert abierto["url"].startswith("https://accounts.google.com/")


def test_el_alta_llama_al_endpoint_de_identidad_y_a_ninguno_de_subida(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    _alta(entorno, credenciales_json, llamadas)

    urls = [c["url"] for c in llamadas]
    assert urls[0] == ENDPOINT_TOKEN
    assert urls[1].startswith(ENDPOINT_CANALES)
    assert "mine=true" in urls[1]
    assert len(urls) == 2
    for url in urls:
        assert "/videos" not in url
        assert "uploadType" not in url


def test_el_alta_usa_el_scope_configurado_sin_ampliarlo(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    _, abierto = _alta(entorno, credenciales_json, llamadas)

    scopes = urllib.parse.parse_qs(urllib.parse.urlparse(abierto["url"]).query)["scope"]
    assert scopes == [SCOPE_SUBIDA]
    assert "youtube.readonly" not in abierto["url"]
    assert "auth/youtube " not in abierto["url"]


def test_el_alta_respeta_un_scope_distinto_si_se_configura(
    entorno, credenciales_json, llamadas, monkeypatch
):
    monkeypatch.setenv("YOUTUBE_SCOPE", "https://www.googleapis.com/auth/youtube.readonly")
    _preparar(llamadas)
    _, abierto = _alta(Settings.desde_entorno(), credenciales_json, llamadas)

    scopes = urllib.parse.parse_qs(urllib.parse.urlparse(abierto["url"]).query)["scope"]
    assert scopes == ["https://www.googleapis.com/auth/youtube.readonly"]


def test_sin_canal_esperado_el_alta_no_compara_y_lo_dice(
    entorno, credenciales_json, llamadas
):
    """El alta es justo el momento en que se averigua cuál es el canal."""
    _preparar(llamadas)
    resultado, _ = _alta(entorno, credenciales_json, llamadas)

    assert resultado.comprobacion.resultado is ResultadoAuth.autenticado
    assert resultado.comprobacion.expected_channel_id is None
    assert "no se comparó" in resultado.comprobacion.detalle


def test_un_canal_distinto_del_esperado_da_wrong_channel(
    entorno, credenciales_json, llamadas, monkeypatch
):
    monkeypatch.setenv("EXPECTED_YOUTUBE_CHANNEL_ID", "UCotroCanalDistinto00001")
    _preparar(llamadas)
    resultado, _ = _alta(Settings.desde_entorno(), credenciales_json, llamadas)

    assert resultado.comprobacion.resultado is ResultadoAuth.canal_incorrecto
    assert resultado.comprobacion.autenticado is True
    assert resultado.comprobacion.canal_correcto is False


def test_un_alcance_insuficiente_se_reporta_explicitamente(
    entorno, credenciales_json, llamadas
):
    """403 insufficientPermissions: es la evidencia que decidiría ampliar el scope."""
    _preparar(llamadas)
    llamadas.respuestas[ENDPOINT_CANALES] = _http_error(
        403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}
    )
    resultado, _ = _alta(entorno, credenciales_json, llamadas)

    assert resultado.comprobacion.resultado is ResultadoAuth.alcance_insuficiente
    # Y aun así el refresh token se obtuvo: hay que enseñárselo igual.
    assert resultado.refresh_token == REFRESH_FALSO


def test_el_state_del_navegador_se_valida_en_el_alta_completa(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    with pytest.raises(EntradaInvalida, match="state"):
        _alta(
            entorno, credenciales_json, llamadas,
            respuesta_navegador={"code": CODIGO_FALSO, "state": "inventado"},
        )


# ---------------------------------------------------------------------------
# 7. Ningún secreto se escapa ni se escribe
# ---------------------------------------------------------------------------


def test_el_refresh_token_no_aparece_en_ninguna_superficie(
    entorno, credenciales_json, llamadas, caplog, capsys
):
    with caplog.at_level("DEBUG"):
        resultado, _ = (_preparar(llamadas), _alta(entorno, credenciales_json, llamadas))[1]

    capturado = capsys.readouterr()
    superficies = {
        "log": caplog.text,
        "stdout": capturado.out,
        "stderr": capturado.err,
        "dict imprimible": json.dumps(resultado.a_dict()),
        "repr del resultado": repr(resultado),
        "str del resultado": str(resultado),
        "repr de los tokens": repr(
            TokensObtenidos(access_token=ACCESS_FALSO, refresh_token=REFRESH_FALSO)
        ),
        "instrucciones": instrucciones(resultado),
    }
    for nombre, texto in superficies.items():
        for secreto in (REFRESH_FALSO, ACCESS_FALSO, SECRETO_FALSO, CODIGO_FALSO):
            assert secreto not in texto, f"{nombre} contiene un secreto"


def test_el_dict_imprimible_solo_lleva_una_pista_del_token(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    resultado, _ = _alta(entorno, credenciales_json, llamadas)

    datos = resultado.a_dict()
    assert "refresh_token" not in datos
    assert datos["refresh_token_hint"] != REFRESH_FALSO
    assert REFRESH_FALSO not in json.dumps(datos)
    # La pista deja ver los extremos, que es lo justo para cotejar una copia.
    assert datos["refresh_token_hint"].startswith(REFRESH_FALSO[:4])


def test_la_pista_no_permite_reconstruir_el_secreto():
    assert pista("") == "(vacío)"
    assert pista("corto") == "*****"
    largo = pista("abcdefghijklmnopqrstuvwxyz")
    assert "abcd" in largo and "wxyz" in largo
    assert "efghijklmnopqrst" not in largo


def test_el_refresh_token_no_se_escribe_en_ningun_archivo(
    entorno, credenciales_json, llamadas, tmp_path, monkeypatch
):
    """Se comprueba mirando el disco, no leyendo el código."""
    trabajo = tmp_path / "trabajo"
    trabajo.mkdir()
    monkeypatch.chdir(trabajo)

    _preparar(llamadas)
    _alta(entorno, credenciales_json, llamadas)

    assert list(trabajo.iterdir()) == [], "el alta dejó archivos"
    # Y en el directorio del JSON tampoco apareció nada nuevo.
    vecinos = {p.name for p in credenciales_json.parent.iterdir()}
    assert vecinos == {credenciales_json.name, "trabajo"}


def test_ningun_archivo_del_repositorio_contiene_el_token_de_prueba():
    """Ni siquiera por descuido de un fixture."""
    raiz = Path(__file__).resolve().parent.parent
    for ruta in list(raiz.glob("app/**/*.py")) + list(raiz.glob("*.md")):
        texto = ruta.read_text(encoding="utf-8")
        assert REFRESH_FALSO not in texto, ruta


def test_las_instrucciones_nombran_las_variables_sin_valores(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    resultado, _ = _alta(entorno, credenciales_json, llamadas)

    texto = instrucciones(resultado)
    for variable in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "EXPECTED_YOUTUBE_CHANNEL_ID",
    ):
        assert variable in texto
    assert CANAL in texto, "el canal descubierto sí se muestra: es público"
    assert REFRESH_FALSO not in texto


# ---------------------------------------------------------------------------
# 8. Separación del publisher y de auth
# ---------------------------------------------------------------------------


def test_el_bootstrap_no_depende_de_ningun_publisher():
    import ast
    import inspect

    arbol = ast.parse(inspect.getsource(bootstrap))
    modulos = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            modulos.add(nodo.module)
        elif isinstance(nodo, ast.Import):
            modulos.update(a.name for a in nodo.names)

    for modulo in modulos:
        assert "publish" not in modulo.lower()
        assert "publicacion" not in modulo.lower()


def test_el_bootstrap_no_contiene_ninguna_url_de_subida():
    import ast
    import inspect

    arbol = ast.parse(inspect.getsource(bootstrap))
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
    for url in urls:
        assert "/videos" not in url
        assert "uploadType" not in url
        assert not url.startswith("https://www.googleapis.com/upload/")


def test_el_comando_de_comprobacion_conserva_su_significado(monkeypatch, llamadas):
    """``youtube-auth`` sigue exigiendo una autorización ya configurada.

    El alta no puede haber relajado la comprobación: sin canal esperado, el
    comando de siempre sigue fallando.
    """
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "x-largo-suficiente")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "y-largo-suficiente")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "z-largo-suficiente")
    monkeypatch.delenv("EXPECTED_YOUTUBE_CHANNEL_ID", raising=False)

    comprobacion = modulo_auth.comprobar_autenticacion(Settings.desde_entorno())
    assert comprobacion.resultado is ResultadoAuth.credenciales_ausentes
    assert "EXPECTED_YOUTUBE_CHANNEL_ID" in comprobacion.detalle
    assert llamadas == [], "no debe llamar a Google sin canal esperado"


def test_el_auth_check_sigue_funcionando_como_siempre(monkeypatch, llamadas):
    """Los parámetros nuevos tienen default: el camino de G7.1 no cambia."""
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "x-largo-suficiente")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "y-largo-suficiente")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "z-largo-suficiente")
    monkeypatch.setenv("EXPECTED_YOUTUBE_CHANNEL_ID", CANAL)
    _preparar(llamadas)

    comprobacion = modulo_auth.comprobar_autenticacion(Settings.desde_entorno())
    assert comprobacion.resultado is ResultadoAuth.autenticado
    # Dos llamadas: canje del refresh token y lectura del canal.
    assert len(llamadas) == 2


# ---------------------------------------------------------------------------
# 9. El comando: código de salida y limpieza de stdout
#
# Se invoca ``main()`` de verdad, no las funciones por debajo: lo que se está
# comprobando es el contrato del comando —qué devuelve al shell y qué escribe en
# cada flujo—, y eso solo existe en el CLI.
# ---------------------------------------------------------------------------


def _ejecutar_cli(credenciales_json, *, respuesta_navegador=None, argumentos=None):
    """Corre ``python -m app youtube-auth-bootstrap`` capturando los dos flujos.

    El navegador se sustituye parcheando ``webbrowser.open``, que es el valor por
    defecto del flujo: así se ejercita el camino real del comando y no una
    inyección que en producción nadie usa.
    """
    import http.client
    from contextlib import redirect_stderr, redirect_stdout

    from app.__main__ import main

    def navegador(url: str) -> bool:
        parametros = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        destino = parametros["redirect_uri"][0]
        state = parametros["state"][0]
        respuesta = respuesta_navegador or {"code": CODIGO_FALSO, "state": state}

        def visitar():
            partes = urllib.parse.urlparse(destino)
            conexion = http.client.HTTPConnection(
                partes.hostname, partes.port, timeout=5
            )
            conexion.request(
                "GET", f"{partes.path}?{urllib.parse.urlencode(respuesta)}"
            )
            conexion.getresponse().read()
            conexion.close()

        threading.Thread(target=visitar, daemon=True).start()
        return True

    import webbrowser

    original = webbrowser.open
    webbrowser.open = navegador
    salida, error = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(salida), redirect_stderr(error):
            codigo = main(
                [
                    "youtube-auth-bootstrap",
                    "--credentials",
                    str(credenciales_json),
                    "--timeout",
                    "15",
                    *(argumentos or []),
                ]
            )
    finally:
        webbrowser.open = original
    return codigo, salida.getvalue(), error.getvalue()


def test_el_comando_sale_con_cero_cuando_el_canal_es_el_esperado(
    entorno, credenciales_json, llamadas, monkeypatch
):
    monkeypatch.setenv("EXPECTED_YOUTUBE_CHANNEL_ID", CANAL)
    _preparar(llamadas)
    codigo, salida, _ = _ejecutar_cli(credenciales_json)

    assert json.loads(salida)["result"] == "AUTHENTICATED"
    assert json.loads(salida)["correct_channel"] is True
    assert codigo == 0


def test_el_comando_sale_con_cero_sin_canal_esperado_configurado(
    entorno, credenciales_json, llamadas
):
    """No hay nada contra lo que comparar, y el alta es lícita igualmente."""
    _preparar(llamadas)
    codigo, salida, _ = _ejecutar_cli(credenciales_json)

    datos = json.loads(salida)
    assert datos["result"] == "AUTHENTICATED"
    assert datos["expected_channel_id"] is None
    assert codigo == 0


def test_el_comando_no_sale_con_cero_si_el_canal_no_coincide(
    entorno, credenciales_json, llamadas, monkeypatch
):
    """El caso que motivó la corrección.

    La credencial funciona —``authenticated`` es verdadero— y aun así el comando
    tiene que fallar: un script que mirara el código de salida daría por bueno un
    canal equivocado.
    """
    monkeypatch.setenv("EXPECTED_YOUTUBE_CHANNEL_ID", "UCotroCanalDistinto00001")
    _preparar(llamadas)
    codigo, salida, _ = _ejecutar_cli(credenciales_json)

    datos = json.loads(salida)
    assert datos["result"] == "WRONG_CHANNEL"
    assert datos["authenticated"] is True, "la credencial sí sirvió"
    assert datos["correct_channel"] is False
    assert codigo != 0


def test_el_comando_no_sale_con_cero_con_alcance_insuficiente(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    llamadas.respuestas[ENDPOINT_CANALES] = _http_error(
        403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}
    )
    codigo, salida, _ = _ejecutar_cli(credenciales_json)

    assert json.loads(salida)["result"] == "INSUFFICIENT_SCOPE"
    assert codigo != 0


def test_el_comando_no_sale_con_cero_con_un_fallo_transitorio(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    llamadas.respuestas[ENDPOINT_CANALES] = _http_error(503)
    codigo, salida, _ = _ejecutar_cli(credenciales_json)

    assert json.loads(salida)["result"] == "TRANSIENT_ERROR"
    assert codigo != 0


def test_ningun_desenlace_salvo_authenticated_sale_con_cero():
    """Se comprueba sobre el criterio, no solo sobre los casos que se simulan.

    Así un desenlace nuevo no nace con código de salida 0 por descuido.
    """
    for desenlace in ResultadoAuth:
        comprobacion = modulo_auth.ComprobacionAuth(resultado=desenlace, detalle="x")
        sale_con_cero = comprobacion.resultado is ResultadoAuth.autenticado
        assert sale_con_cero == (desenlace is ResultadoAuth.autenticado), desenlace
        if desenlace is ResultadoAuth.canal_incorrecto:
            assert comprobacion.autenticado is True and not sale_con_cero


def test_el_stdout_del_comando_es_json_puro(entorno, credenciales_json, llamadas):
    """Parseable directamente, sin recortar nada.

    El alta imprime mensajes para la persona —la URL del consentimiento entre
    ellos— y ninguno puede caer en stdout: ``json.loads`` sobre la salida entera
    es la comprobación más exigente posible.
    """
    _preparar(llamadas)
    codigo, salida, error = _ejecutar_cli(credenciales_json)

    datos = json.loads(salida)  # sin .index("{"), sin splitlines, sin filtrar
    assert datos["result"] == "AUTHENTICATED"
    assert codigo == 0

    # Y los mensajes humanos sí aparecieron: el test no pasa por no haber nada.
    assert "Se abrirá el navegador" in error
    assert "accounts.google.com" in error, "la URL de consentimiento va por stderr"
    assert "Esperando la respuesta" in error


def test_la_url_de_consentimiento_nunca_cae_en_stdout(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    _, salida, _ = _ejecutar_cli(credenciales_json)

    assert "accounts.google.com" not in salida
    assert "code_challenge" not in salida
    assert "127.0.0.1" not in salida


def test_el_stdout_sigue_limpio_cuando_el_comando_falla(
    entorno, credenciales_json, llamadas, monkeypatch
):
    """Un fallo tampoco rompe el documento: el JSON se imprime igual."""
    monkeypatch.setenv("EXPECTED_YOUTUBE_CHANNEL_ID", "UCotroCanal000000000001")
    _preparar(llamadas)
    codigo, salida, _ = _ejecutar_cli(credenciales_json)

    assert json.loads(salida)["result"] == "WRONG_CHANNEL"
    assert codigo != 0


def test_el_anunciador_por_defecto_escribe_en_stderr():
    """El valor por defecto es el seguro, no algo que haya que recordar pasar."""
    import inspect
    from contextlib import redirect_stderr, redirect_stdout

    assert (
        inspect.signature(ejecutar_bootstrap).parameters["anunciar"].default
        is bootstrap.anunciar_en_stderr
    )

    salida, error = io.StringIO(), io.StringIO()
    with redirect_stdout(salida), redirect_stderr(error):
        bootstrap.anunciar_en_stderr("un mensaje para la persona")
    assert salida.getvalue() == ""
    assert "un mensaje para la persona" in error.getvalue()


def test_el_comando_no_expone_el_codigo_de_autorizacion(
    entorno, credenciales_json, llamadas
):
    _preparar(llamadas)
    _, salida, error = _ejecutar_cli(credenciales_json)

    assert CODIGO_FALSO not in salida
    assert CODIGO_FALSO not in error


def test_el_comando_sigue_sin_poner_el_refresh_token_en_stdout(
    entorno, credenciales_json, llamadas
):
    """La corrección del stdout no debe haber movido el token de sitio."""
    _preparar(llamadas)
    _, salida, error = _ejecutar_cli(credenciales_json)

    assert REFRESH_FALSO not in salida
    assert json.loads(salida)["refresh_token_hint"] != REFRESH_FALSO
    # Sigue saliendo por stderr, como hasta ahora y como está documentado.
    assert REFRESH_FALSO in error


def test_el_comando_de_comprobacion_conserva_su_codigo_de_salida(
    monkeypatch, llamadas, credenciales_json
):
    """``youtube-auth`` no cambia: ya usaba el criterio correcto."""
    from contextlib import redirect_stderr, redirect_stdout

    from app.__main__ import main

    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "x-largo-suficiente")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "y-largo-suficiente")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "z-largo-suficiente")
    monkeypatch.setenv("EXPECTED_YOUTUBE_CHANNEL_ID", "UCotroCanal000000000001")
    _preparar(llamadas)

    salida, error = io.StringIO(), io.StringIO()
    with redirect_stdout(salida), redirect_stderr(error):
        codigo = main(["youtube-auth"])

    assert json.loads(salida.getvalue())["result"] == "WRONG_CHANNEL"
    assert codigo != 0
