"""Validaciones lingüísticas, contra fixtures creados para esta prueba.

Se comprueban propiedades objetivas —cifras que sobreviven, idioma, signos,
longitud— y no calidad editorial, que no es medible automáticamente.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline import validacion as v

CASOS = json.loads(
    (Path(__file__).parent / "fixtures" / "casos_linguisticos.json").read_text("utf-8")
)


@pytest.mark.parametrize("nombre", sorted(CASOS))
def test_los_casos_traducidos_pasan_la_validacion(nombre):
    """Una traducción correcta de cada categoría no debe producir errores."""
    caso = CASOS[nombre]
    informe = v.validar_traduccion(caso["en"], caso["es"], "es")
    assert informe.ok, f"{caso['descripcion']}:\n{informe.resumen()}"


def test_perder_una_cifra_es_error():
    caso = CASOS["numeros"]
    sin_cifra = caso["es"].replace("1350 metros", "bastantes metros")
    informe = v.validar_traduccion(caso["en"], sin_cifra, "es")
    assert not informe.ok
    assert "1350" in informe.errores[0]


def test_perder_un_nombre_es_solo_aviso():
    """Un nombre puede cambiar de forma legítimamente al traducirse."""
    caso = CASOS["nombres"]
    sin_nombre = caso["es"].replace("Kepler Labs", "la empresa")
    informe = v.validar_traduccion(caso["en"], sin_nombre, "es")
    assert informe.ok
    assert informe.avisos


def test_no_traducir_es_error_de_idioma():
    caso = CASOS["factual"]
    informe = v.validar_traduccion(caso["en"], caso["en"], "es")
    assert not informe.ok
    assert "en" in informe.errores[0]


def test_traduccion_vacia_es_error():
    assert not v.validar_traduccion("algo", "   ", "es").ok


# --- adaptación ------------------------------------------------------------


def test_inventar_una_cifra_es_error():
    """Condensar es legítimo; inventar un dato no."""
    fuente = CASOS["numeros"]["es"]
    guion = "La ruta tiene 42 kilómetros y la hacen 900 personas cada año."
    informe = v.validar_adaptacion(fuente, guion, "es")
    assert not informe.ok
    assert "900" in informe.errores[0]


def test_omitir_una_cifra_al_condensar_es_solo_aviso():
    fuente = CASOS["numeros"]["es"]
    guion = "La ruta tiene 42 kilómetros. Se sube en 3 etapas."
    informe = v.validar_adaptacion(fuente, guion, "es")
    assert informe.ok
    assert any("1350" in a for a in informe.avisos)


def test_guion_vacio_es_error():
    assert not v.validar_adaptacion("fuente", "", "es").ok


# --- subtitulabilidad ------------------------------------------------------


def test_una_frase_algo_larga_no_invalida_el_guion():
    """No se rechaza un guion solo porque una frase supere los 32 caracteres."""
    guion = "Esta frase tiene bastante más de treinta y dos caracteres, pero se lee."
    informe = v.validar_subtitulabilidad(guion)
    assert informe.ok
    assert informe.avisos


def test_una_frase_imposible_de_subtitular_es_error():
    guion = "Y " + "palabra " * 40 + "final."
    informe = v.validar_subtitulabilidad(guion)
    assert not informe.ok
    assert str(v.LONGITUD_MAXIMA) in informe.errores[0]


def test_signo_de_apertura_sin_cierre_es_error():
    """El defecto exacto que se detectó en Gate 0.5 con el motor de render."""
    informe = v.validar_subtitulabilidad("¿Qué tal. Todo bien.")
    assert not informe.ok
    assert "pareja" in informe.errores[0]


def test_exclamacion_sin_cierre_es_error():
    assert not v.validar_subtitulabilidad("¡Vaya sorpresa. Sigamos.").ok


def test_pregunta_bien_formada_pasa():
    assert v.validar_subtitulabilidad("¿Y qué pasa ahora? Nada. Sigue igual.").ok


# --- duración --------------------------------------------------------------


def test_estimacion_deterministica():
    texto = " ".join(["palabra"] * 119)
    assert v.estimar_duracion_s(texto, 119) == pytest.approx(60.0, abs=0.05)


def test_wpm_invalido_revienta_pronto():
    with pytest.raises(ValueError):
        v.estimar_duracion_s("hola", 0)


def test_duracion_dentro_de_rango():
    informe, veredicto = v.validar_duracion(30.0, 30.0, 0.25)
    assert veredicto == "ok" and informe.ok and not informe.avisos


def test_duracion_larga_se_marca_para_condensar():
    _, veredicto = v.validar_duracion(50.0, 30.0, 0.25)
    assert veredicto == "larga"


def test_duracion_corta_avisa_pero_no_invalida():
    """Alargar exigiría inventar contenido, así que se acepta y se avisa."""
    informe, veredicto = v.validar_duracion(10.0, 30.0, 0.25)
    assert veredicto == "corta"
    assert informe.ok and informe.avisos
