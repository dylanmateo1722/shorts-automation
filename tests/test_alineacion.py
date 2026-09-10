"""Alineación entre los tiempos del TTS y el texto canónico del guion.

Todos los casos corren contra eventos ``WordBoundary`` **reales** de
``es-CR-JuanNeural``, grabados en Gate 0.5. Es lo que hace fiable esta suite:
prueban la conducta que tiene el proveedor de verdad, sin red y sin
credenciales.

La regla que se comprueba una y otra vez: los tiempos vienen del TTS, el texto
viene del guion. Construir el subtítulo con el texto de los eventos perdería la
puntuación —empezando por los ``¿`` y ``¡`` del español— y agruparía tokens
como el propio motor los agrupa.
"""

from __future__ import annotations

import pytest

from app.subtitles import segmenter
from tests.conftest import DURACION_REFERENCIA_S, GUION_REFERENCIA


@pytest.fixture
def cues(limites_reales):
    resultado, _ = segmenter.segmentar(GUION_REFERENCIA, limites_reales)
    return resultado


@pytest.fixture
def texto_unido(cues):
    return " ".join(c.texto for c in cues)


# ---------------------------------------------------------------------------
# El guion es la fuente textual canónica
# ---------------------------------------------------------------------------


def test_el_texto_sale_del_guion_y_no_del_evento(limites_reales, texto_unido):
    """Edge TTS emite 'Sabías'; el guion dice '¿Sabías'. Gana el guion."""
    textos_de_eventos = {e["text"] for e in limites_reales}
    assert "Sabías" in textos_de_eventos
    assert "¿Sabías" not in textos_de_eventos
    assert "¿Sabías" in texto_unido


@pytest.mark.parametrize("signo", ["¿", "?", "¡", "!"])
def test_ningun_signo_de_apertura_o_cierre_se_pierde(signo, texto_unido):
    assert texto_unido.count(signo) == GUION_REFERENCIA.count(signo)


@pytest.mark.parametrize("signo", [",", "."])
def test_la_puntuacion_interna_se_conserva(signo, texto_unido):
    assert texto_unido.count(signo) >= GUION_REFERENCIA.count(signo)


@pytest.mark.parametrize(
    "palabra", ["Sabías", "señora", "Muñoz", "técnicas", "español", "método", "Increíble"]
)
def test_acentos_enes_y_mayusculas_intactos(palabra, texto_unido):
    assert palabra in texto_unido


def test_no_se_inventan_palabras(texto_unido):
    """Toda palabra del subtítulo tiene que estar en el guion."""
    def normaliza(texto: str) -> set[str]:
        limpio = "".join(
            " " if c in segmenter.PUNTUACION_IGNORABLE else c for c in texto
        )
        return {p.casefold() for p in limpio.split() if p}

    assert normaliza(texto_unido) <= normaliza(GUION_REFERENCIA)


def test_no_se_pierden_palabras_en_silencio(texto_unido):
    """Y toda palabra del guion tiene que estar en el subtítulo."""
    def normaliza(texto: str) -> set[str]:
        limpio = "".join(
            " " if c in segmenter.PUNTUACION_IGNORABLE else c for c in texto
        )
        return {p.casefold() for p in limpio.split() if p}

    faltan = normaliza(GUION_REFERENCIA) - normaliza(texto_unido)
    assert not faltan, f"palabras perdidas: {sorted(faltan)}"


# ---------------------------------------------------------------------------
# Diferencias menores entre evento y guion
# ---------------------------------------------------------------------------


def test_los_numeros_agrupados_por_el_tts_se_alinean(limites_reales, texto_unido):
    """Edge TTS emite '73 %' y '3 segundos' como un solo evento."""
    agrupados = {e["text"] for e in limites_reales if " " in e["text"]}
    assert {"73 %", "3 segundos"} <= agrupados
    for fragmento in ("73 %", "3 segundos", "5 técnicas"):
        assert fragmento in texto_unido


def test_la_alineacion_tolera_diferencias_de_mayusculas():
    guion = "MAYÚSCULAS y minúsculas conviven."
    eventos = [
        {"offset": 0, "duration": 5_000_000, "text": "mayúsculas"},
        {"offset": 5_000_000, "duration": 2_000_000, "text": "Y"},
        {"offset": 7_000_000, "duration": 5_000_000, "text": "MINÚSCULAS"},
        {"offset": 12_000_000, "duration": 5_000_000, "text": "conviven"},
    ]
    spans, descartados = segmenter.alinear(guion, eventos)
    assert descartados == 0
    assert len(spans) == 4


def test_la_alineacion_tolera_espacios_y_puntuacion_sobrantes():
    guion = "Hola,   ¿qué tal?  Bien."
    eventos = [
        {"offset": 0, "duration": 3_000_000, "text": "Hola"},
        {"offset": 3_000_000, "duration": 2_000_000, "text": "qué"},
        {"offset": 5_000_000, "duration": 2_000_000, "text": "tal"},
        {"offset": 7_000_000, "duration": 3_000_000, "text": "Bien"},
    ]
    spans, descartados = segmenter.alinear(guion, eventos)
    assert descartados == 0
    assert len(spans) == 4


