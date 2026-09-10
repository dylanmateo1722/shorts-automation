"""QA del MP4 final: se inspecciona el archivo, no el registro de que se hizo.

``render completed = true`` no es evidencia de nada. Aquí se abre el vídeo que
quedó en disco y se leen sus propiedades reales: resolución, códecs, cadencia,
formato de píxel, pista de audio, duración y huella.

Qué puede afirmar la automatización
-----------------------------------

Propiedades técnicas medibles del contenedor y de los flujos, y que el proceso
de composición terminó bien sobre las capas esperadas.

Qué NO puede afirmar
--------------------

Que el subtítulo **se vea**, que el rótulo esté donde debe o que el resultado
sea legible. Un ASS puede componerse sin errores y quedar fuera de cuadro. Por
eso se extraen fotogramas: la comprobación técnica y la inspección visual son
cosas distintas y aquí se mantienen separadas en vez de dejar que una se haga
pasar por la otra.
"""

from __future__ import annotations

from app.adapters.composicion import extraer_frames, hoja_de_contactos
from app.adapters.media import inspeccionar, sha256_archivo
from app.contracts.models import (
    EstadoQA,
    EstadoRender,
    NivelQA,
    OverlaySpec,
    QAResult,
    RenderJob,
    RenderResult,
    SubtitleAsset,
    VoiceAsset,
)
from app.core.errors import ArtefactoCorrupto, ErrorPipeline
from app.core.logging import log_evento
from app.core.stage_runner import ContextoEtapa, Etapa
from app.core.workspace import Workspace
from app.pipeline.qa import Acumulador, _cargar
from app.pipeline.render import ALTO, ANCHO, ARTEFACTO_FINAL, FPS
from app.pipeline.stages import ARTEFACTO_JOB, ARTEFACTO_RESULTADO
from app.pipeline.transformation import ARTEFACTO_OVERLAY, ARTEFACTO_PROCEDENCIA, ARTEFACTO_TRANSFORMACION
from app.pipeline.voice import ARTEFACTO_SUBTITULOS, ARTEFACTO_VOZ

ARTEFACTO_QA_VIDEO = "final_video_qa"

RUTA_FRAMES = "qa/frames"
RUTA_HOJA = "qa/contact_sheet.png"

#: Códecs exigidos. Son los que reproducen sin fricción las plataformas
#: habituales; otro códec puede ser técnicamente correcto y aun así no servir.
CODEC_VIDEO = "h264"
CODEC_AUDIO = "aac"
FORMATOS_PIXEL_ACEPTADOS = ("yuv420p",)

#: Margen entre la duración del audio y la del MP4. El muxing y el cierre del
#: último GOP mueven el final unas décimas; media pantalla de diferencia, no.
TOLERANCIA_DURACION_S = 0.75

#: Cuántos fotogramas se extraen para la inspección visual.
FRAMES_DE_MUESTRA = 5


def _instantes(duracion: float, overlay: OverlaySpec | None) -> list[float]:
    """Elige cuándo mirar: dentro del rótulo y repartido por el resto.

    El primer instante cae dentro de la ventana del overlay a propósito: es el
    único tramo donde se puede ver si el rótulo llegó a componerse.
    """
    instantes: list[float] = []
    if overlay is not None and overlay.end_seconds > overlay.start_seconds:
        instantes.append(
            min(
                (overlay.start_seconds + overlay.end_seconds) / 2,
                max(duracion - 0.1, 0.0),
            )
        )
    restantes = FRAMES_DE_MUESTRA - len(instantes)
    for i in range(restantes):
        instantes.append(duracion * (i + 1) / (restantes + 1))
    return sorted({round(max(t, 0.0), 3) for t in instantes})


