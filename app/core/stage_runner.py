"""Ejecución de etapas con idempotencia y registro en el manifest.

Centraliza lo que si no acabaría duplicado en cada etapa: medir tiempos,
decidir si hay que volver a ejecutar, escribir el artefacto y actualizar el
manifest.

La regla de idempotencia es deliberadamente simple, sin locks ni coordinación
distribuida:

    ¿existe el artefacto y valida?  ->  omitir
    ¿falta, o está corrupto?        ->  ejecutar
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.contracts.models import Artefacto
from app.core.errors import ArtefactoCorrupto, ErrorPipeline
from app.core.logging import log_evento
from app.core.manifest import EstadoEtapa, Manifest
from app.core.workspace import Workspace


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _procedencia(artefacto: Artefacto) -> dict:
    """Extrae proveedor, modelo y versión de prompt de un artefacto de IA.

    Se hace aquí y no en cada etapa para que ninguna pueda olvidarlo: sin esa
    información una salida de modelo no es reproducible ni auditable.
    """
    procedencia = getattr(artefacto, "provenance", None)
    if procedencia is None:
        return {}
    return {
        "provider": procedencia.provider,
        "model": procedencia.model,
        "prompt_version": procedencia.prompt_version,
    }


@dataclass(frozen=True)
class Etapa:
    """Definición declarativa de una etapa del pipeline.

    Attributes:
        nombre: identificador estable, usado en el manifest y en los logs.
        artefacto: nombre lógico del artefacto que produce.
        modelo: contrato con el que se valida al releerlo.
        ejecutar: función que produce el artefacto.
        validar: comprobación adicional opcional sobre un artefacto ya
            existente; sirve para detectar que el JSON es válido pero el
            archivo al que apunta desapareció.
        vigente: comprobación opcional de que un artefacto válido sigue
            correspondiendo a la **configuración actual**. Es distinta de
            ``validar``: un audio puede estar perfectamente bien y aun así
            haber dejado de servir porque cambió la voz o el guion. Recibe el
            contexto entero porque esa decisión necesita la configuración de la
            corrida, no solo el directorio.
    """

    nombre: str
    artefacto: str
    modelo: type[Artefacto]
    ejecutar: Callable[["ContextoEtapa"], Artefacto]
    validar: Callable[[Artefacto, Workspace], None] | None = None
    vigente: Callable[[Artefacto, "ContextoEtapa"], None] | None = None


@dataclass
class ContextoEtapa:
    """Lo que una etapa recibe para hacer su trabajo."""

    workspace: Workspace
    manifest: Manifest
    settings: object
    previos: dict[str, Artefacto]
    # Entradas de la corrida que no son artefactos de una etapa previa: la
    # ruta de un transcript de partida, la duración objetivo, etc.
    parametros: dict = field(default_factory=dict)
    # Objetos caros de construir que deben vivir una sola vez por corrida,
    # como un cliente de proveedor. Se comparte por referencia entre etapas:
    # construir uno por etapa reinicia su estado y multiplica conexiones.
    recursos: dict = field(default_factory=dict)


@dataclass
class ResultadoEtapa:
    nombre: str
    omitida: bool
    artefacto: Artefacto
    duracion_s: float


class StageRunner:
    """Ejecuta etapas aplicando idempotencia y dejando rastro en el manifest."""

    def __init__(
        self,
        workspace: Workspace,
        manifest: Manifest,
        settings: object,
        parametros: dict | None = None,
    ) -> None:
        self.workspace = workspace
        self.manifest = manifest
        self.settings = settings
        self.parametros = parametros or {}
        self.producidos: dict[str, Artefacto] = {}
        self.recursos: dict = {}

    # --- contexto ----------------------------------------------------------

    def _contexto(self) -> ContextoEtapa:
        return ContextoEtapa(
            workspace=self.workspace,
            manifest=self.manifest,
            settings=self.settings,
            previos=dict(self.producidos),
            parametros=dict(self.parametros),
            recursos=self.recursos,
        )

    # --- idempotencia ------------------------------------------------------

    def _artefacto_reutilizable(self, etapa: Etapa) -> Artefacto | None:
        """Devuelve el artefacto existente si es válido, o None si hay que rehacer."""
        if not self.workspace.existe(etapa.artefacto):
            return None
        try:
            artefacto = self.workspace.leer_artefacto(etapa.artefacto, etapa.modelo)
        except ArtefactoCorrupto as exc:
            log_evento(
                self.workspace.run_id, etapa.nombre, "artifact_invalid",
                reason=type(exc).__name__,
            )
            return None
        for comprobacion in (
            (lambda: etapa.validar(artefacto, self.workspace)) if etapa.validar else None,
            (lambda: etapa.vigente(artefacto, self._contexto())) if etapa.vigente else None,
        ):
            if comprobacion is None:
                continue
            try:
                comprobacion()
            except ErrorPipeline as exc:
                log_evento(
                    self.workspace.run_id, etapa.nombre, "artifact_invalid",
                    reason=type(exc).__name__, detail=exc.mensaje,
                )
                return None
        return artefacto

    # --- ejecución ---------------------------------------------------------

    def ejecutar(self, etapa: Etapa, *, forzar: bool = False) -> ResultadoEtapa:
        """Ejecuta una etapa, u omite si su artefacto ya es válido."""
        entrada = self.manifest.registrar_etapa(etapa.nombre)
        run_id = self.workspace.run_id

        if not forzar:
            existente = self._artefacto_reutilizable(etapa)
            if existente is not None:
                entrada.status = EstadoEtapa.omitida
                entrada.finished_at = _ahora()
                entrada.error = None
                # Se fusiona, no se reemplaza: al omitir, la etapa no corre y
                # lo que registró la vez anterior es lo único que hay.
                entrada.metadata = {**entrada.metadata, **_procedencia(existente)}
                if etapa.artefacto not in entrada.artifacts:
                    entrada.artifacts.append(etapa.artefacto)
                self._registrar_artefacto(etapa.artefacto)
                self.producidos[etapa.artefacto] = existente
                log_evento(run_id, etapa.nombre, "skip", reason="artifact_valid")
                self.manifest.guardar(self.workspace.dir)
                return ResultadoEtapa(etapa.nombre, True, existente, 0.0)

        entrada.status = EstadoEtapa.ejecutando
        entrada.started_at = _ahora()
        entrada.error = None
        self.manifest.guardar(self.workspace.dir)
        log_evento(run_id, etapa.nombre, "start")

        inicio = time.monotonic()
        try:
            artefacto = etapa.ejecutar(self._contexto())
        except ErrorPipeline as exc:
            duracion = time.monotonic() - inicio
            entrada.status = EstadoEtapa.fallida
            entrada.finished_at = _ahora()
            entrada.duration_s = round(duracion, 3)
            entrada.error = exc.a_dict()
            self.manifest.registrar_error(exc.a_dict())
            self.manifest.guardar(self.workspace.dir)
            log_evento(
                run_id, etapa.nombre, "failed",
                duration_s=duracion, category=exc.categoria, retryable=exc.retryable,
            )
            raise

        duracion = time.monotonic() - inicio
        self.workspace.escribir_artefacto(etapa.artefacto, artefacto)
        entrada.status = EstadoEtapa.completada
        entrada.finished_at = _ahora()
        entrada.duration_s = round(duracion, 3)
        # Fusión, no reemplazo: una etapa puede haber anotado en el manifest
        # datos que no salen de la procedencia del artefacto (la voz usada, la
        # duración medida). Sobrescribirlos aquí los perdería.
        entrada.metadata = {**entrada.metadata, **_procedencia(artefacto)}
        if etapa.artefacto not in entrada.artifacts:
            entrada.artifacts.append(etapa.artefacto)
        self._registrar_artefacto(etapa.artefacto)
        self.producidos[etapa.artefacto] = artefacto
        self.manifest.guardar(self.workspace.dir)
        log_evento(run_id, etapa.nombre, "success", duration_s=duracion)
        return ResultadoEtapa(etapa.nombre, False, artefacto, duracion)

    def _registrar_artefacto(self, nombre: str) -> None:
        ruta: Path = self.workspace.ruta_artefacto(nombre)
        if ruta.is_file():
            self.manifest.registrar_artefacto(
                nombre, self.workspace.relativa(ruta), ruta.stat().st_size
            )
