"""Ingesta: de una URL real al transcript que el pipeline consume.

Sin red. El transporte se sustituye por respuestas preparadas, igual que en el
resto del proyecto: lo que se prueba es el parseo, el recorte y —sobre todo— qué
se afirma sobre la licencia, no que Wikipedia siga en pie.

El caso que da sentido al archivo es
``test_una_fuente_sin_licencia_no_la_inventa``: inventar una licencia haría que
la procedencia tratase como permitido algo que nadie autorizó.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.adapters import fuente as F
from app.contracts.models import Transcript
from app.core.errors import (
    LimiteDeTasa,
    EntradaInvalida,
    ProveedorNoDisponible,
    RespuestaInvalida,
)

URL_WIKI = "https://en.wikipedia.org/wiki/Stanislav_Petrov"

EXTRACTO = (
    "Stanislav Petrov was a Soviet officer.\n\n"
    "On 26 September 1983 he judged an alert to be a false alarm.\n\n"
    "He died in 2017."
)


def _respuesta_wiki(extracto: str = EXTRACTO, titulo: str = "Stanislav Petrov") -> bytes:
    return json.dumps(
        {"query": {"pages": [{"title": titulo, "extract": extracto}]}}
    ).encode("utf-8")


@pytest.fixture
def sin_red(monkeypatch):
    """Sustituye el transporte y registra las URL pedidas."""
    pedidas: list[str] = []

    def falso(url, *, timeout_s):
        pedidas.append(url)
        return falso.respuesta

    falso.respuesta = _respuesta_wiki()
    monkeypatch.setattr(F, "_pedir", falso)
    return falso, pedidas


# ---------------------------------------------------------------------------
# 1. Wikipedia
# ---------------------------------------------------------------------------


def test_wikipedia_se_pide_por_su_api_y_no_raspando_el_html(sin_red):
    _, pedidas = sin_red
    F.ingerir(URL_WIKI)

    assert len(pedidas) == 1
    assert "/w/api.php" in pedidas[0], "se raspó el HTML en vez de usar la API"
    assert "explaintext=1" in pedidas[0]
    assert "Stanislav_Petrov" in pedidas[0]


def test_wikipedia_declara_su_licencia_y_su_atribucion(sin_red):
    doc = F.ingerir(URL_WIKI)

    assert doc.licencia_consta
    assert doc.license_id == "CC BY-SA 4.0"
    assert doc.license_url
    assert "Stanislav Petrov" in (doc.attribution or "")
    assert doc.provider == "wikipedia"


def test_el_idioma_sale_del_dominio_y_no_de_quien_llama(sin_red):
    """En Wikipedia el dominio es más fiable que el argumento."""
    doc = F.ingerir("https://es.wikipedia.org/wiki/Algo", idioma="pt")
    assert doc.source_language == "es"


def test_un_articulo_que_no_existe_se_rechaza(sin_red):
    falso, _ = sin_red
    falso.respuesta = json.dumps(
        {"query": {"pages": [{"title": "X", "missing": True}]}}
    ).encode("utf-8")

    with pytest.raises(EntradaInvalida) as exc:
        F.ingerir(URL_WIKI)
    assert "no tiene el artículo" in str(exc.value)


def test_un_articulo_sin_texto_se_rechaza(sin_red):
    falso, _ = sin_red
    falso.respuesta = _respuesta_wiki(extracto="   ")
    with pytest.raises(RespuestaInvalida):
        F.ingerir(URL_WIKI)


def test_una_respuesta_que_no_es_json_se_rechaza(sin_red):
    falso, _ = sin_red
    falso.respuesta = b"<html>no soy json</html>"
    with pytest.raises(RespuestaInvalida):
        F.ingerir(URL_WIKI)


# ---------------------------------------------------------------------------
# 2. La licencia no se inventa
# ---------------------------------------------------------------------------


def test_una_fuente_sin_licencia_no_la_inventa(sin_red):
    """El invariante del módulo.

    Una web cualquiera no declara su licencia de forma legible. Devolver una
    por defecto haría que la procedencia la tratara como permitida, que es
    exactamente lo que no debe pasar.
    """
    falso, _ = sin_red
    falso.respuesta = b"<html><body><p>Texto suficiente para extraer.</p></body></html>"

    doc = F.ingerir("https://ejemplo.org/articulo")

    assert doc.license_id is None
    assert not doc.licencia_consta
    assert "NO CONSTA" in doc.a_atribucion()


def test_la_atribucion_de_una_fuente_sin_licencia_avisa_de_que_hay_que_mirarla(sin_red):
    falso, _ = sin_red
    falso.respuesta = b"<html><body><p>Texto suficiente para extraer.</p></body></html>"
    texto = F.ingerir("https://ejemplo.org/x").a_atribucion()
    assert "no significa" in texto.lower() or "NO significa" in texto


# ---------------------------------------------------------------------------
# 3. Web genérica
# ---------------------------------------------------------------------------


def test_el_extractor_quita_scripts_y_estilos(sin_red):
    falso, _ = sin_red
    falso.respuesta = (
        b"<html><head><title>Titulo</title><style>p{color:red}</style></head>"
        b"<body><script>var x=1;</script><p>Contenido visible.</p></body></html>"
    )
    doc = F.ingerir("https://ejemplo.org/x")

    assert "Contenido visible." in doc.text
    assert "var x" not in doc.text
    assert "color:red" not in doc.text
    assert doc.title == "Titulo"


def test_una_pagina_sin_texto_se_rechaza(sin_red):
    falso, _ = sin_red
    falso.respuesta = b"<html><body><script>var x=1;</script></body></html>"
    with pytest.raises(RespuestaInvalida):
        F.ingerir("https://ejemplo.org/x")


# ---------------------------------------------------------------------------
# 4. Recorte
# ---------------------------------------------------------------------------


def test_el_recorte_no_parte_una_frase_por_la_mitad(sin_red):
    """Cortar a mitad de frase produce un transcript que el modelo traducirá
    igual de roto, y el guion heredaría la frase partida."""
    falso, _ = sin_red
    falso.respuesta = _respuesta_wiki(
        extracto="Primer párrafo completo.\n\nSegundo párrafo bastante más largo.\n\nTercero."
    )
    doc = F.ingerir(URL_WIKI, max_caracteres=30)

    assert doc.text == "Primer párrafo completo."
    assert not doc.text.endswith("comple")


def test_max_caracteres_cero_trae_el_texto_entero(sin_red):
    doc = F.ingerir(URL_WIKI, max_caracteres=0)
    assert doc.text.count("\n\n") == 2


# ---------------------------------------------------------------------------
# 5. URL inválidas y fallos de red
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["", "no-es-una-url", "ftp://host/x", "file:///etc/passwd", "https://"]
)
def test_una_url_que_no_es_http_se_rechaza(url, sin_red):
    with pytest.raises(EntradaInvalida):
        F.ingerir(url)


def test_un_5xx_es_transitorio_y_un_4xx_no(monkeypatch):
    """La distinción importa: uno se puede reintentar y el otro no.

    Se parchea ``urlopen`` y no ``_pedir``, porque el mapeo de HTTPError a
    error del dominio vive **dentro** de ``_pedir``: sustituirlo sería saltarse
    justo lo que se quiere comprobar.
    """
    import urllib.error
    import urllib.request

    def lanzar(codigo):
        def _urlopen(peticion, timeout=None):
            raise urllib.error.HTTPError(
                peticion.full_url, codigo, "nope", {}, None
            )

        return _urlopen

    monkeypatch.setattr(F.time, "sleep", lambda _s: None)
    monkeypatch.setattr(urllib.request, "urlopen", lanzar(503))
    with pytest.raises(ProveedorNoDisponible):
        F.ingerir(URL_WIKI)

    monkeypatch.setattr(urllib.request, "urlopen", lanzar(404))
    with pytest.raises(EntradaInvalida):
        F.ingerir(URL_WIKI)


def test_un_429_es_un_limite_de_tasa_y_se_reintenta(monkeypatch):
    """El caso que se encontró en producción.

    Wikipedia devolvió 429 en una ingesta real y la ingesta se rindió, porque
    todo 4xx se trataba como error permanente de entrada. Un 404 no se arregla
    repitiéndolo; un 429 sí, y confundirlos hace que el sistema se dé por
    vencido ante algo que solo pedía esperar.
    """
    import urllib.error
    import urllib.request

    intentos = {"n": 0}

    def _urlopen(peticion, timeout=None):
        intentos["n"] += 1
        if intentos["n"] < 3:
            raise urllib.error.HTTPError(
                peticion.full_url, 429, "slow down", {"Retry-After": "0"}, None
            )
        class _R:
            def read(self):
                return _respuesta_wiki()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return _R()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(F.time, "sleep", lambda _s: None)

    doc = F.ingerir(URL_WIKI)

    assert intentos["n"] == 3, "no reintentó ante el límite de tasa"
    assert doc.title == "Stanislav Petrov"


def test_un_429_persistente_acaba_fallando_como_limite_de_tasa(monkeypatch):
    """Reintentar es acotado: no se insiste para siempre."""
    import urllib.error
    import urllib.request

    intentos = {"n": 0}

    def _urlopen(peticion, timeout=None):
        intentos["n"] += 1
        raise urllib.error.HTTPError(peticion.full_url, 429, "no", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(F.time, "sleep", lambda _s: None)

    with pytest.raises(LimiteDeTasa):
        F.ingerir(URL_WIKI)
    assert intentos["n"] == F.MAX_INTENTOS


def test_un_404_no_se_reintenta(monkeypatch):
    """Insistir contra un artículo que no existe solo gasta tiempo."""
    import urllib.error
    import urllib.request

    intentos = {"n": 0}

    def _urlopen(peticion, timeout=None):
        intentos["n"] += 1
        raise urllib.error.HTTPError(peticion.full_url, 404, "no", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(F.time, "sleep", lambda _s: None)

    with pytest.raises(EntradaInvalida):
        F.ingerir(URL_WIKI)
    assert intentos["n"] == 1


def test_una_red_inalcanzable_es_transitoria(monkeypatch):
    import urllib.error
    import urllib.request

    def _urlopen(peticion, timeout=None):
        raise urllib.error.URLError("sin ruta al host")

    monkeypatch.setattr(F.time, "sleep", lambda _s: None)
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    with pytest.raises(ProveedorNoDisponible):
        F.ingerir(URL_WIKI)


# ---------------------------------------------------------------------------
# 6. Lo que produce encaja con el contrato del pipeline
# ---------------------------------------------------------------------------


def test_el_transcript_producido_valida_contra_su_contrato(sin_red):
    doc = F.ingerir(URL_WIKI)
    datos = doc.a_transcript()
    datos["run_id"] = str(uuid.uuid4())

    transcript = Transcript.model_validate(datos)

    assert transcript.source_language == "en"
    assert transcript.text
    assert len(transcript.segments) == 3
    # Un texto publicado no trae marcas temporales, y no se fingen.
    assert not transcript.tiene_tiempos


def test_el_transcript_no_fija_el_run_id(sin_red):
    """Lo pone la corrida. Un fixture no tiene por qué conocerlo."""
    assert "run_id" not in F.ingerir(URL_WIKI).a_transcript()


def test_la_atribucion_no_se_mezcla_con_el_texto_a_traducir(sin_red):
    """Si la atribución entrara en el transcript, se traduciría y acabaría
    narrada dentro del Short."""
    doc = F.ingerir(URL_WIKI)
    assert "CC BY-SA" not in doc.a_transcript()["text"]
    assert "CC BY-SA" in doc.a_atribucion()
