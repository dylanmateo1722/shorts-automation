"""Adaptadores de YouTube.

Gate 7.1 solo trae ``auth``: cargar credenciales, canjear el refresh token y
comprobar qué canal quedó autorizado. **No hay publisher**, y cuando lo haya
será un módulo hermano que use esto sin mezclarse con ello: la autenticación
tiene que poder comprobarse sin que exista la capacidad de subir nada.
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
