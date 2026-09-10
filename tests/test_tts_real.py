"""Prueba de extremo a extremo contra el Edge TTS **real**.

Esta suite sí habla con un servicio externo, y por eso está separada del resto
y detrás de una marca. Las demás pruebas usan el proveedor falso: son válidas,
pero no demuestran que el servicio funcione. Confundir las dos cosas sería
declarar PASS sobre algo que no se ejecutó.

    RUN_REAL_TTS=1 pytest tests/test_tts_real.py -v

Sin esa variable se omite entera, con el motivo escrito en el informe de
pytest. Un fallo aquí con el resto en verde significa que Edge TTS no estaba
disponible, no que la cadena esté rota: se reporta, no se disimula.

Edge TTS no requiere credencial, así que no hay secreto que proteger.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.adapters.tts import ConfiguracionVoz, ProveedorEdgeTTS
from app.contracts.models import (
    SubtitleAsset,
    VoiceAsset,
    WordBoundaryAsset,
    hash_guion,
)
from app.core.manifest import Manifest
from app.core.stage_runner import StageRunner
from app.core.workspace import Workspace
from app.pipeline import voice
from tests.conftest import GUION_REFERENCIA, construir_guion

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_TTS") != "1",
    reason="prueba contra el Edge TTS real; se activa con RUN_REAL_TTS=1",
)

#: Voz medida en Gate 0.5. Es la única sobre la que hay medición.
VOZ = os.environ.get("TTS_VOICE", "es-CR-JuanNeural")


@pytest.fixture(scope="module")
def sintesis_real(tmp_path_factory):
    """Una sola síntesis real, compartida por todas las comprobaciones.

    Se sintetiza una vez y no una por test: cada llamada es tráfico contra un
    servicio ajeno.
    """
    destino = tmp_path_factory.mktemp("tts_real") / "narration.mp3"
    guion = construir_guion(uuid.uuid4())
    proveedor = ProveedorEdgeTTS(timeout_s=120)
    resultado = proveedor.sintetizar(guion, destino, ConfiguracionVoz(voz=VOZ))
    return proveedor, guion, resultado


def test_el_servicio_devuelve_audio(sintesis_real):
    _, _, resultado = sintesis_real
    assert resultado.audio_path.is_file()
    assert resultado.bytes_audio > 0
    assert resultado.audio_path.stat().st_size == resultado.bytes_audio


def test_el_servicio_devuelve_tiempos_por_palabra(sintesis_real):
    _, _, resultado = sintesis_real
    assert len(resultado.limites) > 10
    for anterior, siguiente in zip(resultado.limites, resultado.limites[1:]):
        assert siguiente.inicio_s >= anterior.inicio_s
        assert anterior.duracion_s > 0


def test_el_audio_real_es_decodificable_y_medible(sintesis_real):
    from app.adapters.media import comprobar_decodificable, inspeccionar_audio

    _, _, resultado = sintesis_real
    comprobar_decodificable(resultado.audio_path)
    info = inspeccionar_audio(resultado.audio_path)

    assert info.duracion_s > 0
    assert info.codec == "mp3"
    assert info.sample_rate_hz > 0
    assert info.canales >= 1


def test_el_audio_real_sigue_sonando_tras_el_ultimo_tiempo(sintesis_real):
    """La medición de Gate 0.5, repetida contra el servicio de hoy.

    Es la razón por la que la duración se mide sobre el archivo: usar el fin del
    último WordBoundary recortaría casi un segundo de narración.
    """
    from app.adapters.media import inspeccionar_audio

    _, _, resultado = sintesis_real
    medida = inspeccionar_audio(resultado.audio_path).duracion_s
    cola = medida - resultado.fin_ultimo_limite_s

    assert cola > 0, (
        f"sin cola: audio {medida:.2f}s, último tiempo "
        f"{resultado.fin_ultimo_limite_s:.2f}s"
    )
    print(
        f"\nEdge TTS real ({VOZ}): audio {medida:.2f}s, "
        f"último WordBoundary {resultado.fin_ultimo_limite_s:.2f}s, "
        f"cola {cola:.2f}s"
    )


def test_los_tiempos_reales_llegan_sin_puntuacion(sintesis_real):
    """Lo que obliga a construir el subtítulo desde el guion, no desde el TTS."""
    _, _, resultado = sintesis_real
    textos = [lim.texto for lim in resultado.limites]
    assert any("Sabías" == t for t in textos)
    assert not any("¿" in t or "¡" in t for t in textos)


def test_la_cadena_completa_con_el_proveedor_real(tmp_path):
    """AdaptedScript → Edge TTS → audio → tiempos → SRT → ASS, de verdad."""
    settings = _settings_real(tmp_path)
    ws = Workspace(settings.raiz_runs, uuid.uuid4())
    ws.crear()
    ws.escribir_artefacto(voice.ARTEFACTO_GUION, construir_guion(ws.run_id))

    manifest, _ = Manifest.cargar_o_crear(
        ws.dir, ws.run_id, config=settings.publico(), versions={}
    )
    runner = StageRunner(ws, manifest, settings)
    voz = runner.ejecutar(voice.ETAPA_VOZ).artefacto
    subtitulos = runner.ejecutar(voice.ETAPA_SUBTITULOS).artefacto

    # Artefactos
    assert isinstance(voz, VoiceAsset)
    assert voz.provider == "edge-tts" and voz.voice == VOZ
    assert voz.audio_duration_seconds > 0
    limites = ws.leer_artefacto(voice.ARTEFACTO_LIMITES, WordBoundaryAsset)
    assert limites.boundaries

    # Archivos físicos
    assert ws.ruta(voz.audio_path).stat().st_size > 0
    srt = ws.ruta(subtitulos.srt_path).read_text(encoding="utf-8")
    ass = ws.ruta(subtitulos.ass_path).read_text(encoding="utf-8")
    assert isinstance(subtitulos, SubtitleAsset) and subtitulos.cue_count > 0

    # El texto es el del guion, con sus signos
    assert voz.source_script_sha256 == hash_guion(GUION_REFERENCIA)
    for fragmento in ("¿Sabías", "¡Increíble!", "Muñoz", "empezar?"):
        assert fragmento in srt, f"falta {fragmento!r} en el SRT real"

    # El ASS usa centésimas
    for linea in ass.splitlines():
        if linea.startswith("Dialogue:"):
            assert len(linea.split(",")[1].split(".")[-1]) == 2

    # La duración de referencia es el audio medido
    assert subtitulos.duration_seconds == voz.audio_duration_seconds
    assert subtitulos.cues[-1].end_seconds <= voz.audio_duration_seconds

    metadata = manifest.etapa("voice").metadata
    print(
        f"\nCadena real: estimada {metadata['estimated_duration_seconds']}s, "
        f"real {metadata['actual_duration_seconds']}s, "
        f"delta {metadata['duration_delta_seconds']}s, "
        f"ratio {metadata['duration_ratio']}, "
        f"cola {metadata['audio_tail_seconds']}s, "
        f"cues {manifest.etapa('subtitles').metadata['cue_count']}"
    )


def test_la_idempotencia_no_gasta_una_segunda_sintesis(tmp_path):
    """Con el proveedor real, repetir una corrida no debe costar otra llamada."""
    settings = _settings_real(tmp_path)
    ws = Workspace(settings.raiz_runs, uuid.uuid4())
    ws.crear()
    ws.escribir_artefacto(voice.ARTEFACTO_GUION, construir_guion(ws.run_id))
    manifest, _ = Manifest.cargar_o_crear(
        ws.dir, ws.run_id, config=settings.publico(), versions={}
    )

    runner = StageRunner(ws, manifest, settings)
    primero = runner.ejecutar(voice.ETAPA_VOZ)
    marca = ws.ruta(primero.artefacto.audio_path).stat().st_mtime_ns

    segundo = StageRunner(ws, manifest, settings).ejecutar(voice.ETAPA_VOZ)
    assert segundo.omitida
    assert ws.ruta(segundo.artefacto.audio_path).stat().st_mtime_ns == marca


def _settings_real(tmp_path):
    from app.config.settings import Settings

    return Settings(
        raiz_proyecto=tmp_path,
        raiz_runs=tmp_path / "runs",
        raiz_mpt=tmp_path / "motor",
        tts_provider="edge",
        tts_voice=VOZ,
    )
