"""Render de extremo a extremo: puerta de procedencia, composición y QA final.

Corren con el motor falso —que respeta el contrato del CLI real: mismos flags,
mismo JSON, mismos códigos de salida— y con FFmpeg de verdad. El MP4 que
inspecciona la QA existe realmente en cada test.

El caso que da sentido a toda la capa es
``test_un_recurso_de_referencia_impide_el_render_sin_llegar_al_motor``: no basta
con que el job falle, hay que demostrar que el motor **ni siquiera se ejecutó**.
"""

from __future__ import annotations

import json

import pytest

from app.adapters.composicion import componer, filtro_de_capas
from app.contracts.models import (
    ClaseFuente,
    EstadoQA,
    EstadoRender,
    OverlaySpec,
    ProvenanceLedger,
    QAResult,
    RenderJob,
    RenderResult,
    SubtitleAsset,
    TransformationSet,
    VoiceAsset,
)
from app.core.errors import (
    ComposicionFallida,
    ErrorPermanente,
    ProcedenciaInvalida,
    TransformacionIncompleta,
)
from app.core.manifest import EstadoEtapa, Manifest
from app.core.stage_runner import StageRunner
from app.pipeline import qa_video, render, transformation, voice
from app.pipeline.stages import ARTEFACTO_JOB, ARTEFACTO_RESULTADO
from tests.conftest import reclasificar, sin_fuente

RUTA_LOG_MOTOR = "render/mpt.log"


@pytest.fixture
def runner(settings_e2e, workspace_e2e) -> StageRunner:
    """Corrida con todo lo de Gate 1 a Gate 4 hecho y el render por delante."""
    manifest = Manifest.cargar(workspace_e2e.dir)
    return StageRunner(workspace_e2e, manifest, settings_e2e)


def _hasta_el_video(runner: StageRunner):
    """Ejecuta job, motor y composición."""
    return [
        runner.ejecutar(e)
        for e in (render.ETAPA_JOB, *_motor_y_composicion())
    ]


def _motor_y_composicion():
    from app.pipeline.stages import ETAPA_RENDER

    return (ETAPA_RENDER, render.ETAPA_COMPOSICION)


def _todo(runner: StageRunner):
    resultados = _hasta_el_video(runner)
    resultados.append(runner.ejecutar(qa_video.ETAPA_QA_VIDEO))
    return resultados


def _reescribir_ledger(runner: StageRunner, nuevo) -> None:
    """Persiste un ledger manipulado y obliga a releerlo desde el disco."""
    runner.workspace.escribir_artefacto(transformation.ARTEFACTO_PROCEDENCIA, nuevo)
    runner.producidos.pop(transformation.ARTEFACTO_PROCEDENCIA, None)


def _ledger_de(runner: StageRunner):
    return runner.workspace.leer_artefacto(
        transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger
    )


def _degradar(runner: StageRunner, ruta: str, clase, *, basis=None) -> None:
    """Cambia a mano la clase de un recurso, como haría alguien editando el JSON.

    El pipeline no produce por sí mismo un recurso bloqueado —todo lo que genera
    es propio y sale autorizado—, así que los casos que importan (que el motor no
    se ejecute) hay que provocarlos.
    """
    _reescribir_ledger(runner, reclasificar(_ledger_de(runner), ruta, clase, basis=basis))


# ---------------------------------------------------------------------------
# RenderJob
# ---------------------------------------------------------------------------


def test_render_job_valido(runner, settings_e2e):
    job = runner.ejecutar(render.ETAPA_JOB).artefacto
    ws = runner.workspace

    assert isinstance(job, RenderJob)
    assert job.task_id == ws.run_id == job.run_id
    assert job.output_path == render.RUTA_FINAL
    assert job.composition == "ffmpeg-libass-burn-in"
    assert len(job.assets_de_render) == 4
    for ruta in job.assets_de_render:
        assert ws.ruta(ruta).is_file(), ruta


