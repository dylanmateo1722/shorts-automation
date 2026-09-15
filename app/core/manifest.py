"""Manifest de la ejecución.

Es el registro que permite saber en qué estado quedó una corrida y reanudarla
sin repetir lo ya hecho.

Regla de seguridad: el manifest **nunca** guarda credenciales. La
configuración que contiene proviene de una lista explícita de campos no
sensibles, no de un volcado del entorno.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.errors import ArtefactoCorrupto

SCHEMA_VERSION_MANIFEST = "1"
NOMBRE_MANIFEST = "manifest.json"


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


class EstadoEtapa(str, Enum):
    pendiente = "pending"
    ejecutando = "running"
    completada = "completed"
    omitida = "skipped"
    fallida = "failed"


class EstadoRun(str, Enum):
    creada = "created"
    ejecutando = "running"
    completada = "completed"
    fallida = "failed"


class EntradaEtapa(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: EstadoEtapa = EstadoEtapa.pendiente
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    artifacts: list[str] = Field(default_factory=list)
    # Datos no sensibles de la etapa: proveedor, modelo y versión de prompt
    # cuando el artefacto lo declara. Nunca credenciales.
    metadata: dict = Field(default_factory=dict)
    error: dict | None = None


class ArtefactoRegistrado(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    bytes: int


class Manifest(BaseModel):
    """Estado completo y persistente de una ejecución."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION_MANIFEST
    run_id: UUID
    status: EstadoRun = EstadoRun.creada
    created_at: datetime = Field(default_factory=_ahora)
    updated_at: datetime = Field(default_factory=_ahora)
    # Solo configuración no sensible; ver Settings.publico().
    config: dict = Field(default_factory=dict)
    versions: dict = Field(default_factory=dict)
    stages: list[EntradaEtapa] = Field(default_factory=list)
    artifacts: dict[str, ArtefactoRegistrado] = Field(default_factory=dict)
    errors: list[dict] = Field(default_factory=list)

    # --- consultas ---------------------------------------------------------

    def etapa(self, nombre: str) -> EntradaEtapa | None:
        for entrada in self.stages:
            if entrada.name == nombre:
                return entrada
        return None

    def etapa_completada(self, nombre: str) -> bool:
        entrada = self.etapa(nombre)
        return entrada is not None and entrada.status in (
            EstadoEtapa.completada,
            EstadoEtapa.omitida,
        )

    # --- mutaciones --------------------------------------------------------

    def registrar_etapa(self, nombre: str) -> EntradaEtapa:
        entrada = self.etapa(nombre)
        if entrada is None:
            entrada = EntradaEtapa(name=nombre)
            self.stages.append(entrada)
        return entrada

    def registrar_artefacto(self, nombre: str, ruta_relativa: str, tamano: int) -> None:
        self.artifacts[nombre] = ArtefactoRegistrado(path=ruta_relativa, bytes=tamano)

    def registrar_error(self, error: dict) -> None:
        self.errors.append({**error, "at": _ahora().isoformat()})

    # --- persistencia ------------------------------------------------------

    def guardar(self, directorio: Path) -> Path:
        """Escribe el manifest de forma atómica."""
        self.updated_at = _ahora()
        directorio.mkdir(parents=True, exist_ok=True)
        destino = directorio / NOMBRE_MANIFEST
        temporal = destino.with_suffix(".json.tmp")
        temporal.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        temporal.replace(destino)
        return destino

    @classmethod
    def cargar(cls, directorio: Path) -> "Manifest":
        ruta = Path(directorio) / NOMBRE_MANIFEST
        if not ruta.is_file():
            raise ArtefactoCorrupto(f"no existe {NOMBRE_MANIFEST} en {directorio}")
        try:
            return cls.model_validate_json(ruta.read_text(encoding="utf-8"))
        except ValidationError as exc:
            raise ArtefactoCorrupto(f"manifest inválido: {exc}") from exc

    @classmethod
    def cargar_o_crear(
        cls, directorio: Path, run_id: UUID, *, config: dict, versions: dict
    ) -> tuple["Manifest", Literal["cargado", "creado"]]:
        try:
            manifest = cls.cargar(directorio)
        except ArtefactoCorrupto:
            return cls(run_id=run_id, config=config, versions=versions), "creado"
        if manifest.run_id != run_id:
            raise ArtefactoCorrupto(
                f"el manifest de {directorio} pertenece a la ejecución "
                f"{manifest.run_id}, no a {run_id}"
            )
        return manifest, "cargado"
