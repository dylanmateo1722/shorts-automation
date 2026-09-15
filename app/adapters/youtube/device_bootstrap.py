"""Alta OAuth por **flujo de dispositivo**: conseguir el refresh token sin navegador.

Este es el segundo mecanismo de alta del proyecto, y existe por una razón
concreta: quien opera este repositorio trabaja desde un teléfono, y el
orquestador corre en una máquina remota sin navegador y sin un ``127.0.0.1``
alcanzable desde ese teléfono. El alta de ``oauth_bootstrap`` —loopback y
navegador local— es correcta y sigue siendo la de una máquina de escritorio,
pero en ese entorno no puede completarse: la redirección de Google iría al
loopback del teléfono, donde no escucha nada.

El flujo de dispositivo resuelve exactamente eso, y lo resuelve de la forma que
Google documenta: la máquina pide un código, la persona lo teclea en otra
pantalla, y la máquina sondea hasta que la autorización aparece. **No hay
redirección, no hay callback y no hay servidor escuchando en ningún puerto.**

La excepción aprobada de D17
----------------------------

D17 fija ``https://www.googleapis.com/auth/youtube.upload`` como alcance mínimo.
**Este flujo no puede pedirlo.** La documentación de Google para el flujo de
dispositivo de la YouTube Data API publica una lista cerrada de alcances
admitidos, y para YouTube solo contiene ``.../auth/youtube`` y
``.../auth/youtube.readonly``. El segundo no sube nada, así que el único que
sirve aquí es el primero.

Es una **excepción de este flujo**, no un cambio de la arquitectura:
``SCOPE_SUBIDA`` sigue siendo el alcance conceptual mínimo del publisher, y el
alta de escritorio lo sigue usando. Lo que cambia es qué puede pedir *este*
camino, y el precio está escrito: ``.../auth/youtube`` es «gestionar tu cuenta
de YouTube», más amplio que «subir vídeos», y un refresh token con ese alcance
hace más daño si se filtra.

Por qué aquí no hay PKCE ni ``state``
-------------------------------------

Porque el protocolo no los tiene. PKCE ata un código de autorización a quien
inició la petición, y ``state`` ata la respuesta del navegador a la petición que
la provocó: las dos cosas protegen una **redirección**, y en este flujo no hay
ninguna. La documentación de Google para el flujo de dispositivo no menciona ni
uno ni otro.

Lo que cumple esa función aquí es el ``device_code``: Google solo entrega los
tokens a quien presente ese código junto al ``client_secret``, y el
``device_code`` no sale nunca de este proceso. Por eso no se muestra ni se
registra en ninguna parte —al contrario que el ``user_code``, que es público por
diseño y la persona tiene que poder leerlo—.

Añadir PKCE o ``state`` de adorno sería peor que no tenerlos: sugeriría una
protección que el protocolo no está aplicando.

Qué se sostiene en documentación oficial
----------------------------------------

* endpoint del código de dispositivo: ``https://oauth2.googleapis.com/device/code``,
  con ``client_id`` y ``scope``;
* respuesta: ``device_code``, ``user_code``, ``verification_url``, ``expires_in``
  e ``interval``;
* sondeo contra ``https://oauth2.googleapis.com/token`` con ``client_id``,
  ``client_secret``, ``device_code`` y
  ``grant_type=urn:ietf:params:oauth:grant-type:device_code``;
* «los refresh tokens se devuelven **siempre** para dispositivos»;
* el tipo de cliente que hay que crear es «TVs and Limited Input devices».

Una diferencia con la documentación que se resuelve por exceso
--------------------------------------------------------------

La tabla de errores de sondeo de Google contiene ocho códigos:
``authorization_pending``, ``slow_down``, ``access_denied``,
``admin_policy_enforced``, ``invalid_client``, ``invalid_grant``,
``unsupported_grant_type`` y ``org_internal``. **No contiene
``expired_token``**: en esa tabla, un ``device_code`` vencido aparece como
``invalid_grant`` («el parámetro device code es inválido o ya fue reclamado»).

``expired_token`` sí está en el RFC 8628, que es el estándar del que procede
este flujo. Como no se puede saber desde fuera cuál de los dos emite Google en
cada caso, se tratan los dos: si Google nunca lo devuelve, esa rama queda
inerte; si lo devuelve, queda cubierta. Cubrir de más no puede equivocarse en
ninguna de las dos direcciones.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, sleep

from app.adapters.youtube.auth import (
    CODIGOS_TRANSITORIOS,
    ENDPOINT_TOKEN,
    ComprobacionAuth,
    TokenAcceso,
    _leer_codigo_de_error,
    comprobar_autenticacion,
)
from app.adapters.youtube.oauth_bootstrap import (
    CredencialesCliente,
    TokensObtenidos,
    anunciar_en_stderr,
    pista,
)
from app.config.settings import Settings
from app.core.errors import (
    AutorizacionInvalida,
    EntradaInvalida,
    ErrorTransitorio,
    RespuestaInvalida,
    TiempoAgotado,
)
from app.core.logging import log_evento
from app.core.redaction import registrar_secreto

#: Endpoint que entrega el par ``device_code`` / ``user_code``.
ENDPOINT_CODIGO_DISPOSITIVO = "https://oauth2.googleapis.com/device/code"

#: Tipo de concesión del sondeo, tal y como lo documenta Google.
GRANT_TYPE_DISPOSITIVO = "urn:ietf:params:oauth:grant-type:device_code"

#: Espera entre sondeos cuando la respuesta no trae ``interval``. Google siempre
#: lo manda; esto es solo para no quedarse sin valor si algún día falta.
INTERVALO_POR_DEFECTO_S = 5

#: Cuánto se alarga la espera ante un ``slow_down``. Google dice que hay que
#: aplicar una estrategia de retroceso pero no fija el número; cinco segundos es
#: lo que indica el RFC 8628, que es de donde viene el protocolo.
INCREMENTO_SLOW_DOWN_S = 5

#: Plazo que se usa si la respuesta no trae ``expires_in``. Es el valor que la
#: documentación cita como habitual. **Nunca se sondea sin plazo**: sin esto, una
#: respuesta incompleta dejaría el proceso girando para siempre.
EXPIRACION_POR_DEFECTO_S = 1800

#: Sigue esperando: la persona todavía no ha terminado.
ERROR_PENDIENTE = "authorization_pending"

#: Sigue esperando, pero más despacio.
ERROR_MAS_DESPACIO = "slow_down"

#: La persona dijo que no. No se reintenta: repetirlo no cambia una negativa.
ERROR_DENEGADO = "access_denied"

#: El código caducó o ya se usó. Hay que empezar de nuevo, no reintentar.
#:
#: ``invalid_grant`` es el que documenta Google; ``expired_token`` es el del
#: RFC 8628. Ver la nota del docstring del módulo.
ERRORES_DE_CODIGO_AGOTADO = frozenset({"invalid_grant", "expired_token"})

#: Errores del sondeo que significan «esta autorización no va a llegar nunca».
#: Todos permanentes: reintentar no arregla ninguno.
ERRORES_DE_CLIENTE = frozenset(
    {"invalid_client", "unauthorized_client", "admin_policy_enforced", "org_internal"}
)

#: Claves bajo las que Google puede guardar el cliente en el JSON descargado.
#:
#: La forma del archivo del cliente «TVs and Limited Input devices» **no está
#: documentada**, así que el contenedor se acepta con tolerancia y los campos con
#: rigor. Equivocarse de contenedor no autoriza nada de más: lo que autoriza es
#: el ``client_id`` que haya dentro.
CLAVES_CONTENEDOR = ("installed", "web")


# ---------------------------------------------------------------------------
# Credenciales del cliente de dispositivo
# ---------------------------------------------------------------------------


def cargar_cliente_dispositivo(ruta: str | Path) -> CredencialesCliente:
    """Lee ``client_id`` y ``client_secret`` del JSON del cliente de dispositivo.

    Se reutiliza ``CredencialesCliente`` de ``oauth_bootstrap`` en vez de definir
    una gemela: es el mismo par de valores con la misma higiene —el secreto fuera
    del ``repr``—, y tener dos versiones de eso sería tener dos sitios donde
    equivocarse.

    Lo que **no** se reutiliza es ``cargar_cliente``: aquel exige la clave
    ``installed`` y rechaza ``web`` explícitamente porque un cliente web no
    admite la redirección de loopback. Aquí no hay redirección de ninguna clase,
    así que ese rechazo no tendría sentido, y como Google no publica la forma del
    JSON de un cliente de dispositivo, exigir una clave concreta sería rechazar
    archivos válidos por una suposición nuestra.

    Sobre el ``client_secret``: en un cliente de esta familia **no es un secreto
    fuerte**. Viaja dentro del programa y Google lo sabe. Aun así no se registra,
    no se imprime y no se persiste, porque «débil» no es «público» y porque la
    disciplina que se relaja una vez no vuelve.

    Raises:
        EntradaInvalida: el archivo no existe, no es JSON, o no trae lo que hace
            falta. El mensaje nunca incluye el contenido del archivo.
    """
    archivo = Path(ruta).expanduser()
    if not archivo.is_file():
        raise EntradaInvalida(
            f"no existe el archivo de credenciales {str(ruta)!r}; se descarga al "
            f"crear el cliente OAuth de tipo «TVs and Limited Input devices» en "
            f"Google Cloud y se guarda **fuera** del repositorio",
            stage="youtube_auth_device",
        )

    try:
        datos = json.loads(archivo.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        # No se propaga el contenido: podría llevar el secreto en el mensaje.
        raise EntradaInvalida(
            f"el archivo de credenciales {archivo.name!r} no es un JSON legible",
            stage="youtube_auth_device",
        ) from exc

    if not isinstance(datos, dict):
        raise EntradaInvalida(
            f"el archivo {archivo.name!r} no contiene un objeto JSON",
            stage="youtube_auth_device",
        )

    seccion = datos
    for clave in CLAVES_CONTENEDOR:
        if isinstance(datos.get(clave), dict):
            seccion = datos[clave]
            break

    faltantes = [
        c for c in ("client_id", "client_secret") if not str(seccion.get(c) or "").strip()
    ]
    if faltantes:
        presentes = sorted(k for k in datos if isinstance(k, str))
        raise EntradaInvalida(
            f"al archivo {archivo.name!r} le falta {', '.join(faltantes)}; sus "
            f"claves de primer nivel son {presentes}",
            stage="youtube_auth_device",
        )

    return CredencialesCliente(
        client_id=str(seccion["client_id"]).strip(),
        client_secret=str(seccion["client_secret"]).strip(),
        origen=archivo.name,
    )


# ---------------------------------------------------------------------------
# El código de dispositivo
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodigoDeDispositivo:
    """Lo que devuelve Google al pedir un alta por dispositivo.

    Los dos códigos son muy distintos y por eso se tratan distinto:

    * ``device_code`` es la credencial de este proceso. Quien lo tenga junto al
      ``client_secret`` puede reclamar la autorización, así que queda fuera del
      ``repr``, fuera de ``a_dict`` y fuera de cualquier salida.
    * ``user_code`` es **público por diseño**: la persona tiene que leerlo y
      teclearlo en otra pantalla. Ocultarlo rompería el flujo.
    """

    device_code: str = field(repr=False)
    user_code: str = ""
    verification_url: str = ""
    expires_in_s: int = EXPIRACION_POR_DEFECTO_S
    intervalo_s: int = INTERVALO_POR_DEFECTO_S

    def __str__(self) -> str:  # pragma: no cover - trivial
        return repr(self)

    def a_dict(self) -> dict:
        """Representación imprimible. **Sin** ``device_code``, a propósito."""
        return {
            "user_code": self.user_code,
            "verification_url": self.verification_url,
            "expires_in_s": self.expires_in_s,
            "interval_s": self.intervalo_s,
        }


class _ErrorDelSondeo(Exception):
    """Un error con código corto, para que el bucle pueda decidir qué hacer.

    No se usa la clasificación de ``auth._peticion`` aquí, y es deliberado: ese
    transporte traduce el fallo a la taxonomía del proyecto y descarta el código,
    que es justo lo que este bucle necesita para saber si sigue esperando o se
    rinde. Un ``authorization_pending`` es un 428 perfectamente normal, no un
    fallo.
    """

    def __init__(self, codigo: str, http: int) -> None:
        super().__init__(codigo or str(http))
        self.codigo = codigo
        self.http = http


def _post(url: str, campos: dict, *, timeout_s: int, que: str) -> dict:
    """POST de formulario que conserva el código corto del error.

    Los fallos que no son HTTP —red caída, timeout— sí se traducen a la
    taxonomía del proyecto, porque ahí no hay ningún código que interpretar.
    """
    peticion = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(campos).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=timeout_s) as respuesta:
            bruto = respuesta.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise _ErrorDelSondeo(_leer_codigo_de_error(exc), exc.code) from exc
    except socket.timeout as exc:
        raise TiempoAgotado(
            f"Google no respondió a {que} en {timeout_s}s",
            stage="youtube_auth_device",
        ) from exc
    except urllib.error.URLError as exc:
        raise ErrorTransitorio(
            f"no se pudo contactar con Google para {que}",
            stage="youtube_auth_device",
        ) from exc

    try:
        datos = json.loads(bruto)
    except ValueError as exc:
        raise RespuestaInvalida(
            f"la respuesta de {que} no es JSON válido", stage="youtube_auth_device"
        ) from exc
    if not isinstance(datos, dict):
        raise RespuestaInvalida(
            f"la respuesta de {que} no es un objeto JSON", stage="youtube_auth_device"
        )
    return datos


def solicitar_codigo(
    cliente: CredencialesCliente, *, scope: str, timeout_s: int = 30
) -> CodigoDeDispositivo:
    """Pide el par de códigos a Google.

    Solo lleva ``client_id`` y ``scope``: el ``client_secret`` entra después, en
    el sondeo, que es donde la documentación lo pide.

    El ``device_code`` se registra en el redactor nada más recibirlo, antes de
    que exista ninguna oportunidad de que aparezca en una traza.

    Raises:
        AutorizacionInvalida: el cliente no sirve para este flujo. El caso
            típico es haber creado un cliente de escritorio en vez de uno de
            «TVs and Limited Input devices».
        RespuestaInvalida: la respuesta no trae los códigos.
    """
    try:
        datos = _post(
            ENDPOINT_CODIGO_DISPOSITIVO,
            {"client_id": cliente.client_id, "scope": scope},
            timeout_s=timeout_s,
            que="la solicitud del código de dispositivo",
        )
    except _ErrorDelSondeo as exc:
        raise AutorizacionInvalida(
            f"Google rechazó la solicitud del código de dispositivo "
            f"({exc.http}{f', {exc.codigo}' if exc.codigo else ''}). Comprueba que "
            f"el cliente OAuth es de tipo «TVs and Limited Input devices» y que el "
            f"alcance solicitado está entre los que ese flujo admite",
            stage="youtube_auth_device",
        ) from exc

    device_code = str(datos.get("device_code") or "").strip()
    user_code = str(datos.get("user_code") or "").strip()
    if not device_code or not user_code:
        raise RespuestaInvalida(
            "la solicitud del código de dispositivo no devolvió device_code y "
            "user_code utilizables",
            stage="youtube_auth_device",
        )

    registrar_secreto(device_code)

    # Google documenta ``verification_url``; el RFC 8628 la llama
    # ``verification_uri``. Se lee la documentada y se acepta la otra: sin
    # dirección que enseñar, la persona no tiene dónde escribir el código.
    url = str(datos.get("verification_url") or datos.get("verification_uri") or "").strip()
    if not url:
        raise RespuestaInvalida(
            "la solicitud del código de dispositivo no devolvió ninguna dirección "
            "de verificación",
            stage="youtube_auth_device",
        )

    return CodigoDeDispositivo(
        device_code=device_code,
        user_code=user_code,
        verification_url=url,
        expires_in_s=int(datos.get("expires_in") or EXPIRACION_POR_DEFECTO_S),
        intervalo_s=int(datos.get("interval") or INTERVALO_POR_DEFECTO_S),
    )


# ---------------------------------------------------------------------------
# El sondeo
# ---------------------------------------------------------------------------


def esperar_autorizacion(
    cliente: CredencialesCliente,
    codigo: CodigoDeDispositivo,
    *,
    timeout_s: int = 30,
    dormir=None,
    reloj=None,
) -> TokensObtenidos:
    """Sondea hasta que la persona autorice, rechace, o se acabe el plazo.

    El plazo es ``expires_in`` y **no hay forma de desactivarlo**: es el propio
    Google quien dice cuánto vale el código, y seguir sondeando después solo
    gastaría peticiones contra un código muerto.

    ``dormir`` y ``reloj`` se inyectan para que los tests puedan recorrer el
    bucle entero sin esperar de verdad. Se resuelven **dentro** de la función y
    no como valor por defecto del parámetro: un ``def f(x=sleep)`` captura la
    función al definirse, y ese detalle ya escondió un fallo en el alta de
    escritorio.

    Un 5xx o un corte de red **no abortan la espera**: se tratan como un sondeo
    más. La persona está en mitad del consentimiento en otra pantalla, y tirar
    todo el proceso por un bache de treinta segundos sería obligarla a empezar de
    cero. El plazo sigue acotándolo todo, así que esto no puede girar sin fin.

    Raises:
        AutorizacionInvalida: la persona denegó, el código caducó o ya se usó, o
            el cliente no sirve. Ninguno se arregla reintentando.
        TiempoAgotado: se acabó el plazo sin respuesta.
        RespuestaInvalida: Google respondió algo inesperado.
    """
    esperar = dormir or sleep
    ahora = reloj or monotonic

    campos = {
        "client_id": cliente.client_id,
        "client_secret": cliente.client_secret,
        "device_code": codigo.device_code,
        "grant_type": GRANT_TYPE_DISPOSITIVO,
    }
    intervalo = max(1, codigo.intervalo_s)
    limite = ahora() + max(1, codigo.expires_in_s)

    while ahora() < limite:
        try:
            datos = _post(
                ENDPOINT_TOKEN,
                campos,
                timeout_s=timeout_s,
                que="el sondeo de la autorización",
            )
        except _ErrorDelSondeo as exc:
            if exc.codigo == ERROR_PENDIENTE:
                esperar(intervalo)
                continue
            if exc.codigo == ERROR_MAS_DESPACIO or exc.http == 429:
                intervalo += INCREMENTO_SLOW_DOWN_S
                log_evento(
                    "-", "youtube_auth_device", "sondeo_ralentizado",
                    interval_s=intervalo,
                )
                esperar(intervalo)
                continue
            if exc.codigo == ERROR_DENEGADO:
                raise AutorizacionInvalida(
                    "la autorización fue denegada desde la pantalla de "
                    "consentimiento; no se reintenta",
                    stage="youtube_auth_device",
                ) from exc
            if exc.codigo in ERRORES_DE_CODIGO_AGOTADO:
                raise AutorizacionInvalida(
                    f"el código de dispositivo ya no sirve ({exc.codigo}): caducó "
                    f"o ya se había reclamado. Vuelve a ejecutar el alta para "
                    f"obtener uno nuevo",
                    stage="youtube_auth_device",
                ) from exc
            if exc.codigo in ERRORES_DE_CLIENTE:
                raise AutorizacionInvalida(
                    f"el cliente OAuth no puede completar este alta "
                    f"({exc.http}, {exc.codigo}); no se reintenta",
                    stage="youtube_auth_device",
                ) from exc
            if exc.http in CODIGOS_TRANSITORIOS:
                # Un bache de Google no es el final del consentimiento.
                esperar(intervalo)
                continue
            # Cualquier código que no se conozca se trata como definitivo: si
            # Google añade uno nuevo, pararse y decirlo es mejor que sondear en
            # bucle contra algo que no se entiende.
            raise RespuestaInvalida(
                f"Google respondió al sondeo con {exc.http}"
                f"{f' ({exc.codigo})' if exc.codigo else ''}",
                stage="youtube_auth_device",
            ) from exc
        except (ErrorTransitorio, TiempoAgotado):
            # Ídem para la red. El plazo sigue mandando.
            esperar(intervalo)
            continue

        access = str(datos.get("access_token") or "").strip()
        refresh = str(datos.get("refresh_token") or "").strip()
        if not access:
            raise RespuestaInvalida(
                "el sondeo terminó sin devolver ningún access_token utilizable",
                stage="youtube_auth_device",
            )
        if not refresh:
            # La documentación dice que «los refresh tokens se devuelven siempre
            # para dispositivos». Si aun así no viene, se dice: sin él este alta
            # no tiene producto, y seguir como si nada dejaría al pipeline sin
            # credencial justo cuando cree tenerla.
            raise RespuestaInvalida(
                "el sondeo devolvió access_token pero ningún refresh_token, que es "
                "el único producto que justifica este alta. Retira el acceso de la "
                "aplicación en la cuenta de Google y vuelve a ejecutarlo",
                stage="youtube_auth_device",
            )

        registrar_secreto(refresh)
        return TokensObtenidos(
            access_token=access,
            refresh_token=refresh,
            scope=str(datos.get("scope") or ""),
            expires_in_s=int(datos.get("expires_in") or 0),
        )

    raise TiempoAgotado(
        f"nadie completó la autorización en los {codigo.expires_in_s}s que duraba "
        f"el código; vuelve a ejecutar el alta para obtener uno nuevo",
        stage="youtube_auth_device",
    )


# ---------------------------------------------------------------------------
# El alta completa
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultadoDispositivo:
    """Lo que el alta deja: una comprobación y un token que hay que guardar.

    Mismo criterio que el alta de escritorio: el refresh token está aquí porque
    es el producto y hay que enseñárselo a quien ejecuta el alta, pero fuera del
    ``repr`` y sin que nada lo escriba a disco. ``a_dict`` **no lo incluye**.

    El ``device_code`` no está aquí en absoluto: ya cumplió su función y no hay
    ningún motivo para que sobreviva al proceso.
    """

    comprobacion: ComprobacionAuth
    refresh_token: str = field(repr=False, default="")
    scope_concedido: str = ""
    scope_solicitado: str = ""
    client_id: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return repr(self)

    @property
    def pista_refresh(self) -> str:
        return pista(self.refresh_token)

    def a_dict(self) -> dict:
        """Representación imprimible. **Sin** el refresh token, a propósito.

        Lleva ``flow`` porque ahora hay dos altas y su salida es casi idéntica:
        quien lea este documento tiene que poder saber cuál lo produjo, sobre
        todo porque los alcances que pide cada una son distintos.
        """
        return {
            **self.comprobacion.a_dict(),
            "flow": "device",
            "refresh_token_hint": self.pista_refresh,
            "scope_requested": self.scope_solicitado,
            "scope_granted": self.scope_concedido,
        }


def ejecutar_alta_dispositivo(
    settings: Settings,
    ruta_credenciales: str | Path,
    *,
    abrir_navegador=None,
    anunciar=anunciar_en_stderr,
    dormir=None,
    reloj=None,
) -> ResultadoDispositivo:
    """Hace el alta completa y termina con el AUTH CHECK de siempre.

    El alcance es ``YOUTUBE_DEVICE_SCOPE``, que por defecto es el único que este
    flujo admite y que puede subir. **No se toca ``YOUTUBE_SCOPE``**: esa sigue
    siendo la configuración del alta de escritorio y del alcance conceptual
    mínimo de D17.

    ``anunciar`` escribe por **stderr**: lo que va por stdout es solo el
    documento del resultado. El valor por defecto ya es el seguro para que el
    próximo llamante no reintroduzca el problema por olvidarse de pasarlo.

    ``abrir_navegador`` es una cortesía y nunca un requisito: en la máquina donde
    esto está pensado para correr no hay navegador, y que no lo haya no puede ser
    un fallo. Si abrirlo revienta, se sigue: la dirección ya está anunciada.
    """
    cliente = cargar_cliente_dispositivo(ruta_credenciales)
    scope = settings.youtube_device_scope

    codigo = solicitar_codigo(cliente, scope=scope, timeout_s=settings.youtube_timeout_s)
    log_evento(
        "-", "youtube_auth_device", "codigo_obtenido",
        scope=scope, expires_in_s=codigo.expires_in_s, interval_s=codigo.intervalo_s,
    )

    anunciar(
        f"\nAbre esta dirección en tu teléfono o en cualquier navegador:\n"
        f"\n    {codigo.verification_url}\n"
        f"\ny escribe este código cuando te lo pida:\n"
        f"\n    {codigo.user_code}\n"
        f"\nAlcance que se está solicitando:\n"
        f"\n    {scope}\n"
        f"\nEsperando la autorización (el código caduca en "
        f"{codigo.expires_in_s}s)…"
    )

    try:
        (abrir_navegador or webbrowser.open)(codigo.verification_url)
    except Exception:  # noqa: BLE001 - abrir el navegador es opcional, no crítico
        # En una máquina sin navegador esto es lo normal, no un fallo: la
        # dirección ya se anunció y la persona la abre donde quiera.
        pass

    tokens = esperar_autorizacion(
        cliente,
        codigo,
        timeout_s=settings.youtube_timeout_s,
        dormir=dormir,
        reloj=reloj,
    )
    log_evento(
        "-", "youtube_auth_device", "tokens_obtenidos",
        scope=tokens.scope, expires_in_s=tokens.expires_in_s,
    )

    # El AUTH CHECK de G7.1, con el token recién obtenido en vez del del entorno.
    # El canal esperado es opcional aquí porque el alta es justo el momento en
    # que se averigua cuál es, y el alcance se pasa explícito para que el
    # resultado no declare el de D17, que no es el que se pidió.
    comprobacion = comprobar_autenticacion(
        settings,
        token=TokenAcceso(
            access_token=tokens.access_token,
            expires_in_s=tokens.expires_in_s,
            scope=tokens.scope,
        ),
        exigir_canal_esperado=False,
        scope_solicitado=scope,
    )

    return ResultadoDispositivo(
        comprobacion=comprobacion,
        refresh_token=tokens.refresh_token,
        scope_concedido=tokens.scope,
        scope_solicitado=scope,
        client_id=cliente.client_id,
    )


def instrucciones_dispositivo(resultado: ResultadoDispositivo) -> str:
    """Qué tiene que hacer ahora la persona que ejecutó el alta.

    Se nombran las variables y **no** se imprime ningún valor sensible: el
    refresh token se muestra aparte, una sola vez, por quien invoca esto.
    """
    canal = resultado.comprobacion.channel_id or "(no se pudo leer)"
    return (
        "Guarda el refresh token en tu gestor de secretos. No lo escribas en el\n"
        "repositorio, ni en runs/, ni en un archivo que vaya a versionarse.\n"
        "\n"
        f"Este token tiene alcance {resultado.scope_concedido or resultado.scope_solicitado}.\n"
        "Es MÁS AMPLIO que el mínimo de D17: permite gestionar el canal, no solo\n"
        "subir vídeos. Trátalo en consecuencia.\n"
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