def test_render_job_rechaza_uuid_invalido():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RenderJob(
            run_id="no-es-un-uuid", task_id="tampoco", script="x",
            audio_path="voice/n.mp3", materials=["material/base.mp4"],
        )


@pytest.mark.parametrize(
    "campo, valor",
    [
        ("audio_path", "/tmp/narration.mp3"),
        ("materials", ["/var/material.mp4"]),
        ("subtitle_path", "/tmp/subs.ass"),
        ("overlay_path", "C:\\overlay.ass"),
        ("output_path", "/tmp/short.mp4"),
    ],
)
def test_render_job_rechaza_rutas_absolutas(campo, valor):
    from pydantic import ValidationError
    import uuid as _uuid

    rid = _uuid.uuid4()
    base = dict(
        run_id=rid, task_id=rid, script="guion",
        audio_path="voice/narration.mp3", materials=["material/base.mp4"],
    )
    base[campo] = valor
    with pytest.raises(ValidationError):
        RenderJob(**base)


def test_render_job_rechaza_un_asset_inexistente(runner):
    """Un job cuyo material desapareció no puede ejecutarse."""
    job = runner.ejecutar(render.ETAPA_JOB).artefacto
    runner.workspace.ruta(job.materials[0]).unlink()

    with pytest.raises(ProcedenciaInvalida, match="el archivo no existe"):
        runner.ejecutar(render.ETAPA_JOB, forzar=True)


def test_render_job_acepta_lo_que_la_procedencia_autoriza(runner):
    ws = runner.workspace
    ledger = ws.leer_artefacto(transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger)
    job = runner.ejecutar(render.ETAPA_JOB).artefacto

    for ruta in job.assets_de_render:
        assert ledger.permite_render(ruta), ruta
        assert ledger.clase(ruta) is ClaseFuente.render_permitido


def test_render_job_rechaza_un_recurso_de_referencia(runner):
    material = runner.workspace.leer_artefacto(
        render.ARTEFACTO_MATERIAL, RenderResult
    ).output_path
    _degradar(runner, material, ClaseFuente.solo_referencia)

    with pytest.raises(ProcedenciaInvalida, match="reference_only"):
        runner.ejecutar(render.ETAPA_JOB, forzar=True)


def test_render_job_rechaza_un_recurso_sin_declarar(runner):
    """Sin procedencia declarada no se entra al render, aunque el archivo esté."""
    ws = runner.workspace
    material = ws.leer_artefacto(render.ARTEFACTO_MATERIAL, RenderResult).output_path
    _reescribir_ledger(runner, sin_fuente(_ledger_de(runner), material))

    with pytest.raises(ProcedenciaInvalida, match="sin procedencia declarada"):
        runner.ejecutar(render.ETAPA_JOB, forzar=True)


def test_un_recurso_de_referencia_impide_el_render_sin_llegar_al_motor(runner):
    """La prueba que importa: el motor **no se ejecuta**.

    Que el job falle no basta. Si el motor llegara a arrancar, el vídeo ya
    estaría hecho con material que nadie autorizó, y eso no se deshace.
    """
    ws = runner.workspace
    material = ws.leer_artefacto(render.ARTEFACTO_MATERIAL, RenderResult).output_path
    _degradar(runner, material, ClaseFuente.solo_referencia)

    assert not ws.ruta(RUTA_LOG_MOTOR).exists(), "el motor ya había corrido"

    with pytest.raises(ProcedenciaInvalida):
        runner.ejecutar(render.ETAPA_JOB, forzar=True)

    # El motor deja siempre su log al ejecutarse, en éxito o en fallo.
    assert not ws.ruta(RUTA_LOG_MOTOR).exists(), "el motor se ejecutó pese al bloqueo"
    assert not (ws.dir / "render").exists()
    assert not ws.existe(ARTEFACTO_JOB)
    assert not ws.existe(ARTEFACTO_RESULTADO)