def test_un_evento_que_no_casa_se_descarta_sin_desincronizar_el_resto():
    """No se adivina: el evento intruso se cuenta y los demás siguen alineados."""
    guion = "Uno dos tres."
    eventos = [
        {"offset": 0, "duration": 2_000_000, "text": "Uno"},
        {"offset": 2_000_000, "duration": 2_000_000, "text": "veinticuatro"},
        {"offset": 4_000_000, "duration": 2_000_000, "text": "dos"},
        {"offset": 6_000_000, "duration": 2_000_000, "text": "tres"},
    ]
    spans, descartados = segmenter.alinear(guion, eventos)
    assert descartados == 1
    assert len(spans) == 3
    assert [guion[s.inicio_car:s.fin_car] for s in spans] == ["Uno", "dos", "tres"]


def test_un_desajuste_grave_se_detecta_y_no_se_disimula():
    """Tiempos de otro guion: casi todo se descarta, y eso es visible."""
    guion = "Un texto completamente distinto del que se narró."
    eventos = [
        {"offset": i * 2_000_000, "duration": 2_000_000, "text": palabra}
        for i, palabra in enumerate(["banana", "helicóptero", "wolframio", "arpegio"])
    ]
    spans, descartados = segmenter.alinear(guion, eventos)
    assert descartados == len(eventos)
    assert spans == []


def test_los_incidentes_de_alineacion_se_reportan_no_se_ocultan():
    guion = "Uno dos tres."
    eventos = [
        {"offset": 0, "duration": 2_000_000, "text": "Uno"},
        {"offset": 2_000_000, "duration": 2_000_000, "text": "intruso"},
        {"offset": 4_000_000, "duration": 2_000_000, "text": "dos"},
        {"offset": 6_000_000, "duration": 2_000_000, "text": "tres"},
    ]
    _, incidencias = segmenter.segmentar(guion, eventos)
    assert any("descartados" in i for i in incidencias)


# ---------------------------------------------------------------------------
# Preguntas y exclamaciones
# ---------------------------------------------------------------------------


def test_las_preguntas_conservan_apertura_y_cierre(cues):
    """Una pregunta puede repartirse entre cues, pero ningún signo desaparece."""
    completo = " ".join(c.texto for c in cues)
    assert completo.count("¿") == completo.count("?") == 2
    assert "¿Sabías" in completo and "segundos?" in completo


def test_la_exclamacion_llega_entera(cues):
    assert any("¡Increíble!" in c.texto for c in cues)


def test_una_pregunta_corta_no_se_parte():
    guion = "¿Listo para empezar?"
    eventos = [
        {"offset": 0, "duration": 3_000_000, "text": "Listo"},
        {"offset": 3_000_000, "duration": 2_000_000, "text": "para"},
        {"offset": 5_000_000, "duration": 4_000_000, "text": "empezar"},
    ]
    cues, _ = segmenter.segmentar(guion, eventos)
    assert len(cues) == 1
    assert cues[0].texto == "¿Listo para empezar?"


def test_una_pregunta_partida_reparte_los_signos_correctamente():
    """Si no cabe, se parte; los signos quedan donde el español los pide."""
    guion = (
        "¿Sabes por qué la mayoría de los espectadores abandona un video corto "
        "durante los primeros instantes de reproducción continua?"
    )
    palabras = guion.replace("¿", "").replace("?", "").split()
    eventos = [
        {"offset": i * 6_000_000, "duration": 5_500_000, "text": p}
        for i, p in enumerate(palabras)
    ]
    cues, _ = segmenter.segmentar(guion, eventos)
    unido = " ".join(c.texto for c in cues)

    assert len(cues) > 1, "el caso solo prueba algo si la pregunta se parte"
    assert cues[0].texto.startswith("¿")
    assert cues[-1].texto.endswith("?")
    assert unido.count("¿") == unido.count("?") == 1
    assert all(c.fragmento_atomico for c in cues)


# ---------------------------------------------------------------------------
# Los tiempos son los del TTS
# ---------------------------------------------------------------------------


def test_los_tiempos_de_los_cues_vienen_de_los_eventos(cues, limites_reales):
    primero = limites_reales[0]
    ultimo = limites_reales[-1]
    assert cues[0].inicio_s == pytest.approx(
        primero["offset"] / segmenter.TICKS_POR_SEGUNDO, abs=1e-6
    )
    assert cues[-1].fin_s == pytest.approx(
        (ultimo["offset"] + ultimo["duration"]) / segmenter.TICKS_POR_SEGUNDO, abs=1e-6
    )


def test_el_ultimo_cue_termina_antes_del_final_del_audio(cues):
    """Regresión de Gate 0.5: hay cola de audio tras el último WordBoundary.

    El subtitulado no la cubre, y no debe: rellenar hasta el final alargaría el
    último cue sobre un tramo en el que ya no se dice nada.
    """
    assert cues[-1].fin_s < DURACION_REFERENCIA_S
    cola = DURACION_REFERENCIA_S - cues[-1].fin_s
    assert 0.5 < cola < 1.5, f"cola inesperada de {cola:.2f}s"
