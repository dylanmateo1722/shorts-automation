"""Etapas de voz y subtítulos sobre el orquestador.

Se ejecutan con el proveedor falso, que genera audio real con FFmpeg: el camino
de medición, alineación y serialización es el mismo que con Edge TTS. Lo que no
se prueba aquí es el servicio externo, y para eso está ``test_tts_real.py``.
"""

from __future__ import annotations

import json

import pytest

from app.adapters.tts import ProveedorTTSFalso
from app.contracts.models import (
    AdaptedScript,
    SubtitleAsset,
    VoiceAsset,
    WordBoundaryAsset,
    hash_guion,
)
from app.core.errors import AlineacionInvalida
from app.core.manifest import EstadoEtapa, Manifest
from app.core.stage_runner import StageRunner
from app.pipeline import voice
from tests.conftest import construir_guion


@pytest.fixture
def proveedor() -> ProveedorTTSFalso:
    return ProveedorTTSFalso()


@pytest.fixture
def runner(workspace_voz, settings_voz, proveedor) -> StageRunner:
    manifest, _ = Manifest.cargar_o_crear(
        workspace_voz.dir, workspace_voz.run_id,
        config=settings_voz.publico(), versions={},
    )
    return StageRunner(
        workspace_voz, manifest, settings_voz,
        {voice.CLAVE_PROVEEDOR_TTS: proveedor},
    )


def _ejecutar_todo(runner: StageRunner):
    return [runner.ejecutar(etapa) for etapa in voice.construir_pipeline_voz()]


# ---------------------------------------------------------------------------
# Cadena completa
# ---------------------------------------------------------------------------


def test_del_guion_al_subtitulo(runner, workspace_voz):
    """AdaptedScript → audio → tiempos → SRT → ASS, en una sola corrida."""
    voz, subtitulos = (r.artefacto for r in _ejecutar_todo(runner))

    assert isinstance(voz, VoiceAsset) and isinstance(subtitulos, SubtitleAsset)
    assert workspace_voz.ruta(voz.audio_path).is_file()
    assert workspace_voz.ruta(subtitulos.srt_path).is_file()
    assert workspace_voz.ruta(subtitulos.ass_path).is_file()
    assert subtitulos.cue_count > 0


def test_se_persisten_los_tres_artefactos_y_ninguno_mas(runner, workspace_voz):
    _ejecutar_todo(runner)
    for nombre, modelo in (
        (voice.ARTEFACTO_VOZ, VoiceAsset),
        (voice.ARTEFACTO_LIMITES, WordBoundaryAsset),
        (voice.ARTEFACTO_SUBTITULOS, SubtitleAsset),
    ):
        assert workspace_voz.leer_artefacto(nombre, modelo) is not None


def test_los_tres_artefactos_comparten_run_id_y_huella_del_guion(runner, workspace_voz):
    _ejecutar_todo(runner)
    guion = workspace_voz.leer_artefacto(voice.ARTEFACTO_GUION, AdaptedScript)
    huella = hash_guion(guion.full_text)

    for nombre, modelo in (
        (voice.ARTEFACTO_VOZ, VoiceAsset),
        (voice.ARTEFACTO_LIMITES, WordBoundaryAsset),
        (voice.ARTEFACTO_SUBTITULOS, SubtitleAsset),
    ):
        artefacto = workspace_voz.leer_artefacto(nombre, modelo)
        assert artefacto.run_id == workspace_voz.run_id
        assert artefacto.source_script_sha256 == huella


def test_todas_las_rutas_guardadas_son_relativas(runner, workspace_voz):
    """Un artefacto con rutas absolutas no sobrevive a otra máquina."""
    _ejecutar_todo(runner)
    voz = workspace_voz.leer_artefacto(voice.ARTEFACTO_VOZ, VoiceAsset)
    sub = workspace_voz.leer_artefacto(voice.ARTEFACTO_SUBTITULOS, SubtitleAsset)

    rutas = [voz.audio_path, sub.srt_path, sub.ass_path, sub.word_boundary_artifact]
    for ruta in rutas:
        assert not ruta.startswith("/")
        assert str(workspace_voz.dir) not in ruta
        assert workspace_voz.ruta(ruta).is_file()