def test_la_transformacion_incompleta_bloquea_el_render(runner):
    ws = runner.workspace
    conjunto = ws.leer_artefacto(
        transformation.ARTEFACTO_TRANSFORMACION, TransformationSet
    )
    ws.escribir_artefacto(
        transformation.ARTEFACTO_TRANSFORMACION,
        conjunto.model_copy(update={"elements": conjunto.elements[:3]}),
    )
    runner.producidos.pop(transformation.ARTEFACTO_TRANSFORMACION, None)

    with pytest.raises(TransformacionIncompleta, match="visual_overlay"):
        runner.ejecutar(render.ETAPA_JOB, forzar=True)
    assert not ws.ruta(RUTA_LOG_MOTOR).exists()


def test_la_transformacion_completa_permite_el_render(runner):
    conjunto = runner.workspace.leer_artefacto(
        transformation.ARTEFACTO_TRANSFORMACION, TransformationSet
    )
    assert conjunto.faltantes == set()
    assert runner.ejecutar(render.ETAPA_JOB).artefacto is not None


# ---------------------------------------------------------------------------
# Motor y composición
# ---------------------------------------------------------------------------


def test_el_motor_produce_un_video_y_la_composicion_el_final(runner):
    job, motor, final = (r.artefacto for r in _hasta_el_video(runner))
    ws = runner.workspace

    assert motor.status is EstadoRender.exito
    assert ws.ruta(motor.output_path).is_file()
    assert final.output_path == render.RUTA_FINAL
    assert ws.ruta(final.output_path).is_file()
    # El final no es el intermedio del motor.
    assert final.output_path != motor.output_path
    assert final.combined_path == motor.output_path


def test_el_render_result_final_lleva_metadata_medida_del_archivo(runner):
    from app.adapters.media import sha256_archivo

    final = _hasta_el_video(runner)[-1].artefacto
    destino = runner.workspace.ruta(final.output_path)

    assert final.sha256 == sha256_archivo(destino)
    assert final.file_size_bytes == destino.stat().st_size
    assert (final.width, final.height) == (1080, 1920)
    assert final.video_codec == "h264"
    assert final.audio_codec == "aac"
    assert final.pixel_format == "yuv420p"
    assert final.audio_sample_rate_hz > 0
    assert final.duration_s > 0
    assert final.renderer == "ffmpeg-libass-burn-in"
    assert final.inspected_with in ("ffprobe", "ffmpeg")


def test_la_composicion_superpone_subtitulos_y_overlay(runner):
    job = runner.ejecutar(render.ETAPA_JOB).artefacto
    ws = runner.workspace
    subtitulos = ws.leer_artefacto(voice.ARTEFACTO_SUBTITULOS, SubtitleAsset)
    overlay = ws.leer_artefacto(transformation.ARTEFACTO_OVERLAY, OverlaySpec)

    assert job.subtitle_path == subtitulos.ass_path
    assert job.overlay_path == overlay.overlay_path
    filtro = filtro_de_capas([ws.ruta(job.subtitle_path), ws.ruta(job.overlay_path)])
    assert filtro.count("ass=") == 2


def test_la_composicion_no_reutiliza_los_subtitulos_de_otro_sitio(runner):
    """No hay un segundo generador: el ASS es el que produjo Gate 3."""
    job = runner.ejecutar(render.ETAPA_JOB).artefacto
    subtitulos = runner.workspace.leer_artefacto(
        voice.ARTEFACTO_SUBTITULOS, SubtitleAsset
    )
    assert job.subtitle_path == subtitulos.ass_path
    assert job.subtitle_path.endswith(".ass")


def test_un_fallo_del_motor_no_produce_video_final(runner, monkeypatch):
    monkeypatch.setenv("FAKE_MPT_MODE", "fail")
    runner.ejecutar(render.ETAPA_JOB)

    with pytest.raises(ErrorPermanente, match="el motor terminó con código"):
        runner.ejecutar(_motor_y_composicion()[0])

    assert not runner.workspace.existe(render.ARTEFACTO_FINAL)
    entrada = runner.manifest.etapa("render")
    assert entrada.status is EstadoEtapa.fallida
    assert entrada.error["type"] == "ErrorPermanente"


