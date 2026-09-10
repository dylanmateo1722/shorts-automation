"""Fixtures compartidas de la suite."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.adapters.media import binario_ffmpeg
from app.config.settings import Settings
from app.core.workspace import Workspace

RAIZ_TESTS = Path(__file__).resolve().parent


@pytest.fixture(scope="session")
def mp4_minimo(tmp_path_factory) -> Path:
    """Un MP4 real y diminuto, generado una sola vez con FFmpeg."""
    destino = tmp_path_factory.mktemp("fixtures") / "minimo.mp4"
    subprocess.run(
        [binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:size=1080x1920:rate=30:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(destino)],
        check=True, timeout=300,
    )
    return destino


@pytest.fixture
def motor_falso(tmp_path, mp4_minimo, monkeypatch) -> Path:
    """Directorio que imita la disposición de MoneyPrinterTurbo.

    Contiene ``cli.py`` y un ``.venv/bin/python`` que apunta al intérprete
    actual, de modo que el adaptador ejecute un subproceso real sin cambiar
    ni una línea de su lógica.
    """
    raiz = tmp_path / "motor"
    (raiz / ".venv" / "bin").mkdir(parents=True)
    shutil.copy2(RAIZ_TESTS / "motor_falso" / "cli.py", raiz / "cli.py")
    (raiz / ".venv" / "bin" / "python").symlink_to(sys.executable)
    monkeypatch.setenv("FAKE_MPT_FIXTURE", str(mp4_minimo))
    monkeypatch.setenv("FAKE_MPT_MODE", "ok")
    return raiz


@pytest.fixture
def settings_falsos(tmp_path, motor_falso) -> Settings:
    """Configuración apuntando al motor falso y a un runs/ temporal."""
    return Settings(
        raiz_proyecto=tmp_path,
        raiz_runs=tmp_path / "runs",
        raiz_mpt=motor_falso,
        mpt_timeout_s=120,
        log_level="INFO",
    )


@pytest.fixture
def workspace(settings_falsos) -> Workspace:
    ws = Workspace(settings_falsos.raiz_runs, uuid.uuid4())
    ws.crear()
    return ws


@pytest.fixture
def entrada_controlada(workspace) -> dict:
    """Audio y material reales dentro del directorio de la corrida."""
    entrada = workspace.dir / "input"
    entrada.mkdir(parents=True, exist_ok=True)
    audio = entrada / "audio.mp3"
    material = entrada / "material.mp4"
    subprocess.run(
        [binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=1",
         "-c:a", "libmp3lame", str(audio)],
        check=True, timeout=300,
    )
    subprocess.run(
        [binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=blue:size=320x240:rate=30:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(material)],
        check=True, timeout=300,
    )
    return {"audio": audio, "material": material}


def ultima_invocacion(motor: Path) -> dict:
    """Argumentos con los que se llamó al motor falso."""
    return json.loads((motor / "ultima_invocacion.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Voz y subtítulos
# ---------------------------------------------------------------------------

#: Guion de referencia de Gate 0.5. Es exactamente el texto que se narró para
#: grabar ``fixtures/wordboundaries_es_cr.json``, así que esos tiempos reales
#: de Edge TTS sirven para probar la alineación sin red y sin credenciales.
GUION_REFERENCIA = (
    "¿Sabías que el 73 % de los videos cortos se abandonan antes de los 3 segundos? "
    "¡Increíble! En este video te explico, paso a paso, cómo diseñar un gancho que retenga "
    "a tu audiencia. Analizaremos 5 técnicas comprobadas con ejemplos reales de canales en "
    "español. La señora Muñoz, experta en marketing digital, comparte además su método "
    "favorito. ¿Listo para empezar?"
)

#: Duración real de ese MP3, medida en Gate 0.5. El último WordBoundary termina
#: antes: hay casi un segundo de cola de audio.
DURACION_REFERENCIA_S = 30.29


@pytest.fixture
def limites_reales() -> list[dict]:
    """Eventos WordBoundary auténticos de ``es-CR-JuanNeural``."""
    ruta = RAIZ_TESTS / "fixtures" / "wordboundaries_es_cr.json"
    return json.loads(ruta.read_text(encoding="utf-8"))


def construir_guion(run_id: uuid.UUID, texto: str = GUION_REFERENCIA):
    """AdaptedScript mínimo y válido a partir de un texto."""
    from app.contracts.models import (
        AdaptedScript,
        ProcedenciaIA,
        SeccionGuion,
        TipoSeccion,
    )
    from app.pipeline import validacion

    return AdaptedScript(
        run_id=run_id,
        language="es",
        hook=texto.split(".")[0].strip() or texto,
        sections=[SeccionGuion(kind=TipoSeccion.hook, text=texto, order=0)],
        full_text=texto,
        target_duration_seconds=30.0,
        estimated_duration_seconds=validacion.estimar_duracion_s(texto, 119),
        provenance=ProcedenciaIA(provider="fake", model="fake-1", prompt_version="v1"),
    )


@pytest.fixture
def settings_voz(tmp_path) -> Settings:
    """Configuración con el proveedor de TTS falso y un runs/ temporal."""
    return Settings(
        raiz_proyecto=tmp_path,
        raiz_runs=tmp_path / "runs",
        raiz_mpt=tmp_path / "motor",
        log_level="INFO",
        tts_provider="fake",
        tts_voice="es-CR-JuanNeural",
    )


@pytest.fixture
def workspace_voz(settings_voz) -> Workspace:
    """Directorio de corrida con un guion adaptado ya escrito."""
    ws = Workspace(settings_voz.raiz_runs, uuid.uuid4())
    ws.crear()
    ws.escribir_artefacto("adapted_script", construir_guion(ws.run_id))
    return ws


# ---------------------------------------------------------------------------
# Render de extremo a extremo
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def mp4_vertical(tmp_path_factory) -> Path:
    """MP4 vertical real con vídeo y audio, para probar la composición suelta.

    1080×1920, H.264 y AAC: las propiedades que la QA final exige, para que los
    tests las comprueben sobre un archivo de verdad y no sobre una simulación.
    """
    return _mp4_vertical(tmp_path_factory.mktemp("fixtures") / "vertical.mp4", 3.0)


@pytest.fixture(scope="session")
def mp4_sin_audio(tmp_path_factory) -> Path:
    """MP4 vertical sin pista de audio, para el camino de fallo de la QA."""
    destino = tmp_path_factory.mktemp("fixtures") / "mudo.mp4"
    subprocess.run(
        [binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:size=1080x1920:rate=30:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(destino)],
        check=True, timeout=600,
    )
    return destino


@pytest.fixture(scope="session")
def motor_falso_compartido(tmp_path_factory, mp4_vertical) -> Path:
    """Motor falso reutilizable por toda la sesión.

    Igual que ``motor_falso`` pero sin depender de fixtures por test, para que
    la corrida base se pueda construir una sola vez.
    """
    raiz = tmp_path_factory.mktemp("motor_sesion")
    (raiz / ".venv" / "bin").mkdir(parents=True)
    shutil.copy2(RAIZ_TESTS / "motor_falso" / "cli.py", raiz / "cli.py")
    (raiz / ".venv" / "bin" / "python").symlink_to(sys.executable)
    return raiz


def _settings_e2e(raiz: Path, motor: Path) -> Settings:
    return Settings(
        raiz_proyecto=raiz,
        raiz_runs=raiz / "runs",
        raiz_mpt=motor,
        mpt_timeout_s=300,
        log_level="INFO",
        tts_provider="fake",
        tts_voice="es-CR-JuanNeural",
    )


#: Guion corto para el extremo a extremo. Lleva los signos del español y un
#: número agrupado, que es lo que hace interesante el subtitulado, pero dura
#: unos segundos: cada test recodifica el vídeo entero y un guion de treinta
#: segundos multiplicaría ese coste sin probar nada distinto.
GUION_CORTO = "¿Sabías que el 73 % abandona antes de tres segundos? ¡Increíble!"


def _mp4_vertical(destino: Path, duracion_s: float) -> Path:
    """MP4 vertical real con vídeo H.264 y audio AAC."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi",
         "-i", f"color=c=0x101820:size=1080x1920:rate=30:duration={duracion_s:.3f}",
         "-f", "lavfi", "-i", f"sine=frequency=220:duration={duracion_s:.3f}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(destino)],
        check=True, timeout=600,
    )
    return destino


