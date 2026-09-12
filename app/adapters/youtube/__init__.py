"""Adaptadores de YouTube.

Gate 7.1 trae dos módulos y ninguno publica nada:

* ``auth`` comprueba una autorización **que ya existe**: canjea el refresh token
  y lee qué canal quedó autorizado. Es lo que corre en cada comprobación y en CI.
* ``oauth_bootstrap`` **obtiene** esa autorización, una sola vez, a mano, en una
  máquina con navegador. Nunca corre en CI.

**No hay publisher**, y cuando lo haya será un módulo hermano que use esto sin
mezclarse con ello: la autenticación tiene que poder comprobarse sin que exista
la capacidad de subir nada.
"""

from app.adapters.youtube.auth import (  # noqa: F401
    ENDPOINT_AUTORIZACION,
    ENDPOINT_CANALES,
    ENDPOINT_TOKEN,
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
