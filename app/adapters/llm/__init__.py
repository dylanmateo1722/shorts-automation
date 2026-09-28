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

#: Base URL conocida de cada proveedor. Solo se usa cuando no se indica
#: ``LLM_BASE_URL``: quien la indique manda, porque un proveedor puede servirse
#: desde otro sitio y adivinárselo sería peor que dejarle decirlo.
BASE_URL_POR_PROVEEDOR = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "moonshot": "https://api.moonshot.cn/v1",
}

#: Proveedores donde pedir ``response_format: json_object`` es arriesgado por
#: defecto.
#:
#: OpenRouter documenta que, si el modelo elegido no soporta salidas
#: estructuradas, **la petición falla** en vez de ignorarse el parámetro. Sus
#: modelos gratuitos a menudo no las soportan, así que activarlo por defecto
#: convertiría la primera llamada en un error de los que no dicen nada útil.
#:
#: No perdemos nada: los tres prompts ya piden JSON explícitamente y
#: ``interpretar_json`` sabe desenvolverlo aunque el modelo lo adorne. Quien use
#: un modelo que sí lo soporta puede activarlo con ``LLM_JSON_MODE=1``.
SIN_JSON_MODE_POR_DEFECTO = frozenset({"openrouter"})


def _modo_json(nombre: str) -> bool:
    """Si se pide ``response_format: json_object`` a este proveedor."""
    bruto = os.environ.get("LLM_JSON_MODE", "").strip()
    if bruto:
        return bruto != "0"
    return nombre not in SIN_JSON_MODE_POR_DEFECTO


def _atribucion_openrouter(nombre: str) -> dict[str, str]:
    """Las cabeceras opcionales de atribución de OpenRouter.

    Son opcionales y solo sirven para aparecer en sus tablas públicas, así que
    no se envían salvo que alguien las configure: mandar por defecto la URL de
    un proyecto ajeno sería atribuirle un uso que no hizo.
    """
    if nombre != "openrouter":
        return {}
    cabeceras: dict[str, str] = {}
    if sitio := os.environ.get("OPENROUTER_SITE_URL", "").strip():
        cabeceras["HTTP-Referer"] = sitio
    if titulo := os.environ.get("OPENROUTER_APP_NAME", "").strip():
        cabeceras["X-Title"] = titulo
    return cabeceras


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
            base_url=settings.llm_base_url or BASE_URL_POR_PROVEEDOR.get(nombre, ""),
            api_key=settings.llm_api_key,
            modelo=settings.llm_model,
            nombre=nombre,
            timeout_s=settings.llm_timeout_s,
            max_tokens=settings.llm_max_tokens,
            modo_json=_modo_json(nombre),
            cabeceras_extra=_atribucion_openrouter(nombre),
        )

    raise ConfiguracionInvalida(
        f"proveedor LLM desconocido: {settings.llm_provider!r}. "
        f"Admitidos: 'fake' o uno compatible con OpenAI "
        f"({', '.join(sorted(COMPATIBLES_OPENAI))})"
    )
