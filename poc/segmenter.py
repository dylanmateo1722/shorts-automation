"""Reexporta el segmentador de subtítulos, que ahora vive en ``app.subtitles``.

El módulo se escribió y validó aquí en Gate 0.5. Gate 3 lo trasladó a
``app/subtitles/segmenter.py`` para que el pipeline no dependa del Proof of
Concept, sin reescribir ni una línea. Este archivo mantiene funcionando al PoC
tal como se aprobó.
"""

from __future__ import annotations

from app.subtitles.segmenter import (  # noqa: F401
    APERTURAS,
    PUNTUACION_IGNORABLE,
    TICKS_POR_SEGUNDO,
    Cue,
    Span,
    _expandir_derecha,
    _expandir_izquierda,
    _grupos_atomicos,
    _partir_lineas,
    alinear,
    segmentar,
)
