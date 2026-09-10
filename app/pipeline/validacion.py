"""Validaciones lingüísticas objetivas.

Todo lo que se comprueba aquí es una propiedad verificable del texto: hay
cifras que estaban y ya no están, hay una frase imposible de subtitular, el
idioma no es el pedido. **No** se puntúa la calidad editorial: eso es un juicio
humano y ninguna métrica automática lo sustituye.

Tampoco existe aquí ninguna medida de "cuánto se diferencia" un texto de otro.
La transformación editorial es una cuestión de calidad y originalidad, no un
umbral técnico, y este proyecto no implementa mecanismos de evasión.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Dos líneas de 32 caracteres: el presupuesto cómodo de un subtítulo vertical.
LONGITUD_COMODA = 64
# Por encima de esto la frase deja de ser subtitulable de forma evidente. En
# Gate 0.5 una pregunta de 78 caracteres ya no cupo en dos líneas.
LONGITUD_MAXIMA = 140
# Proporción de frases largas a partir de la cual se avisa del conjunto.
PROPORCION_LARGAS_AVISO = 0.5

_FIN_DE_FRASE = re.compile(r"(?<=[.!?…])\s+")
_NUMERO = re.compile(r"\d+(?:[.,]\d+)*")
_PALABRA = re.compile(r"[^\W\d_]+", re.UNICODE)

# Marcadores frecuentes. No es un identificador de idioma: es una comprobación
# barata para detectar el caso evidente de que el modelo no tradujo.
_MARCADORES = {
    "es": {
        "de", "la", "que", "el", "en", "y", "los", "se", "del", "las", "por",
        "un", "con", "para", "una", "su", "al", "es", "lo", "como", "más",
        "pero", "sus", "le", "ya", "o", "este", "sí", "porque", "esta", "son",
    },
    "en": {
        "the", "of", "and", "to", "in", "is", "that", "it", "for", "was", "on",
        "as", "with", "at", "by", "this", "from", "or", "an", "be", "are",
        "you", "your", "we", "they", "but", "not", "have", "has", "will",
    },
}

APERTURAS = {"¿": "?", "¡": "!"}


@dataclass
class Informe:
    """Resultado de una validación: errores bloquean, avisos no."""

    errores: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errores

    def combinar(self, otro: "Informe") -> "Informe":
        return Informe(self.errores + otro.errores, self.avisos + otro.avisos)

    def resumen(self) -> str:
        lineas = [f"  [ERROR] {e}" for e in self.errores]
        lineas += [f"  [aviso] {a}" for a in self.avisos]
        return "\n".join(lineas) or "  sin incidencias"


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------


def frases(texto: str) -> list[str]:
    """Parte el texto en frases conservando su puntuación."""
    return [f.strip() for f in _FIN_DE_FRASE.split(texto.strip()) if f.strip()]


def contar_palabras(texto: str) -> int:
    return len(texto.split())


def estimar_duracion_s(texto: str, wpm: int) -> float:
    """Estima la duración de la narración a partir del número de palabras.

    Es determinista y aproximada. **No** equivale a la duración real de una
    voz sintetizada: eso solo se sabe midiendo el audio.
    """
    if wpm <= 0:
        raise ValueError("wpm debe ser mayor que cero")
    return round(contar_palabras(texto) / wpm * 60, 2)


def numeros(texto: str) -> set[str]:
    """Cifras presentes en el texto, normalizadas sin separadores."""
    return {n.replace(".", "").replace(",", "") for n in _NUMERO.findall(texto)}


def _sin_acentos(palabra: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", palabra) if not unicodedata.combining(c)
    )


def nombres_propios(texto: str) -> set[str]:
    """Palabras capitalizadas que no abren frase.

    Heurística: sirve para avisar de un nombre desaparecido, no para afirmar
    con certeza que algo es un nombre propio.
    """
    encontrados: set[str] = set()
    for frase in frases(texto):
        palabras = _PALABRA.findall(frase)
        for palabra in palabras[1:]:
            if palabra[:1].isupper() and not palabra.isupper():
                encontrados.add(palabra)
    return encontrados


def idioma_probable(texto: str) -> str | None:
    """Devuelve ``es``, ``en`` o None si no hay evidencia suficiente."""
    palabras = [_sin_acentos(p).lower() for p in _PALABRA.findall(texto)]
    if len(palabras) < 8:
        return None
    conteos = {
        codigo: sum(1 for p in palabras if p in {_sin_acentos(m) for m in marcadores})
        for codigo, marcadores in _MARCADORES.items()
    }
    mejor = max(conteos, key=lambda c: conteos[c])
    if conteos[mejor] == 0:
        return None
    # Un empate no es evidencia: se prefiere no afirmar nada.
    otros = [v for k, v in conteos.items() if k != mejor]
    if otros and conteos[mejor] <= max(otros):
        return None
    return mejor


def signos_desparejados(texto: str) -> list[str]:
    """Frases donde falta la apertura o el cierre de ¿? o ¡!."""
    problemas = []
    for frase in frases(texto):
        for apertura, cierre in APERTURAS.items():
            if frase.count(apertura) != frase.count(cierre):
                problemas.append(frase[:60])
                break
    return problemas


# ---------------------------------------------------------------------------
# Validaciones de etapa
# ---------------------------------------------------------------------------


def validar_idioma(texto: str, esperado: str) -> Informe:
    informe = Informe()
    detectado = idioma_probable(texto)
    if detectado is not None and detectado != esperado:
        informe.errores.append(
            f"el texto parece estar en {detectado!r} y se esperaba {esperado!r}"
        )
    return informe


def validar_traduccion(texto_origen: str, texto_traducido: str, idioma_destino: str) -> Informe:
    """Comprueba que la traducción preservó lo que debía preservar.

    Las cifras son un error si desaparecen: una traducción fiel no altera un
    número. Los nombres son un aviso, porque pueden cambiar legítimamente de
    forma al traducirse.
    """
    informe = Informe()
    if not texto_traducido.strip():
        informe.errores.append("la traducción está vacía")
        return informe

    informe = informe.combinar(validar_idioma(texto_traducido, idioma_destino))

    faltantes = numeros(texto_origen) - numeros(texto_traducido)
    if faltantes:
        informe.errores.append(
            f"la traducción perdió cifras del original: {sorted(faltantes)}"
        )

    nombres_faltantes = nombres_propios(texto_origen) - nombres_propios(texto_traducido)
    if nombres_faltantes:
        informe.avisos.append(
            f"posibles nombres propios ausentes en la traducción: "
            f"{sorted(nombres_faltantes)[:5]}"
        )
    return informe


def validar_subtitulabilidad(texto: str) -> Informe:
    """Detecta problemas evidentes de legibilidad como subtítulo.

    No se rechaza un guion por una frase algo larga: se rechaza por frases
    imposibles de mostrar en dos líneas y por puntuación rota.
    """
    informe = Informe()
    lista = frases(texto)
    if not lista:
        informe.errores.append("el texto no contiene ninguna frase")
        return informe

    imposibles = [f[:60] for f in lista if len(f) > LONGITUD_MAXIMA]
    if imposibles:
        informe.errores.append(
            f"{len(imposibles)} frase(s) superan {LONGITUD_MAXIMA} caracteres y no "
            f"son subtitulables: {imposibles[:2]}"
        )

    largas = [f for f in lista if LONGITUD_COMODA < len(f) <= LONGITUD_MAXIMA]
    if largas:
        proporcion = len(largas) / len(lista)
        detalle = f"{len(largas)} de {len(lista)} frases superan {LONGITUD_COMODA} caracteres"
        if proporcion >= PROPORCION_LARGAS_AVISO:
            informe.avisos.append(f"{detalle}: costará segmentarlas en dos líneas")
        else:
            informe.avisos.append(detalle)

    if desparejados := signos_desparejados(texto):
        informe.errores.append(
            f"signos de interrogación o exclamación sin pareja en: {desparejados[:2]}"
        )
    return informe


def validar_duracion(
    estimada_s: float, objetivo_s: float, tolerancia: float
) -> tuple[Informe, str]:
    """Compara la duración estimada con el objetivo.

    Returns:
        (informe, veredicto) donde veredicto es ``ok``, ``larga`` o ``corta``.
        Quedarse corto no es un error: alargar exigiría inventar contenido.
    """
    informe = Informe()
    maximo = objetivo_s * (1 + tolerancia)
    minimo = objetivo_s * (1 - tolerancia)

    if estimada_s > maximo:
        informe.avisos.append(
            f"duración estimada {estimada_s:.1f}s por encima del máximo {maximo:.1f}s"
        )
        return informe, "larga"
    if estimada_s < minimo:
        informe.avisos.append(
            f"duración estimada {estimada_s:.1f}s por debajo del mínimo {minimo:.1f}s; "
            f"no se alarga el guion porque exigiría inventar contenido"
        )
        return informe, "corta"
    return informe, "ok"


def validar_adaptacion(
    texto_traduccion: str, texto_guion: str, idioma_destino: str
) -> Informe:
    """Comprueba fidelidad y subtitulabilidad del guion adaptado.

    Aquí la asimetría es la contraria a la traducción: condensar es legítimo,
    así que perder una cifra es un aviso; **inventarla** es un error.
    """
    informe = Informe()
    if not texto_guion.strip():
        informe.errores.append("el guion adaptado está vacío")
        return informe

    informe = informe.combinar(validar_idioma(texto_guion, idioma_destino))

    inventadas = numeros(texto_guion) - numeros(texto_traduccion)
    if inventadas:
        informe.errores.append(
            f"el guion introduce cifras que no estaban en la fuente: {sorted(inventadas)}"
        )

    omitidas = numeros(texto_traduccion) - numeros(texto_guion)
    if omitidas:
        informe.avisos.append(
            f"cifras de la fuente no usadas en el guion: {sorted(omitidas)[:5]}"
        )

    nombres_inventados = nombres_propios(texto_guion) - nombres_propios(texto_traduccion)
    if nombres_inventados:
        informe.avisos.append(
            f"posibles nombres no presentes en la fuente: "
            f"{sorted(nombres_inventados)[:5]}"
        )

    return informe.combinar(validar_subtitulabilidad(texto_guion))
