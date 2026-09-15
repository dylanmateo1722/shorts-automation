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

# Puntuación que marca una frontera natural para cortar un cue.
CIERRES_DE_FRASE = (",", ";", ":", ".", "?", "!", "…")

# Palabras que no pueden quedar al final de un cue porque ligan con lo que
# viene detrás: preposiciones, artículos, conjunciones y determinantes. Partir
# en «lo puso a | prueba» se lee mal en pantalla: la primera línea queda
# colgando a mitad de la construcción.
PALABRAS_LIGADAS = frozenset(
    """
    a ante bajo cabe con contra de desde durante en entre hacia hasta mediante
    para por según sin so sobre tras versus vía
    el la los las un una unos unas lo al del
    y e o u ni que pero porque aunque como cuando donde si mientras pues sino
    cuyo cuya cuyos cuyas cual cuales quien quienes
    su sus mi mis tu tus nuestro nuestra nuestros nuestras
    este esta estos estas ese esa esos esas aquel aquella aquellos aquellas
    cada todo toda todos todas otro otra otros otras
    no muy tan más menos
    """.split()
)

# Un corte tras puntuación solo se prefiere si aprovecha al menos esta parte
# del presupuesto. Sin el mínimo, una coma temprana produciría cues de dos
# palabras seguidos de uno abarrotado.
APROVECHAMIENTO_MINIMO = 0.55

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


def _texto_de(guion: str, spans: list[Span], bloque: list[int]) -> str:
    """Texto del guion que cubre un bloque de spans, con su puntuación."""
    inicio = _expandir_izquierda(guion, spans[bloque[0]].inicio_car)
    fin = _expandir_derecha(guion, spans[bloque[-1]].fin_car)
    return " ".join(guion[inicio:fin].split())


def _liga_con_lo_siguiente(texto: str) -> bool:
    """True si el texto acaba en una palabra que arrastra a la siguiente.

    Una palabra seguida de puntuación nunca liga: en «dije que no.» el «no»
    cierra la frase en lugar de apoyarse en lo que venga después.
    """
    limpio = texto.rstrip()
    if limpio.endswith(CIERRES_DE_FRASE):
        return False
    palabras = limpio.split()
    if not palabras:
        return False
    ultima = "".join(c for c in palabras[-1] if c not in PUNTUACION_IGNORABLE)
    return ultima.casefold() in PALABRAS_LIGADAS


def _rompe_unidad(antes: str, despues: str) -> bool:
    """True si cortar entre estos dos textos partiría una unidad inseparable.

    Dos heurísticas deterministas, sin reconocimiento de entidades:

    * dos palabras capitalizadas seguidas suelen ser un nombre propio
      («Instituto Vega»). Se exige que la primera no sea la inicial de la
      frase, que está capitalizada por ortografía y no por ser nombre;
    * un número nunca se separa de lo que cuantifica («240 voluntarios»).
    """
    izquierda, derecha = antes.split(), despues.split()
    if not izquierda or not derecha:
        return False
    ultima = "".join(c for c in izquierda[-1] if c not in PUNTUACION_IGNORABLE)
    primera = "".join(c for c in derecha[0] if c not in PUNTUACION_IGNORABLE)
    if not ultima or not primera:
        return False
    if any(c.isdigit() for c in ultima):
        return True
    return len(izquierda) > 1 and ultima[:1].isupper() and primera[:1].isupper()


def _mejor_corte(
    guion: str,
    spans: list[Span],
    pendiente: list[int],
    *,
    presupuesto: int,
    dur_max_s: float,
) -> tuple[int, bool]:
    """Elige dónde partir un grupo que no cabe en un solo cue.

    La prioridad es la de la decisión de subtitulado: puntuación primero,
    después cualquier frontera que no deje una preposición o un artículo
    colgando, y solo como último recurso el límite de caracteres. Entre cortes
    de la misma prioridad gana el más largo, que aprovecha mejor las dos líneas.

    Returns:
        (nº de spans que entran en el cue, si hubo que romper una ligadura)
    """
    # Hasta dónde se puede llegar sin pasarse de presupuesto ni de duración.
    limite = 1
    for n in range(1, len(pendiente) + 1):
        texto = _texto_de(guion, spans, pendiente[:n])
        duracion = spans[pendiente[n - 1]].fin_s - spans[pendiente[0]].inicio_s
        if n > 1 and (len(texto) > presupuesto or duracion > dur_max_s):
            break
        limite = n

    minimo = max(1, int(presupuesto * APROVECHAMIENTO_MINIMO))
    mejor: tuple[tuple[int, int], int] | None = None
    for n in range(1, limite + 1):
        texto = _texto_de(guion, spans, pendiente[:n])
        holgado = len(texto) >= minimo or n == limite
        resto = (
            _texto_de(guion, spans, pendiente[n:]) if n < len(pendiente) else ""
        )
        if _rompe_unidad(texto, resto):
            prioridad = 0
        elif texto.rstrip().endswith(CIERRES_DE_FRASE) and holgado:
            prioridad = 3
        elif not _liga_con_lo_siguiente(texto) and holgado:
            prioridad = 2
        elif not _liga_con_lo_siguiente(texto):
            prioridad = 1
        else:
            prioridad = 0
        clave = (prioridad, n)
        if mejor is None or clave > mejor[0]:
            mejor = (clave, n)

    assert mejor is not None  # limite >= 1 garantiza al menos un candidato
    return mejor[1], mejor[0][0] == 0


def _partir_lineas(texto: str, max_car: int) -> list[str]:
    """Reparte el texto en como mucho dos líneas.

    Entre los cortes posibles se prefiere el que no deja una preposición, un
    artículo o una conjunción al final de la primera línea; a igualdad de
    calidad, el más equilibrado. El salto de línea se lee igual que un corte de
    cue: «A las 12 semanas, el / 68 por ciento» obliga a saltar con la frase a
    medias.
    """
    if len(texto) <= max_car:
        return [texto]
    palabras = texto.split()
    mejor, mejor_clave = None, None
    for corte in range(1, len(palabras)):
        a = " ".join(palabras[:corte])
        b = " ".join(palabras[corte:])
        if len(a) > max_car or len(b) > max_car:
            continue
        # Menor es mejor: primero no partir una unidad inseparable, después no
        # dejar ligadura colgando, y por último que las líneas queden parejas.
        clave = (
            _rompe_unidad(a, b),
            _liga_con_lo_siguiente(a),
            abs(len(a) - len(b)),
        )
        if mejor_clave is None or clave < mejor_clave:
            mejor, mejor_clave = (a, b), clave
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

        # Partir buscando la frontera semántica, no el carácter 64.
        primer_bloque_del_grupo = len(bloques)
        pendiente = list(grupo)
        while pendiente:
            corte, ligadura_rota = _mejor_corte(
                guion, spans, pendiente,
                presupuesto=presupuesto, dur_max_s=dur_max_s,
            )
            bloques.append(pendiente[:corte])
            if ligadura_rota:
                incidencias.append(
                    f"cue partido tras una palabra que liga con la siguiente: "
                    f"{_texto_de(guion, spans, pendiente[:corte])[-32:]!r}"
                )
            pendiente = pendiente[corte:]
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
