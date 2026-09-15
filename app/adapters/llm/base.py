"""Interfaz del proveedor de modelo de lenguaje.

El pipeline no conoce ningún SDK ni ningún proveedor concreto: solo esta
interfaz. Cambiar de proveedor es escribir otra implementación, no tocar las
etapas.

La interfaz es deliberadamente estrecha —una llamada que devuelve JSON— para
que cada proveedor nuevo no tenga que reimplementar carga de prompts, parseo
ni construcción de contratos. Esa lógica vive una sola vez, en la capa
lingüística.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

from app.core.errors import RespuestaInvalida

# Algunos modelos envuelven el JSON en un bloque de código pese a pedirles que
# no lo hagan. Quitarlo es determinista y barato; no quitarlo obliga a
# descartar una respuesta por lo demás correcta.
_VALLA = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class ProveedorLLM(ABC):
    """Un proveedor capaz de devolver un objeto JSON a partir de un prompt."""

    #: Nombre corto del proveedor, tal como se registra en los artefactos.
    nombre: str = "desconocido"
    #: Identificador del modelo concreto.
    modelo: str = "desconocido"

    @abstractmethod
    def generar_json(self, prompt: str, *, max_tokens: int = 4000) -> dict:
        """Envía el prompt y devuelve la respuesta ya interpretada como JSON.

        Raises:
            ErrorTransitorio: fallos de transporte (timeout, 429, 5xx).
            RespuestaInvalida: la respuesta no es un objeto JSON utilizable.
        """

    @staticmethod
    def interpretar_json(texto: str) -> dict:
        """Convierte la respuesta del modelo en un diccionario.

        Raises:
            RespuestaInvalida: si no hay JSON, o si no es un objeto.
        """
        if not texto or not texto.strip():
            raise RespuestaInvalida("el proveedor devolvió una respuesta vacía")
        limpio = texto.strip()
        if valla := _VALLA.match(limpio):
            limpio = valla.group(1)
        try:
            datos = json.loads(limpio)
        except ValueError as exc:
            raise RespuestaInvalida(
                f"la respuesta del proveedor no es JSON válido: {exc}"
            ) from exc
        if not isinstance(datos, dict):
            raise RespuestaInvalida(
                f"se esperaba un objeto JSON y llegó {type(datos).__name__}"
            )
        return datos