def test_un_fallo_de_ffmpeg_no_marca_el_video_como_exitoso(runner, monkeypatch):
    _hasta_el_video(runner)[0]
    runner.ejecutar(_motor_y_composicion()[0])

    # Se rompe la capa que FFmpeg tiene que componer.
    ws = runner.workspace
    job = ws.leer_artefacto(ARTEFACTO_JOB, RenderJob)
    ws.ruta(job.overlay_path).unlink()

    with pytest.raises(ComposicionFallida, match="faltan capas"):
        runner.ejecutar(render.ETAPA_COMPOSICION, forzar=True)

    entrada = runner.manifest.etapa("composition")
    assert entrada.status is EstadoEtapa.fallida
    assert entrada.error["type"] == "ComposicionFallida"
    assert entrada.error["retryable"] is False


def test_un_mp4_parcial_no_queda_como_final(runner, tmp_path, mp4_vertical):
    """Si FFmpeg falla a mitad, el archivo final no debe existir siquiera."""
    salida = tmp_path / "final" / "short.mp4"
    inexistente = tmp_path / "no_existe.ass"

    with pytest.raises(ComposicionFallida):
        componer(entrada=mp4_vertical, capas_ass=[inexistente], salida=salida)

    assert not salida.exists()
    assert not salida.with_name("short.parcial.mp4").exists()


def test_componer_sin_capas_es_un_error(tmp_path, mp4_vertical):
    """El vídeo final sin composición sería el intermedio del motor."""
    with pytest.raises(ComposicionFallida, match="ninguna capa"):
        componer(entrada=mp4_vertical, capas_ass=[], salida=tmp_path / "x.mp4")


def test_el_fallo_del_motor_y_el_de_ffmpeg_se_distinguen():
    """Categorías distintas: repetir el motor tras un fallo de FFmpeg no arregla nada."""
    assert ComposicionFallida("x").categoria == "permanente"
    assert ComposicionFallida("x", stage="composition").stage == "composition"
    assert ComposicionFallida is not ErrorPermanente


# ---------------------------------------------------------------------------
# QA del vídeo final
# ---------------------------------------------------------------------------


def test_la_qa_final_aprueba_un_video_correcto(runner):
    informe = _todo(runner)[-1].artefacto

    assert isinstance(informe, QAResult)
    assert informe.technical_qa_ok is True, informe.resumen()
    assert informe.status in (EstadoQA.aprobado, EstadoQA.aprobado_con_avisos)
    assert informe.errors == []


def test_la_qa_final_inspecciona_el_archivo_y_no_el_registro(runner):
    informe = _todo(runner)[-1].artefacto
    nombres = " ".join(c.name for c in informe.checks)

    for esperado in (
        "final: el archivo existe",
        "final: SHA-256 calculable",
        "final: resolución vertical 1080×1920",
        "final: códec de vídeo h264",
        "final: códec de audio aac",
        "final: formato de píxel compatible",
        "sincronía: el vídeo dura lo que la narración",
        "composición: se compusieron subtítulos y overlay",
    ):
        assert esperado in nombres, esperado


def test_la_qa_final_falla_si_el_video_no_existe(runner):
    _hasta_el_video(runner)
    ws = runner.workspace
    final = ws.leer_artefacto(render.ARTEFACTO_FINAL, RenderResult)
    ws.ruta(final.output_path).unlink()

    informe, _ = qa_video.evaluar_video(ws)
    assert informe.technical_qa_ok is False
    assert any("el archivo existe" in c.name for c in informe.errors)


