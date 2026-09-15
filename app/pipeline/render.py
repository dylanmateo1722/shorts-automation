"""Render de extremo a extremo: material, motor, composición y vídeo final.

Cuatro etapas:

* **render_material** genera el material visual propio y determinista con el
  que se construye la pieza. No descarga nada de terceros.
* **render_job** arma la entrada del motor y **es la puerta de procedencia**:
  resuelve cada recurso que entraría al vídeo contra el ledger de Gate 4 y
  falla antes de que el motor llegue a ejecutarse si alguno no está autorizado.
* **render** invoca MoneyPrinterTurbo a través del adaptador único. Se reutiliza
  tal cual la etapa de Gate 1: hay un solo punto de contacto con el motor.
* **composition** superpone subtítulos y overlay con FFmpeg y produce el MP4
  final, que es el artefacto que se publica —nunca el intermedio del motor—.

Por qué el motor y la composición son etapas separadas: si FFmpeg falla después
de un render correcto, repetir el motor costaría minutos sin arreglar nada. La
idempotencia debe poder reutilizar el intermedio.
"""

from __future__ import annotations

import subprocess

from app.adapters.composicion import NOMBRE_COMPOSICION, componer
from app.adapters.media import binario_ffmpeg, inspeccionar, sha256_archivo
from app.contracts.models import (
    AdaptedScript,
    ClaseFuente,
    EstadoRender,
    OverlaySpec,
    ProvenanceLedger,
    RenderJob,
    RenderResult,
    SubtitleAsset,
    TRANSFORMACIONES_OBLIGATORIAS,
    TransformationSet,
    VoiceAsset,
)
from app.core.errors import (
    ArtefactoCorrupto,
    ComposicionFallida,
    DependenciaAusente,
    ProcedenciaInvalida,
    TiempoAgotado,
    TransformacionIncompleta,
)
from app.core.logging import log_evento
from app.core.stage_runner import ContextoEtapa, Etapa
from app.core.workspace import Workspace
from app.pipeline.stages import ARTEFACTO_JOB, ARTEFACTO_RESULTADO, ETAPA_RENDER
from app.pipeline.transformation import (
    ARTEFACTO_OVERLAY,
    ARTEFACTO_PROCEDENCIA,
    ARTEFACTO_TRANSFORMACION,
)
from app.pipeline.voice import (
    ARTEFACTO_GUION,
    ARTEFACTO_SUBTITULOS,
    ARTEFACTO_VOZ,
)

ARTEFACTO_MATERIAL = "render_material"
ARTEFACTO_FINAL = "final_video"

#: Ruta del material visual propio, relativa al directorio de la corrida. Es
#: fija y conocida para que la procedencia de Gate 4 pueda declararla.
RUTA_MATERIAL = "material/base.mp4"
RUTA_FINAL = "final/short.mp4"

#: Resolución y cadencia objetivo del Short.
ANCHO = 1080
ALTO = 1920
FPS = 30


def _leer(ctx: ContextoEtapa, nombre: str, modelo):
    artefacto = ctx.previos.get(nombre)
    if artefacto is None:
        artefacto = ctx.workspace.leer_artefacto(nombre, modelo)
    return artefacto


# ---------------------------------------------------------------------------
# Material visual propio
# ---------------------------------------------------------------------------


