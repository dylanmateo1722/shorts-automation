"""Proveedores de síntesis de voz.

``construir_proveedor_tts`` es el único punto donde el proyecto decide qué
proveedor usar, a partir de la configuración. Ninguna etapa importa una
implementación concreta.

Sobre el fallback de la decisión D3
-----------------------------------

La interfaz admite proveedores alternativos —un ``ProveedorElevenLabs`` sería
otra clase y nada más—, pero en Gate 3 **no existe ninguno**:

    fallback provider = NOT IMPLEMENTED

Pedir ``TTS_PROVIDER=elevenlabs`` falla con un mensaje que lo dice. No se
registra como disponible algo que no está escrito, y no hay conmutación
automática a un proveedor inexistente.
"""

from __future__ import annotations

from app.adapters.tts.base import (
    RITMO_NEUTRO,
    TONO_NEUTRO,
    ConfiguracionVoz,
    LimiteTemporal,
    ProveedorTTS,
    ResultadoSintesis,
)
from app.adapters.tts.edge import VOZ_POR_DEFECTO, ProveedorEdgeTTS
from app.adapters.tts.fake import ProveedorTTSFalso
from app.core.errors import ConfiguracionInvalida

__all__ = [
    "RITMO_NEUTRO",
    "TONO_NEUTRO",
    "VOZ_POR_DEFECTO",
    "ConfiguracionVoz",
    "LimiteTemporal",
    "ProveedorEdgeTTS",
    "ProveedorTTS",
    "ProveedorTTSFalso",
    "ResultadoSintesis",
    "construir_proveedor_tts",
    "configuracion_voz",
]

#: Proveedores implementados. Cualquier otro nombre es un error explícito.
IMPLEMENTADOS = ("edge", "fake")

#: Proveedores previstos por la arquitectura pero sin implementación. Están
#: aquí para que pedirlos dé un mensaje claro en vez de "desconocido".
NO_IMPLEMENTADOS = ("elevenlabs",)


def construir_proveedor_tts(settings) -> ProveedorTTS:
    """Instancia el proveedor de TTS indicado por la configuración.

    Raises:
        ConfiguracionInvalida: si el nombre no se reconoce o corresponde a un
            proveedor previsto pero no implementado.
    """
    nombre = (settings.tts_provider or "").strip().lower()

    if nombre == "edge":
        return ProveedorEdgeTTS(timeout_s=settings.tts_timeout_s)
    if nombre == "fake":
        return ProveedorTTSFalso(palabras_por_minuto=settings.default_wpm)
    if nombre in NO_IMPLEMENTADOS:
        raise ConfiguracionInvalida(
            f"el proveedor de TTS {nombre!r} está previsto por la arquitectura "
            f"pero NO está implementado en este Gate; no hay fallback automático. "
            f"Implementados: {', '.join(IMPLEMENTADOS)}"
        )
    raise ConfiguracionInvalida(
        f"proveedor de TTS desconocido: {settings.tts_provider!r}. "
        f"Implementados: {', '.join(IMPLEMENTADOS)}"
    )


def configuracion_voz(settings) -> ConfiguracionVoz:
    """Traduce la configuración de la corrida a ajustes de voz."""
    return ConfiguracionVoz(
        voz=settings.tts_voice,
        ritmo=settings.tts_rate or RITMO_NEUTRO,
        tono=settings.tts_pitch or TONO_NEUTRO,
    )
