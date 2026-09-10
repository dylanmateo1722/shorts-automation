"""Quemado de subtítulos propios con FFmpeg.

MPT no acepta un SRT externo, así que renderiza sin subtítulos y nosotros los
incrustamos después. El coste es una segunda pasada de codificación; se usa un
CRF alto de calidad porque este es el artefacto final.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def binario_ffmpeg() -> str:
    """Resuelve el binario de FFmpeg.

    Prioriza el del sistema y cae al que empaqueta ``imageio-ffmpeg``, que es
    el mismo mecanismo de resolución que usa MoneyPrinterTurbo.
    """
    import shutil

    del_sistema = shutil.which("ffmpeg")
    if del_sistema:
        return del_sistema
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def quemar(
    *,
    entrada: Path,
    ass: Path,
    salida: Path,
    crf: int = 18,
    preset: str = "medium",
    timeout_s: int = 1800,
) -> Path:
    """Incrusta el ASS en el video y devuelve la ruta del resultado.

    El audio se copia sin recodificar: la segunda pasada solo debe afectar al
    video, no degradar la narración.
    """
    salida.parent.mkdir(parents=True, exist_ok=True)
    # El filtro ass necesita la ruta escapada porque ':' y '\' son separadores
    # dentro de la descripción del filtro.
    ruta_filtro = str(ass.resolve()).replace("\\", "/").replace(":", r"\:")
    argv = [
        binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(entrada.resolve()),
        "-vf", f"ass='{ruta_filtro}'",
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(salida.resolve()),
    ]
    proceso = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    if proceso.returncode != 0:
        raise RuntimeError(
            f"FFmpeg falló al quemar subtítulos: {(proceso.stderr or '').strip()[-400:]}"
        )
    if not salida.exists() or salida.stat().st_size == 0:
        raise RuntimeError("FFmpeg terminó con éxito pero no produjo archivo")
    return salida