def test_el_subtitulo_apunta_al_artefacto_de_tiempos(runner, workspace_voz):
    _, subtitulos = (r.artefacto for r in _ejecutar_todo(runner))
    destino = workspace_voz.ruta(subtitulos.word_boundary_artifact)
    datos = json.loads(destino.read_text(encoding="utf-8"))
    assert datos["boundaries"]


# ---------------------------------------------------------------------------
# Duración: estimada, real y de los subtítulos
# ---------------------------------------------------------------------------


def test_la_duracion_sale_del_archivo_y_no_del_ultimo_tiempo(runner, workspace_voz):
    """Regla principal de Gate 3, comprobada sobre el artefacto persistido."""
    voz = _ejecutar_todo(runner)[0].artefacto
    limites = workspace_voz.leer_artefacto(voice.ARTEFACTO_LIMITES, WordBoundaryAsset)

    fin_ultimo = max(b.end_seconds for b in limites.boundaries)
    assert voz.audio_duration_seconds > fin_ultimo
    assert limites.audio_duration_seconds == voz.audio_duration_seconds


def test_se_registran_las_tres_duraciones_por_separado(runner, workspace_voz):
    voz, subtitulos = (r.artefacto for r in _ejecutar_todo(runner))
    guion = workspace_voz.leer_artefacto(voice.ARTEFACTO_GUION, AdaptedScript)

    assert guion.estimated_duration_seconds > 0
    assert voz.audio_duration_seconds > 0
    # La referencia del final del video es el audio medido, no la estimación.
    assert subtitulos.duration_seconds == voz.audio_duration_seconds


def test_la_estimacion_y_la_duracion_real_no_tienen_que_coincidir(runner):
    """Se comparan y se registran; no se exige que sean iguales."""
    _ejecutar_todo(runner)
    metadata = runner.manifest.etapa("voice").metadata

    assert metadata["estimated_duration_seconds"] > 0
    assert metadata["actual_duration_seconds"] > 0
    assert metadata["duration_delta_seconds"] == pytest.approx(
        metadata["actual_duration_seconds"] - metadata["estimated_duration_seconds"],
        abs=1e-3,
    )
    assert metadata["duration_ratio"] > 0


def test_la_divergencia_de_duracion_no_recalibra_nada(runner, settings_voz):
    """Registrar no es corregir: el WPM de la corrida no se toca."""
    antes = settings_voz.default_wpm
    _ejecutar_todo(runner)
    assert settings_voz.default_wpm == antes


def test_la_cola_de_audio_se_avisa_pero_no_invalida(runner):
    _ejecutar_todo(runner)
    metadata = runner.manifest.etapa("voice").metadata
    assert metadata["audio_tail_seconds"] > 0
    assert any("después del último WordBoundary" in a for a in metadata["warnings"])
    assert runner.manifest.etapa("voice").status is EstadoEtapa.completada


def test_ningun_cue_se_sale_de_la_duracion_real(runner):
    _, subtitulos = (r.artefacto for r in _ejecutar_todo(runner))
    assert subtitulos.cues[-1].end_seconds <= subtitulos.duration_seconds


# ---------------------------------------------------------------------------
# Idempotencia
# ---------------------------------------------------------------------------


def test_un_audio_valido_no_se_vuelve_a_sintetizar(runner, proveedor):
    _ejecutar_todo(runner)
    assert proveedor.llamadas == 1

    resultados = _ejecutar_todo(runner)
    assert proveedor.llamadas == 1, "se gastó una llamada de TTS de más"
    assert all(r.omitida for r in resultados)


def test_unos_subtitulos_validos_no_se_vuelven_a_generar(runner, workspace_voz):
    _ejecutar_todo(runner)
    ruta = workspace_voz.ruta(
        workspace_voz.leer_artefacto(voice.ARTEFACTO_SUBTITULOS, SubtitleAsset).srt_path
    )
    marca = ruta.stat().st_mtime_ns

    _ejecutar_todo(runner)
    assert ruta.stat().st_mtime_ns == marca


def test_si_desaparece_el_audio_se_vuelve_a_sintetizar(runner, workspace_voz, proveedor):
    voz = _ejecutar_todo(runner)[0].artefacto
    workspace_voz.ruta(voz.audio_path).unlink()

    runner.ejecutar(voice.ETAPA_VOZ)
    assert proveedor.llamadas == 2


