"""Reglas de subtitulado, serialización a SRT y derivación del ASS.

El SRT es el formato canónico y el ASS se deriva de los mismos cues. Aquí se
comprueba que las dos salidas dicen lo mismo y que el ASS usa las unidades que
libass entiende, que es donde Gate 0.5 encontró el defecto más caro.
"""

from __future__ import annotations

import pytest

from app.contracts.models import CARACTERES_OBJETIVO_LINEA, MAX_LINEAS_SUBTITULO
from app.subtitles import reglas, segmenter, serializers
from tests.conftest import DURACION_REFERENCIA_S, GUION_REFERENCIA


@pytest.fixture
def cues(limites_reales):
    resultado, _ = segmenter.segmentar(
        GUION_REFERENCIA,
        limites_reales,
        max_car_linea=CARACTERES_OBJETIVO_LINEA,
        max_lineas=MAX_LINEAS_SUBTITULO,
    )
    return resultado


# ---------------------------------------------------------------------------
# Reglas de los cues
# ---------------------------------------------------------------------------


def test_las_reglas_se_cumplen_sobre_la_salida_real(cues):
    validacion = reglas.validar(cues, DURACION_REFERENCIA_S, guion=GUION_REFERENCIA)
    assert validacion.ok, validacion.resumen()


def test_ningun_cue_pasa_de_dos_lineas(cues):
    for cue in cues:
        assert len(cue.lineas) <= MAX_LINEAS_SUBTITULO


def test_ninguna_linea_pasa_del_objetivo_de_caracteres(cues):
    """32 caracteres es heurística, pero sobre este guion se respeta entera."""
    for cue in cues:
        for linea in cue.lineas:
            assert len(linea) <= CARACTERES_OBJETIVO_LINEA, linea


def test_ningun_cue_baja_de_la_duracion_minima(cues):
    for indice, cue in enumerate(cues, start=1):
        assert cue.fin_s - cue.inicio_s >= 0.7, f"cue {indice} demasiado corto"


def test_los_cues_van_en_orden_y_no_se_solapan(cues):
    for anterior, siguiente in zip(cues, cues[1:]):
        assert anterior.fin_s <= siguiente.inicio_s + 1e-6


def test_no_hay_tiempos_negativos(cues):
    assert all(c.inicio_s >= 0 and c.fin_s > c.inicio_s for c in cues)


def test_ningun_cue_se_sale_del_audio(cues):
    assert cues[-1].fin_s <= DURACION_REFERENCIA_S


def test_los_cues_demasiado_cortos_se_fusionan_con_su_vecino():
    """Un evento suelto de 0,2 s no puede quedarse como cue propio."""
    guion = "Sí. Continuamos con la explicación completa del asunto."
    eventos = [
        {"offset": 0, "duration": 2_000_000, "text": "Sí"},
        {"offset": 2_000_000, "duration": 8_000_000, "text": "Continuamos"},
        {"offset": 10_000_000, "duration": 3_000_000, "text": "con"},
        {"offset": 13_000_000, "duration": 2_000_000, "text": "la"},
        {"offset": 15_000_000, "duration": 8_000_000, "text": "explicación"},
    ]
    cues, _ = segmenter.segmentar(guion, eventos, dur_min_s=0.7)
    assert all(c.fin_s - c.inicio_s >= 0.7 for c in cues)


def test_una_regla_incumplida_se_reporta_con_su_nombre():
    """Un booleano opaco no diría qué falló."""
    solapados = [
        segmenter.Cue(0.0, 2.0, "Primero", ["Primero"]),
        segmenter.Cue(1.0, 3.0, "Segundo", ["Segundo"]),
    ]
    validacion = reglas.validar(solapados, 5.0, guion="Primero Segundo")
    assert not validacion.ok
    assert any("solapamiento" in c["nombre"] for c in validacion.errores)


def test_perder_un_signo_invalida_el_subtitulado():
    """Es el defecto exacto que se verificó en MoneyPrinterTurbo."""
    mutilados = [segmenter.Cue(0.0, 2.0, "¿Qué tal", ["¿Qué tal"])]
    validacion = reglas.validar(mutilados, 5.0, guion="¿Qué tal?")
    assert not validacion.ok
    assert any("puntuación" in c["nombre"] for c in validacion.errores)


def test_una_linea_larga_avisa_pero_no_invalida():
    """32 caracteres es una heurística de legibilidad, no una ley."""
    largo = "Una línea deliberadamente más larga que el objetivo heurístico"
    validacion = reglas.validar(
        [segmenter.Cue(0.0, 3.0, largo, [largo])], 5.0, guion=largo
    )
    assert validacion.ok
    assert any("caracteres por línea" in c["nombre"] for c in validacion.avisos)


# ---------------------------------------------------------------------------
# Fronteras semánticas
# ---------------------------------------------------------------------------


def test_no_se_parte_dejando_una_preposicion_colgando():
    guion = (
        "Un equipo del Instituto Vega lo puso a prueba durante doce semanas "
        "seguidas con voluntarios de varias ciudades del país."
    )
    palabras = guion.replace(".", "").split()
    eventos = [
        {"offset": i * 5_000_000, "duration": 4_500_000, "text": p}
        for i, p in enumerate(palabras)
    ]
    cues, _ = segmenter.segmentar(guion, eventos)
    assert len(cues) > 1
    for cue in cues[:-1]:
        ultima = cue.texto.split()[-1].strip(".,;:").casefold()
        assert ultima not in segmenter.PALABRAS_LIGADAS, cue.texto


