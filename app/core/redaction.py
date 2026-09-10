"""Ocultación de secretos en texto destinado a logs o al manifest.

El repositorio es público y los logs de sus workflows también, así que un
secreto que llegue a un log queda expuesto al mundo. Este módulo es la última
barrera: se aplica al texto ya formateado, justo antes de emitirlo.

No sustituye a la disciplina de no registrar configuración: es defensa en
profundidad.
"""

from __future__ import annotations

import os
import re

MASCARA = "***"

# Nombres de variable cuyo valor se considera secreto por convención.
PATRON_NOMBRE_SECRETO = re.compile(
    r"(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_?KEY)", re.IGNORECASE
)

# Valores por debajo de esta longitud no se enmascaran: sustituirlos causaría
# falsos positivos ruidosos (por ejemplo, un valor "1" o "es").
LONGITUD_MINIMA = 8

# Formas de credencial reconocibles aunque no estén en el entorno.
PATRONES_VALOR = (
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_\-]{16,}\b"),      # claves tipo OpenAI
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),            # tokens de GitHub
    re.compile(r"\bya29\.[A-Za-z0-9_\-]{10,}\b"),             # OAuth de Google
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b"),
    re.compile(r"\bApikey\s+[A-Za-z0-9._\-]{16,}\b"),
)


def valores_secretos(entorno: dict[str, str] | None = None) -> list[str]:
    """Devuelve los valores del entorno que deben ocultarse.

    Se lee el entorno en cada llamada en vez de cachearlo, para que una
    variable definida después del arranque también quede cubierta.
    """
    fuente = os.environ if entorno is None else entorno
    valores = []
    for nombre, valor in fuente.items():
        if not valor or len(valor) < LONGITUD_MINIMA:
            continue
        if PATRON_NOMBRE_SECRETO.search(nombre):
            valores.append(valor)
    # Los más largos primero: evita que ocultar un valor corto rompa uno largo
    # que lo contiene.
    return sorted(set(valores), key=len, reverse=True)


def redactar(texto: str, entorno: dict[str, str] | None = None) -> str:
    """Sustituye por ``***`` cualquier secreto reconocible en el texto."""
    if not texto:
        return texto
    resultado = texto
    for secreto in valores_secretos(entorno):
        resultado = resultado.replace(secreto, MASCARA)
    for patron in PATRONES_VALOR:
        resultado = patron.sub(MASCARA, resultado)
    return resultado


def contiene_secreto(texto: str, entorno: dict[str, str] | None = None) -> bool:
    """Indica si el texto todavía contiene algún secreto del entorno."""
    return any(secreto in texto for secreto in valores_secretos(entorno))
