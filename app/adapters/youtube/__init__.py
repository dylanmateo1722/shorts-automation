"""Adaptadores de YouTube.

Gate 7.1 trae tres módulos y ninguno publica nada:

* ``auth`` comprueba una autorización **que ya existe**: canjea el refresh token
  y lee qué canal quedó autorizado. Es lo que corre en cada comprobación y en CI.
* ``oauth_bootstrap`` **obtiene** esa autorización, una sola vez, a mano, en una
  máquina con navegador y loopback propio. Nunca corre en CI.
* ``device_bootstrap`` la obtiene también, pero **sin navegador ni callback**:
  la máquina enseña un código y la persona lo teclea en otra pantalla. Es el
  camino cuando quien opera solo tiene un teléfono. Nunca corre en CI.

Las dos altas son alternativas, no etapas de lo mismo, y piden alcances
distintos: el flujo de dispositivo admite una lista cerrada de alcances en la que
``youtube.upload`` **no está**. Ver ``device_bootstrap`` para la excepción
aprobada de D17 y su precio.

**No hay publisher**, y cuando lo haya será un módulo hermano que use esto sin
mezclarse con ello: la autenticación tiene que poder comprobarse sin que exista
la capacidad de subir nada.
"""

from app.adapters.youtube.auth import (  # noqa: F401
    ENDPOINT_AUTORIZACION,
    ENDPOINT_CANALES,
    ENDPOINT_TOKEN,
    SCOPE_GESTION,
    SCOPE_SUBIDA,
    ComprobacionAuth,
    CredencialesOAuth,
    IdentidadCanal,
    ResultadoAuth,
    RESULTADOS_REINTENTABLES,
    TokenAcceso,
    cargar_credenciales,
    comprobar_autenticacion,
    identidad_del_canal,
    obtener_access_token,
)
from app.adapters.youtube.oauth_bootstrap import (  # noqa: F401
    HOST_CALLBACK,
    RUTA_CALLBACK,
    TIMEOUT_CALLBACK_S,
    CredencialesCliente,
    ParametrosPKCE,
    ResultadoBootstrap,
    ServidorCallback,
    TokensObtenidos,
    anunciar_en_stderr,
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
from app.adapters.youtube.device_bootstrap import (  # noqa: F401
    ENDPOINT_CODIGO_DISPOSITIVO,
    EXPIRACION_POR_DEFECTO_S,
    GRANT_TYPE_DISPOSITIVO,
    INCREMENTO_SLOW_DOWN_S,
    INTERVALO_POR_DEFECTO_S,
    CodigoDeDispositivo,
    ResultadoDispositivo,
    cargar_cliente_dispositivo,
    ejecutar_alta_dispositivo,
    esperar_autorizacion,
    instrucciones_dispositivo,
    solicitar_codigo,
)

# Gate 7.2: publicación. El publisher es un módulo hermano de ``auth``, no una
# ampliación suyo: la autenticación tiene que poder comprobarse sin que exista
# la capacidad de publicar.
from app.adapters.youtube.upload import (  # noqa: F401
    MULTIPLO_FRAGMENTO,
    TAMANO_FRAGMENTO_BYTES,
    URL_SUBIDA,
    URL_VIDEOS,
    EstadoRemoto,
    RecursoRemoto,
    RespuestaHTTP,
    RespuestaPerdida,
    SesionDesconocida,
    Transporte,
    TransporteUrllib,
    consultar_progreso,
    huella_metadata,
    iniciar_sesion,
    leer_video,
    subir_fragmento,
    url_publica,
)
from app.adapters.youtube.reconciliation import (  # noqa: F401
    MOTIVOS_CON_RIESGO_DE_DUPLICADO,
    reconciliar,
)
from app.adapters.youtube.publisher import (  # noqa: F401
    MAX_INTENTOS,
    MAX_SESIONES,
    ResultadoPublicacion,
    Verificacion,
    publicar,
    verificar,
)