def generar_material(ctx: ContextoEtapa) -> RenderResult:
    """Genera el material visual con el que se construye la pieza.

    Determinista, local y propio: un degradado en vertical del largo de la
    narración. **No se descarga nada de terceros**, y un Short ajeno jamás es
    la fuente del render: si existe material de referencia, sigue clasificado
    ``reference_only`` y no llega hasta aquí.

    Que sea sobrio es intencionado. Gate 5 demuestra que la cadena produce un
    vídeo técnicamente válido; el material visual de producción es una decisión
    editorial posterior.
    """
    ws: Workspace = ctx.workspace
    voz = _leer(ctx, ARTEFACTO_VOZ, VoiceAsset)

    destino = ws.ruta(RUTA_MATERIAL)
    destino.parent.mkdir(parents=True, exist_ok=True)
    # Se genera algo más largo que el audio: el motor recorta al audio, y
    # quedarse corto obligaría a repetir el material.
    duracion = max(voz.audio_duration_seconds + 2.0, 4.0)
    argv = [
        binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"gradients=size={ANCHO}x{ALTO}:rate={FPS}:duration={duracion:.3f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(destino),
    ]
    try:
        proceso = subprocess.run(argv, capture_output=True, text=True, timeout=900)
    except FileNotFoundError as exc:
        raise DependenciaAusente(f"no se pudo ejecutar FFmpeg: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TiempoAgotado("FFmpeg no terminó el material a tiempo", stage="render_material") from exc
    if proceso.returncode != 0 or not destino.is_file() or destino.stat().st_size == 0:
        raise ComposicionFallida(
            f"FFmpeg no produjo material utilizable: "
            f"{(proceso.stderr or '').strip()[-300:]}",
            stage="render_material",
        )

    info = inspeccionar(destino)
    relativa = ws.relativa(destino)

    entrada = ctx.manifest.registrar_etapa("render_material")
    entrada.metadata = {
        **entrada.metadata,
        "path": relativa,
        "seconds": round(info.duracion_s, 3),
        "resolution": f"{info.ancho}x{info.alto}",
        "source": "generado por el pipeline con FFmpeg (lavfi)",
    }
    log_evento(
        ws.run_id, "render_material", "generated",
        path=relativa, seconds=info.duracion_s,
        resolution=f"{info.ancho}x{info.alto}",
    )

    return RenderResult(
        run_id=ws.run_id,
        status=EstadoRender.exito,
        exit_code=0,
        output_path=relativa,
        duration_s=info.duracion_s,
        file_size_bytes=destino.stat().st_size,
        width=info.ancho,
        height=info.alto,
        fps=info.fps,
        video_codec=info.codec_video,
        audio_codec=info.codec_audio or None,
        pixel_format=info.formato_pixel or None,
        sha256=sha256_archivo(destino),
        renderer="ffmpeg-lavfi",
        inspected_with=info.leido_con,
    )


def validar_material(artefacto: RenderResult, ws: Workspace) -> None:
    if not artefacto.output_path:
        raise ArtefactoCorrupto("el material no indica ninguna ruta")
    destino = ws.ruta(artefacto.output_path)
    if not destino.is_file() or destino.stat().st_size == 0:
        raise ArtefactoCorrupto(f"falta el material {artefacto.output_path!r}")


def material_vigente(artefacto: RenderResult, ctx: ContextoEtapa) -> None:
    """Un material más corto que la narración actual ya no sirve."""
    voz = ctx.workspace.leer_artefacto(ARTEFACTO_VOZ, VoiceAsset)
    if (artefacto.duration_s or 0) < voz.audio_duration_seconds:
        raise ArtefactoCorrupto(
            f"el material dura {artefacto.duration_s}s y la narración "
            f"{voz.audio_duration_seconds}s"
        )


# ---------------------------------------------------------------------------
# Puerta de procedencia
# ---------------------------------------------------------------------------


def verificar_procedencia(
    ws: Workspace, ledger: ProvenanceLedger, rutas: list[str], *, etapa: str
) -> None:
    """Rechaza cualquier recurso que no pueda entrar al vídeo.

    Se ejecuta **antes** de invocar el motor. Un recurso de referencia que
    llegara al render ya no se puede deshacer: el vídeo estaría hecho.

    Lo que exige de cada recurso, en este orden: que una fuente del ledger lo
    declare; que esa fuente tenga una decisión de licencia; que la decisión sea
    ``render_allowed``; que el archivo exista; y que su contenido sea el mismo
    sobre el que se decidió. Solo entonces el motor puede ejecutarse.

    No mira el nombre del archivo en ningún momento, y un recurso sin declarar se
    trata como no autorizado en vez de como un descuido tolerable.

    ``render_allowed`` aquí significa que la política técnica lo permite. No
    significa que el material esté libre de reclamaciones: ese juicio es humano y
    vive en ``QAResult.editorial_legal_assessment``.
    """
    for ruta in rutas:
        fuente = ledger.fuente_de(ruta)
        if fuente is None:
            raise ProcedenciaInvalida(
                f"{ruta!r} entraría al render sin procedencia declarada",
                stage=etapa,
            )
        decision = ledger.decision(fuente.asset_id)
        if decision is None:
            raise ProcedenciaInvalida(
                f"{ruta!r} entraría al render sin ninguna decisión de licencia; "
                f"la fuente {fuente.asset_id!r} está registrada pero sin decidir",
                stage=etapa,
            )
        if decision.decision is not ClaseFuente.render_permitido:
            raise ProcedenciaInvalida(
                f"{ruta!r} está clasificado {decision.decision.value!r} y no puede "
                f"entrar al render: {decision.reason}",
                stage=etapa,
            )
        archivo = ws.ruta(ruta)
        if not archivo.is_file():
            raise ProcedenciaInvalida(
                f"{ruta!r} está autorizado pero el archivo no existe", stage=etapa
            )
        # Integridad: la decisión se tomó sobre un contenido concreto. Si el
        # archivo ya no es ese, la autorización no habla de lo que hay en disco y
        # se rechaza —no se degrada a needs_review, porque una decisión que
        # parece aplicable y no lo es es peor que ninguna.
        if fuente.sha256 is not None and sha256_archivo(archivo) != fuente.sha256:
            raise ProcedenciaInvalida(
                f"{ruta!r} cambió desde que se autorizó: la huella registrada "
                f"{fuente.sha256[:12]}… no corresponde al archivo actual",
                stage=etapa,
            )


def verificar_transformacion(conjunto: TransformationSet, *, etapa: str) -> None:
    """Sin los cinco elementos editoriales propios no se renderiza."""
    faltantes = sorted(t.value for t in conjunto.faltantes)
    if faltantes:
        raise TransformacionIncompleta(
            f"faltan elementos de transformación obligatorios: {faltantes}; "
            f"no hay evidencia de qué es propio en la pieza",
            stage=etapa,
        )


# ---------------------------------------------------------------------------
# Construcción del trabajo de render
# ---------------------------------------------------------------------------


def construir_job(ctx: ContextoEtapa) -> RenderJob:
    """Arma la entrada del motor tras comprobar procedencia y transformación."""
    ws: Workspace = ctx.workspace
    guion = _leer(ctx, ARTEFACTO_GUION, AdaptedScript)
    voz = _leer(ctx, ARTEFACTO_VOZ, VoiceAsset)
    subtitulos = _leer(ctx, ARTEFACTO_SUBTITULOS, SubtitleAsset)
    overlay = _leer(ctx, ARTEFACTO_OVERLAY, OverlaySpec)
    ledger = _leer(ctx, ARTEFACTO_PROCEDENCIA, ProvenanceLedger)
    conjunto = _leer(ctx, ARTEFACTO_TRANSFORMACION, TransformationSet)
    material = _leer(ctx, ARTEFACTO_MATERIAL, RenderResult)

    verificar_transformacion(conjunto, etapa="render_job")

    job = RenderJob(
        run_id=ws.run_id,
        task_id=ws.run_id,
        script=guion.full_text,
        audio_path=voz.audio_path,
        materials=[material.output_path],
        subtitle_path=subtitulos.ass_path,
        overlay_path=overlay.overlay_path,
        output_path=RUTA_FINAL,
        composition=NOMBRE_COMPOSICION,
        engine_commit=_commit_motor(ctx),
    )

    # La puerta: nada llega al motor sin pasar por aquí.
    verificar_procedencia(ws, ledger, job.assets_de_render, etapa="render_job")

    entrada = ctx.manifest.registrar_etapa("render_job")
    entrada.metadata = {
        **entrada.metadata,
        "render_assets": job.assets_de_render,
        "composition": job.composition,
        "engine_commit": job.engine_commit,
        "transformation_elements": sorted(t.value for t in conjunto.tipos),
        "policy_version": sorted(ledger.versiones_de_politica()),
        "reference_only_blocked": ledger.rutas_por_clase(ClaseFuente.solo_referencia),
        "needs_review_blocked": ledger.rutas_por_clase(ClaseFuente.revision_pendiente),
        "blocked": ledger.rutas_por_clase(ClaseFuente.bloqueado),
    }
    log_evento(
        ws.run_id, "render_job", "authorized",
        assets=len(job.assets_de_render),
        elements=len(TRANSFORMACIONES_OBLIGATORIAS),
        composition=job.composition,
    )
    return job


def _commit_motor(ctx: ContextoEtapa) -> str | None:
    try:
        proceso = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ctx.settings.raiz_mpt,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proceso.stdout.strip() or None


def validar_job(artefacto: RenderJob, ws: Workspace) -> None:
    """Un job cuyos recursos desaparecieron ya no puede ejecutarse."""
    for ruta in artefacto.assets_de_render:
        if not ws.ruta(ruta).is_file():
            raise ArtefactoCorrupto(f"falta el recurso de render {ruta!r}")


def job_vigente(artefacto: RenderJob, ctx: ContextoEtapa) -> None:
    """El job debe describir los artefactos de ahora, no los de antes."""
    ws = ctx.workspace
    guion = ws.leer_artefacto(ARTEFACTO_GUION, AdaptedScript)
    if artefacto.script != guion.full_text:
        raise ArtefactoCorrupto("el job se armó con otro guion")

    voz = ws.leer_artefacto(ARTEFACTO_VOZ, VoiceAsset)
    if artefacto.audio_path != voz.audio_path:
        raise ArtefactoCorrupto("el job apunta a otro audio")

    subtitulos = ws.leer_artefacto(ARTEFACTO_SUBTITULOS, SubtitleAsset)
    overlay = ws.leer_artefacto(ARTEFACTO_OVERLAY, OverlaySpec)
    if artefacto.subtitle_path != subtitulos.ass_path:
        raise ArtefactoCorrupto("el job apunta a otros subtítulos")
    if artefacto.overlay_path != overlay.overlay_path:
        raise ArtefactoCorrupto("el job apunta a otro overlay")

    # Si cualquier dependencia se regeneró, el job es anterior a ella.
    for nombre, modelo in (
        (ARTEFACTO_VOZ, VoiceAsset),
        (ARTEFACTO_SUBTITULOS, SubtitleAsset),
        (ARTEFACTO_OVERLAY, OverlaySpec),
        (ARTEFACTO_PROCEDENCIA, ProvenanceLedger),
        (ARTEFACTO_MATERIAL, RenderResult),
    ):
        if ws.leer_artefacto(nombre, modelo).created_at > artefacto.created_at:
            raise ArtefactoCorrupto(f"{nombre} cambió después de armar el job")

    # Y la procedencia manda también al reutilizar: si algo dejó de estar
    # autorizado, el job de antes no vale aunque el archivo siga ahí.
    ledger = ws.leer_artefacto(ARTEFACTO_PROCEDENCIA, ProvenanceLedger)
    try:
        verificar_procedencia(ws, ledger, artefacto.assets_de_render, etapa="render_job")
    except ProcedenciaInvalida as exc:
        raise ArtefactoCorrupto(f"la procedencia ya no autoriza el job: {exc.mensaje}") from exc


# ---------------------------------------------------------------------------
# Composición final
# ---------------------------------------------------------------------------


def componer_final(ctx: ContextoEtapa) -> RenderResult:
    """Superpone subtítulos y overlay sobre el vídeo del motor."""
    ws: Workspace = ctx.workspace
    job = _leer(ctx, ARTEFACTO_JOB, RenderJob)
    render = _leer(ctx, ARTEFACTO_RESULTADO, RenderResult)

    if render.status is not EstadoRender.exito or not render.output_path:
        raise ComposicionFallida(
            f"el motor no dejó un vídeo que componer (estado {render.status.value})",
            stage="composition",
        )

    capas = [ws.ruta(r) for r in (job.subtitle_path, job.overlay_path) if r]
    destino = ws.ruta(job.output_path or RUTA_FINAL)
    resultado = componer(
        entrada=ws.ruta(render.output_path), capas_ass=capas, salida=destino
    )

    # Se mide el archivo compuesto, no se hereda lo que dijera el motor.
    info = inspeccionar(destino)
    huella = sha256_archivo(destino)
    relativa = ws.relativa(destino)

    entrada = ctx.manifest.registrar_etapa("composition")
    entrada.metadata = {
        **entrada.metadata,
        "output_path": relativa,
        "layers": resultado.capas,
        "composition": NOMBRE_COMPOSICION,
        "sha256": huella,
        "resolution": f"{info.ancho}x{info.alto}",
        "fps": info.fps,
        "video_codec": info.codec_video,
        "audio_codec": info.codec_audio,
        "duration_s": round(info.duracion_s, 3),
        "inspected_with": info.leido_con,
    }
    log_evento(
        ws.run_id, "composition", "composed",
        layers=len(capas), output=relativa,
        resolution=f"{info.ancho}x{info.alto}", fps=info.fps,
        codec=info.codec_video, audio=info.codec_audio,
        duration_s=info.duracion_s, inspected_with=info.leido_con,
    )

    return RenderResult(
        run_id=ws.run_id,
        status=EstadoRender.exito,
        exit_code=resultado.exit_code,
        output_path=relativa,
        # El intermedio del motor queda referenciado, no perdido: es lo que se
        # compuso, y sirve para diagnosticar sin volver a renderizar.
        combined_path=render.output_path,
        duration_s=info.duracion_s,
        file_size_bytes=destino.stat().st_size,
        width=info.ancho,
        height=info.alto,
        fps=info.fps,
        video_codec=info.codec_video,
        audio_codec=info.codec_audio or None,
        pixel_format=info.formato_pixel or None,
        audio_sample_rate_hz=info.audio_sample_rate_hz or None,
        sha256=huella,
        engine_commit=render.engine_commit,
        renderer=NOMBRE_COMPOSICION,
        inspected_with=info.leido_con,
    )


def validar_final(artefacto: RenderResult, ws: Workspace) -> None:
    if artefacto.status is not EstadoRender.exito or not artefacto.output_path:
        raise ArtefactoCorrupto("el vídeo final registrado no fue exitoso")
    destino = ws.ruta(artefacto.output_path)
    if not destino.is_file() or destino.stat().st_size == 0:
        raise ArtefactoCorrupto(f"falta el vídeo final {artefacto.output_path!r}")
    # Un MP4 sustituido por otro con el mismo nombre no es el que se validó.
    if artefacto.sha256 and sha256_archivo(destino) != artefacto.sha256:
        raise ArtefactoCorrupto(
            f"el vídeo final {artefacto.output_path!r} ya no es el que se registró"
        )


def final_vigente(artefacto: RenderResult, ctx: ContextoEtapa) -> None:
    """Un vídeo anterior a cualquiera de sus entradas está hecho con material viejo."""
    ws = ctx.workspace
    for nombre, modelo in (
        (ARTEFACTO_JOB, RenderJob),
        (ARTEFACTO_RESULTADO, RenderResult),
    ):
        if ws.leer_artefacto(nombre, modelo).created_at > artefacto.created_at:
            raise ArtefactoCorrupto(f"{nombre} cambió después de componer el vídeo")


# ---------------------------------------------------------------------------
# Etapas
# ---------------------------------------------------------------------------


ETAPA_MATERIAL = Etapa(
    nombre="render_material",
    artefacto=ARTEFACTO_MATERIAL,
    modelo=RenderResult,
    ejecutar=generar_material,
    validar=validar_material,
    vigente=material_vigente,
)

ETAPA_JOB = Etapa(
    nombre="render_job",
    artefacto=ARTEFACTO_JOB,
    modelo=RenderJob,
    ejecutar=construir_job,
    validar=validar_job,
    vigente=job_vigente,
)

ETAPA_COMPOSICION = Etapa(
    nombre="composition",
    artefacto=ARTEFACTO_FINAL,
    modelo=RenderResult,
    ejecutar=componer_final,
    validar=validar_final,
    vigente=final_vigente,
)


def construir_pipeline_render() -> list[Etapa]:
    """Material → job → motor → composición. ``ETAPA_RENDER`` es la de Gate 1."""
    return [ETAPA_MATERIAL, ETAPA_JOB, ETAPA_RENDER, ETAPA_COMPOSICION]
