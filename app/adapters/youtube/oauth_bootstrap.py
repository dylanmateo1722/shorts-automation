"""Alta OAuth interactiva y local: conseguir el refresh token una sola vez.

Esto es lo que se ejecuta **una vez, a mano, en una máquina con navegador**. Su
único producto es un refresh token que después se guarda como secreto fuera del
repositorio. No publica nada, no sube nada y no sabe qué es un publisher.

La diferencia con ``auth.py``
-----------------------------

``auth.py`` comprueba una autorización **que ya existe**. Este módulo la
**obtiene**. Son dos momentos distintos de la vida del proyecto: el primero
ocurre en cada corrida y en CI, el segundo ocurre una vez cada muchos meses y
nunca en CI.

Lo que este módulo **no** hace, y es deliberado
----------------------------------------------

* **No guarda el refresh token en ningún archivo.** Ni en el repositorio, ni en
  ``runs/``, ni en un ``.env`` que escriba por su cuenta. Escribirlo sería
  decidir por quien lo ejecuta dónde vive su credencial más sensible, y crear un
  archivo que alguien acabaría subiendo sin querer.
* **No lo imprime entero.** Muestra una pista de los primeros y últimos
  caracteres, suficiente para comprobar que se copió bien y nada más.
* **No copia el JSON de Google a ninguna parte.** Se lee de la ruta que se le
  indique y se extraen solo ``client_id`` y ``client_secret``.

Qué se sostiene en documentación oficial
----------------------------------------

* ``code_verifier``: cadena aleatoria de alta entropía, **mínimo 43 y máximo 128
  caracteres**, con los caracteres no reservados;
* ``code_challenge``: «el hash SHA256 del code verifier, codificado en Base64URL
  sin relleno», con ``code_challenge_method=S256``;
* redirección para aplicaciones de escritorio: ``http://127.0.0.1:puerto``,
  «sustituyendo *puerto* por el número de puerto real en el que escucha la
  aplicación»; cualquier puerto libre sirve;
* canje del código: ``grant_type=authorization_code`` con ``code``,
  ``redirect_uri``, ``client_id``, ``client_secret`` y ``code_verifier``;
* «los refresh tokens se devuelven siempre para aplicaciones instaladas».

Sobre ese último punto hay un matiz honesto: la documentación de aplicaciones
nativas afirma que siempre se devuelve refresh token, pero **no** describe qué
ocurre al reautorizar una aplicación que ya tenía permiso. Por eso este módulo no
da por hecho que venga: si no viene, lo dice con claridad y ofrece el remedio
(``--forzar-consentimiento``, que añade ``prompt=consent``), en vez de fallar con
un ``KeyError``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from time import monotonic

from app.adapters.youtube.auth import (
    ENDPOINT_AUTORIZACION,
    ENDPOINT_TOKEN,
    ComprobacionAuth,
    TokenAcceso,
    _peticion,
    comprobar_autenticacion,
)
from app.config.settings import Settings
from app.core.errors import (
    ConfiguracionInvalida,
    EntradaInvalida,
    RespuestaInvalida,
    TiempoAgotado,
)
from app.core.logging import log_evento

#: Dirección a la que se ata el servidor del callback. Loopback y solo loopback:
#: atarlo a 0.0.0.0 lo dejaría accesible desde la red local durante el minuto que
#: dura el consentimiento, y por ahí entraría un código de autorización ajeno.
HOST_CALLBACK = "127.0.0.1"

#: Ruta del callback. Cualquier otra petición que llegue al servidor (el
#: ``/favicon.ico`` que pide el navegador, por ejemplo) se ignora sin consumir el
#: turno de espera.
RUTA_CALLBACK = "/oauth2callback"

#: Cuánto se espera al consentimiento antes de rendirse. Un minuto es poco para
#: una persona que tiene que elegir cuenta y leer una pantalla; diez es lo que
#: tarda alguien que se levanta a por un café.
TIMEOUT_CALLBACK_S = 300

#: Longitudes que la documentación fija para el ``code_verifier``.
VERIFICADOR_MIN = 43
VERIFICADOR_MAX = 128

#: Caracteres no reservados admitidos en el ``code_verifier``.
_VERIFICADOR_VALIDO = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")


# ---------------------------------------------------------------------------
# Credenciales del cliente de escritorio
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredencialesCliente:
    """Lo único que hace falta del JSON que descarga Google.

    El secreto queda fuera del ``repr`` por el mismo motivo que en ``auth.py``:
    un ``dataclass`` imprime todos sus campos, y basta con que una excepción
    arrastre el objeto para que acabe en un log.
    """

    client_id: str
    client_secret: str = field(repr=False)
    #: De dónde salió, para poder decirlo sin volver a abrir el archivo.
    origen: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return repr(self)


def cargar_cliente(ruta: str | Path) -> CredencialesCliente:
    """Lee ``client_id`` y ``client_secret`` del JSON de cliente de escritorio.

    Se acepta la clave ``installed``, que es la que Google usa para los clientes
    de escritorio. Un JSON con ``web`` se rechaza en vez de intentar adaptarlo:
    un cliente web no admite la redirección de loopback y descubrirlo aquí es
    mucho más barato que descubrirlo a mitad del consentimiento.

    El archivo **no se copia a ninguna parte**: se lee y se olvida.

    Raises:
        EntradaInvalida: el archivo no existe, no es JSON, o no trae lo que hace
            falta. El mensaje nunca incluye el contenido del archivo.
    """
    archivo = Path(ruta).expanduser()
    if not archivo.is_file():
        raise EntradaInvalida(
            f"no existe el archivo de credenciales {str(ruta)!r}; se descarga del "
            f"cliente OAuth de escritorio en Google Cloud y se guarda **fuera** "
            f"del repositorio",
            stage="youtube_auth_bootstrap",
        )

    try:
        datos = json.loads(archivo.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        # No se propaga el contenido: podría llevar el secreto en el mensaje.
        raise EntradaInvalida(
            f"el archivo de credenciales {archivo.name!r} no es un JSON legible",
            stage="youtube_auth_bootstrap",
        ) from exc

    if not isinstance(datos, dict):
        raise EntradaInvalida(
            f"el archivo {archivo.name!r} no contiene un objeto JSON",
            stage="youtube_auth_bootstrap",
        )

    if "installed" not in datos:
        otras = sorted(k for k in datos if isinstance(k, str))
        pista = (
            "ese es un cliente 'web', y un cliente web no admite la redirección "
            "de loopback que usa este flujo"
            if "web" in datos
            else f"no trae la clave 'installed'; tiene {otras}"
        )
        raise EntradaInvalida(
            f"el archivo {archivo.name!r} no es de un cliente OAuth de "
            f"escritorio: {pista}",
            stage="youtube_auth_bootstrap",
        )

    seccion = datos["installed"]
    if not isinstance(seccion, dict):
        raise EntradaInvalida(
            f"la clave 'installed' de {archivo.name!r} no es un objeto",
            stage="youtube_auth_bootstrap",
        )

    faltantes = [c for c in ("client_id", "client_secret") if not str(seccion.get(c) or "").strip()]
    if faltantes:
        raise EntradaInvalida(
            f"al archivo {archivo.name!r} le falta {', '.join(faltantes)} dentro "
            f"de 'installed'",
            stage="youtube_auth_bootstrap",
        )

    return CredencialesCliente(
        client_id=str(seccion["client_id"]).strip(),
        client_secret=str(seccion["client_secret"]).strip(),
        origen=archivo.name,
    )


# ---------------------------------------------------------------------------
# PKCE y state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParametrosPKCE:
    """El par verificador/desafío de PKCE.

    El verificador nunca sale del proceso salvo en el canje del código, y queda
    fuera del ``repr``: quien lo tenga junto al código de autorización puede
    completar el canje.
    """

    verificador: str = field(repr=False)
    desafio: str = ""
    metodo: str = "S256"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return repr(self)


def generar_pkce(longitud: int = 64) -> ParametrosPKCE:
    """Genera el verificador y su desafío S256.

    ``secrets.token_urlsafe`` produce exactamente los caracteres no reservados
    que la especificación admite, así que no hay que filtrarlos después. La
    longitud por defecto cae holgadamente dentro del rango documentado de 43 a
    128 caracteres.
    """
    if not VERIFICADOR_MIN <= longitud <= VERIFICADOR_MAX:
        raise ConfiguracionInvalida(
            f"el code_verifier debe medir entre {VERIFICADOR_MIN} y "
            f"{VERIFICADOR_MAX} caracteres; se pidieron {longitud}"
        )

    # token_urlsafe devuelve ~1.3 caracteres por byte; se recorta a la longitud
    # pedida, que sigue teniendo entropía de sobra.
    verificador = secrets.token_urlsafe(VERIFICADOR_MAX)[:longitud]
    digest = hashlib.sha256(verificador.encode("ascii")).digest()
    desafio = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return ParametrosPKCE(verificador=verificador, desafio=desafio)


def verificador_valido(verificador: str) -> bool:
    """Si el verificador cumple lo que la documentación exige."""
    return bool(_VERIFICADOR_VALIDO.match(verificador))


def generar_state() -> str:
    """Valor aleatorio que ata la respuesta del navegador a esta petición.

    Sin él, cualquiera que consiguiera que el navegador visitara la URL del
    callback con un código suyo podría hacer que este proceso canjeara una
    autorización ajena.
    """
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# URL de consentimiento
# ---------------------------------------------------------------------------


def construir_url_autorizacion(
    cliente: CredencialesCliente,
    *,
    redirect_uri: str,
    scope: str,
    state: str,
    pkce: ParametrosPKCE,
    forzar_consentimiento: bool = False,
) -> str:
    """Arma la URL del consentimiento con los parámetros documentados.

    ``access_type=offline`` se envía porque es lo que la arquitectura pide para
    obtener acceso sin el usuario delante. La documentación de aplicaciones
    nativas dice además que «los refresh tokens se devuelven siempre para
    aplicaciones instaladas», así que enviarlo no estorba y cubre el caso general.

    ``prompt=consent`` solo se añade a petición: fuerza la pantalla de
    consentimiento aunque ya se hubiera concedido, que es el remedio cuando una
    reautorización no devuelve refresh token.
    """
    parametros = {
        "client_id": cliente.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "code_challenge": pkce.desafio,
        "code_challenge_method": pkce.metodo,
        "access_type": "offline",
    }
    if forzar_consentimiento:
        parametros["prompt"] = "consent"
    return f"{ENDPOINT_AUTORIZACION}?{urllib.parse.urlencode(parametros)}"


# ---------------------------------------------------------------------------
# Servidor del callback
# ---------------------------------------------------------------------------


_PAGINA = """<!doctype html>
<html lang="es"><meta charset="utf-8">
<title>Shorts Automation</title>
<body style="font-family:system-ui;max-width:34rem;margin:4rem auto;line-height:1.5">
<h1>{titulo}</h1>
<p>{mensaje}</p>
<p style="color:#666">Ya puedes cerrar esta pestaña y volver a la terminal.</p>
</body></html>"""


class _ManejadorCallback(BaseHTTPRequestHandler):
    """Atiende exactamente una redirección de Google y se calla.

    No registra nada en la salida estándar: el ``BaseHTTPRequestHandler`` por
    defecto escribe cada petición en stderr, **incluida la query**, y ahí viaja
    el código de autorización. Por eso ``log_message`` queda anulado.
    """

    def log_message(self, formato, *args):  # noqa: D102 - silenciar a propósito
        return

    def do_GET(self):  # noqa: N802 - lo impone BaseHTTPRequestHandler
        partes = urllib.parse.urlparse(self.path)
        if partes.path != RUTA_CALLBACK:
            # El navegador pide /favicon.ico y cosas así. Ni se responde con
            # contenido ni se da por recibido el callback.
            self.send_response(404)
            self.end_headers()
            return

        consulta = urllib.parse.parse_qs(partes.query)
        estado = (consulta.get("state") or [""])[0]
        codigo = (consulta.get("code") or [""])[0]
        error = (consulta.get("error") or [""])[0]

        if estado != self.server.state_esperado:  # type: ignore[attr-defined]
            # No se dice cuál se esperaba: eso ayudaría a quien lo esté probando.
            self.server.fallo = (  # type: ignore[attr-defined]
                "el parámetro 'state' de la respuesta no coincide con el de la "
                "petición; la respuesta se descarta"
            )
            self._responder(403, "Respuesta descartada", "El «state» no coincide.")
            return

        if error:
            self.server.fallo = f"Google devolvió el error {error!r}"  # type: ignore[attr-defined]
            self._responder(
                400, "Autorización no concedida", f"Google respondió: {error}."
            )
            return

        if not codigo:
            self.server.fallo = "la respuesta no trae código de autorización"  # type: ignore[attr-defined]
            self._responder(400, "Respuesta incompleta", "No llegó ningún código.")
            return

        self.server.codigo = codigo  # type: ignore[attr-defined]
        self._responder(
            200, "Autorización recibida", "Shorts Automation ya tiene el código."
        )

    def _responder(self, estado: int, titulo: str, mensaje: str) -> None:
        cuerpo = _PAGINA.format(titulo=titulo, mensaje=mensaje).encode("utf-8")
        self.send_response(estado)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)


class ServidorCallback:
    """Servidor de un solo uso atado al loopback.

    Se abre en un puerto que elige el sistema (``port=0``), se usa para una
    redirección y se cierra. Como contexto para que no quede escuchando si algo
    revienta a mitad.
    """

    def __init__(self, *, host: str = HOST_CALLBACK, puerto: int = 0) -> None:
        self._servidor = HTTPServer((host, puerto), _ManejadorCallback)
        self._servidor.codigo = ""  # type: ignore[attr-defined]
        self._servidor.fallo = ""  # type: ignore[attr-defined]
        self._servidor.state_esperado = ""  # type: ignore[attr-defined]

    @property
    def puerto(self) -> int:
        return self._servidor.server_address[1]

    @property
    def redirect_uri(self) -> str:
        return f"http://{HOST_CALLBACK}:{self.puerto}{RUTA_CALLBACK}"

    def __enter__(self) -> "ServidorCallback":
        return self

    def __exit__(self, *args) -> bool:
        self.cerrar()
        return False

    def cerrar(self) -> None:
        self._servidor.server_close()

    def esperar_codigo(self, state: str, *, timeout_s: int = TIMEOUT_CALLBACK_S) -> str:
        """Espera la redirección y devuelve el código, o falla diciendo por qué.

        El servidor se cierra en cuanto llega un callback válido —no se queda
        escuchando ni un segundo más del necesario— y las peticiones que no son
        el callback (favicon y demás) no gastan el turno de espera.

        Raises:
            TiempoAgotado: nadie completó el consentimiento a tiempo.
            EntradaInvalida: el ``state`` no coincidía, Google devolvió un error
                o la respuesta llegó sin código.
        """
        self._servidor.state_esperado = state  # type: ignore[attr-defined]
        self._servidor.timeout = 1
        limite = monotonic() + timeout_s

        while monotonic() < limite:
            self._servidor.handle_request()
            if self._servidor.codigo:  # type: ignore[attr-defined]
                self.cerrar()
                return self._servidor.codigo  # type: ignore[attr-defined]
            if self._servidor.fallo:  # type: ignore[attr-defined]
                motivo = self._servidor.fallo  # type: ignore[attr-defined]
                self.cerrar()
                raise EntradaInvalida(motivo, stage="youtube_auth_bootstrap")

        self.cerrar()
        raise TiempoAgotado(
            f"no llegó ninguna respuesta al callback en {timeout_s}s; el "
            f"consentimiento no se completó",
            stage="youtube_auth_bootstrap",
        )


# ---------------------------------------------------------------------------
# Canje del código
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokensObtenidos:
    """Lo que devuelve el canje. Ni se persiste ni se imprime entero."""

    access_token: str = field(repr=False, default="")
    refresh_token: str = field(repr=False, default="")
    scope: str = ""
    expires_in_s: int = 0

    def __str__(self) -> str:  # pragma: no cover - trivial
        return repr(self)

    @property
    def pista_refresh(self) -> str:
        """Unos pocos caracteres del refresh token, para cotejar que se copió bien.

        Ni los primeros ni los últimos por sí solos permiten reconstruirlo, y
        sirven para lo único que hace falta aquí: que quien lo pegue en su gestor
        de secretos pueda comprobar que pegó el correcto.
        """
        return pista(self.refresh_token)


def pista(secreto: str, *, visibles: int = 4) -> str:
    """Enmascara un secreto dejando ver solo sus extremos."""
    limpio = (secreto or "").strip()
    if not limpio:
        return "(vacío)"
    if len(limpio) <= visibles * 2:
        return "*" * len(limpio)
    return f"{limpio[:visibles]}…{limpio[-visibles:]} ({len(limpio)} caracteres)"


def canjear_codigo(
    cliente: CredencialesCliente,
    *,
    codigo: str,
    redirect_uri: str,
    pkce: ParametrosPKCE,
    timeout_s: int = 30,
) -> TokensObtenidos:
    """Canjea el código de autorización por los tokens.

    Usa el mismo transporte y la misma clasificación de errores que ``auth.py``:
    no hay dos maneras de hablar con el endpoint de tokens.

    Raises:
        RespuestaInvalida: el canje no devolvió access token, o no devolvió
            refresh token —que es el único producto que justifica este flujo—.
    """
    cuerpo = urllib.parse.urlencode(
        {
            "client_id": cliente.client_id,
            "client_secret": cliente.client_secret,
            "code": codigo,
            "code_verifier": pkce.verificador,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")

    peticion = urllib.request.Request(
        ENDPOINT_TOKEN,
        data=cuerpo,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    datos = _peticion(peticion, timeout_s=timeout_s, que="el canje del código")

    access = str(datos.get("access_token") or "").strip()
    refresh = str(datos.get("refresh_token") or "").strip()
    if not access:
        raise RespuestaInvalida("el canje no devolvió ningún access_token utilizable")
    if not refresh:
        # Ocurre al reautorizar una aplicación que ya tenía permiso. La
        # documentación de aplicaciones nativas dice que el refresh token se
        # devuelve siempre, pero no describe ese caso, así que no se da por hecho.
        raise RespuestaInvalida(
            "el canje devolvió access_token pero ningún refresh_token. Suele "
            "pasar al reautorizar una aplicación que ya tenía permiso concedido: "
            "vuelve a ejecutar el alta con --forzar-consentimiento, o retira el "
            "acceso de la aplicación en la cuenta de Google y repítelo"
        )

    return TokensObtenidos(
        access_token=access,
        refresh_token=refresh,
        scope=str(datos.get("scope") or ""),
        expires_in_s=int(datos.get("expires_in") or 0),
    )


# ---------------------------------------------------------------------------
# El alta completa
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultadoBootstrap:
    """Lo que el alta deja: una comprobación y un token que hay que guardar.

    El refresh token está aquí porque hay que enseñárselo a quien ejecuta el
    alta —es el producto—, pero fuera del ``repr`` y sin que nada lo escriba a
    disco. ``a_dict`` **no lo incluye**: eso es lo que puede imprimirse.
    """

    comprobacion: ComprobacionAuth
    refresh_token: str = field(repr=False, default="")
    scope_concedido: str = ""
    client_id: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return repr(self)

    @property
    def pista_refresh(self) -> str:
        return pista(self.refresh_token)

    def a_dict(self) -> dict:
        """Representación imprimible. **Sin** el refresh token, a propósito."""
        return {
            **self.comprobacion.a_dict(),
            "refresh_token_hint": self.pista_refresh,
            "scope_granted": self.scope_concedido,
        }


def ejecutar_bootstrap(
    settings: Settings,
    ruta_credenciales: str | Path,
    *,
    timeout_callback_s: int = TIMEOUT_CALLBACK_S,
    forzar_consentimiento: bool = False,
    abrir_navegador=webbrowser.open,
    anunciar=print,
) -> ResultadoBootstrap:
    """Hace el alta completa y termina comprobando contra el AUTH CHECK existente.

    ``abrir_navegador`` y ``anunciar`` se inyectan para poder probar el flujo
    entero sin abrir un navegador ni ensuciar la salida de los tests.

    El alcance es el que diga ``YOUTUBE_SCOPE``, que por defecto sigue siendo el
    mínimo. **Este flujo no lo amplía por su cuenta**: si resulta que no basta, lo
    dirá el resultado y ampliarlo será una decisión posterior.
    """
    cliente = cargar_cliente(ruta_credenciales)
    scope = settings.youtube_scope
    pkce = generar_pkce()
    state = generar_state()

    with ServidorCallback() as servidor:
        url = construir_url_autorizacion(
            cliente,
            redirect_uri=servidor.redirect_uri,
            scope=scope,
            state=state,
            pkce=pkce,
            forzar_consentimiento=forzar_consentimiento,
        )
        log_evento(
            "-", "youtube_auth_bootstrap", "esperando_consentimiento",
            port=servidor.puerto, scope=scope,
        )
        anunciar(
            f"\nSe abrirá el navegador para autorizar «{cliente.client_id.split('-')[0]}…».\n"
            f"Si no se abre solo, copia esta dirección:\n\n{url}\n\n"
            f"Esperando la respuesta en {servidor.redirect_uri} "
            f"(hasta {timeout_callback_s}s)…"
        )
        abrir_navegador(url)
        codigo = servidor.esperar_codigo(state, timeout_s=timeout_callback_s)

    tokens = canjear_codigo(
        cliente,
        codigo=codigo,
        redirect_uri=servidor.redirect_uri,
        pkce=pkce,
        timeout_s=settings.youtube_timeout_s,
    )
    log_evento(
        "-", "youtube_auth_bootstrap", "tokens_obtenidos",
        scope=tokens.scope, expires_in_s=tokens.expires_in_s,
    )

    # El AUTH CHECK de G7.1, con el token recién obtenido en vez del del entorno:
    # la comparación de canal, los desenlaces y la clasificación son los mismos.
    # El canal esperado es opcional aquí porque el alta es justo el momento en que
    # se averigua cuál es.
    comprobacion = comprobar_autenticacion(
        settings,
        token=TokenAcceso(
            access_token=tokens.access_token,
            expires_in_s=tokens.expires_in_s,
            scope=tokens.scope,
        ),
        exigir_canal_esperado=False,
    )

    return ResultadoBootstrap(
        comprobacion=comprobacion,
        refresh_token=tokens.refresh_token,
        scope_concedido=tokens.scope,
        client_id=cliente.client_id,
    )


def instrucciones(resultado: ResultadoBootstrap) -> str:
    """Qué tiene que hacer ahora la persona que ejecutó el alta.

    Se nombran las variables y **no** se imprime ningún valor sensible: el
    refresh token se muestra aparte, una sola vez, por quien invoca esto.
    """
    canal = resultado.comprobacion.channel_id or "(no se pudo leer)"
    return (
        "Guarda el refresh token en tu gestor de secretos. No lo escribas en el\n"
        "repositorio, ni en runs/, ni en un archivo que vaya a versionarse.\n"
        "\n"
        "Para desarrollo local, en tu .env (que está en .gitignore):\n"
        "\n"
        "    YOUTUBE_CLIENT_ID=...\n"
        "    YOUTUBE_CLIENT_SECRET=...\n"
        "    YOUTUBE_REFRESH_TOKEN=...\n"
        f"    EXPECTED_YOUTUBE_CHANNEL_ID={canal}\n"
        "\n"
        "Para GitHub Actions, como secretos del repositorio:\n"
        "\n"
        "    YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN\n"
        "\n"
        "Después, comprueba que quedó bien con:\n"
        "\n"
        "    python -m app youtube-auth\n"
    )