def test_la_qa_final_falla_si_el_archivo_no_se_puede_abrir(runner):
    """Un MP4 corrupto no pasa por tener el nombre correcto."""
    _hasta_el_video(runner)
    ws = runner.workspace
    final = ws.leer_artefacto(render.ARTEFACTO_FINAL, RenderResult)
    ws.ruta(final.output_path).write_bytes(b"esto no es un MP4")

    informe, _ = qa_video.evaluar_video(ws)
    assert informe.technical_qa_ok is False


def test_la_qa_final_falla_con_una_resolucion_que_no_es_vertical(runner, tmp_path):
    import subprocess

    from app.adapters.media import binario_ffmpeg, sha256_archivo

    _hasta_el_video(runner)
    ws = runner.workspace
    final = ws.leer_artefacto(render.ARTEFACTO_FINAL, RenderResult)
    destino = ws.ruta(final.output_path)
    subprocess.run(
        [binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:size=1920x1080:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(destino)],
        check=True, timeout=600,
    )
    ws.escribir_artefacto(
        render.ARTEFACTO_FINAL,
        final.model_copy(update={"sha256": sha256_archivo(destino)}),
    )

    informe, _ = qa_video.evaluar_video(ws)
    assert informe.technical_qa_ok is False
    assert any("resolución vertical" in c.name for c in informe.errors)