def test_no_se_parte_un_nombre_propio_entre_lineas():
    lineas = segmenter._partir_lineas(
        "Un equipo del Instituto Vega lo puso a prueba", CARACTERES_OBJETIVO_LINEA
    )
    assert len(lineas) == 2
    assert not (lineas[0].endswith("Instituto") and lineas[1].startswith("Vega"))


def test_no_se_separa_un_numero_de_lo_que_cuantifica():
    lineas = segmenter._partir_lineas(
        "Dieron a 240 voluntarios una tarea diaria", CARACTERES_OBJETIVO_LINEA
    )
    assert not lineas[0].rstrip().endswith("240")


def test_se_prefiere_cortar_tras_la_puntuacion():
    lineas = segmenter._partir_lineas(
        "Los datos son prometedores, no concluyentes", CARACTERES_OBJETIVO_LINEA
    )
    assert lineas[0].endswith(",")


# ---------------------------------------------------------------------------
# SRT
# ---------------------------------------------------------------------------


def test_el_srt_tiene_indice_marca_y_texto_en_orden(cues):
    texto = serializers.a_srt(cues)
    bloques = [b for b in texto.split("\n\n") if b.strip()]
    assert len(bloques) == len(cues)
    for indice, bloque in enumerate(bloques, start=1):
        lineas = bloque.splitlines()
        assert lineas[0] == str(indice)
        assert " --> " in lineas[1]
        assert len(lineas) >= 3


def test_el_srt_usa_milisegundos_con_tres_digitos():
    assert serializers._marca(2.34) == "00:00:02,340"
    assert serializers._marca(3661.5) == "01:01:01,500"


def test_el_srt_nunca_escribe_tiempos_negativos():
    assert serializers._marca(-1.0) == "00:00:00,000"


def test_el_redondeo_del_srt_es_determinista():
    assert serializers._marca(1.0005) == serializers._marca(1.0005)
    assert serializers._marca(1.9999) == "00:00:02,000"


def test_el_srt_conserva_el_texto_completo_del_guion(cues):
    srt = serializers.a_srt(cues)
    for palabra in ("¿Sabías", "73 %", "Muñoz", "¡Increíble!", "empezar?"):
        assert palabra in srt


# ---------------------------------------------------------------------------
# ASS
# ---------------------------------------------------------------------------


def test_la_marca_ass_usa_centesimas_no_milisegundos():
    """Regresión de Gate 0.5.

    Derivar la marca del ASS desde la del SRT producía ``0:00:03.610``. libass
    no lo lee como 3,61 s y desincronizaba el subtitulado entero; se comprobó
    sobre el video renderizado, no sobre papel.
    """
    assert serializers._marca_ass(3.61) == "0:00:03.61"
    assert serializers._marca_ass(65.019) == "0:01:05.02"
    assert serializers._marca_ass(0.0) == "0:00:00.00"
    assert serializers._marca_ass(3600.0) == "1:00:00.00"
    # El defecto concreto: tres dígitos tras el punto.
    assert serializers._marca_ass(3.61) != "0:00:03.610"


def test_toda_marca_del_ass_lleva_exactamente_dos_decimales(cues):
    for linea in serializers.a_ass(cues).splitlines():
        if linea.startswith("Dialogue:"):
            inicio, fin = linea.split(",")[1], linea.split(",")[2]
            assert len(inicio.split(".")[-1]) == 2, inicio
            assert len(fin.split(".")[-1]) == 2, fin


def test_el_ass_nunca_escribe_tiempos_negativos():
    assert serializers._marca_ass(-2.0) == "0:00:00.00"


def test_el_ass_lleva_un_estilo_legible_para_vertical(cues):
    ass = serializers.a_ass(cues, margen_vertical=420, margen_horizontal=60)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "[V4+ Styles]" in ass
    # Alineación 2 (abajo centrado) con el margen que sube el texto sobre la
    # franja que tapa la interfaz de YouTube.
    assert ",2,60,60,420,1" in ass


def test_los_margenes_del_ass_son_configurables(cues):
    ass = serializers.a_ass(cues, margen_vertical=260, margen_horizontal=90)
    assert ",2,90,90,260,1" in ass


def test_el_ass_marca_el_salto_de_linea_con_su_propia_secuencia(cues):
    multilinea = [c for c in cues if len(c.lineas) == 2]
    assert multilinea, "el guion de referencia debería producir cues de dos líneas"
    ass = serializers.a_ass(cues)
    assert "\\N" in ass


def test_el_ass_escapa_las_llaves():
    """Sin escapar, ``{`` abriría una etiqueta de estilo dentro del texto."""
    cue = segmenter.Cue(0.0, 2.0, "Llaves {raras}", ["Llaves {raras}"])
    assert "\\{raras\\}" in serializers.a_ass([cue])


def test_el_ass_y_el_srt_dicen_lo_mismo(cues):
    """El ASS se deriva de los mismos cues: no es una segunda fuente de verdad."""
    srt = serializers.a_srt(cues)
    ass = serializers.a_ass(cues)
    for cue in cues:
        for linea in cue.lineas:
            assert linea in srt
            assert linea in ass


def test_el_ass_se_puede_regenerar_desde_el_contrato(cues):
    """El artefacto conserva el reparto en líneas, así que el ASS es reproducible."""
    from app.contracts.models import SubtitleCue

    contrato = [
        SubtitleCue(
            index=i, start_seconds=c.inicio_s, end_seconds=c.fin_s,
            text="\n".join(c.lineas),
        )
        for i, c in enumerate(cues, start=1)
    ]
    assert serializers.a_ass(serializers.desde_contrato(contrato)) == serializers.a_ass(cues)
