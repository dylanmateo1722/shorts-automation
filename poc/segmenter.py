"""Alineación de WordBoundary y segmentación de subtítulos en español.

El problema que resuelve este módulo:

Edge TTS emite eventos ``WordBoundary`` con tiempos reales por palabra, pero
el texto del evento **viene sin puntuación** y a veces **agrupa varios tokens**
del guion en un solo evento. Comprobado contra la salida real de
``es-CR-JuanNeural``:

    text='Sabías'      (el guion decía "¿Sabías")
    text='73 %'        (un evento, dos tokens del guion)
    text='3 segundos'  (idem)

Por eso no se puede construir el subtítulo a partir del texto de los eventos.
La estrategia es la inversa: se alinea cada evento contra el **guion original**
y se conserva el texto del guion, que sí tiene los acentos, las comas y —lo
importante para el español— los signos de apertura ``¿`` y ``¡``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Signos que Edge TTS omite en el texto del evento y que por tanto deben
# saltarse al alinear. No se eliminan del guion: solo se ignoran al comparar.
PUNTUACION_IGNORABLE = set("¿¡?!.,;:\"“”‘’'()[]—–…\n\t")

# Apertura y cierre que nunca deben quedar separados en cues distintos.
APERTURAS = {"¿": "?", "¡": "!"}

TICKS_POR_SEGUNDO = 10_000_000  # Edge TTS mide en unidades de 100 ns.


@dataclass
class Span:
    """Un evento de Edge TTS ya anclado a un tramo del guion original."""

    inicio_car: int
    fin_car: int
    inicio_s: float
    fin_s: float


@dataclass
class Cue:
    """Un subtítulo listo para serializar."""

    inicio_s: float
    fin_s: float
    texto: str
    lineas: list[str] = field(default_factory=list)
    # True cuando el cue es un fragmento de una pregunta o exclamación que no
    # cabía entera. Permite al validador distinguir "partido" de "perdido".
    fragmento_atomico: bool = False


def alinear(guion: str, eventos: list[dict]) -> tuple[list[Span], int]:
    """Ancla cada evento de Edge TTS a un tramo de caracteres del guion.

    Recorre guion y evento con dos punteros, saltando en el guion los signos
    que el evento no trae. Un evento que no case limpiamente se descarta en
    lugar de desincronizar el resto.

    Returns:
        (spans, descartados)
    """
    spans: list[Span] = []
    descartados = 0
    cursor = 0

    for evento in eventos:
        objetivo = str(evento.get("text") or "")
        if not objetivo.strip():
            descartados += 1
            continue

        i = cursor
        while i < len(guion) and (guion[i].isspace() or guion[i] in PUNTUACION_IGNORABLE):
            i += 1
        inicio = i
        j = 0

        while j < len(objetivo) and i < len(guion):
            if objetivo[j].isspace():
                j += 1
                continue
            if guion[i].isspace() or guion[i] in PUNTUACION_IGNORABLE:
                i += 1
                continue
            if guion[i].casefold() == objetivo[j].casefold():
                i += 1
                j += 1
            else:
                break

        if j < len(objetivo):
            # El evento no corresponde al guion en esta posición (por ejemplo,
            # un número verbalizado). Se descarta sin mover el cursor.
            descartados += 1
            continue

        spans.append(
            Span(
                inicio_car=inicio,
                fin_car=i,
                inicio_s=evento["offset"] / TICKS_POR_SEGUNDO,
                fin_s=(evento["offset"] + evento["duration"]) / TICKS_POR_SEGUNDO,
            )
        )
        cursor = i

    return spans, descartados


def _expandir_izquierda(guion: str, pos: int) -> int:
    """Incluye los signos de apertura que preceden al tramo."""
    while pos > 0 and guion[pos - 1] in "¿¡\"“«(":
        pos -= 1
    return pos


def _expandir_derecha(guion: str, pos: int) -> int:
    """Incluye la puntuación de cierre que sigue al tramo."""
    while pos < len(guion) and guion[pos] in "?!.,;:\"”»)…":
        pos += 1
    return pos


def _grupos_atomicos(guion: str, spans: list[Span]) -> list[list[int]]:
    """Agrupa spans que no pueden separarse: preguntas y exclamaciones enteras.

    Una pregunta como ``¿Sabías que ... segundos?`` se trata como una unidad.
    Es la regla que el motor de MoneyPrinterTurbo incumple: su segmentador
    parte por comas y descarta los signos, produciendo cues como ``"¿Qué tal"``.
    """
    grupos: list[list[int]] = []
    actual: list[int] = []
    cierre_pendiente: str | None = None

    for idx, span in enumerate(spans):
        actual.append(idx)
        tramo = guion[_expandir_izquierda(guion, span.inicio_car) : _expandir_derecha(guion, span.fin_car)]

        if cierre_pendiente is None:
            for apertura, cierre in APERTURAS.items():
                if apertura in tramo and cierre not in tramo.split(apertura, 1)[1]:
                    cierre_pendiente = cierre
                    break

        if cierre_pendiente is not None:
            if cierre_pendiente in tramo:
                cierre_pendiente = None
                grupos.append(actual)
                actual = []
            continue

        # Fin de oración normal.
        if any(t in tramo for t in ".?!"):
            grupos.append(actual)
            actual = []

    if actual:
        grupos.append(actual)
    return grupos


def _partir_lineas(texto: str, max_car: int) -> list[str]:
    """Reparte el texto en como mucho dos líneas equilibradas."""
    if len(texto) <= max_car:
        return [texto]
    palabras = texto.split()
    mejor, mejor_dif = None, None
    for corte in range(1, len(palabras)):
        a = " ".join(palabras[:corte])
        b = " ".join(palabras[corte:])
        if len(a) > max_car or len(b) > max_car:
            continue
        dif = abs(len(a) - len(b))
        if mejor_dif is None or dif < mejor_dif:
            mejor, mejor_dif = (a, b), dif
    if mejor:
        return list(mejor)
    # No hay corte que respete el límite: se parte por la mitad de palabras.
    mitad = max(1, len(palabras) // 2)
    return [" ".join(palabras[:mitad]), " ".join(palabras[mitad:])]


def segmentar(
    guion: str,
    eventos: list[dict],
    *,
    max_car_linea: int = 32,
    max_lineas: int = 2,
    techo_car_atomico: int = 84,
    dur_min_s: float = 0.7,
    dur_max_s: float = 3.5,
    hueco_min_s: float = 0.08,
) -> tuple[list[Cue], list[str]]:
    """Convierte eventos de Edge TTS en cues de subtítulo en español.

    Returns:
        (cues, incidencias). ``incidencias`` recoge los casos en los que una
        regla no pudo cumplirse; se reportan en lugar de aplicarse en silencio.
    """
    incidencias: list[str] = []
    spans, descartados = alinear(guion, eventos)
    if descartados:
        incidencias.append(f"{descartados} evento(s) de TTS descartados por no casar con el guion")
    if not spans:
        return [], incidencias

    presupuesto = max_car_linea * max_lineas
    grupos = _grupos_atomicos(guion, spans)
    bloques: list[list[int]] = []
    fragmentados: set[int] = set()

    for grupo in grupos:
        inicio_car = _expandir_izquierda(guion, spans[grupo[0]].inicio_car)
        fin_car = _expandir_derecha(guion, spans[grupo[-1]].fin_car)
        texto = " ".join(guion[inicio_car:fin_car].split())
        duracion = spans[grupo[-1]].fin_s - spans[grupo[0]].inicio_s

        if len(texto) <= presupuesto and duracion <= dur_max_s:
            bloques.append(grupo)
            continue

        # El grupo no cabe. Si es atómico (pregunta o exclamación) se permite
        # sobrepasar el presupuesto hasta el techo antes de partirlo.
        es_atomico = any(a in texto for a in APERTURAS)
        if es_atomico and len(texto) <= techo_car_atomico and duracion <= dur_max_s * 1.6:
            bloques.append(grupo)
            continue
        if es_atomico:
            incidencias.append(
                f"grupo atómico partido por exceder {techo_car_atomico} caracteres: {texto[:48]!r}…"
            )

        # Partir por presupuesto, cortando preferentemente tras coma.
        primer_bloque_del_grupo = len(bloques)
        actual: list[int] = []
        for idx in grupo:
            candidato = actual + [idx]
            ini = _expandir_izquierda(guion, spans[candidato[0]].inicio_car)
            fin = _expandir_derecha(guion, spans[candidato[-1]].fin_car)
            txt = " ".join(guion[ini:fin].split())
            dur = spans[candidato[-1]].fin_s - spans[candidato[0]].inicio_s
            if actual and (len(txt) > presupuesto or dur > dur_max_s):
                bloques.append(actual)
                actual = [idx]
            else:
                actual = candidato
        if actual:
            bloques.append(actual)
        if es_atomico:
            fragmentados.update(range(primer_bloque_del_grupo, len(bloques)))

    # Materializar cues.
    cues: list[Cue] = []
    for posicion, bloque in enumerate(bloques):
        ini_car = _expandir_izquierda(guion, spans[bloque[0]].inicio_car)
        fin_car = _expandir_derecha(guion, spans[bloque[-1]].fin_car)
        texto = " ".join(guion[ini_car:fin_car].split())
        cues.append(
            Cue(
                spans[bloque[0]].inicio_s,
                spans[bloque[-1]].fin_s,
                texto,
                fragmento_atomico=posicion in fragmentados,
            )
        )

    # Fusionar cues por debajo de la duración mínima con su vecino más corto.
    i = 0
    while i < len(cues):
        if cues[i].fin_s - cues[i].inicio_s >= dur_min_s or len(cues) == 1:
            i += 1
            continue
        anterior = cues[i - 1] if i > 0 else None
        siguiente = cues[i + 1] if i + 1 < len(cues) else None
        objetivo = anterior if siguiente is None else (
            siguiente if anterior is None
            else (anterior if len(anterior.texto) <= len(siguiente.texto) else siguiente)
        )
        if objetivo is anterior:
            anterior.texto = f"{anterior.texto} {cues[i].texto}".strip()
            anterior.fin_s = cues[i].fin_s
            anterior.fragmento_atomico = anterior.fragmento_atomico or cues[i].fragmento_atomico
            cues.pop(i)
            i = max(0, i - 1)
        else:
            siguiente.texto = f"{cues[i].texto} {siguiente.texto}".strip()
            siguiente.inicio_s = cues[i].inicio_s
            siguiente.fragmento_atomico = siguiente.fragmento_atomico or cues[i].fragmento_atomico
            cues.pop(i)

    # Cerrar huecos mínimos y calcular líneas.
    for idx, cue in enumerate(cues):
        if idx + 1 < len(cues):
            hueco = cues[idx + 1].inicio_s - cue.fin_s
            if 0 < hueco < hueco_min_s:
                cue.fin_s = cues[idx + 1].inicio_s
        cue.lineas = _partir_lineas(cue.texto, max_car_linea)
        if len(cue.lineas) > max_lineas:
            incidencias.append(f"cue con más de {max_lineas} líneas: {cue.texto[:48]!r}…")

    return cues, incidencias