def evaluar_video(ws: Workspace) -> tuple[QAResult, list[str]]:
    """Comprueba el MP4 final y devuelve el informe y los fotogramas extraídos."""
    acc = Acumulador()
    frames_relativos: list[str] = []

    final = _cargar(acc, ws, ARTEFACTO_FINAL, RenderResult)
    job = _cargar(acc, ws, ARTEFACTO_JOB, RenderJob)
    motor = _cargar(acc, ws, ARTEFACTO_RESULTADO, RenderResult)
    voz = _cargar(acc, ws, ARTEFACTO_VOZ, VoiceAsset)
    subtitulos = _cargar(acc, ws, ARTEFACTO_SUBTITULOS, SubtitleAsset)
    overlay = _cargar(acc, ws, ARTEFACTO_OVERLAY, OverlaySpec)

    if final is None or not final.output_path:
        acc.error("final: hay vídeo que inspeccionar", False, "no hay artefacto de vídeo final")
        return _informe(ws, acc), frames_relativos

    # --- archivo -----------------------------------------------------------
    ruta = final.output_path
    acc.error(
        "final: la ruta es relativa",
        not ruta.startswith("/") and not ruta.startswith("\\"),
        ruta,
    )
    destino = ws.ruta(ruta)
    existe = destino.is_file()
    acc.error("final: el archivo existe", existe, ruta if existe else f"no existe {ruta!r}")
    if not existe:
        return _informe(ws, acc), frames_relativos

    tamano = destino.stat().st_size
    acc.error("final: tamaño mayor que cero", tamano > 0, f"{tamano} bytes")

    huella = sha256_archivo(destino)
    acc.error("final: SHA-256 calculable", bool(huella), huella)
    acc.error(
        "final: la huella registrada es la del archivo",
        final.sha256 == huella,
        f"registrada {(final.sha256 or '—')[:16]}…, calculada {huella[:16]}…",
    )

    # --- inspección real ---------------------------------------------------
    try:
        info = inspeccionar(destino)
    except ErrorPipeline as exc:
        acc.error("final: el archivo se puede inspeccionar", False, exc.mensaje[:300])
        return _informe(ws, acc), frames_relativos
    acc.error(
        "final: el archivo se puede inspeccionar", True, f"leído con {info.leido_con}"
    )

    # --- vídeo -------------------------------------------------------------
    acc.error(
        "final: resolución vertical 1080×1920",
        (info.ancho, info.alto) == (ANCHO, ALTO),
        f"{info.ancho}×{info.alto}",
    )
    acc.error(
        f"final: códec de vídeo {CODEC_VIDEO}",
        info.codec_video == CODEC_VIDEO,
        info.codec_video or "sin flujo de vídeo",
    )
    acc.error("final: cadencia válida", info.fps > 0, f"{info.fps} fps")
    acc.aviso(
        f"final: cadencia objetivo {FPS} fps",
        abs(info.fps - FPS) < 0.5,
        f"{info.fps} fps (objetivo {FPS})",
    )
    acc.error("final: duración positiva", info.duracion_s > 0, f"{info.duracion_s:.2f}s")
    acc.error(
        "final: formato de píxel compatible",
        info.formato_pixel in FORMATOS_PIXEL_ACEPTADOS,
        info.formato_pixel or "desconocido",
    )

    # --- audio -------------------------------------------------------------
    acc.error(
        "final: tiene pista de audio", info.tiene_audio, info.codec_audio or "sin audio"
    )
    acc.error(
        f"final: códec de audio {CODEC_AUDIO}",
        info.codec_audio == CODEC_AUDIO,
        info.codec_audio or "sin audio",
    )
    acc.error(
        "final: frecuencia de muestreo válida",
        info.audio_sample_rate_hz > 0,
        f"{info.audio_sample_rate_hz} Hz",
    )

    # --- coherencia temporal ----------------------------------------------
    if voz is not None:
        diferencia = info.duracion_s - voz.audio_duration_seconds
        acc.error(
            "sincronía: el vídeo dura lo que la narración",
            abs(diferencia) <= TOLERANCIA_DURACION_S,
            f"vídeo {info.duracion_s:.2f}s, narración {voz.audio_duration_seconds:.2f}s, "
            f"diferencia {diferencia:+.2f}s (tolerancia ±{TOLERANCIA_DURACION_S}s)",
        )
        acc.aviso(
            "sincronía: el vídeo y la narración duran exactamente lo mismo",
            abs(diferencia) < 0.05,
            f"diferencia {diferencia:+.2f}s por muxing y cierre del último GOP",
        )
    if subtitulos is not None:
        fin_subtitulos = (
            subtitulos.cues[-1].end_seconds if subtitulos.cues else 0.0
        )
        acc.error(
            "sincronía: los subtítulos caben en el vídeo",
            fin_subtitulos <= info.duracion_s + TOLERANCIA_DURACION_S,
            f"último cue en {fin_subtitulos:.2f}s, vídeo {info.duracion_s:.2f}s",
        )

    # --- composición -------------------------------------------------------
    if job is not None:
        capas = [r for r in (job.subtitle_path, job.overlay_path) if r]
        acc.error(
            "composición: se compusieron subtítulos y overlay",
            len(capas) == 2,
            f"{len(capas)} capa(s): {capas}",
        )
        if subtitulos is not None:
            acc.error(
                "composición: el ASS compuesto es el del SubtitleAsset",
                job.subtitle_path == subtitulos.ass_path,
                f"{job.subtitle_path!r} frente a {subtitulos.ass_path!r}",
            )
        if overlay is not None:
            acc.error(
                "composición: el overlay compuesto es el de la transformación",
                job.overlay_path == overlay.overlay_path,
                f"{job.overlay_path!r} frente a {overlay.overlay_path!r}",
            )
        for capa in capas:
            acc.error(
                f"composición: existe la capa {capa}",
                ws.ruta(capa).is_file(),
                capa if ws.ruta(capa).is_file() else f"no existe {capa!r}",
            )
    acc.error(
        "composición: FFmpeg terminó correctamente",
        final.exit_code == 0 and final.status is EstadoRender.exito,
        f"código {final.exit_code}, estado {final.status.value}",
    )
    if motor is not None:
        acc.error(
            "composición: el final no es el intermedio del motor",
            final.output_path != motor.output_path,
            f"final {final.output_path!r}, motor {motor.output_path!r}",
        )

    # --- inspección visual -------------------------------------------------
    frames = extraer_frames(destino, _instantes(info.duracion_s, overlay), ws.ruta(RUTA_FRAMES))
    frames_relativos = [ws.relativa(f) for f in frames]
    acc.error(
        "visual: se pudieron extraer fotogramas",
        len(frames) >= 2,
        f"{len(frames)} fotograma(s) en {RUTA_FRAMES}",
    )
    hoja_de_contactos(frames, ws.ruta(RUTA_HOJA))
    # Deliberadamente un aviso, no un error: que existan los fotogramas no
    # demuestra que el subtítulo se vea. Eso lo decide una persona mirándolos.
    acc.aviso(
        "visual: la presencia del subtítulo y del rótulo requiere revisión humana",
        False,
        f"comprobado técnicamente que las capas se compusieron; mirar "
        f"{RUTA_FRAMES} y {RUTA_HOJA} para confirmar que se ven",
    )

    return _informe(ws, acc), frames_relativos


