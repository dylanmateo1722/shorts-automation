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