def test_la_qa_final_falla_con_un_codec_de_video_distinto(runner):
    import subprocess

    from app.adapters.media import binario_ffmpeg, sha256_archivo

    _hasta_el_video(runner)
    ws = runner.workspace
    final = ws.leer_artefacto(render.ARTEFACTO_FINAL, RenderResult)
    destino = ws.ruta(final.output_path)
    subprocess.run(
        [binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:size=1080x1920:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
         "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(destino)],
        check=True, timeout=600,
    )
    ws.escribir_artefacto(
        render.ARTEFACTO_FINAL,
        final.model_copy(update={"sha256": sha256_archivo(destino)}),
    )

    informe, _ = qa_video.evaluar_video(ws)
    assert informe.technical_qa_ok is False
    assert any("códec de vídeo" in c.name for c in informe.errors)


def test_la_qa_final_falla_sin_pista_de_audio(runner, mp4_sin_audio):
    import shutil

    from app.adapters.media import sha256_archivo

    _hasta_el_video(runner)
    ws = runner.workspace
    final = ws.leer_artefacto(render.ARTEFACTO_FINAL, RenderResult)
    destino = ws.ruta(final.output_path)
    shutil.copy2(mp4_sin_audio, destino)
    ws.escribir_artefacto(
        render.ARTEFACTO_FINAL,
        final.model_copy(update={"sha256": sha256_archivo(destino)}),
    )

    informe, _ = qa_video.evaluar_video(ws)
    assert informe.technical_qa_ok is False
    assert any("pista de audio" in c.name for c in informe.errors)


def test_la_qa_final_falla_con_una_duracion_incoherente(runner):
    """Un vídeo mucho más corto que la narración se cortó por algún sitio."""
    _hasta_el_video(runner)
    ws = runner.workspace
    voz = ws.leer_artefacto(voice.ARTEFACTO_VOZ, VoiceAsset)
    ws.escribir_artefacto(
        voice.ARTEFACTO_VOZ,
        voz.model_copy(update={"audio_duration_seconds": voz.audio_duration_seconds + 30}),
    )

    informe, _ = qa_video.evaluar_video(ws)
    assert informe.technical_qa_ok is False
    assert any("dura lo que la narración" in c.name for c in informe.errors)


def test_una_diferencia_pequena_de_duracion_es_un_aviso(runner):
    """El muxing mueve el final unas décimas; eso no invalida nada."""
    informe = _todo(runner)[-1].artefacto

    exacta = next(c for c in informe.checks if "exactamente lo mismo" in c.name)
    assert exacta.level.value == "warning"
    dentro = next(c for c in informe.checks if "dura lo que la narración" in c.name)
    assert dentro.ok


def test_la_qa_final_separa_lo_medible_de_lo_que_requiere_mirar(runner):
    """Que las capas se compusieran no demuestra que el subtítulo se vea."""
    informe = _todo(runner)[-1].artefacto

    revision = next(c for c in informe.warnings if "revisión humana" in c.name)
    assert revision.level.value == "warning"
    assert "qa/frames" in revision.detail
    # Y la comprobación técnica, que sí es afirmable, va aparte y en verde.
    compuestas = next(c for c in informe.checks if "se compusieron" in c.name)
    assert compuestas.ok


def test_la_qa_final_extrae_fotogramas_para_inspeccion_visual(runner):
    _todo(runner)
    _, frames = qa_video.evaluar_video(runner.workspace)

    assert len(frames) >= 2
    for frame in frames:
        assert not frame.startswith("/")
        assert runner.workspace.ruta(frame).stat().st_size > 0
    assert runner.workspace.ruta(qa_video.RUTA_HOJA).is_file()


def test_la_qa_final_no_mezcla_lo_tecnico_con_el_juicio_editorial(runner):
    informe = _todo(runner)[-1].artefacto
    assert informe.technical_qa_ok is True
    assert informe.editorial_legal_assessment.status.value == "NOT_ASSESSED"
    assert informe.editorial_legal_assessment.automated_similarity_threshold_applied is False


# ---------------------------------------------------------------------------
# Idempotencia e invalidación
# ---------------------------------------------------------------------------


def test_una_segunda_corrida_no_vuelve_a_renderizar(runner):
    _todo(runner)
    ws = runner.workspace
    final = ws.leer_artefacto(render.ARTEFACTO_FINAL, RenderResult)
    marca = ws.ruta(final.output_path).stat().st_mtime_ns

    segundos = _todo(runner)
    assert all(r.omitida for r in segundos)
    assert ws.ruta(final.output_path).stat().st_mtime_ns == marca


def test_un_fallo_de_composicion_no_obliga_a_repetir_el_motor(runner):
    """La razón de que motor y composición sean etapas distintas."""
    runner.ejecutar(render.ETAPA_JOB)
    motor = runner.ejecutar(_motor_y_composicion()[0]).artefacto
    marca = runner.workspace.ruta(motor.output_path).stat().st_mtime_ns

    assert runner.ejecutar(_motor_y_composicion()[0]).omitida
    assert runner.workspace.ruta(motor.output_path).stat().st_mtime_ns == marca


def test_regenerar_el_audio_invalida_el_job(runner):
    """Un job armado antes de la narración actual describe otra pieza."""
    from datetime import timedelta

    _todo(runner)
    ws = runner.workspace
    voz = ws.leer_artefacto(voice.ARTEFACTO_VOZ, VoiceAsset)
    ws.escribir_artefacto(
        voice.ARTEFACTO_VOZ,
        voz.model_copy(update={"created_at": voz.created_at + timedelta(minutes=5)}),
    )
    runner.producidos.pop(voice.ARTEFACTO_VOZ, None)

    assert not runner.ejecutar(render.ETAPA_JOB).omitida


def test_un_audio_que_desaparece_bloquea_el_job(runner):
    """Y si además el archivo no está, la puerta de procedencia lo dice."""
    _todo(runner)
    ws = runner.workspace
    voz = ws.leer_artefacto(voice.ARTEFACTO_VOZ, VoiceAsset)
    ws.ruta(voz.audio_path).unlink()

    with pytest.raises(ProcedenciaInvalida, match="el archivo no existe"):
        runner.ejecutar(render.ETAPA_JOB, forzar=True)


def test_cambiar_el_guion_invalida_el_job(runner):
    from tests.conftest import construir_guion

    _todo(runner)
    ws = runner.workspace
    ws.escribir_artefacto(
        voice.ARTEFACTO_GUION, construir_guion(ws.run_id, texto="Otro guion distinto.")
    )
    runner.producidos.pop(voice.ARTEFACTO_GUION, None)

    assert not runner.ejecutar(render.ETAPA_JOB).omitida


def test_perder_la_autorizacion_invalida_un_job_ya_hecho(runner):
    """Reutilizar no puede saltarse la procedencia."""
    runner.ejecutar(render.ETAPA_JOB)
    material = runner.workspace.leer_artefacto(
        render.ARTEFACTO_MATERIAL, RenderResult
    ).output_path
    _degradar(runner, material, ClaseFuente.solo_referencia)

    with pytest.raises(ProcedenciaInvalida):
        runner.ejecutar(render.ETAPA_JOB)


def test_si_el_mp4_final_cambia_en_disco_deja_de_ser_valido(runner):
    """La huella registrada protege de reutilizar un archivo sustituido."""
    _todo(runner)
    ws = runner.workspace
    final = ws.leer_artefacto(render.ARTEFACTO_FINAL, RenderResult)
    ws.ruta(final.output_path).write_bytes(b"otro contenido")

    assert not runner.ejecutar(render.ETAPA_COMPOSICION).omitida


def test_regenerar_el_video_invalida_la_qa(runner):
    _todo(runner)
    assert runner.ejecutar(qa_video.ETAPA_QA_VIDEO).omitida

    runner.ejecutar(render.ETAPA_COMPOSICION, forzar=True)
    assert not runner.ejecutar(qa_video.ETAPA_QA_VIDEO).omitida


# ---------------------------------------------------------------------------
# Manifest y seguridad
# ---------------------------------------------------------------------------


def test_el_manifest_registra_las_etapas_del_render(runner):
    _todo(runner)
    for nombre in ("render_material", "render_job", "render", "composition", "final_video_qa"):
        entrada = runner.manifest.etapa(nombre)
        assert entrada is not None, nombre
        assert entrada.status is EstadoEtapa.completada


def test_el_manifest_deja_por_escrito_que_assets_entraron_al_render(runner):
    _todo(runner)
    metadata = runner.manifest.etapa("render_job").metadata

    assert len(metadata["render_assets"]) == 4
    assert metadata["composition"] == "ffmpeg-libass-burn-in"
    assert sorted(metadata["transformation_elements"]) == [
        "added_context", "own_narration", "own_subtitles",
        "restructured_script", "visual_overlay",
    ]


def test_el_manifest_registra_la_huella_del_video_final(runner):
    _todo(runner)
    metadata = runner.manifest.etapa("composition").metadata

    assert len(metadata["sha256"]) == 64
    assert metadata["resolution"] == "1080x1920"
    assert metadata["layers"] == ["subtitles.ass", "overlay.ass"]


def test_el_manifest_del_render_no_guarda_secretos(runner, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-no-debe-aparecer-jamas")
    _todo(runner)
    runner.manifest.guardar(runner.workspace.dir)

    contenido = (runner.workspace.dir / "manifest.json").read_text(encoding="utf-8")
    assert "sk-no-debe-aparecer-jamas" not in contenido
    assert "api_key" not in contenido.lower()


def test_las_rutas_del_video_final_son_relativas(runner):
    _todo(runner)
    ws = runner.workspace
    final = ws.leer_artefacto(render.ARTEFACTO_FINAL, RenderResult)
    job = ws.leer_artefacto(ARTEFACTO_JOB, RenderJob)

    rutas = [final.output_path, final.combined_path, *job.assets_de_render, job.output_path]
    for ruta in rutas:
        assert ruta and not ruta.startswith("/")
        assert str(ws.dir) not in ruta


def test_el_artefacto_final_se_puede_releer_desde_json(runner):
    _todo(runner)
    ws = runner.workspace
    crudo = json.loads(ws.ruta_artefacto(render.ARTEFACTO_FINAL).read_text(encoding="utf-8"))

    assert crudo["renderer"] == "ffmpeg-libass-burn-in"
    assert RenderResult.model_validate(crudo) == ws.leer_artefacto(
        render.ARTEFACTO_FINAL, RenderResult
    )
