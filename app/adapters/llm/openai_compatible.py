"""Proveedor sobre la API de Chat Completions compatible con OpenAI.

Una sola implementación cubre a la mayoría de proveedores —OpenAI, Moonshot,
DeepSeek, Groq, OpenRouter y otros— porque todos exponen el mismo contrato
HTTP; lo único que cambia es ``base_url`` y el nombre del modelo. Eso es
exactamente lo que Gate 0 pedía: no casarse con un proveedor antes de la
evaluación ciega.

Se usa ``urllib`` de la biblioteca estándar en vez de un SDK: la llamada es un
POST con JSON, y añadir una dependencia por eso acoplaría el proyecto a la
forma de un proveedor concreto.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

from app.adapters.llm.base import ProveedorLLM
from app.core.errors import (
    ConfiguracionInvalida,
    ErrorTransitorio,
    LimiteDeTasa,
    RespuestaInvalida,
    TiempoAgotado,
)

RUTA_COMPLETIONS = "/chat/completions"

# Códigos que merecen reintento: el servidor no dijo que la petición esté mal,
# dijo que ahora no puede.
CODIGOS_TRANSITORIOS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class ProveedorOpenAICompatible(ProveedorLLM):
    """Cliente de Chat Completions para cualquier proveedor compatible."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        modelo: str,
        nombre: str = "openai_compatible",
        timeout_s: int = 120,
        modo_json: bool = True,
        temperatura: float = 0.3,
    ) -> None:
        if not base_url:
            raise ConfiguracionInvalida("falta LLM_BASE_URL para el proveedor LLM")
        if not modelo:
            raise ConfiguracionInvalida("falta LLM_MODEL para el proveedor LLM")
        if not api_key:
            raise ConfiguracionInvalida(
                "falta LLM_API_KEY en el entorno; la credencial nunca se lee "
                "del código ni del manifest"
            )
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.modelo = modelo
        self.nombre = nombre
        self.timeout_s = timeout_s
        self.modo_json = modo_json
        self.temperatura = temperatura

    def _cuerpo(self, prompt: str, max_tokens: int) -> dict:
        cuerpo = {
            "model": self.modelo,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperatura,
            "max_tokens": max_tokens,
        }
        if self.modo_json:
            # Cuando el proveedor lo soporta, evita que el modelo envuelva la
            # respuesta en prosa. La validación estricta se hace igualmente.
            cuerpo["response_format"] = {"type": "json_object"}
        return cuerpo

    def generar_json(self, prompt: str, *, max_tokens: int = 4000) -> dict:
        peticion = urllib.request.Request(
            f"{self.base_url}{RUTA_COMPLETIONS}",
            data=json.dumps(self._cuerpo(prompt, max_tokens)).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(peticion, timeout=self.timeout_s) as respuesta:
                bruto = respuesta.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # El cuerpo del error puede repetir la credencial: no se propaga.
            if exc.code == 429:
                raise LimiteDeTasa(
                    f"el proveedor {self.nombre} aplicó límite de tasa (429)"
                ) from exc
            if exc.code in CODIGOS_TRANSITORIOS:
                raise ErrorTransitorio(
                    f"el proveedor {self.nombre} respondió {exc.code}"
                ) from exc
            raise RespuestaInvalida(
                f"el proveedor {self.nombre} rechazó la petición con {exc.code}"
            ) from exc
        except socket.timeout as exc:
            raise TiempoAgotado(
                f"el proveedor {self.nombre} no respondió en {self.timeout_s}s"
            ) from exc
        except urllib.error.URLError as exc:
            raise ErrorTransitorio(
                f"no se pudo contactar con el proveedor {self.nombre}"
            ) from exc

        return self.interpretar_json(self._extraer_contenido(bruto))

    @staticmethod
    def _extraer_contenido(bruto: str) -> str:
        """Saca el texto del mensaje de la envoltura de Chat Completions."""
        try:
            datos = json.loads(bruto)
            return datos["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RespuestaInvalida(
                f"la envoltura de la respuesta no tiene la forma esperada: {exc}"
            ) from exc
