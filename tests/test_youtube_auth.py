"""Autenticación OAuth contra YouTube: los seis desenlaces y ningún secreto.

**Ningún test de este archivo habla con Google.** ``urllib.request.urlopen`` se
sustituye por respuestas preparadas, igual que en ``test_llm_provider.py``, así
que la suite es determinista y no necesita credenciales ni red.

Los dos casos que dan sentido al resto:

* ``test_un_canal_distinto_no_autoriza_a_seguir``: la credencial funciona y aun
  así no se continúa. «Autenticado» y «canal correcto» son cosas distintas.
* ``test_el_refresh_token_no_aparece_en_ningun_sitio``: recorre logs, excepciones
  y el resultado serializado buscando la credencial de prueba.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from app.adapters.youtube import auth
from app.adapters.youtube.auth import (
    ENDPOINT_CANALES,
    ENDPOINT_TOKEN,
    SCOPE_SUBIDA,
    ComprobacionAuth,
    CredencialesOAuth,
    ResultadoAuth,
    TokenAcceso,
    cargar_credenciales,
    comprobar_autenticacion,
    identidad_del_canal,
    obtener_access_token,
)
from app.config.settings import Settings
from app.core.errors import (
    AutorizacionInvalida,
    ConfiguracionInvalida,
    ErrorTransitorio,
    LimiteDeTasa,
    RespuestaInvalida,
    TiempoAgotado,
)

# Credenciales de mentira. Ninguna vale contra Google, y esa es la idea: si algún
# día una de estas cadenas funcionara, el problema sería que es real.
CLIENT_ID_FALSO = "fake-client-id.apps.googleusercontent.invalid"
SECRETO_FALSO = "fake-client-secret-no-vale-para-nada"
REFRESH_FALSO = "fake-refresh-token-1234567890"
ACCESS_FALSO = "fake-access-token-0987654321"

CANAL_ESPERADO = "UCfakeChannelForTests0001"
OTRO_CANAL = "UCfakeOtherChannelForTest2"


# ---------------------------------------------------------------------------
# Utilidades de simulación
# ---------------------------------------------------------------------------


class _Respuesta(io.BytesIO):
    """Lo mínimo que ``urlopen`` devuelve y este código usa."""

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
    """Registro de peticiones con las respuestas preparadas al lado.

    Hereda de ``list`` para poder afirmar sobre las llamadas igual que sobre una
    lista, y lleva ``respuestas`` como atributo para que cada test prepare lo que
    quiera devolver sin pasar la tabla por todas partes.
    """

    def __init__(self) -> None:
        super().__init__()
        self.respuestas: dict[str, object] = {}


@pytest.fixture
def llamadas(monkeypatch) -> _Llamadas:
    """Registra cada URL pedida y responde según la ruta.

    Sirve para dos cosas a la vez: dar respuestas deterministas y **dejar por
    escrito a qué endpoints se llamó**, que es como se comprueba que no se tocó
    ninguno de subida.
    """
    registro = _Llamadas()

    def fake_urlopen(peticion, timeout=None):
        url = peticion.full_url
        registro.append(
            {
                "url": url,
                "method": peticion.get_method(),
                "headers": dict(peticion.header_items()),
                "body": peticion.data.decode("utf-8") if peticion.data else "",
                "timeout": timeout,
            }
        )
        for prefijo, respuesta in registro.respuestas.items():
            if url.startswith(prefijo):
                if isinstance(respuesta, Exception):
                    raise respuesta
                return respuesta() if callable(respuesta) else respuesta
        raise AssertionError(f"nadie preparó una respuesta para {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return registro


def _preparar(llamadas, *, token=None, canal=None):
    """Deja lista la pareja de respuestas del camino feliz."""
    llamadas.respuestas[ENDPOINT_TOKEN] = lambda: _ok(
        token
        if token is not None
        else {
            "access_token": ACCESS_FALSO,
            "expires_in": 3599,
            "scope": SCOPE_SUBIDA,
            "token_type": "Bearer",
        }
    )
    llamadas.respuestas[ENDPOINT_CANALES] = lambda: _ok(
        canal
        if canal is not None
        else {"kind": "youtube#channelListResponse", "items": [{"id": CANAL_ESPERADO}]}
    )


@pytest.fixture
def entorno(monkeypatch):
    """Entorno con las cuatro variables puestas a valores de mentira."""
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", CLIENT_ID_FALSO)
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", SECRETO_FALSO)
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", REFRESH_FALSO)
    monkeypatch.setenv("EXPECTED_YOUTUBE_CHANNEL_ID", CANAL_ESPERADO)
    monkeypatch.delenv("YOUTUBE_SCOPE", raising=False)
    return Settings.desde_entorno()


def _credenciales() -> CredencialesOAuth:
    return CredencialesOAuth(
        client_id=CLIENT_ID_FALSO,
        client_secret=SECRETO_FALSO,
        refresh_token=REFRESH_FALSO,
    )


# ---------------------------------------------------------------------------
# 1-2. Credenciales: ausentes y cargadas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variable",
    ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"],
)
def test_una_credencial_ausente_se_nombra_sin_revelar_las_demas(
    entorno, monkeypatch, variable
):
    monkeypatch.delenv(variable, raising=False)
    with pytest.raises(ConfiguracionInvalida) as capturado:
        cargar_credenciales(Settings.desde_entorno())

    mensaje = capturado.value.mensaje
    assert variable in mensaje
    for secreto in (SECRETO_FALSO, REFRESH_FALSO):
        assert secreto not in mensaje


def test_credenciales_vacias_cuentan_como_ausentes(entorno, monkeypatch):
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "   ")
    with pytest.raises(ConfiguracionInvalida, match="YOUTUBE_REFRESH_TOKEN"):
        cargar_credenciales(Settings.desde_entorno())


def test_las_credenciales_se_cargan_del_entorno(entorno):
    credenciales = cargar_credenciales(entorno)
    assert credenciales.client_id == CLIENT_ID_FALSO
    assert credenciales.client_secret == SECRETO_FALSO
    assert credenciales.refresh_token == REFRESH_FALSO


def test_las_credenciales_no_son_campos_del_dataclass_de_configuracion():
    """Un campo entraría en el ``repr`` de Settings y de ahí a cualquier traza."""
    campos = {c.name for c in Settings.__dataclass_fields__.values()}
    for prohibido in (
        "youtube_client_secret", "youtube_refresh_token", "youtube_client_id",
    ):
        assert prohibido not in campos


def test_la_configuracion_publica_no_lleva_credenciales(entorno):
    """``publico()`` es lo que acaba en el manifest, que sí se versiona."""
    publico = entorno.publico()
    serializado = json.dumps(publico)
    for secreto in (SECRETO_FALSO, REFRESH_FALSO, CLIENT_ID_FALSO):
        assert secreto not in serializado
    for clave in publico:
        assert "token" not in clave.lower()
        assert "secret" not in clave.lower()


# ---------------------------------------------------------------------------
# 3-4. Access token y flujo de refresh
# ---------------------------------------------------------------------------


def test_el_refresh_token_se_canjea_por_un_access_token(llamadas):
    _preparar(llamadas)
    token = obtener_access_token(_credenciales())

    assert token.access_token == ACCESS_FALSO
    assert token.expires_in_s == 3599
    assert token.scope == SCOPE_SUBIDA


def test_el_canje_usa_el_endpoint_y_los_parametros_documentados(llamadas):
    """Los valores salen de la documentación de Google, no de un blog."""
    _preparar(llamadas)
    obtener_access_token(_credenciales())

    canje = llamadas[0]
    assert canje["url"] == "https://oauth2.googleapis.com/token"
    assert canje["method"] == "POST"
    assert "grant_type=refresh_token" in canje["body"]
    for parametro in ("client_id", "client_secret", "refresh_token"):
        assert f"{parametro}=" in canje["body"]


def test_un_canje_sin_access_token_utilizable_se_rechaza(llamadas):
    _preparar(llamadas, token={"expires_in": 3599})
    with pytest.raises(RespuestaInvalida, match="access_token"):
        obtener_access_token(_credenciales())


def test_el_timeout_configurado_llega_a_la_peticion(llamadas):
    _preparar(llamadas)
    obtener_access_token(_credenciales(), timeout_s=7)
    assert llamadas[0]["timeout"] == 7


# ---------------------------------------------------------------------------
# 5-7. Autenticación correcta e identidad del canal
# ---------------------------------------------------------------------------


def test_autenticacion_correcta_con_el_canal_esperado(entorno, llamadas):
    _preparar(llamadas)
    comprobacion = comprobar_autenticacion(entorno)

    assert comprobacion.resultado is ResultadoAuth.autenticado
    assert comprobacion.autenticado is True
    assert comprobacion.canal_correcto is True
    assert comprobacion.channel_id == CANAL_ESPERADO
    assert comprobacion.expected_channel_id == CANAL_ESPERADO
    assert comprobacion.reintentable is False


def test_la_identidad_se_lee_del_endpoint_documentado(llamadas):
    _preparar(llamadas)
    identidad = identidad_del_canal(TokenAcceso(access_token=ACCESS_FALSO))

    peticion = llamadas[0]
    assert peticion["url"].startswith("https://www.googleapis.com/youtube/v3/channels")
    assert "mine=true" in peticion["url"]
    assert "part=id" in peticion["url"]
    assert peticion["method"] == "GET"
    assert identidad.channel_id == CANAL_ESPERADO
    assert identidad.leido_con == "channels.list?mine=true"


def test_se_piden_solo_los_datos_necesarios(llamadas):
    """``part=id`` y nada más: comparar identidades no necesita el título."""
    _preparar(llamadas)
    identidad_del_canal(TokenAcceso(access_token=ACCESS_FALSO))
    assert "snippet" not in llamadas[0]["url"]
    assert "statistics" not in llamadas[0]["url"]


def test_una_autorizacion_sin_canal_asociado_se_detecta(llamadas):
    _preparar(llamadas, canal={"items": []})
    with pytest.raises(RespuestaInvalida, match="ningún canal"):
        identidad_del_canal(TokenAcceso(access_token=ACCESS_FALSO))


# ---------------------------------------------------------------------------
# 8. Canal distinto del esperado
# ---------------------------------------------------------------------------


def test_un_canal_distinto_no_autoriza_a_seguir(entorno, llamadas):
    """La credencial funciona y aun así no se continúa.

    Es la distinción que la arquitectura pide no confundir: ``autenticado`` es
    verdadero porque el token sirvió, y ``canal_correcto`` es falso porque el
    canal no es el que se esperaba. Solo lo segundo autoriza a seguir.
    """
    _preparar(llamadas, canal={"items": [{"id": OTRO_CANAL}]})
    comprobacion = comprobar_autenticacion(entorno)

    assert comprobacion.resultado is ResultadoAuth.canal_incorrecto
    assert comprobacion.autenticado is True
    assert comprobacion.canal_correcto is False
    assert comprobacion.channel_id == OTRO_CANAL
    assert comprobacion.expected_channel_id == CANAL_ESPERADO
    assert comprobacion.reintentable is False


def test_autenticado_y_canal_correcto_no_son_lo_mismo():
    """Se comprueba sobre los desenlaces, no solo sobre un caso."""
    autenticados = {
        r
        for r in ResultadoAuth
        if ComprobacionAuth(resultado=r, detalle="x").autenticado
    }
    correctos = {
        r
        for r in ResultadoAuth
        if ComprobacionAuth(resultado=r, detalle="x").canal_correcto
    }
    assert autenticados == {ResultadoAuth.autenticado, ResultadoAuth.canal_incorrecto}
    assert correctos == {ResultadoAuth.autenticado}
    assert correctos < autenticados


def test_sin_canal_esperado_no_se_da_por_bueno_ninguno(entorno, monkeypatch, llamadas):
    monkeypatch.delenv("EXPECTED_YOUTUBE_CHANNEL_ID", raising=False)
    comprobacion = comprobar_autenticacion(Settings.desde_entorno())

    assert comprobacion.resultado is ResultadoAuth.credenciales_ausentes
    assert comprobacion.canal_correcto is False
    assert llamadas == [], "no debe llamarse a YouTube sin saber qué se espera"


# ---------------------------------------------------------------------------
# 9. Credenciales inválidas o revocadas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "codigo, cuerpo",
    [
        (400, {"error": "invalid_grant"}),
        (401, {"error": "invalid_client"}),
        (400, {"error": "unauthorized_client"}),
        (400, {"error": "admin_policy_enforced"}),
    ],
)
def test_una_autorizacion_revocada_o_invalida_no_se_reintenta(
    entorno, llamadas, codigo, cuerpo
):
    llamadas.respuestas[ENDPOINT_TOKEN] = _http_error(codigo, cuerpo)
    comprobacion = comprobar_autenticacion(entorno)

    assert comprobacion.resultado is ResultadoAuth.autorizacion_invalida
    assert comprobacion.autenticado is False
    assert comprobacion.reintentable is False, "reintentar no la desrevoca"


def test_el_error_de_autorizacion_dice_qué_hacer(entorno, llamadas):
    llamadas.respuestas[ENDPOINT_TOKEN] = _http_error(400, {"error": "invalid_grant"})
    comprobacion = comprobar_autenticacion(entorno)
    assert "consentimiento" in comprobacion.detalle


def test_un_access_token_rechazado_por_la_api_es_autorizacion_invalida(llamadas):
    llamadas.respuestas[ENDPOINT_CANALES] = _http_error(401, {"error": "unauthorized"})
    with pytest.raises(AutorizacionInvalida):
        identidad_del_canal(TokenAcceso(access_token=ACCESS_FALSO))


def test_un_alcance_insuficiente_es_su_propio_desenlace(entorno, llamadas):
    """403 ``insufficientPermissions``, tal como lo documenta Google.

    Es el desenlace que resolvería la incertidumbre sobre si ``youtube.upload``
    basta para leer la identidad del canal: si aparece, la respuesta está en el
    resultado y no hay que deducirla.
    """
    _preparar(llamadas)
    llamadas.respuestas[ENDPOINT_CANALES] = _http_error(
        403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}
    )
    comprobacion = comprobar_autenticacion(entorno)

    assert comprobacion.resultado is ResultadoAuth.alcance_insuficiente
    assert comprobacion.reintentable is False
    assert "alcance" in comprobacion.detalle


# ---------------------------------------------------------------------------
# 10-11. Fallos transitorios y permanentes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("codigo", [500, 502, 503, 504])
def test_un_error_de_servidor_es_transitorio(entorno, llamadas, codigo):
    llamadas.respuestas[ENDPOINT_TOKEN] = _http_error(codigo)
    comprobacion = comprobar_autenticacion(entorno)

    assert comprobacion.resultado is ResultadoAuth.error_transitorio
    assert comprobacion.reintentable is True


def test_un_limite_de_tasa_es_transitorio(entorno, llamadas):
    llamadas.respuestas[ENDPOINT_TOKEN] = _http_error(429)
    comprobacion = comprobar_autenticacion(entorno)
    assert comprobacion.resultado is ResultadoAuth.error_transitorio
    assert comprobacion.reintentable is True


def test_la_cuota_agotada_se_trata_como_transitoria(entorno, llamadas):
    """Se repone sola, sin que intervenga nadie; por eso no es permanente."""
    _preparar(llamadas)
    llamadas.respuestas[ENDPOINT_CANALES] = _http_error(
        403, {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    )
    comprobacion = comprobar_autenticacion(entorno)
    assert comprobacion.resultado is ResultadoAuth.error_transitorio


def test_una_red_caida_es_transitoria(entorno, llamadas):
    llamadas.respuestas[ENDPOINT_TOKEN] = urllib.error.URLError("sin ruta al host")
    comprobacion = comprobar_autenticacion(entorno)
    assert comprobacion.resultado is ResultadoAuth.error_transitorio
    assert comprobacion.reintentable is True


def test_un_timeout_es_transitorio(entorno, llamadas):
    import socket

    llamadas.respuestas[ENDPOINT_TOKEN] = socket.timeout("tardó demasiado")
    comprobacion = comprobar_autenticacion(entorno)
    assert comprobacion.resultado is ResultadoAuth.error_transitorio


def test_un_error_permanente_de_la_api_no_se_reintenta(entorno, llamadas):
    llamadas.respuestas[ENDPOINT_TOKEN] = _http_error(400, {"error": "invalid_request"})
    comprobacion = comprobar_autenticacion(entorno)

    assert comprobacion.resultado is ResultadoAuth.error_api
    assert comprobacion.reintentable is False


def test_una_respuesta_que_no_es_json_se_rechaza(llamadas):
    llamadas.respuestas[ENDPOINT_TOKEN] = lambda: _Respuesta(b"<html>502</html>")
    with pytest.raises(RespuestaInvalida, match="JSON"):
        obtener_access_token(_credenciales())


def test_la_semantica_de_reintento_es_coherente():
    """Solo los transitorios se reintentan; lo demás exige una persona."""
    reintentables = {
        r
        for r in ResultadoAuth
        if ComprobacionAuth(resultado=r, detalle="x").reintentable
    }
    assert reintentables == {ResultadoAuth.error_transitorio}


def test_los_errores_del_proyecto_conservan_su_categoria():
    """La taxonomía existente se reutiliza, no se duplica."""
    assert AutorizacionInvalida("x").retryable is False
    assert AutorizacionInvalida("x").categoria == "permanente"
    assert ErrorTransitorio("x").retryable is True
    assert LimiteDeTasa("x").retryable is True
    assert TiempoAgotado("x").retryable is True
    assert ConfiguracionInvalida("x").retryable is False


# ---------------------------------------------------------------------------
# 12-14. Ningún secreto se escapa
# ---------------------------------------------------------------------------


def test_el_refresh_token_no_aparece_en_ningun_sitio(entorno, llamadas, caplog, capsys):
    """El test de seguridad: se recorre todo lo que sale del proceso.

    Logs, salida estándar, el resultado serializado y el ``repr`` de cada objeto
    que toca la credencial.
    """
    _preparar(llamadas)
    with caplog.at_level("DEBUG"):
        comprobacion = comprobar_autenticacion(entorno)

    capturado = capsys.readouterr()
    superficies = {
        "log": caplog.text,
        "stdout": capturado.out,
        "stderr": capturado.err,
        "resultado": json.dumps(comprobacion.a_dict()),
        "repr del resultado": repr(comprobacion),
        "repr de las credenciales": repr(cargar_credenciales(entorno)),
        "str de las credenciales": str(cargar_credenciales(entorno)),
        "repr del token": repr(TokenAcceso(access_token=ACCESS_FALSO)),
    }
    for nombre, texto in superficies.items():
        for secreto in (REFRESH_FALSO, SECRETO_FALSO, ACCESS_FALSO):
            assert secreto not in texto, f"{nombre} contiene una credencial"


def test_ninguna_excepcion_arrastra_la_credencial(entorno, llamadas):
    """Ni el mensaje, ni los args, ni el cuerpo de la respuesta de error.

    El cuerpo se descarta a propósito: puede repetir lo que se envió, y el
    adaptador de LLM ya toma la misma precaución por el mismo motivo.
    """
    llamadas.respuestas[ENDPOINT_TOKEN] = _http_error(
        400,
        {
            "error": "invalid_grant",
            "error_description": f"Token {REFRESH_FALSO} has been revoked",
        },
    )
    with pytest.raises(AutorizacionInvalida) as capturado:
        obtener_access_token(_credenciales())

    excepcion = capturado.value
    for texto in (str(excepcion), excepcion.mensaje, repr(excepcion.args)):
        assert REFRESH_FALSO not in texto
    assert REFRESH_FALSO not in json.dumps(excepcion.a_dict())


def test_el_resultado_serializado_no_lleva_credenciales(entorno, llamadas):
    _preparar(llamadas)
    datos = comprobar_autenticacion(entorno).a_dict()

    serializado = json.dumps(datos)
    for secreto in (REFRESH_FALSO, SECRETO_FALSO, ACCESS_FALSO, CLIENT_ID_FALSO):
        assert secreto not in serializado
    for clave in datos:
        assert "token" not in clave.lower()
        assert "secret" not in clave.lower()


def test_el_redactor_del_proyecto_cubre_estas_variables(entorno):
    """Segunda barrera: si una credencial llegara a un log, se enmascara.

    Funciona porque los nombres de las variables contienen TOKEN y SECRET, que es
    lo que ``app.core.redaction`` reconoce. No es casualidad: por eso se
    conservaron los nombres que ya estaban en ``.env.example``.
    """
    from app.core.redaction import redactar, valores_secretos

    secretos = valores_secretos()
    assert REFRESH_FALSO in secretos
    assert SECRETO_FALSO in secretos

    texto = f"se usó {REFRESH_FALSO} con {SECRETO_FALSO}"
    limpio = redactar(texto)
    assert REFRESH_FALSO not in limpio
    assert SECRETO_FALSO not in limpio


def test_la_comprobacion_no_escribe_nada_en_disco(entorno, llamadas, tmp_path, monkeypatch):
    """Ni artefactos, ni ``runs/``, ni un token cacheado."""
    monkeypatch.chdir(tmp_path)
    _preparar(llamadas)
    comprobar_autenticacion(entorno)

    assert list(tmp_path.iterdir()) == [], "la comprobación dejó archivos"


# ---------------------------------------------------------------------------
# 15-17. Ni subida, ni videos.insert, ni acoplamiento con el publisher
# ---------------------------------------------------------------------------


def test_no_se_llama_a_ningun_endpoint_de_subida(entorno, llamadas):
    """Se comprueba sobre las URLs realmente pedidas, no sobre el código."""
    _preparar(llamadas)
    comprobar_autenticacion(entorno)

    assert len(llamadas) == 2
    urls = [c["url"] for c in llamadas]
    assert urls[0].startswith(ENDPOINT_TOKEN)
    assert urls[1].startswith(ENDPOINT_CANALES)
    for url in urls:
        for prohibido in ("upload", "videos", "/v3/videos", "uploadType", "resumable"):
            assert prohibido not in url, f"{url} toca algo de subida"


def test_ninguna_peticion_usa_un_metodo_de_escritura(entorno, llamadas):
    """El canje es POST por contrato de OAuth; la lectura del canal es GET.

    No hay ningún PUT, PATCH ni DELETE: esta fase no modifica nada en YouTube.
    """
    _preparar(llamadas)
    comprobar_autenticacion(entorno)
    metodos = [c["method"] for c in llamadas]
    assert metodos == ["POST", "GET"]


def _urls_del_codigo() -> set[str]:
    """Toda cadena que empieza por http en el código de ``auth``, sin docstrings.

    Los docstrings se descartan porque ahí sí se nombra ``videos.insert``, y deben
    nombrarlo: explicar qué queda fuera es documentación. Lo que importa es a qué
    URLs puede llegar el código.
    """
    import ast
    import inspect

    arbol = ast.parse(inspect.getsource(auth))
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

    return {
        nodo.value
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Constant)
        and isinstance(nodo.value, str)
        and nodo.value.startswith("http")
    }


def test_el_codigo_solo_puede_alcanzar_los_endpoints_de_autenticacion():
    """Invariante fuerte: el módulo no tiene ninguna otra URL escrita.

    No se buscan palabras prohibidas —``upload`` aparece legítimamente en la URL
    del alcance— sino el conjunto completo de URLs que el código contiene. Si
    fuera exactamente este, no hay ninguna ruta por la que llegar a un endpoint
    de subida, porque no está escrita en ninguna parte.
    """
    assert _urls_del_codigo() == {
        "https://oauth2.googleapis.com/token",
        "https://www.googleapis.com/youtube/v3/channels",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://www.googleapis.com/auth/youtube.upload",
    }


def test_ninguna_url_del_codigo_es_de_subida():
    """Y de esas cuatro, ninguna es un endpoint de escritura de vídeos."""
    for url in _urls_del_codigo():
        assert "/videos" not in url
        assert "uploadType" not in url
        assert not url.startswith("https://www.googleapis.com/upload/")


def test_el_modulo_de_auth_no_depende_de_ningun_publisher():
    """La autenticación tiene que poder comprobarse sin capacidad de publicar."""
    import ast
    import inspect

    arbol = ast.parse(inspect.getsource(auth))
    importados = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            importados.add(nodo.module)
        elif isinstance(nodo, ast.Import):
            importados.update(a.name for a in nodo.names)

    for modulo in importados:
        assert "publish" not in modulo.lower()
        assert "publicacion" not in modulo.lower()
    # Y tampoco de los contratos de publicación de G7.0: auth es anterior a ellos.
    assert not any("contracts" in m for m in importados)


def test_no_se_instalo_ninguna_libreria_de_google():
    """Sin el cliente oficial, ``videos.insert`` no está a una línea de distancia."""
    for modulo in ("googleapiclient", "google.oauth2", "google_auth_oauthlib"):
        with pytest.raises(ImportError):
            __import__(modulo)


# ---------------------------------------------------------------------------
# 18-19. Configuración
# ---------------------------------------------------------------------------


def test_el_alcance_por_defecto_es_el_minimo(entorno):
    assert entorno.youtube_scope == SCOPE_SUBIDA
    assert entorno.youtube_scope.endswith("/auth/youtube.upload")


def test_el_alcance_es_configurable_sin_tocar_codigo(monkeypatch):
    """Porque la documentación no confirma que el mínimo baste para leer el canal."""
    monkeypatch.setenv("YOUTUBE_SCOPE", "https://www.googleapis.com/auth/youtube.readonly")
    assert Settings.desde_entorno().youtube_scope.endswith("youtube.readonly")


def test_el_alcance_solicitado_queda_registrado_en_el_resultado(entorno, llamadas):
    _preparar(llamadas)
    comprobacion = comprobar_autenticacion(entorno)
    assert comprobacion.a_dict()["scope_requested"] == SCOPE_SUBIDA
    assert comprobacion.a_dict()["scope_granted"] == SCOPE_SUBIDA


def test_un_timeout_mal_configurado_se_detecta(monkeypatch):
    monkeypatch.setenv("YOUTUBE_TIMEOUT_S", "pronto")
    with pytest.raises(ConfiguracionInvalida, match="YOUTUBE_TIMEOUT_S"):
        Settings.desde_entorno()


def test_un_channel_id_con_espacios_se_normaliza(monkeypatch):
    monkeypatch.setenv("EXPECTED_YOUTUBE_CHANNEL_ID", f"  {CANAL_ESPERADO}  ")
    assert Settings.desde_entorno().expected_youtube_channel_id == CANAL_ESPERADO


def test_no_se_valida_el_formato_del_channel_id(entorno, monkeypatch, llamadas):
    """Decisión deliberada: se compara, no se valida la forma.

    La documentación de Google garantiza que cada canal tiene un identificador
    único, pero no publica una gramática normativa para él. Rechazar lo que no
    empiece por «UC» sería inventar una regla, y además inútil: lo que protege de
    publicar en el canal equivocado es la **comparación** con el identificador
    esperado, no el aspecto de la cadena.
    """
    monkeypatch.setenv("EXPECTED_YOUTUBE_CHANNEL_ID", "XYnoEmpiezaPorUC")
    entorno = Settings.desde_entorno()
    _preparar(llamadas, canal={"items": [{"id": "XYnoEmpiezaPorUC"}]})

    comprobacion = comprobar_autenticacion(entorno)
    assert comprobacion.resultado is ResultadoAuth.autenticado


# ---------------------------------------------------------------------------
# 20. Determinismo
# ---------------------------------------------------------------------------


def test_la_comprobacion_es_determinista_bajo_la_api_simulada(entorno, llamadas):
    _preparar(llamadas)
    primero = comprobar_autenticacion(entorno).a_dict()
    _preparar(llamadas)
    segundo = comprobar_autenticacion(entorno).a_dict()

    del primero["checked_at"], segundo["checked_at"]
    assert primero == segundo


def test_ningun_test_de_este_archivo_sale_a_la_red(llamadas):
    """``urlopen`` está sustituido: una URL no preparada revienta el test."""
    peticion = urllib.request.Request("https://www.googleapis.com/youtube/v3/videos")
    with pytest.raises(AssertionError, match="nadie preparó una respuesta"):
        urllib.request.urlopen(peticion)


def test_el_resultado_es_inmutable(entorno, llamadas):
    _preparar(llamadas)
    comprobacion = comprobar_autenticacion(entorno)
    with pytest.raises(Exception):
        comprobacion.resultado = ResultadoAuth.autenticado  # type: ignore[misc]
    # Cambiarlo exige construir otro, y entonces las propiedades se recalculan.
    otro = replace(comprobacion, resultado=ResultadoAuth.canal_incorrecto)
    assert otro.canal_correcto is False
    assert comprobacion.canal_correcto is True
