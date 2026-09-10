"""Proveedores de modelo de lenguaje.

``construir_proveedor`` es el único punto donde el proyecto decide qué
proveedor usar, a partir de la configuración. Ninguna etapa importa una
implementación concreta.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.adapters.llm.base import ProveedorLLM
from app.adapters.llm.fake import ProveedorFalso
from app.adapters.llm.openai_compatible import ProveedorOpenAICompatible
from app.core.errors import ConfiguracionInvalida

__all__ = [
    "ProveedorLLM",
    "ProveedorFalso",
    "ProveedorOpenAICompatible",
    "construir_proveedor",
]

# Proveedores conocidos que hablan Chat Completions. La lista existe para dar
# un error claro ante un nombre mal escrito, no para restringir: cualquier
# proveedor compatible funciona indicando su base_url.
COMPATIBLES_OPENAI = frozenset(
    {"openai", "moonshot", "deepseek", "groq", "openrouter", "openai_compatible"}
)


def construir_proveedor(settings) -> ProveedorLLM:
    """Instancia el proveedor indicado por la configuración.

    Raises:
        ConfiguracionInvalida: si falta configuración o el nombre no se
            reconoce, nombrando los valores admitidos.
    """
    settings.verificar_llm()
    nombre = settings.llm_provider.lower()

    if nombre == "fake":
        ruta = os.environ.get("LLM_FAKE_RESPONSES", "").strip()
        if ruta:
            return ProveedorFalso.desde_archivo(
                Path(ruta), modelo=settings.llm_model or "fake-1"
            )
        return ProveedorFalso(modelo=settings.llm_model or "fake-1")

    if nombre in COMPATIBLES_OPENAI:
        return ProveedorOpenAICompatible(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            modelo=settings.llm_model,
            nombre=nombre,
            timeout_s=settings.llm_timeout_s,
            modo_json=os.environ.get("LLM_JSON_MODE", "1") != "0",
        )

    raise ConfiguracionInvalida(
        f"proveedor LLM desconocido: {settings.llm_provider!r}. "
        f"Admitidos: 'fake' o uno compatible con OpenAI "
        f"({', '.join(sorted(COMPATIBLES_OPENAI))})"
    )
