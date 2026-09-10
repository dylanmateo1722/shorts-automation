"""Reexporta la composición con FFmpeg, que ahora vive en ``app.adapters``.

El quemado de subtítulos se escribió y validó aquí en Gate 0.5. Gate 5 lo
trasladó a ``app/adapters/composicion.py`` para que el pipeline no dependa del
Proof of Concept, y lo extendió a varias capas ASS. Este archivo mantiene
funcionando al PoC tal como se aprobó.
"""

from __future__ import annotations

from pathlib import Path

from app.adapters.composicion import componer
from app.adapters.media import binario_ffmpeg  # noqa: F401


def quemar(
    *,
    entrada: Path,
    ass: Path,
    salida: Path,
    crf: int = 18,
    preset: str = "medium",
    timeout_s: int = 1800,
) -> Path:
    """Incrusta un único ASS en el vídeo y devuelve la ruta del resultado."""
    return componer(
        entrada=entrada, capas_ass=[ass], salida=salida,
        crf=crf, preset=preset, timeout_s=timeout_s,
    ).salida
