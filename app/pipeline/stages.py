"""Etapas del pipeline mínimo de Gate 1.

Solo dos, y a propósito:

1. ``preparar_entrada`` — produce una entrada controlada y determinista, sin
   red y sin credenciales. Demuestra producción de artefactos e idempotencia.
2. ``render`` — invoca el motor a través del adaptador.

Las etapas lingüísticas (transcripción, traducción, adaptación, TTS,
subtítulos) llegan en Gates posteriores. Aquí solo se prueba la orquestación.
"""

from __future__ import annotations

import subprocess

from app.adapters.media import binario_ffmpeg
from app.adapters.mpt import MPTAdapter
from app.contracts.models import EstadoRender, RenderJob, RenderResult
from app.core.errors import (
    ArtefactoCorrupto,
    DependenciaAusente,
    ErrorPermanente,
    TiempoAgotado,
)
from app.core.stage_runner import ContextoEtapa, Etapa
from app.core.workspace import Workspace

ARTEFACTO_JOB = "render_job"
ARTEFACTO_RESULTADO = "render_result"

# Entrada controlada: corta a propósito, para que la prueba de integración sea
# barata en CI sin dejar de ejercitar el camino completo.
DURACION_AUDIO_S = 8
DURACION_MATERIAL_S = 6

GUION_CONTROLADO = (
    "Entrada controlada de Gate 1. Este texto no se sintetiza ni se subtitula: "
    "solo acompaña al render para ejercitar el adaptador."
)


def _ffmpeg(argv: list[str], *, timeout_s: int = 600) -> None:
    try:
        proceso = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError as exc:
        raise DependenciaAusente(f"no se pudo ejecutar FFmpeg: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TiempoAgotado("FFmpeg no terminó a tiempo generando la entrada") from exc
    if proceso.returncode != 0:
        raise DependenciaAusente(
            f"FFmpeg falló generando la entrada controlada: "
            f"{(proceso.stderr or '').strip()[-300:]}"
        )


def _preparar_entrada(ctx: ContextoEtapa) -> RenderJob:
    """Genera audio y material locales y devuelve el trabajo de render."""
    ws: Workspace = ctx.workspace
    entrada = ws.dir / "input"
    entrada.mkdir(parents=True, exist_ok=True)
    ffmpeg = binario_ffmpeg()

    audio = entrada / "audio.mp3"
    _ffmpeg([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={DURACION_AUDIO_S}",
        "-c:a", "libmp3lame", str(audio),
    ])

    material = entrada / "material.mp4"
    _ffmpeg([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"testsrc2=size=1280x720:rate=30:duration={DURACION_MATERIAL_S}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(material),
    ])

    return RenderJob(
        run_id=ws.run_id,
        task_id=ws.run_id,
        script=GUION_CONTROLADO,
        audio_path=ws.relativa(audio),
        materials=[ws.relativa(material)],
    )


def _validar_job(artefacto: RenderJob, ws: Workspace) -> None:
    """Un job cuyo audio o material desapareció debe rehacerse."""
    if not ws.ruta(artefacto.audio_path).is_file():
        raise ArtefactoCorrupto(f"falta el audio {artefacto.audio_path!r}")
    for material in artefacto.materials:
        if not ws.ruta(material).is_file():
            raise ArtefactoCorrupto(f"falta el material {material!r}")


def _render(ctx: ContextoEtapa) -> RenderResult:
    """Ejecuta el motor a través del adaptador, nunca directamente."""
    ws: Workspace = ctx.workspace
    job = ctx.previos.get(ARTEFACTO_JOB)
    if job is None:
        job = ws.leer_artefacto(ARTEFACTO_JOB, RenderJob)

    adaptador = MPTAdapter(ctx.settings, ws)
    resultado = adaptador.render(job)

    if resultado.status is EstadoRender.fallo:
        # El motor corrió y falló. Es un fallo de la etapa, no del adaptador:
        # el adaptador cumplió su contrato devolviendo un RenderResult.
        raise ErrorPermanente(
            f"el motor terminó con código {resultado.exit_code}: "
            f"{resultado.error or 'sin detalle'}",
            stage="render",
        )
    return resultado


def _validar_resultado(artefacto: RenderResult, ws: Workspace) -> None:
    """Un resultado cuyo MP4 desapareció ya no es reutilizable."""
    if artefacto.status is not EstadoRender.exito:
        raise ArtefactoCorrupto("el resultado registrado no fue exitoso")
    if not artefacto.output_path:
        raise ArtefactoCorrupto("el resultado no indica archivo de salida")
    salida = ws.ruta(artefacto.output_path)
    if not salida.is_file() or salida.stat().st_size == 0:
        raise ArtefactoCorrupto(f"falta el video de salida {artefacto.output_path!r}")


ETAPA_PREPARAR = Etapa(
    nombre="prepare_input",
    artefacto=ARTEFACTO_JOB,
    modelo=RenderJob,
    ejecutar=_preparar_entrada,
    validar=_validar_job,
)

ETAPA_RENDER = Etapa(
    nombre="render",
    artefacto=ARTEFACTO_RESULTADO,
    modelo=RenderResult,
    ejecutar=_render,
    validar=_validar_resultado,
)