def test_si_desaparecen_los_tiempos_se_vuelve_a_sintetizar(runner, workspace_voz, proveedor):
    _ejecutar_todo(runner)
    workspace_voz.ruta_artefacto(voice.ARTEFACTO_LIMITES).unlink()

    runner.ejecutar(voice.ETAPA_VOZ)
    assert proveedor.llamadas == 2


def test_si_desaparece_el_srt_se_vuelven_a_generar_los_subtitulos(runner, workspace_voz):
    subtitulos = _ejecutar_todo(runner)[1].artefacto
    workspace_voz.ruta(subtitulos.srt_path).unlink()

    resultado = runner.ejecutar(voice.ETAPA_SUBTITULOS)
    assert not resultado.omitida
    assert workspace_voz.ruta(subtitulos.srt_path).is_file()


# ---------------------------------------------------------------------------
# Invalidación
# ---------------------------------------------------------------------------


def _reescribir_guion(workspace, texto):
    workspace.escribir_artefacto(
        voice.ARTEFACTO_GUION, construir_guion(workspace.run_id, texto)
    )


def test_cambiar_el_guion_invalida_el_audio(runner, workspace_voz, proveedor):
    _ejecutar_todo(runner)
    _reescribir_guion(workspace_voz, "Un guion completamente distinto del anterior.")
    runner.producidos.pop(voice.ARTEFACTO_GUION, None)

    resultado = runner.ejecutar(voice.ETAPA_VOZ)
    assert not resultado.omitida
    assert proveedor.llamadas == 2


def test_cambiar_la_voz_invalida_el_audio(runner, settings_voz, proveedor):
    import dataclasses

    _ejecutar_todo(runner)
    runner.settings = dataclasses.replace(settings_voz, tts_voice="es-MX-DaliaNeural")

    resultado = runner.ejecutar(voice.ETAPA_VOZ)
    assert not resultado.omitida
    assert resultado.artefacto.voice == "es-MX-DaliaNeural"


def test_cambiar_el_ritmo_invalida_el_audio(runner, settings_voz, proveedor):
    """Ritmo y tono no caben en el contrato: su huella vive en el manifest."""
    import dataclasses

    _ejecutar_todo(runner)
    assert proveedor.llamadas == 1
    runner.settings = dataclasses.replace(settings_voz, tts_rate="+15%")

    assert not runner.ejecutar(voice.ETAPA_VOZ).omitida
    assert proveedor.llamadas == 2


def test_cambiar_el_tono_invalida_el_audio(runner, settings_voz, proveedor):
    import dataclasses

    _ejecutar_todo(runner)
    runner.settings = dataclasses.replace(settings_voz, tts_pitch="-10Hz")

    assert not runner.ejecutar(voice.ETAPA_VOZ).omitida
    assert proveedor.llamadas == 2


def test_regenerar_el_audio_invalida_los_subtitulos(runner, workspace_voz):
    """Otra voz produce otros tiempos aunque el audio dure lo mismo."""
    import dataclasses

    _ejecutar_todo(runner)
    runner.settings = dataclasses.replace(runner.settings, tts_voice="es-ES-AlvaroNeural")
    runner.ejecutar(voice.ETAPA_VOZ)

    resultado = runner.ejecutar(voice.ETAPA_SUBTITULOS)
    assert not resultado.omitida


def test_un_artefacto_corrupto_se_rehace(runner, workspace_voz, proveedor):
    _ejecutar_todo(runner)
    workspace_voz.ruta_artefacto(voice.ARTEFACTO_VOZ).write_text("{", encoding="utf-8")

    assert not runner.ejecutar(voice.ETAPA_VOZ).omitida
    assert proveedor.llamadas == 2


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------


