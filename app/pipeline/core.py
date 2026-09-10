"""Pipeline mínimo de Gate 1.

Demuestra la orquestación completa sobre dos etapas:

    crear run -> cargar config -> validar run_id -> preparar entrada
              -> render con MPT -> validar resultado -> actualizar manifest

Todavía no hay transcripción, traducción, adaptación ni TTS: esas etapas
llegan en Gates posteriores y se enchufarán a este mismo mecanismo.
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from uuid import UUID

from app import __version__
from app.config.settings import Settings
from app.core.errors import ErrorPipeline
from app.core.logging import log_evento
from app.core.manifest import EstadoRun, Manifest
from app.core.stage_runner import StageRunner
from app.core.workspace import Workspace
from app.pipeline.stages import ETAPA_PREPARAR, ETAPA_RENDER


def construir_pipeline():
    """Etapas en orden de ejecución."""
    return [ETAPA_PREPARAR, ETAPA_RENDER]


def _versiones(settings: Settings) -> dict:
    """Versiones relevantes para reproducir la corrida. Sin secretos."""
    commit_mpt = None
    try:
        proceso = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=settings.raiz_mpt,
            capture_output=True, text=True, timeout=30,
        )
        commit_mpt = proceso.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        commit_mpt = None
    return {
        "app": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "mpt_commit": commit_mpt,
    }


@dataclass
class ResultadoRun:
    run_id: UUID
    manifest: Manifest
    exito: bool


def ejecutar_run(
    run_id: UUID,
    settings: Settings | None = None,
    *,
    forzar: str | None = None,
    parametros: dict | None = None,
    etapas: list | None = None,
) -> ResultadoRun:
    """Ejecuta el pipeline completo para un ``run_id``.

    Reanudar es ejecutar de nuevo con el mismo ``run_id``: las etapas cuyo
    artefacto siga siendo válido se omiten.

    Args:
        forzar: nombre de una etapa a re-ejecutar aunque su artefacto valide.
        parametros: entradas de la corrida que no provienen de otra etapa.
        etapas: pipeline a ejecutar; por defecto, el de render de Gate 1.
    """
    settings = settings or Settings.desde_entorno()
    workspace = Workspace(settings.raiz_runs, run_id)
    workspace.crear()

    manifest, origen = Manifest.cargar_o_crear(
        workspace.dir, run_id, config=settings.publico(), versions=_versiones(settings)
    )
    manifest.status = EstadoRun.ejecutando
    manifest.guardar(workspace.dir)
    log_evento(run_id, "pipeline", "start", manifest=origen)

    secuencia = etapas if etapas is not None else construir_pipeline()
    runner = StageRunner(workspace, manifest, settings, parametros)
    try:
        for etapa in secuencia:
            runner.ejecutar(etapa, forzar=(forzar == etapa.nombre))
    except ErrorPipeline as exc:
        manifest.status = EstadoRun.fallida
        manifest.guardar(workspace.dir)
        log_evento(
            run_id, "pipeline", "failed",
            category=exc.categoria, failed_stage=exc.stage, retryable=exc.retryable,
        )
        return ResultadoRun(run_id, manifest, False)

    manifest.status = EstadoRun.completada
    manifest.guardar(workspace.dir)
    log_evento(run_id, "pipeline", "success", stages=len(manifest.stages))
    return ResultadoRun(run_id, manifest, True)


def validar_run(
    run_id: UUID, settings: Settings | None = None, *, etapas: list | None = None
) -> tuple[bool, list[str]]:
    """Revalida los artefactos de una corrida contra sus contratos.

    Returns:
        (ok, problemas)
    """
    settings = settings or Settings.desde_entorno()
    workspace = Workspace(settings.raiz_runs, run_id)
    problemas: list[str] = []

    try:
        manifest = Manifest.cargar(workspace.dir)
    except ErrorPipeline as exc:
        return False, [exc.mensaje]

    if manifest.run_id != run_id:
        problemas.append(f"el manifest declara run_id {manifest.run_id}")

    for etapa in (etapas if etapas is not None else construir_pipeline()):
        entrada = manifest.etapa(etapa.nombre)
        if entrada is None:
            problemas.append(f"etapa {etapa.nombre!r} no registrada")
            continue
        if not manifest.etapa_completada(etapa.nombre):
            problemas.append(
                f"etapa {etapa.nombre!r} en estado {entrada.status.value}"
            )
            continue
        try:
            artefacto = workspace.leer_artefacto(etapa.artefacto, etapa.modelo)
        except ErrorPipeline as exc:
            problemas.append(f"{etapa.artefacto}: {exc.mensaje}")
            continue
        if etapa.validar is not None:
            try:
                etapa.validar(artefacto, workspace)
            except ErrorPipeline as exc:
                problemas.append(f"{etapa.artefacto}: {exc.mensaje}")

    return not problemas, problemas