@pytest.fixture(scope="session")
def corrida_base(tmp_path_factory, motor_falso_compartido) -> dict:
    """Una corrida con todo lo de Gate 1 a Gate 4 ya hecho, construida una vez.

    Generar el material visual de 1080×1920 cuesta segundos de codificación
    reales. Hacerlo por test multiplicaría ese coste por cuarenta sin probar
    nada nuevo, así que se construye una vez y cada test trabaja sobre su copia.

    El MP4 del motor falso se genera **con la duración de la narración**, que es
    lo que hace el motor real: devolver un vídeo del largo del audio. Fijarlo a
    ciegas haría fallar la comprobación de sincronía por culpa de la fixture.
    """
    import os

    from app.adapters.tts import ProveedorTTSFalso
    from app.contracts.models import VoiceAsset
    from app.core.manifest import Manifest
    from app.core.stage_runner import StageRunner
    from app.pipeline import qa as modulo_qa
    from app.pipeline import render, voice

    raiz = tmp_path_factory.mktemp("corrida_base")
    settings = _settings_e2e(raiz, motor_falso_compartido)
    ws = Workspace(settings.raiz_runs, uuid.uuid4())
    ws.crear()
    ws.escribir_artefacto(voice.ARTEFACTO_GUION, construir_guion(ws.run_id, GUION_CORTO))

    manifest, _ = Manifest.cargar_o_crear(
        ws.dir, ws.run_id, config=settings.publico(), versions={}
    )
    runner = StageRunner(
        ws, manifest, settings, {voice.CLAVE_PROVEEDOR_TTS: ProveedorTTSFalso()}
    )
    for etapa in voice.construir_pipeline_voz():
        runner.ejecutar(etapa)

    voz = ws.leer_artefacto(voice.ARTEFACTO_VOZ, VoiceAsset)
    fixture = _mp4_vertical(raiz / "motor_fixture.mp4", voz.audio_duration_seconds)

    previo = os.environ.get("FAKE_MPT_FIXTURE")
    os.environ["FAKE_MPT_FIXTURE"] = str(fixture)
    try:
        runner.ejecutar(render.ETAPA_MATERIAL)
        for etapa in modulo_qa.construir_pipeline_transformacion():
            runner.ejecutar(etapa)
    finally:
        if previo is None:
            os.environ.pop("FAKE_MPT_FIXTURE", None)
        else:
            os.environ["FAKE_MPT_FIXTURE"] = previo

    return {"dir": ws.dir, "run_id": ws.run_id, "fixture": fixture}


@pytest.fixture
def settings_e2e(tmp_path, motor_falso_compartido, corrida_base, monkeypatch) -> Settings:
    """Configuración con TTS falso y el motor falso que respeta el CLI real."""
    monkeypatch.setenv("FAKE_MPT_FIXTURE", str(corrida_base["fixture"]))
    monkeypatch.setenv("FAKE_MPT_MODE", "ok")
    return _settings_e2e(tmp_path, motor_falso_compartido)


@pytest.fixture
def workspace_e2e(tmp_path, settings_e2e, corrida_base) -> Workspace:
    """Copia privada de la corrida base, aislada para cada test."""
    destino = settings_e2e.raiz_runs / corrida_base["dir"].name
    destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(corrida_base["dir"], destino)
    return Workspace(settings_e2e.raiz_runs, corrida_base["run_id"])