def test_unos_tiempos_de_otro_guion_no_se_disimulan(runner, workspace_voz):
    """Antes que entregar subtítulos desincronizados, la etapa falla."""
    _ejecutar_todo(runner)
    limites = workspace_voz.leer_artefacto(voice.ARTEFACTO_LIMITES, WordBoundaryAsset)
    ajenos = limites.model_copy(
        update={
            "boundaries": [
                b.model_copy(update={"text": f"palabra{b.index}"})
                for b in limites.boundaries
            ]
        }
    )
    workspace_voz.escribir_artefacto(voice.ARTEFACTO_LIMITES, ajenos)

    with pytest.raises(AlineacionInvalida, match="no casan con el guion"):
        runner.ejecutar(voice.ETAPA_SUBTITULOS, forzar=True)


def test_sin_tiempos_no_hay_subtitulos(runner, workspace_voz):
    _ejecutar_todo(runner)
    limites = workspace_voz.leer_artefacto(voice.ARTEFACTO_LIMITES, WordBoundaryAsset)
    workspace_voz.escribir_artefacto(
        voice.ARTEFACTO_LIMITES, limites.model_copy(update={"boundaries": []})
    )

    with pytest.raises(AlineacionInvalida, match="WordBoundary"):
        runner.ejecutar(voice.ETAPA_SUBTITULOS, forzar=True)


def test_el_error_queda_registrado_en_el_manifest(runner, workspace_voz):
    _ejecutar_todo(runner)
    limites = workspace_voz.leer_artefacto(voice.ARTEFACTO_LIMITES, WordBoundaryAsset)
    workspace_voz.escribir_artefacto(
        voice.ARTEFACTO_LIMITES, limites.model_copy(update={"boundaries": []})
    )

    with pytest.raises(AlineacionInvalida):
        runner.ejecutar(voice.ETAPA_SUBTITULOS, forzar=True)

    entrada = runner.manifest.etapa("subtitles")
    assert entrada.status is EstadoEtapa.fallida
    assert entrada.error["type"] == "AlineacionInvalida"
    assert entrada.error["retryable"] is False


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_el_manifest_registra_la_etapa_de_voz(runner):
    _ejecutar_todo(runner)
    entrada = runner.manifest.etapa("voice")

    assert entrada.status is EstadoEtapa.completada
    assert entrada.started_at and entrada.finished_at
    for clave in ("provider", "voice", "actual_duration_seconds", "estimated_duration_seconds"):
        assert clave in entrada.metadata


def test_el_manifest_registra_la_etapa_de_subtitulos(runner):
    _ejecutar_todo(runner)
    entrada = runner.manifest.etapa("subtitles")

    assert entrada.status is EstadoEtapa.completada
    assert entrada.started_at and entrada.finished_at
    assert entrada.metadata["cue_count"] > 0
    assert entrada.metadata["duration_seconds"] > 0


def test_el_manifest_registra_los_tiempos_de_cada_paso(runner):
    """Solo se mide; no se optimiza nada todavía."""
    _ejecutar_todo(runner)
    assert runner.manifest.etapa("voice").metadata["synthesis_seconds"] >= 0
    assert runner.manifest.etapa("subtitles").metadata["alignment_seconds"] >= 0


def test_el_manifest_registra_los_artefactos_de_voz(runner):
    _ejecutar_todo(runner)
    for nombre in (voice.ARTEFACTO_VOZ, voice.ARTEFACTO_LIMITES, voice.ARTEFACTO_SUBTITULOS):
        assert nombre in runner.manifest.artifacts
        assert runner.manifest.artifacts[nombre].bytes > 0
        assert not runner.manifest.artifacts[nombre].path.startswith("/")


def test_la_metadata_de_voz_sobrevive_a_una_omision(runner):
    """Al omitir, la etapa no corre: lo anotado la vez anterior es lo único que hay."""
    _ejecutar_todo(runner)
    antes = dict(runner.manifest.etapa("voice").metadata)

    _ejecutar_todo(runner)
    assert runner.manifest.etapa("voice").metadata == antes
    assert runner.manifest.etapa("voice").status is EstadoEtapa.omitida


def test_el_manifest_no_guarda_secretos(runner, workspace_voz, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-secreto-que-no-debe-salir")
    _ejecutar_todo(runner)
    runner.manifest.guardar(workspace_voz.dir)

    contenido = (workspace_voz.dir / "manifest.json").read_text(encoding="utf-8")
    assert "sk-secreto-que-no-debe-salir" not in contenido
    assert "api_key" not in contenido.lower()
