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
