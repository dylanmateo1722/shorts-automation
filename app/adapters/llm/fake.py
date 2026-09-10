"""Proveedor falso, para tests y para CI.

No hace ninguna llamada de red y no necesita credenciales. Devuelve respuestas
preparadas, de modo que las etapas, las validaciones y la idempotencia se
ejerciten de verdad sin gastar dinero ni depender de un servicio externo.

Cuenta las llamadas: es lo que permite demostrar que un artefacto válido evita
llamar al modelo.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from app.adapters.llm.base import ProveedorLLM
from app.core.errors import ConfiguracionInvalida, RespuestaInvalida


class ProveedorFalso(ProveedorLLM):
    """Devuelve respuestas preparadas, en orden o según el prompt recibido."""

    nombre = "fake"

    def __init__(
        self,
        respuestas: list[dict] | Callable[[str], dict] | None = None,
        *,
        modelo: str = "fake-1",
    ) -> None:
        self.modelo = modelo
        self._respuestas = respuestas
        self.prompts_recibidos: list[str] = []

    @property
    def llamadas(self) -> int:
        """Número de veces que se pidió una generación."""
        return len(self.prompts_recibidos)

    @classmethod
    def desde_archivo(cls, ruta: Path, *, modelo: str = "fake-1") -> "ProveedorFalso":
        """Carga las respuestas de un JSON con una lista de objetos."""
        try:
            datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfiguracionInvalida(
                f"no se pudieron leer las respuestas del proveedor falso: {exc}"
            ) from exc
        if not isinstance(datos, list):
            raise ConfiguracionInvalida(
                "el archivo de respuestas debe contener una lista de objetos"
            )
        return cls(datos, modelo=modelo)

    def generar_json(self, prompt: str, *, max_tokens: int = 4000) -> dict:
        self.prompts_recibidos.append(prompt)

        if callable(self._respuestas):
            return self._respuestas(prompt)

        if not self._respuestas:
            raise ConfiguracionInvalida(
                "el proveedor falso no tiene respuestas preparadas; "
                "configura LLM_FAKE_RESPONSES o pásalas al construirlo"
            )

        indice = min(self.llamadas - 1, len(self._respuestas) - 1)
        respuesta = self._respuestas[indice]
        if not isinstance(respuesta, dict):
            raise RespuestaInvalida(
                f"la respuesta preparada nº{indice} no es un objeto JSON"
            )
        return respuesta
