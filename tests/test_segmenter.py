"""Pruebas del segmentador de subtítulos en español.

Usan eventos WordBoundary reales grabados de ``es-CR-JuanNeural``, así que
corren sin red y sin credenciales: en CI validan la lógica aunque el servicio
de TTS no esté disponible.
"""

from __future__ import annotations

import json
from pathlib import Path

from poc import segmenter, srt

FIXTURE = Path(__file__).parent / "fixtures" / "wordboundaries_es_cr.json"

GUION = (
    "¿Sabías que el 73 % de los videos cortos se abandonan antes de los 3 segundos? "
    "¡Increíble! En este video te explico, paso a paso, cómo diseñar un gancho que retenga "
    "a tu audiencia. Analizaremos 5 técnicas comprobadas con ejemplos reales de canales en "
    "español. La señora Muñoz, experta en marketing digital, comparte además su método "
    "favorito. ¿Listo para empezar?"
)
DURACION_AUDIO_S = 30.29


def _cues():
    eventos = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cues, _ = segmenter.segmentar(GUION, eventos)
    return cues


def test_alineacion_conserva_puntuacion():
    """El texto sale del guion, no del evento, así que mantiene los signos."""
    unido = " ".join(c.texto for c in _cues())
    for signo in "¿¡?!":
        assert unido.count(signo) == GUION.count(signo), f"se perdió {signo!r}"


def test_numeros_agrupados_por_tts_se_alinean():
    """Edge TTS emite '73 %' y '3 segundos' como un solo evento."""
    unido = " ".join(c.texto for c in _cues())
    for fragmento in ("73 %", "3 segundos", "5 técnicas"):
        assert fragmento in unido


def test_acentos_y_ene_intactos():
    unido = " ".join(c.texto for c in _cues())
    for palabra in ("Sabías", "señora", "Muñoz", "técnicas", "español", "método"):
        assert palabra in unido


def test_reglas_de_subtitulado():
    cues = _cues()
    validacion = srt.validar(cues, DURACION_AUDIO_S, guion=GUION)
    errores = [c for c in validacion.comprobaciones if c["nivel"] == "error" and not c["ok"]]
    assert not errores, validacion.resumen()


def test_srt_valido():
    """El SRT debe tener índice, marca y texto por bloque, en orden."""
    texto = srt.a_srt(_cues())
    bloques = [b for b in texto.split("\n\n") if b.strip()]
    assert bloques
    for indice, bloque in enumerate(bloques, start=1):
        lineas = bloque.splitlines()
        assert lineas[0] == str(indice)
        assert " --> " in lineas[1]
        assert len(lineas) >= 3


def test_marca_ass_usa_centesimas():
    """Regresión: el ASS usa H:MM:SS.cc, no milisegundos.

    Derivarlo del SRT producía '0:00:03.610' y desincronizaba el subtitulado
    entero al quemarlo con libass.
    """
    assert srt._marca_ass(3.61) == "0:00:03.61"
    assert srt._marca_ass(65.019) == "0:01:05.02"
    for linea in srt.a_ass(_cues()).splitlines():
        if linea.startswith("Dialogue:"):
            inicio = linea.split(",")[1]
            assert len(inicio.split(".")[-1]) == 2, f"marca ASS inválida: {inicio}"
