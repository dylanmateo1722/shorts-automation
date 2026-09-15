"""Alineación, segmentación y serialización de subtítulos en español.

El código de este paquete se validó en el Proof of Concept de Gate 0.5 contra
la salida real de Edge TTS y se trasladó aquí sin reescribirlo: Gate 3 lo
reutiliza tal cual en lugar de mantener una segunda implementación paralela.
``poc/`` sigue funcionando porque reexporta desde aquí.
"""

from app.subtitles.reglas import ResultadoValidacion, validar  # noqa: F401
from app.subtitles.segmenter import (  # noqa: F401
    APERTURAS,
    PUNTUACION_IGNORABLE,
    TICKS_POR_SEGUNDO,
    Cue,
    Span,
    alinear,
    segmentar,
)
from app.subtitles.serializers import a_ass, a_srt  # noqa: F401
