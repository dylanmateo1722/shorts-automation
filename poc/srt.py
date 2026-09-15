"""Subtítulos del Proof of Concept.

La serialización a SRT/ASS y las reglas de subtitulado se escribieron aquí en
Gate 0.5 y Gate 3 las trasladó a ``app.subtitles`` para que el pipeline no
dependa del Proof of Concept. Este módulo las reexporta y conserva lo que sí
es exclusivo del PoC: la placa de título.
"""

from __future__ import annotations

from app.subtitles.reglas import ResultadoValidacion, validar  # noqa: F401
from app.subtitles.serializers import (  # noqa: F401
    _escapar_ass,
    _marca,
    _marca_ass,
    a_ass,
    a_srt,
)


def ass_titulo(
    texto: str,
    duracion_s: float,
    *,
    ancho: int = 1080,
    alto: int = 1920,
    fuente: str = "DejaVu Sans",
    tamano: int = 84,
) -> str:
    """Genera un ASS con un rótulo centrado, para placas de título.

    Existe porque el FFmpeg que empaqueta ``imageio-ffmpeg`` **no incluye el
    filtro ``drawtext``**, aunque sí trae libass. Todo el texto sobre video
    debe pasar por ASS; comprobado sobre el binario real
    ``ffmpeg-linux-x86_64-v7.0.2``.
    """
    cabecera = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {ancho}
PlayResY: {alto}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Titulo,{fuente},{tamano},&H00FFFFFF,&H00000000,&H00C4442F,-1,0,0,0,100,100,2,0,3,18,0,5,80,80,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    fin = _marca_ass(duracion_s)
    linea = "\\N".join(_escapar_ass(p) for p in texto.split("\n"))
    return cabecera + f"Dialogue: 0,0:00:00.00,{fin},Titulo,,0,0,0,,{linea}\n"
