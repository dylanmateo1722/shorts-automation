"""Serialización y validación de subtítulos SRT.

El SRT es el artefacto canónico: se archiva, se audita y se puede subir a
YouTube como pista de subtítulos. El ASS se deriva de él solo para el burn-in,
porque lleva fuente, tamaño y posición exactos.
"""

from __future__ import annotations

from dataclasses import dataclass

from .segmenter import APERTURAS, Cue


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
    renderizado: los cues aparecían con varios segundos de retraso.
    """
    if segundos < 0:
        segundos = 0.0
    centesimas_totales = int(round(segundos * 100))
    horas, resto = divmod(centesimas_totales, 360_000)
    minutos, resto = divmod(resto, 6_000)
    seg, cs = divmod(resto, 100)
    return f"{horas:d}:{minutos:02d}:{seg:02d}.{cs:02d}"


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
) -> str:
    """Deriva un ASS con estilo explícito para el burn-in.

    ``margen_vertical`` sube los subtítulos sobre el borde inferior, que en
    Shorts queda tapado por la interfaz de YouTube (título, canal, descripción).
    El valor exacto de esa franja está pendiente de medición sobre la app real;
    aquí se deja parametrizado, no fijado como verdad.
    """
    cabecera = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {ancho}
PlayResY: {alto}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Base,{fuente},{tamano},&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,0,2,60,60,{margen_vertical},1

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


@dataclass
class ResultadoValidacion:
    """Resultado de validar un conjunto de cues contra las reglas de D4."""

    ok: bool
    comprobaciones: list[dict]

    def resumen(self) -> str:
        etiqueta = {
            (True, "error"): "OK   ", (False, "error"): "FALLA",
            (True, "aviso"): "OK   ", (False, "aviso"): "AVISO",
        }
        return "\n".join(
            f"  [{etiqueta[(c['ok'], c['nivel'])]}] {c['nombre']}: {c['detalle']}"
            for c in self.comprobaciones
        )


def validar(
    cues: list[Cue],
    duracion_audio_s: float,
    guion: str = "",
    *,
    max_car_linea: int = 32,
    max_lineas: int = 2,
    dur_min_s: float = 0.7,
    tolerancia_final_s: float = 0.25,
) -> ResultadoValidacion:
    """Comprueba las reglas de subtitulado aprobadas en D4.

    Cada regla se evalúa por separado para que un fallo diga exactamente qué
    se incumplió, en lugar de devolver un booleano opaco.
    """
    comprobaciones: list[dict] = []

    def añadir(nombre: str, ok: bool, detalle: str, nivel: str = "error") -> None:
        comprobaciones.append(
            {"nombre": nombre, "ok": bool(ok), "detalle": detalle, "nivel": nivel}
        )

    añadir("hay cues", bool(cues), f"{len(cues)} cue(s) generados")
    if not cues:
        return ResultadoValidacion(False, comprobaciones)

    # (1) ERROR — ningún signo puede perderse. Este es exactamente el defecto
    # verificado en MoneyPrinterTurbo, que producía cues como "¿Qué tal" sin
    # el signo de cierre.
    unido = " ".join(c.texto for c in cues)
    perdidos = []
    for signo in "¿¡?!.,":
        esperado, obtenido = guion.count(signo), unido.count(signo)
        if obtenido < esperado:
            perdidos.append(f"{signo!r}: {obtenido} de {esperado}")
    añadir(
        "puntuación preservada",
        not perdidos,
        "ningún signo perdido" if not perdidos else "; ".join(perdidos),
    )

    # (2) AVISO — una pregunta o exclamación repartida entre cues consecutivos
    # conserva ambos signos y es práctica normal de subtitulado. Solo es un
    # defecto si el fragmento NO viene de una partición controlada.
    huerfanos, partidas = [], []
    for indice, cue in enumerate(cues, start=1):
        for apertura, cierre in APERTURAS.items():
            if cue.texto.count(apertura) != cue.texto.count(cierre):
                (partidas if cue.fragmento_atomico else huerfanos).append(
                    f"cue {indice}: {cue.texto[:40]!r}"
                )
    añadir(
        "signos ¿? y ¡! sin huérfanos inesperados",
        not huerfanos,
        "ninguno" if not huerfanos else "; ".join(huerfanos),
    )
    añadir(
        "preguntas y exclamaciones en un solo cue",
        not partidas,
        "todas completas" if not partidas
        else f"{len(partidas)} partida(s) por no caber en 2 líneas: " + "; ".join(partidas),
        nivel="aviso",
    )

    # Duración mínima.
    cortos = [
        f"cue {i} = {cue.fin_s - cue.inicio_s:.2f}s"
        for i, cue in enumerate(cues, start=1)
        if cue.fin_s - cue.inicio_s < dur_min_s
    ]
    añadir(
        f"ningún cue menor de {dur_min_s}s",
        not cortos,
        "todos por encima del mínimo" if not cortos else "; ".join(cortos),
    )

    # Solapamientos y orden.
    solapes = [
        f"cue {i} empieza en {cues[i].inicio_s:.2f}s antes de que termine el {i} en {cues[i-1].fin_s:.2f}s"
        for i in range(1, len(cues))
        if cues[i].inicio_s < cues[i - 1].fin_s - 1e-6
    ]
    añadir("sin solapamientos", not solapes, "ninguno" if not solapes else "; ".join(solapes))

    invertidos = [f"cue {i}" for i, c in enumerate(cues, start=1) if c.fin_s <= c.inicio_s]
    añadir("tiempos crecientes", not invertidos, "correctos" if not invertidos else "; ".join(invertidos))

    # Límites de línea.
    excedidos = [
        f"cue {i} línea de {max(len(l) for l in (c.lineas or [c.texto]))} car."
        for i, c in enumerate(cues, start=1)
        if max(len(l) for l in (c.lineas or [c.texto])) > max_car_linea
    ]
    añadir(
        f"máximo {max_car_linea} caracteres por línea",
        not excedidos,
        "dentro del límite" if not excedidos else "; ".join(excedidos),
    )

    multilinea = [
        f"cue {i} con {len(c.lineas)} líneas" for i, c in enumerate(cues, start=1)
        if len(c.lineas or [c.texto]) > max_lineas
    ]
    añadir(
        f"máximo {max_lineas} líneas",
        not multilinea,
        "dentro del límite" if not multilinea else "; ".join(multilinea),
    )

    # El último cue no puede exceder la duración del audio.
    fin = cues[-1].fin_s
    añadir(
        "último cue dentro del audio",
        fin <= duracion_audio_s + tolerancia_final_s,
        f"último cue termina en {fin:.2f}s, audio dura {duracion_audio_s:.2f}s",
    )

    return ResultadoValidacion(
        all(c["ok"] for c in comprobaciones if c["nivel"] == "error"), comprobaciones
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
