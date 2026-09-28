"""Ingesta: de una URL real a un transcript que el pipeline puede consumir.

Qué resuelve
------------

Hasta ahora el pipeline empezaba en un ``transcript.json`` que alguien escribía
a mano. Eso basta para probar, pero convierte «hacer otro Short» en «editar un
JSON», y por tanto el sistema no se podía repetir con otro contenido sin
intervención. Este módulo cierra ese hueco: recibe una URL, trae el texto real y
devuelve lo que la etapa de ingesta espera.

Qué **no** hace, y por qué importa
----------------------------------

No transcribe audio ni vídeo. Traer un texto que ya existe y transcribir un
medio hablado son dos problemas distintos, y el segundo necesita un motor de
reconocimiento que este proyecto no tiene. Decir «transcripción» de lo primero
sería llamar a las cosas por un nombre que no les toca: aquí se **extrae** texto
publicado, no se transcribe nada.

Tampoco decide si el contenido se puede usar. Devuelve lo que la fuente declara
sobre su licencia —o admite que no lo sabe— y esa declaración va al
``ProvenanceLedger``, que es quien decide. Una fuente cuya licencia no consta se
registra como desconocida, no como permitida.

Cómo extrae
-----------

* **Wikipedia**: por su API, que devuelve texto plano ya limpio y la licencia de
  la obra. Es la vía fiable; raspar el HTML de un artículo daría el mismo texto
  con peor calidad y más frágil.
* **Cualquier otra web**: se quita el marcado y se conserva el texto. Funciona,
  pero no adivina la licencia: sale ``None`` y la procedencia lo tratará como
  desconocido.

Sin dependencias nuevas: ``urllib`` y ``html.parser`` de la biblioteca estándar.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser

from app.core.errors import (
    EntradaInvalida,
    ErrorPipeline,
    LimiteDeTasa,
    ProveedorNoDisponible,
    RespuestaInvalida,
    TiempoAgotado,
)

#: Un User-Agent que identifica al proyecto. Wikipedia pide explícitamente que
#: los clientes se identifiquen, y un agente anónimo puede recibir un bloqueo.
AGENTE = "shorts-automation/1.0 (+https://github.com/dylanmateo1722/shorts-automation)"

TIMEOUT_POR_DEFECTO_S = 30

#: Cuánto texto se trae por defecto. Un Short de 45 s son unas 90 palabras, así
#: que un artículo entero es material de sobra: lo que sobra encarece el prompt
#: de traducción sin mejorar el guion. Se corta por párrafos completos, nunca a
#: mitad de una frase.
MAX_CARACTERES_POR_DEFECTO = 1500

_ESPACIOS = re.compile(r"[ \t ]+")
_SALTOS = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class DocumentoFuente:
    """El texto real de una fuente, con lo que se sabe de su procedencia.

    ``license_id`` es ``None`` cuando la fuente no lo declara. Es una ausencia
    deliberada y no un valor por defecto: inventar una licencia es peor que
    admitir que no consta, porque la procedencia trataría como permitido algo
    que nadie autorizó.
    """

    url: str
    title: str
    text: str
    source_language: str
    provider: str
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    license_id: str | None = None
    license_url: str | None = None
    attribution: str | None = None

    @property
    def licencia_consta(self) -> bool:
        return bool(self.license_id)

    def a_transcript(self) -> dict:
        """El JSON que espera la etapa de ingesta.

        No lleva ``run_id``: lo pone la corrida. Los párrafos van como segmentos
        sin tiempos, porque un texto publicado no los tiene; ``Transcript``
        distingue ese caso y no finge una estructura temporal que no existe.
        """
        parrafos = [p for p in self.text.split("\n\n") if p.strip()]
        return {
            "source_language": self.source_language,
            "text": self.text,
            "segments": [{"text": p} for p in parrafos],
        }

    def a_atribucion(self) -> str:
        """El documento de atribución, legible y verificable a mano."""
        lineas = [
            "FUENTE REAL — ingerida por shorts-automation",
            "=" * 46,
            "",
            f"Título:      {self.title}",
            f"URL:         {self.url}",
            f"Proveedor:   {self.provider}",
            f"Idioma:      {self.source_language}",
            f"Consultado:  {self.retrieved_at.date().isoformat()}",
        ]
        if self.licencia_consta:
            lineas.append(f"Licencia:    {self.license_id}")
            if self.license_url:
                lineas.append(f"             {self.license_url}")
        else:
            lineas += [
                "Licencia:    NO CONSTA",
                "",
                "             La fuente no declara licencia por un medio que se",
                "             pueda leer automáticamente. Esto NO significa que",
                "             el contenido sea libre: significa que alguien tiene",
                "             que comprobarlo antes de usarlo. La procedencia lo",
                "             registrará como desconocido.",
            ]
        if self.attribution:
            lineas += ["", f"Atribución:  {self.attribution}"]
        lineas += ["", "Texto extraído", "-" * 14, "", self.text, ""]
        return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Transporte
# ---------------------------------------------------------------------------


#: Reintentos ante un fallo transitorio, y cuánto se espera entre ellos. Son
#: pocos y cortos a propósito: la ingesta es interactiva, y un usuario prefiere
#: un error claro en diez segundos a un éxito en dos minutos.
MAX_INTENTOS = 3
ESPERA_BASE_S = 2.0


def _peticion_unica(url: str, *, timeout_s: int) -> bytes:
    """Una sola petición, traduciendo el fallo al error del dominio que toca.

    La distinción no es cosmética: un 404 no se arregla repitiéndolo y un 429
    sí. Tratar ambos igual haría que la ingesta se rindiera ante un límite de
    tasa, o que insistiera contra un artículo que no existe.
    """
    peticion = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    try:
        with urllib.request.urlopen(peticion, timeout=timeout_s) as respuesta:
            return respuesta.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (408, 429):
            limite = LimiteDeTasa(
                f"la fuente limitó la petición ({exc.code}) para {url}",
                stage="ingest",
            )
            # Se cuelga del error lo que el servidor pidió esperar, para que
            # quien reintente no tenga que volver a leer las cabeceras.
            limite.espera_s = _retry_after(exc)
            raise limite from exc
        if 500 <= exc.code < 600:
            raise ProveedorNoDisponible(
                f"la fuente devolvió {exc.code} para {url}", stage="ingest"
            ) from exc
        raise EntradaInvalida(
            f"la fuente devolvió {exc.code} para {url}", stage="ingest"
        ) from exc
    except TimeoutError as exc:
        raise TiempoAgotado(
            f"la fuente no respondió en {timeout_s}s: {url}", stage="ingest"
        ) from exc
    except urllib.error.URLError as exc:
        raise ProveedorNoDisponible(
            f"no se pudo alcanzar {url}: {exc.reason}", stage="ingest"
        ) from exc


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """El ``Retry-After`` que pida el servidor, si lo pide en segundos.

    Se respeta cuando existe: esperar lo que el servidor dice es más educado —y
    más eficaz— que insistir con nuestro propio ritmo.
    """
    try:
        bruto = (exc.headers or {}).get("Retry-After")
    except Exception:  # noqa: BLE001 - unas cabeceras raras no deben romper nada
        return None
    if not bruto:
        return None
    try:
        return max(0.0, float(str(bruto).strip()))
    except ValueError:
        # También admite una fecha HTTP; no se interpreta, se usa el backoff.
        return None


def _pedir(
    url: str, *, timeout_s: int, dormir=None, max_intentos: int = MAX_INTENTOS
) -> bytes:
    """Pide la URL, reintentando solo lo que tiene sentido reintentar.

    ``dormir`` se resuelve al llamar y no como valor por defecto: un valor por
    defecto se evalúa al importar el módulo, y entonces un test que sustituya
    ``time.sleep`` después no tendría ningún efecto y la suite esperaría de
    verdad los segundos del backoff.
    """
    dormir = dormir or time.sleep
    ultimo: ErrorPipeline | None = None
    for intento in range(1, max_intentos + 1):
        try:
            return _peticion_unica(url, timeout_s=timeout_s)
        except (LimiteDeTasa, ProveedorNoDisponible, TiempoAgotado) as exc:
            ultimo = exc
            if intento == max_intentos:
                break
            espera = ESPERA_BASE_S * (2 ** (intento - 1))
            if isinstance(exc, LimiteDeTasa):
                pedida = getattr(exc, "espera_s", None)
                if isinstance(pedida, (int, float)) and pedida > 0:
                    espera = max(espera, float(pedida))
            dormir(espera)
    raise ultimo


def _limpiar(texto: str) -> str:
    """Normaliza espacios sin tocar la separación en párrafos."""
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    texto = _ESPACIOS.sub(" ", texto)
    texto = "\n".join(linea.strip() for linea in texto.split("\n"))
    return _SALTOS.sub("\n\n", texto).strip()


def _recortar(texto: str, max_caracteres: int) -> str:
    """Corta por párrafos completos, nunca a mitad de frase.

    Un corte a mitad de frase produce un transcript que el modelo traducirá
    igual de mal que lo recibió, y el guion heredará la frase rota.
    """
    if max_caracteres <= 0 or len(texto) <= max_caracteres:
        return texto
    salida: list[str] = []
    total = 0
    for parrafo in texto.split("\n\n"):
        if salida and total + len(parrafo) > max_caracteres:
            break
        salida.append(parrafo)
        total += len(parrafo) + 2
    return "\n\n".join(salida) if salida else texto[:max_caracteres]


# ---------------------------------------------------------------------------
# Wikipedia
# ---------------------------------------------------------------------------

_WIKIPEDIA = re.compile(r"^([a-z]{2,3})\.(m\.)?wikipedia\.org$")


def _es_wikipedia(host: str) -> re.Match | None:
    return _WIKIPEDIA.match(host.lower())


def _wikipedia(url: str, coincidencia: re.Match, *, timeout_s: int) -> DocumentoFuente:
    """Trae un artículo por la API de Wikipedia, no raspando su HTML.

    La API devuelve el texto ya en plano y dice cuál es el título canónico, así
    que el resultado no depende de que el maquetado de la página no cambie.
    """
    idioma = coincidencia.group(1)
    partes = urllib.parse.urlparse(url)
    titulo = urllib.parse.unquote(partes.path.rsplit("/", 1)[-1])
    if not titulo:
        raise EntradaInvalida(
            f"la URL de Wikipedia no nombra ningún artículo: {url}", stage="ingest"
        )

    consulta = urllib.parse.urlencode(
        {
            "action": "query",
            "prop": "extracts",
            "explaintext": "1",
            "exsectionformat": "plain",
            "redirects": "1",
            "format": "json",
            "formatversion": "2",
            "titles": titulo,
        }
    )
    api = f"https://{idioma}.wikipedia.org/w/api.php?{consulta}"
    try:
        datos = json.loads(_pedir(api, timeout_s=timeout_s))
    except ValueError as exc:
        raise RespuestaInvalida(
            f"la API de Wikipedia no devolvió JSON interpretable: {exc}", stage="ingest"
        ) from exc

    paginas = (datos.get("query") or {}).get("pages") or []
    if not paginas or paginas[0].get("missing"):
        raise EntradaInvalida(
            f"Wikipedia no tiene el artículo {titulo!r} en {idioma}", stage="ingest"
        )
    pagina = paginas[0]
    texto = _limpiar(pagina.get("extract") or "")
    if not texto:
        raise RespuestaInvalida(
            f"el artículo {titulo!r} no trajo texto", stage="ingest"
        )

    canonico = pagina.get("title") or titulo
    return DocumentoFuente(
        url=f"https://{idioma}.wikipedia.org/wiki/"
        + urllib.parse.quote(canonico.replace(" ", "_")),
        title=canonico,
        text=texto,
        source_language=idioma,
        provider="wikipedia",
        license_id="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0/",
        attribution=f"«{canonico}», Wikipedia, colaboradores de Wikipedia",
    )


# ---------------------------------------------------------------------------
# Web genérica
# ---------------------------------------------------------------------------


class _ExtractorTexto(HTMLParser):
    """Quita el marcado y conserva el texto, respetando los párrafos.

    No intenta adivinar cuál es el «cuerpo» del artículo: eso exige heurísticos
    que fallan en cuanto cambia la plantilla del sitio. Devuelve el texto y deja
    que quien lo use decida, que es más honesto que acertar a veces.
    """

    IGNORADAS = {"script", "style", "noscript", "template", "svg", "head"}
    BLOQUE = {
        "p", "div", "section", "article", "br", "li",
        "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self._ignorando = 0
        self.titulo: str | None = None
        self._en_titulo = False

    def handle_starttag(self, tag, attrs):
        if tag in self.IGNORADAS:
            self._ignorando += 1
        if tag == "title":
            self._en_titulo = True
        if tag in self.BLOQUE:
            self.partes.append("\n\n")

    def handle_endtag(self, tag):
        if tag in self.IGNORADAS and self._ignorando:
            self._ignorando -= 1
        if tag == "title":
            self._en_titulo = False
        if tag in self.BLOQUE:
            self.partes.append("\n\n")

    def handle_data(self, data):
        if self._en_titulo and self.titulo is None:
            self.titulo = data.strip() or None
        if not self._ignorando:
            self.partes.append(data)

    @property
    def texto(self) -> str:
        return _limpiar("".join(self.partes))


def _generica(url: str, *, timeout_s: int, idioma: str) -> DocumentoFuente:
    crudo = _pedir(url, timeout_s=timeout_s)
    try:
        html = crudo.decode("utf-8")
    except UnicodeDecodeError:
        html = crudo.decode("latin-1", errors="replace")

    extractor = _ExtractorTexto()
    extractor.feed(html)
    texto = extractor.texto
    if not texto:
        raise RespuestaInvalida(
            f"no se pudo extraer texto de {url}", stage="ingest"
        )

    return DocumentoFuente(
        url=url,
        title=extractor.titulo or urllib.parse.urlparse(url).netloc,
        text=texto,
        source_language=idioma,
        provider=urllib.parse.urlparse(url).netloc,
        # Deliberadamente sin licencia: no consta, y no se inventa.
        license_id=None,
    )


# ---------------------------------------------------------------------------
# Entrada pública
# ---------------------------------------------------------------------------


def ingerir(
    url: str,
    *,
    timeout_s: int = TIMEOUT_POR_DEFECTO_S,
    max_caracteres: int = MAX_CARACTERES_POR_DEFECTO,
    idioma: str = "en",
) -> DocumentoFuente:
    """Trae el texto real de una URL.

    ``idioma`` solo se usa para las fuentes genéricas: en Wikipedia sale del
    propio dominio, que es más fiable que lo que declare quien invoca.

    Raises:
        EntradaInvalida: URL mal formada, o la fuente respondió 4xx.
        ProveedorNoDisponible: no se pudo alcanzar, o respondió 5xx.
        TiempoAgotado: no respondió a tiempo.
        RespuestaInvalida: respondió, pero sin texto utilizable.
    """
    partes = urllib.parse.urlparse(url)
    if partes.scheme not in ("http", "https") or not partes.netloc:
        raise EntradaInvalida(
            f"no es una URL http(s) válida: {url!r}", stage="ingest"
        )

    if coincidencia := _es_wikipedia(partes.netloc):
        documento = _wikipedia(url, coincidencia, timeout_s=timeout_s)
    else:
        documento = _generica(url, timeout_s=timeout_s, idioma=idioma)

    texto = _recortar(documento.text, max_caracteres)
    if texto == documento.text:
        return documento
    # dataclass congelado: recortar produce otro documento, no muta este.
    return DocumentoFuente(
        url=documento.url,
        title=documento.title,
        text=texto,
        source_language=documento.source_language,
        provider=documento.provider,
        retrieved_at=documento.retrieved_at,
        license_id=documento.license_id,
        license_url=documento.license_url,
        attribution=documento.attribution,
    )
