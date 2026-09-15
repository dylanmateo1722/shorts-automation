"""Reglas de subtitulado en español, comprobadas una por una.

Cada regla se evalúa por separado para que un fallo diga exactamente qué se
incumplió, en lugar de devolver un booleano opaco. Las reglas de nivel
``error`` invalidan el subtitulado; las de nivel ``aviso`` se registran y no
bloquean.

Aprobadas en la decisión D4 y validadas en Gate 0.5.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.subtitles.segmenter import APERTURAS, Cue


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

    @property
    def errores(self) -> list[dict]:
        return [c for c in self.comprobaciones if c["nivel"] == "error" and not c["ok"]]

    @property
    def avisos(self) -> list[dict]:
        return [c for c in self.comprobaciones if c["nivel"] == "aviso" and not c["ok"]]


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
    """Comprueba las reglas de subtitulado aprobadas en D4."""
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

    negativos = [f"cue {i}" for i, c in enumerate(cues, start=1) if c.inicio_s < 0]
    añadir("sin tiempos negativos", not negativos, "ninguno" if not negativos else "; ".join(negativos))

    # Límites de línea. Los 32 caracteres son una heurística de legibilidad, no
    # una garantía: pasarse es un aviso, no invalida el subtitulado.
    excedidos = [
        f"cue {i} línea de {max(len(x) for x in (c.lineas or [c.texto]))} car."
        for i, c in enumerate(cues, start=1)
        if max(len(x) for x in (c.lineas or [c.texto])) > max_car_linea
    ]
    añadir(
        f"máximo {max_car_linea} caracteres por línea",
        not excedidos,
        "dentro del límite" if not excedidos else "; ".join(excedidos),
        nivel="aviso",
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

    # El último cue no puede exceder la duración real del audio.
    fin = cues[-1].fin_s
    añadir(
        "último cue dentro del audio",
        fin <= duracion_audio_s + tolerancia_final_s,
        f"último cue termina en {fin:.2f}s, audio dura {duracion_audio_s:.2f}s",
    )

    return ResultadoValidacion(
        all(c["ok"] for c in comprobaciones if c["nivel"] == "error"), comprobaciones
    )
