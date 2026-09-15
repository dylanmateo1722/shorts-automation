"""Serialización de subtítulos a SRT y a ASS.

El SRT es el formato canónico: se archiva, se audita y se puede subir a
YouTube como pista de subtítulos. El ASS se deriva de los mismos cues solo
para el burn-in, porque lleva fuente, tamaño y posición exactos; nunca es una
segunda fuente de verdad.

Validado en Gate 0.5 sobre video renderizado de verdad.
"""

from __future__ import annotations

from app.subtitles.segmenter import Cue


def _marca(segundos: float) -> str:
    """Formatea un tiempo en la marca ``HH:MM:SS,mmm`` del formato SRT."""
    if segundos < 0:
        segundos = 0.0
    ms_totales = int(round(segundos * 1000))
    horas, resto = divmod(ms_totales, 3_600_000)
    minutos, resto = divmod(resto, 60_000)
    seg, ms = divmod(resto, 1000)
    return f"{horas:02d}:{minutos:02d}:{seg:02d},{ms:03d}"


def _marca_ass(segundos: float) -> str:
    """Formatea un tiempo en la marca ``H:MM:SS.cc`` del formato ASS.

    ASS usa **centésimas de segundo con dos dígitos**, no milisegundos. Derivar
    la marca desde la del SRT produce ``0:00:03.610``, que libass no interpreta
    como 3,61 s y desincroniza todo el subtitulado. Comprobado sobre el video
    renderizado en Gate 0.5: los cues aparecían con varios segundos de retraso.
    """
    if segundos < 0:
        segundos = 0.0
    centesimas_totales = int(round(segundos * 100))
    horas, resto = divmod(centesimas_totales, 360_000)
    minutos, resto = divmod(resto, 6_000)
    seg, cs = divmod(resto, 100)
    return f"{horas:d}:{minutos:02d}:{seg:02d}.{cs:02d}"


def desde_contrato(cues) -> list[Cue]:
    """Convierte ``SubtitleCue`` del contrato en los cues del segmentador.

    El salto de línea del subtítulo viaja dentro de ``SubtitleCue.text``, así
    que el reparto en líneas no se recalcula aquí: se lee. Es lo que permite
    que SRT y ASS salgan del mismo artefacto sin poder discrepar.
    """
    return [
        Cue(
            inicio_s=cue.start_seconds,
            fin_s=cue.end_seconds,
            texto=" ".join(cue.text.split()),
            lineas=cue.text.split("\n"),
        )
        for cue in cues
    ]


def a_srt(cues: list[Cue]) -> str:
    """Serializa los cues al formato SRT."""
    bloques = []
    for indice, cue in enumerate(cues, start=1):
        lineas = cue.lineas or [cue.texto]
        bloques.append(
            f"{indice}\n{_marca(cue.inicio_s)} --> {_marca(cue.fin_s)}\n" + "\n".join(lineas)
        )
    return "\n\n".join(bloques) + "\n"


def _escapar_ass(texto: str) -> str:
    return texto.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def a_ass(
    cues: list[Cue],
    *,
    ancho: int = 1080,
    alto: int = 1920,
    fuente: str = "DejaVu Sans",
    tamano: int = 64,
    margen_vertical: int = 420,
    margen_horizontal: int = 60,
) -> str:
    """Deriva un ASS con estilo explícito para el burn-in.

    Los márgenes son configurables y sus valores por defecto son **heurísticos**:
    ``margen_vertical`` sube los subtítulos sobre el borde inferior porque en
    Shorts esa franja la tapa la interfaz de YouTube (título, canal,
    descripción), pero la altura exacta de esa franja está pendiente de
    medición sobre la app real. No se asume que el fondo del video sea zona
    segura; el estilo definitivo se decide con inspección visual en Gate 5.
    """
    cabecera = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {ancho}
PlayResY: {alto}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Base,{fuente},{tamano},&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,0,2,{margen_horizontal},{margen_horizontal},{margen_vertical},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    filas = []
    for cue in cues:
        texto = "\\N".join(_escapar_ass(linea) for linea in (cue.lineas or [cue.texto]))
        filas.append(
            f"Dialogue: 0,{_marca_ass(cue.inicio_s)},"
            f"{_marca_ass(cue.fin_s)},Base,,0,0,0,,{texto}"
        )
    return cabecera + "\n".join(filas) + "\n"