def _informe(ws: Workspace, acc: Acumulador) -> QAResult:
    fallos = [c for c in acc.checks if not c.ok and c.level is NivelQA.error]
    avisos = [c for c in acc.checks if not c.ok and c.level is NivelQA.aviso]
    estado = (
        EstadoQA.reprobado
        if fallos
        else (EstadoQA.aprobado_con_avisos if avisos else EstadoQA.aprobado)
    )
    return QAResult(
        run_id=ws.run_id,
        status=estado,
        technical_qa_ok=not fallos,
        checks=acc.checks,
        transformation_artifact=f"{ARTEFACTO_TRANSFORMACION}.json",
        provenance_artifact=f"{ARTEFACTO_PROCEDENCIA}.json",
        # El vídeo es técnicamente válido. Si debe publicarse sigue sin
        # evaluarse: es un juicio humano y su estado por defecto lo dice.
        ready_for_render=not fallos,
    )


def ejecutar_qa_video(ctx: ContextoEtapa) -> QAResult:
    ws: Workspace = ctx.workspace
    informe, frames = evaluar_video(ws)

    entrada = ctx.manifest.registrar_etapa("final_video_qa")
    entrada.metadata = {
        **entrada.metadata,
        "status": informe.status.value,
        "technical_qa_ok": informe.technical_qa_ok,
        "checks": len(informe.checks),
        "errors": [c.name for c in informe.errors],
        "warnings": [c.name for c in informe.warnings],
        "frames": frames,
        "contact_sheet": RUTA_HOJA if frames else None,
        "editorial_legal_assessment": informe.editorial_legal_assessment.status.value,
    }

    log_evento(
        ws.run_id, "final_video_qa", "evaluated",
        status=informe.status.value, checks=len(informe.checks),
        errors=len(informe.errors), warnings=len(informe.warnings),
        frames=len(frames),
        editorial_legal=informe.editorial_legal_assessment.status.value,
    )
    for check in informe.errors:
        log_evento(ws.run_id, "final_video_qa", "error", check=check.name, detail=check.detail)
    for check in informe.warnings:
        log_evento(ws.run_id, "final_video_qa", "warning", check=check.name, detail=check.detail)

    return informe


def qa_video_vigente(artefacto: QAResult, ctx: ContextoEtapa) -> None:
    final = ctx.workspace.leer_artefacto(ARTEFACTO_FINAL, RenderResult)
    if final.created_at > artefacto.created_at:
        raise ArtefactoCorrupto("el vídeo final cambió después de esta QA")


ETAPA_QA_VIDEO = Etapa(
    nombre="final_video_qa",
    artefacto=ARTEFACTO_QA_VIDEO,
    modelo=QAResult,
    ejecutar=ejecutar_qa_video,
    vigente=qa_video_vigente,
)
