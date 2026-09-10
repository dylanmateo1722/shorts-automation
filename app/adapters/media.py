"""Inspección de archivos de medios con FFmpeg.

Se usa ``ffmpeg -i`` y no ``ffprobe`` porque el binario que empaqueta
``imageio-ffmpeg`` no incluye ffprobe, y no queremos exigir un FFmpeg de
sistema para poder ejecutar en un runner limpio.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import DependenciaAusente, TiempoAgotado


def binario_ffmpeg() -> str:
    """Resuelve el binario de FFmpeg: primero el del sistema, luego el empaquetado."""
    del_sistema = shutil.which("ffmpeg")
    if del_sistema:
        return del_sistema
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise DependenciaAusente(
            "no hay FFmpeg en el PATH ni el paquete imageio-ffmpeg instalado"
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


@dataclass(frozen=True)
class InfoMedia:
    duracion_s: float
    ancho: int
    alto: int
    fps: float
    codec_video: str
    codec_audio: str

    @property
    def tiene_audio(self) -> bool:
        return bool(self.codec_audio)


def inspeccionar(path: Path, *, timeout_s: int = 300) -> InfoMedia:
    """Lee resolución, fps, duración y códecs de un archivo de medios."""
    try:
        proceso = subprocess.run(
            [binario_ffmpeg(), "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise TiempoAgotado(f"FFmpeg no respondió al inspeccionar {path}") from exc

    salida = (proceso.stderr or "") + (proceso.stdout or "")

    duracion = 0.0
    if m := re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", salida):
        duracion = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    ancho = alto = 0
    codec_video = ""
    if m := re.search(r"Stream #\d+:\d+.*?: Video: (\w+).*?, (\d+)x(\d+)", salida, re.S):
        codec_video, ancho, alto = m.group(1), int(m.group(2)), int(m.group(3))

    fps = 0.0
    if m := re.search(r"(\d+(?:\.\d+)?) fps", salida):
        fps = float(m.group(1))

    codec_audio = ""
    if m := re.search(r"Stream #\d+:\d+.*?: Audio: (\w+)", salida):
        codec_audio = m.group(1)

    return InfoMedia(duracion, ancho, alto, fps, codec_video, codec_audio)
