"""El rótulo y el subtítulo no pueden decir lo mismo a la vez.

Defecto real, visto en un render: durante los tres segundos del rótulo la
narración está diciendo el gancho, así que el subtítulo repetía debajo, palabra
por palabra, lo que el rótulo ya enseñaba arriba y en grande.

El caso que da sentido al archivo es
``test_un_cue_que_cruza_el_final_de_la_ventana_tambien_se_quita``: el primer
intento de arreglo exigía que el cue estuviera contenido **entero** en la
ventana, y como el rótulo dura un tiempo fijo y la narración lo que dure, el cue
siempre la cruzaba por unas décimas. No quitaba nada y el defecto seguía ahí.
"""

from __future__ import annotations

from app.subtitles.serializers import sin_cues_dentro_de

CABECERA = """[Script Info]
ScriptType: v4.00+

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass(*cues: tuple[str, str, str]) -> str:
    filas = [
        f"Dialogue: 0,{inicio},{fin},Base,,0,0,0,,{texto}"
        for inicio, fin, texto in cues
    ]
    return CABECERA + "\n".join(filas) + "\n"


def _dialogos(ass: str) -> list[str]:
    return [l for l in ass.splitlines() if l.startswith("Dialogue")]


def test_un_cue_bajo_el_rotulo_se_quita():
    ass = _ass(("0:00:00.10", "0:00:02.50", "el gancho"))
    assert _dialogos(sin_cues_dentro_de(ass, 0.0, 3.0)) == []


def test_un_cue_que_cruza_el_final_de_la_ventana_tambien_se_quita():
    """El caso real medido: cue 0,10–3,49 s contra un rótulo de 3,00 s.

    Su punto medio (1,795 s) cae bajo el rótulo, así que el texto es suyo. Con
    el criterio de contención completa este cue se conservaba y el defecto no se
    corregía nunca.
    """
    ass = _ass(("0:00:00.10", "0:00:03.49", "En 1983, un hombre desobedeció"))
    assert _dialogos(sin_cues_dentro_de(ass, 0.0, 3.0)) == []


def test_un_cue_que_solo_roza_el_principio_se_conserva():
    """Su punto medio cae fuera: lo que narra ya no lo muestra el rótulo, así
    que quitarlo dejaría sin subtítulo palabras que sí se están diciendo."""
    ass = _ass(("0:00:02.97", "0:00:06.40", "una orden y evitó una guerra"))
    assert len(_dialogos(sin_cues_dentro_de(ass, 0.0, 3.0))) == 1


def test_los_cues_posteriores_no_se_tocan():
    ass = _ass(
        ("0:00:00.10", "0:00:02.50", "el gancho"),
        ("0:00:04.00", "0:00:06.00", "lo que sigue"),
        ("0:00:06.00", "0:00:08.00", "y lo demás"),
    )
    conservados = _dialogos(sin_cues_dentro_de(ass, 0.0, 3.0))
    assert len(conservados) == 2
    assert "lo que sigue" in conservados[0]


def test_la_cabecera_del_ass_se_conserva():
    """Sin cabecera libass no sabe ni la resolución ni los estilos."""
    salida = sin_cues_dentro_de(_ass(("0:00:00.10", "0:00:02.50", "x")), 0.0, 3.0)
    assert salida.startswith("[Script Info]")
    assert "Format: Layer, Start, End" in salida


def test_una_ventana_vacia_no_quita_nada():
    """Sin rótulo no hay duplicación que evitar."""
    ass = _ass(("0:00:00.10", "0:00:02.50", "x"))
    assert sin_cues_dentro_de(ass, 0.0, 0.0) == ass
    assert sin_cues_dentro_de(ass, 3.0, 1.0) == ass


def test_los_minutos_y_las_horas_se_interpretan():
    """Un Short no llega al minuto, pero el parseo no debe asumirlo."""
    ass = _ass(("0:01:10.00", "0:01:12.00", "tarde"))
    assert _dialogos(sin_cues_dentro_de(ass, 69.0, 73.0)) == []
    assert len(_dialogos(sin_cues_dentro_de(ass, 0.0, 3.0))) == 1


def test_el_artefacto_de_subtitulos_sigue_completo():
    """La función devuelve texto y no toca nada.

    El SRT y el ASS de la corrida son la transcripción completa: lo que se
    recorta es solo la copia que se quema.
    """
    original = _ass(
        ("0:00:00.10", "0:00:02.50", "el gancho"),
        ("0:00:04.00", "0:00:06.00", "lo que sigue"),
    )
    copia = original
    sin_cues_dentro_de(original, 0.0, 3.0)
    assert original == copia
