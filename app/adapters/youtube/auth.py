"""Autenticación OAuth 2.0 contra YouTube, y nada más.

**Único punto de contacto con el OAuth de Google.** Aquí se cargan las
credenciales, se canjea el refresh token por un access token y se lee qué canal
quedó autorizado. No hay subida, no hay ``videos.insert`` y no hay publisher:
cuando exista, vivirá en un módulo hermano y usará esto, no lo ampliará.

Qué se sostiene en documentación oficial
----------------------------------------

Los valores de este módulo salen de la documentación de Google, no de blogs:

* endpoint del token: ``https://oauth2.googleapis.com/token``, y el canje del
  refresh token lleva ``grant_type=refresh_token``, ``client_id``,
  ``client_secret`` y ``refresh_token``;
* identidad del canal: ``GET https://www.googleapis.com/youtube/v3/channels``
  con ``mine=true``, que «devuelve solo los canales propiedad del usuario
  autenticado»;
* un refresh token caducado, revocado o inválido hace que el endpoint responda
  ``invalid_grant``;
* un token cuyo alcance no cubre la petición produce **403 con razón
  ``insufficientPermissions``**: «el token OAuth 2.0 especifica alcances
  insuficientes para acceder a los datos solicitados».

Una incertidumbre que no se resuelve inventando
-----------------------------------------------

``videos.insert`` publica la lista de alcances que acepta. ``channels.list``
**no publica ninguna**: su apartado de autorización solo habla del caso especial
de ``auditDetails``. Por tanto la documentación **no afirma** que
``youtube.upload`` —el alcance mínimo que fija D17— baste para leer la identidad
del canal con ``mine=true``, y tampoco afirma lo contrario.

Eso no se puede comprobar sin una autorización real contra un proyecto real de
Google. Así que este módulo hace tres cosas en vez de adivinar: usa el alcance
mínimo que manda D17, lo deja configurable, y representa el 403 de alcance
insuficiente como un resultado propio y explícito que nombra el remedio. Si al
primer consentimiento real aparece ``ALCANCE_INSUFICIENTE``, la respuesta está
en el resultado y no hay que deducirla de un stacktrace.

Por qué urllib y no las librerías de Google
-------------------------------------------

Porque ``app/adapters/llm/openai_compatible.py`` ya habla HTTP con urllib y
clasifica sus errores igual, así que esto no introduce una segunda manera de
hacer lo mismo. Y porque con ``google-api-python-client`` el objeto de servicio
deja ``videos.insert`` a una línea de distancia: no tenerlo hace que lo prohibido
esté **ausente**, no solo sin escribir.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from app.config.settings import Settings
from app.core.errors import (
    AutorizacionInvalida,
    ConfiguracionInvalida,
    ErrorTransitorio,
    LimiteDeTasa,
    RespuestaInvalida,
    TiempoAgotado,
)
from app.core.logging import log_evento

#: Endpoint del canje de tokens (documentación oficial de Google).
ENDPOINT_TOKEN = "https://oauth2.googleapis.com/token"

#: Endpoint de la identidad del canal (YouTube Data API v3).
ENDPOINT_CANALES = "https://www.googleapis.com/youtube/v3/channels"

#: Endpoint del consentimiento. Solo se documenta aquí para el alta manual: este
#: módulo **no** abre navegadores ni sirve redirecciones.
ENDPOINT_AUTORIZACION = "https://accounts.google.com/o/oauth2/v2/auth"

#: Alcance mínimo que fija D17. Uno solo, y el más estrecho que permite subir.
#: **Sigue siendo el alcance conceptual mínimo del publisher**: la excepción de
#: más abajo es del alta por dispositivo, no de la arquitectura.
SCOPE_SUBIDA = "https://www.googleapis.com/auth/youtube.upload"

#: Alcance del alta por dispositivo, y **solo** de ella. Es una excepción
#: aprobada de D17, no su sustituto.
#:
#: Existe porque el flujo de dispositivo admite una lista cerrada de alcances y
#: ``youtube.upload`` **no está en ella**: la documentación de Google para el
#: flujo de dispositivo de la YouTube Data API solo permite
#: ``.../auth/youtube`` y ``.../auth/youtube.readonly``. El segundo no puede
#: subir, así que el único que sirve es este.
#:
#: Que sirva está comprobado en la otra punta: ``videos.insert`` publica su lista
#: de alcances autorizantes y ``.../auth/youtube`` está en ella.
#:
#: Es **más amplio** que ``SCOPE_SUBIDA``: «gestionar tu cuenta de YouTube», no
#: «subir vídeos». Un refresh token con este alcance puede hacer más daño si se
#: filtra, y por eso la excepción está escrita aquí y en el README en vez de
#: quedar como un valor por defecto que nadie recuerda haber elegido.
SCOPE_GESTION = "https://www.googleapis.com/auth/youtube"

#: Códigos HTTP que merecen otro intento. 429 se trata aparte.
CODIGOS_TRANSITORIOS = frozenset({500, 502, 503, 504})

#: Códigos de error del endpoint de tokens que significan «hay que volver a
#: autorizar». No se reintentan: reintentar una autorización revocada la deja
#: igual de revocada.
ERRORES_DE_AUTORIZACION = frozenset(
    {"invalid_grant", "invalid_client", "unauthorized_client", "admin_policy_enforced"}
)

#: Razón documentada de un 403 cuando el alcance no alcanza.
RAZON_ALCANCE_INSUFICIENTE = "insufficientPermissions"

#: Razón documentada de un 403 por cuota agotada.
RAZON_CUOTA = "quotaExceeded"

TIMEOUT_POR_DEFECTO_S = 30


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Credenciales
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredencialesOAuth:
    """Las tres piezas del canje, con las dos sensibles fuera del ``repr``.

    ``repr=False`` en el secreto y en el refresh token no es decoración: un
    ``dataclass`` imprime todos sus campos por defecto, y bastaría un
    ``print(cred)`` en una depuración, o que una excepción arrastre el objeto, para
    que la credencial acabara en un log público.

    El ``client_id`` sí aparece: en una aplicación instalada viaja dentro del
    propio programa y no es el secreto que protege la cuenta.
    """

    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return repr(self)


def cargar_credenciales(settings: Settings) -> CredencialesOAuth:
    """Lee las credenciales del entorno y dice exactamente cuál falta.

    Nunca se leen del código ni de un archivo versionado. El mensaje nombra la
    variable que falta y **jamás** su valor.

    Raises:
        ConfiguracionInvalida: si falta alguna de las tres.
    """
    faltantes = [
        nombre
        for nombre, valor in (
            ("YOUTUBE_CLIENT_ID", settings.youtube_client_id),
            ("YOUTUBE_CLIENT_SECRET", settings.youtube_client_secret),
            ("YOUTUBE_REFRESH_TOKEN", settings.youtube_refresh_token),
        )
        if not valor.strip()
    ]
    if faltantes:
        raise ConfiguracionInvalida(
            f"faltan credenciales de YouTube en el entorno: {', '.join(faltantes)}; "
            f"se obtienen una sola vez con el consentimiento OAuth local y se "
            f"guardan fuera del repositorio"
        )
    return CredencialesOAuth(
        client_id=settings.youtube_client_id.strip(),
        client_secret=settings.youtube_client_secret.strip(),
        refresh_token=settings.youtube_refresh_token.strip(),
    )


@dataclass(frozen=True)
class TokenAcceso:
    """Un access token vivo. Dura lo que diga ``expires_in`` y no se persiste.

    No se escribe en ningún artefacto ni en ``runs/``: es efímero por diseño, y
    guardarlo solo añadiría un sitio desde el que filtrarse.
    """

    access_token: str = field(repr=False)
    expires_in_s: int = 0
    scope: str = ""
    obtenido_en: datetime = field(default_factory=_ahora)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return repr(self)


@dataclass(frozen=True)
class IdentidadCanal:
    """Qué canal quedó autorizado, según YouTube."""

    channel_id: str
    #: Cómo se obtuvo, para que el resultado no oculte su procedencia.
    leido_con: str = "channels.list?mine=true"


# ---------------------------------------------------------------------------
# Resultado del AUTH CHECK
# ---------------------------------------------------------------------------


class ResultadoAuth(str, Enum):
    """Los desenlaces posibles de la comprobación, todos explícitos.

    ``AUTENTICADO`` y ``CANAL_INCORRECTO`` comparten algo importante: en los dos
    la credencial **funcionó**. Lo que los separa es si el canal es el que se
    esperaba. Confundir «autenticado» con «canal correcto» es el error que esta
    separación existe para impedir.

    ``ALCANCE_INSUFICIENTE`` no está en la lista original de la arquitectura: se
    añadió porque la documentación de Google lo documenta como 403
    ``insufficientPermissions`` y porque es el desenlace que resolvería la
    incertidumbre sobre si ``youtube.upload`` basta para leer el canal.
    """

    autenticado = "AUTHENTICATED"
    canal_incorrecto = "WRONG_CHANNEL"
    credenciales_ausentes = "CREDENTIALS_MISSING"
    autorizacion_invalida = "AUTHORIZATION_INVALID"
    alcance_insuficiente = "INSUFFICIENT_SCOPE"
    error_transitorio = "TRANSIENT_ERROR"
    error_api = "API_ERROR"


#: Desenlaces en los que la credencial funcionó, se haya acertado el canal o no.
RESULTADOS_CON_CREDENCIAL_VALIDA = frozenset(
    {ResultadoAuth.autenticado, ResultadoAuth.canal_incorrecto}
)

#: Los únicos desenlaces que merece la pena repetir. Se declara en positivo a
#: propósito: con una lista de excluidos, cualquier desenlace nuevo nacería
#: reintentable por descuido, y un éxito reintentable es tan absurdo como una
#: autorización revocada que se reintenta sola.
RESULTADOS_REINTENTABLES = frozenset({ResultadoAuth.error_transitorio})


@dataclass(frozen=True)
class ComprobacionAuth:
    """El resultado verificable del AUTH CHECK.

    No lleva ninguna credencial, ni entera ni en fragmentos, y ``a_dict`` es lo
    que puede escribirse en un artefacto o imprimirse en un log sin revisar nada
    más.
    """

    resultado: ResultadoAuth
    detalle: str
    channel_id: str | None = None
    expected_channel_id: str | None = None
    scope_solicitado: str = SCOPE_SUBIDA
    scope_concedido: str = ""
    comprobado_en: datetime = field(default_factory=_ahora)

    @property
    def autenticado(self) -> bool:
        """La credencial funcionó y YouTube dijo de qué canal se trata.

        **No** implica que sea el canal correcto: ver ``canal_correcto``.
        """
        return self.resultado in RESULTADOS_CON_CREDENCIAL_VALIDA

    @property
    def canal_correcto(self) -> bool:
        """El canal autenticado es el esperado. Lo único que autoriza a seguir."""
        return self.resultado is ResultadoAuth.autenticado

    @property
    def reintentable(self) -> bool:
        """Si volver a intentarlo podría dar otro resultado.

        Solo los fallos transitorios. Una autorización revocada o un canal
        equivocado no se arreglan repitiendo la petición, y reintentarlos
        automáticamente solo gastaría cuota y ocultaría el problema. Un éxito
        tampoco se reintenta: ya salió bien.
        """
        return self.resultado in RESULTADOS_REINTENTABLES

    def a_dict(self) -> dict:
        """Representación serializable. Libre de credenciales por construcción."""
        return {
            "result": self.resultado.value,
            "authenticated": self.autenticado,
            "correct_channel": self.canal_correcto,
            "retryable": self.reintentable,
            "channel_id": self.channel_id,
            "expected_channel_id": self.expected_channel_id,
            "scope_requested": self.scope_solicitado,
            "scope_granted": self.scope_concedido,
            "detail": self.detalle,
            "checked_at": self.comprobado_en.isoformat(),
        }


# ---------------------------------------------------------------------------
# Transporte
# ---------------------------------------------------------------------------


def _leer_codigo_de_error(exc: urllib.error.HTTPError) -> str:
    """Saca el código corto del error y **nada más** del cuerpo.

    El cuerpo de una respuesta de error puede repetir lo que se envió —el
    adaptador de LLM ya toma esta precaución por el mismo motivo—, así que de él
    se extrae solo el campo ``error``, que es una etiqueta corta y conocida, y se
    descarta el resto, ``error_description`` incluida.
    """
    try:
        cuerpo = json.loads(exc.read().decode("utf-8"))
    except (ValueError, OSError, AttributeError):
        return ""
    codigo = cuerpo.get("error") if isinstance(cuerpo, dict) else None
    # En las respuestas de la API de datos, ``error`` es un objeto con su propia
    # lista de razones; en las del endpoint de tokens es una cadena.
    if isinstance(codigo, str):
        return codigo
    if isinstance(codigo, dict):
        for detalle in codigo.get("errors") or []:
            if isinstance(detalle, dict) and detalle.get("reason"):
                return str(detalle["reason"])
    return ""


def _peticion(
    peticion: urllib.request.Request, *, timeout_s: int, que: str
) -> dict:
    """Ejecuta la petición y clasifica el fallo según la taxonomía del proyecto.

    Ningún mensaje de error incluye el cuerpo de la respuesta ni la credencial:
    solo el código HTTP y, cuando lo hay, la etiqueta corta del error.
    """
    try:
        with urllib.request.urlopen(peticion, timeout=timeout_s) as respuesta:
            bruto = respuesta.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        codigo = _leer_codigo_de_error(exc)
        if exc.code == 429 or codigo == RAZON_CUOTA:
            raise LimiteDeTasa(
                f"YouTube limitó la petición de {que} ({exc.code}"
                f"{f', {codigo}' if codigo else ''})"
            ) from exc
        if exc.code in CODIGOS_TRANSITORIOS:
            raise ErrorTransitorio(
                f"YouTube respondió {exc.code} a la petición de {que}"
            ) from exc
        if codigo == RAZON_ALCANCE_INSUFICIENTE:
            raise AutorizacionInvalida(
                f"el alcance del token no cubre {que} (403 {codigo}); hay que "
                f"volver a autorizar con un alcance que lo permita"
            ) from exc
        if codigo in ERRORES_DE_AUTORIZACION or exc.code == 401:
            raise AutorizacionInvalida(
                f"la autorización no es utilizable para {que} ({exc.code}"
                f"{f', {codigo}' if codigo else ''}); hay que repetir el "
                f"consentimiento OAuth"
            ) from exc
        raise RespuestaInvalida(
            f"YouTube rechazó la petición de {que} con {exc.code}"
            f"{f' ({codigo})' if codigo else ''}"
        ) from exc
    except socket.timeout as exc:
        raise TiempoAgotado(
            f"YouTube no respondió a la petición de {que} en {timeout_s}s"
        ) from exc
    except urllib.error.URLError as exc:
        raise ErrorTransitorio(f"no se pudo contactar con YouTube para {que}") from exc

    try:
        datos = json.loads(bruto)
    except ValueError as exc:
        raise RespuestaInvalida(
            f"la respuesta de {que} no es JSON válido"
        ) from exc
    if not isinstance(datos, dict):
        raise RespuestaInvalida(f"la respuesta de {que} no es un objeto JSON")
    return datos


# ---------------------------------------------------------------------------
# Canje del refresh token
# ---------------------------------------------------------------------------


def obtener_access_token(
    credenciales: CredencialesOAuth, *, timeout_s: int = TIMEOUT_POR_DEFECTO_S
) -> TokenAcceso:
    """Canjea el refresh token por un access token.

    Es el flujo ``refresh_token`` documentado por Google: no abre navegador, no
    pide consentimiento y no sirve para obtener la autorización inicial. Asume
    una autorización **ya concedida**, que es exactamente lo que esta fase
    necesita.

    Raises:
        AutorizacionInvalida: la autorización está revocada, caducada o no
            corresponde a estas credenciales. No reintentar.
        LimiteDeTasa, ErrorTransitorio, TiempoAgotado: fallos reintentables.
        RespuestaInvalida: Google respondió algo que no se puede usar.
    """
    cuerpo = urllib.parse.urlencode(
        {
            "client_id": credenciales.client_id,
            "client_secret": credenciales.client_secret,
            "refresh_token": credenciales.refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")

    peticion = urllib.request.Request(
        ENDPOINT_TOKEN,
        data=cuerpo,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    datos = _peticion(peticion, timeout_s=timeout_s, que="el canje del token")

    token = datos.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise RespuestaInvalida(
            "el canje del token no devolvió ningún access_token utilizable"
        )
    return TokenAcceso(
        access_token=token,
        expires_in_s=int(datos.get("expires_in") or 0),
        scope=str(datos.get("scope") or ""),
    )


# ---------------------------------------------------------------------------
# Identidad del canal
# ---------------------------------------------------------------------------


def identidad_del_canal(
    token: TokenAcceso, *, timeout_s: int = TIMEOUT_POR_DEFECTO_S
) -> IdentidadCanal:
    """Pregunta a YouTube de qué canal es el token.

    Se pide ``part=id`` y nada más. Traer el título o las estadísticas no haría
    falta para comparar identidades, y pedir menos datos es la misma disciplina
    que pedir el alcance mínimo.

    Raises:
        AutorizacionInvalida: el token no sirve, o su alcance no cubre esta
            lectura (403 ``insufficientPermissions``).
        RespuestaInvalida: la respuesta no trae ningún canal.
    """
    url = f"{ENDPOINT_CANALES}?{urllib.parse.urlencode({'part': 'id', 'mine': 'true'})}"
    peticion = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token.access_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    datos = _peticion(peticion, timeout_s=timeout_s, que="la identidad del canal")

    elementos = datos.get("items")
    if not isinstance(elementos, list) or not elementos:
        raise RespuestaInvalida(
            "la autorización no tiene ningún canal asociado: 'mine=true' no "
            "devolvió elementos"
        )
    primero = elementos[0]
    channel_id = primero.get("id") if isinstance(primero, dict) else None
    if not isinstance(channel_id, str) or not channel_id.strip():
        raise RespuestaInvalida("el canal devuelto no trae identificador")
    return IdentidadCanal(channel_id=channel_id.strip())


# ---------------------------------------------------------------------------
# AUTH CHECK
# ---------------------------------------------------------------------------


def comprobar_autenticacion(
    settings: Settings,
    *,
    timeout_s: int | None = None,
    token: TokenAcceso | None = None,
    exigir_canal_esperado: bool = True,
    scope_solicitado: str | None = None,
) -> ComprobacionAuth:
    """Comprueba credenciales, identidad del canal y coincidencia con el esperado.

    Devuelve un resultado en vez de lanzar: es una *comprobación*, y quien la
    llama necesita distinguir los seis desenlaces para decidir qué hacer, no
    recibir una excepción genérica. Por debajo sí se usan los errores del
    proyecto, que es donde vive la semántica de reintento.

    **Nunca devuelve un resultado que autorice a seguir si el canal no coincide.**

    Los tres parámetros opcionales existen para las altas interactivas, que
    llegan aquí en una situación distinta y no deberían duplicar esta lógica:

    * ``token`` evita el canje del refresh token. El alta acaba de obtener un
      access token y todavía **no** hay refresh token en el entorno: leerlo de
      ahí fallaría por algo que no es el problema.
    * ``exigir_canal_esperado=False`` permite terminar sin
      ``EXPECTED_YOUTUBE_CHANNEL_ID``, porque el alta es justo el momento en que
      se averigua cuál es el canal. Si está configurado se compara igual, y una
      discrepancia sigue dando ``WRONG_CHANNEL``.
    * ``scope_solicitado`` dice qué alcance se pidió **de verdad**. Hace falta
      porque el alta por dispositivo no usa ``YOUTUBE_SCOPE``: usa el suyo, más
      amplio, y sin esto el resultado declararía un alcance que nadie pidió. El
      alcance *concedido* sigue saliendo de lo que responda Google, no de aquí.

    Con los valores por defecto el comportamiento es exactamente el de siempre.
    """
    timeout = timeout_s if timeout_s is not None else settings.youtube_timeout_s
    esperado = settings.expected_youtube_channel_id.strip()
    scope = scope_solicitado if scope_solicitado is not None else settings.youtube_scope

    def resultado(tipo: ResultadoAuth, detalle: str, **extra) -> ComprobacionAuth:
        comprobacion = ComprobacionAuth(
            resultado=tipo,
            detalle=detalle,
            expected_channel_id=esperado or None,
            scope_solicitado=scope,
            **extra,
        )
        # El detalle ya viene sin credenciales, y el filtro de redacción del
        # logger es la última barrera por si alguna se colara.
        log_evento(
            "-", "youtube_auth", comprobacion.resultado.value.lower(),
            authenticated=comprobacion.autenticado,
            correct_channel=comprobacion.canal_correcto,
            retryable=comprobacion.reintentable,
            channel_id=comprobacion.channel_id,
        )
        return comprobacion

    if not esperado and exigir_canal_esperado:
        return resultado(
            ResultadoAuth.credenciales_ausentes,
            "falta EXPECTED_YOUTUBE_CHANNEL_ID: sin canal esperado no hay nada "
            "contra lo que comparar, y dar por bueno cualquier canal sería peor "
            "que no comprobar",
        )

    if token is None:
        try:
            credenciales = cargar_credenciales(settings)
        except ConfiguracionInvalida as exc:
            return resultado(ResultadoAuth.credenciales_ausentes, exc.mensaje)

    try:
        if token is None:
            token = obtener_access_token(credenciales, timeout_s=timeout)
        identidad = identidad_del_canal(token, timeout_s=timeout)
    except AutorizacionInvalida as exc:
        tipo = (
            ResultadoAuth.alcance_insuficiente
            if RAZON_ALCANCE_INSUFICIENTE in exc.mensaje
            else ResultadoAuth.autorizacion_invalida
        )
        return resultado(tipo, exc.mensaje)
    except (LimiteDeTasa, ErrorTransitorio, TiempoAgotado) as exc:
        return resultado(ResultadoAuth.error_transitorio, exc.mensaje)
    except RespuestaInvalida as exc:
        return resultado(ResultadoAuth.error_api, exc.mensaje)

    if not esperado:
        # Solo ocurre en el alta interactiva, que es donde se descubre el canal.
        # Se dice que no se comparó en vez de dejarlo implícito: un
        # ``AUTHENTICATED`` sin comparación no afirma lo mismo que uno con ella.
        return resultado(
            ResultadoAuth.autenticado,
            "la credencial es válida; no había canal esperado configurado, así "
            "que no se comparó con ninguno",
            channel_id=identidad.channel_id,
            scope_concedido=token.scope,
        )

    if identidad.channel_id != esperado:
        return resultado(
            ResultadoAuth.canal_incorrecto,
            "la credencial es válida pero autoriza otro canal; no se continúa",
            channel_id=identidad.channel_id,
            scope_concedido=token.scope,
        )

    return resultado(
        ResultadoAuth.autenticado,
        "la credencial es válida y el canal autenticado es el esperado",
        channel_id=identidad.channel_id,
        scope_concedido=token.scope,
    )
