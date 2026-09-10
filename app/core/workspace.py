"""Directorio de trabajo de una ejecución.

Toda ruta que se guarde en un artefacto es **relativa a este directorio**.
Es lo que permite que una corrida hecha en local siga siendo interpretable en
un runner de CI, donde el prefijo absoluto es distinto.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar
from uuid import UUID

from pydantic import ValidationError

from app.contracts.models import Artefacto
from app.core.errors import ArtefactoCorrupto, PermisoDenegado

T = TypeVar("T", bound=Artefacto)


class Workspace:
    """Acceso al directorio ``runs/<run_id>/`` de una ejecución."""

    def __init__(self, raiz_runs: Path, run_id: UUID) -> None:
        self.run_id = run_id
        self.raiz_runs = Path(raiz_runs)
        self.dir = self.raiz_runs / str(run_id)

    @classmethod
    def en_directorio(cls, directorio: Path, run_id: UUID) -> "Workspace":
        """Crea un Workspace sobre un directorio ya conocido.

        Útil para quien decide la ruta por su cuenta en vez de derivarla de
        ``runs/<run_id>``; el resto del comportamiento es idéntico.
        """
        instancia = cls(Path(directorio).parent, run_id)
        instancia.dir = Path(directorio)
        return instancia

    def crear(self) -> Path:
        """Crea el directorio de la ejecución si no existe."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PermisoDenegado(
                f"no se pudo crear el directorio de la ejecución: {exc}"
            ) from exc
        return self.dir

    # --- rutas -------------------------------------------------------------

    def ruta(self, relativa: str) -> Path:
        """Convierte una ruta relativa del artefacto en una ruta absoluta."""
        return self.dir / relativa

    def relativa(self, absoluta: Path | str) -> str:
        """Convierte una ruta absoluta en relativa al directorio de la corrida.

        Una ruta de fuera del directorio se guarda tal cual: es preferible un
        path explícitamente externo a uno con ``../..`` que nadie sabe resolver.
        """
        camino = Path(absoluta)
        try:
            return str(camino.resolve().relative_to(self.dir.resolve()))
        except ValueError:
            return str(camino)

    # --- artefactos --------------------------------------------------------

    def ruta_artefacto(self, nombre: str) -> Path:
        return self.dir / f"{nombre}.json"

    def existe(self, nombre: str) -> bool:
        ruta = self.ruta_artefacto(nombre)
        return ruta.is_file() and ruta.stat().st_size > 0

    def escribir_artefacto(self, nombre: str, artefacto: Artefacto) -> Path:
        """Persiste un artefacto como JSON de forma atómica."""
        destino = self.ruta_artefacto(nombre)
        destino.parent.mkdir(parents=True, exist_ok=True)
        temporal = destino.with_suffix(".json.tmp")
        temporal.write_text(
            artefacto.model_dump_json(indent=2), encoding="utf-8"
        )
        # El reemplazo atómico evita dejar medio artefacto si el proceso muere.
        temporal.replace(destino)
        return destino

    def leer_artefacto(self, nombre: str, modelo: type[T]) -> T:
        """Lee y valida un artefacto.

        Raises:
            ArtefactoCorrupto: si falta, no es JSON o no cumple el contrato.
        """
        ruta = self.ruta_artefacto(nombre)
        if not ruta.is_file():
            raise ArtefactoCorrupto(f"no existe el artefacto {nombre!r}")
        try:
            return modelo.model_validate_json(ruta.read_text(encoding="utf-8"))
        except (ValidationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ArtefactoCorrupto(
                f"el artefacto {nombre!r} no cumple su contrato: {exc}"
            ) from exc
