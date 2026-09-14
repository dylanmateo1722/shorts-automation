"""Ocultación de secretos en texto destinado a logs o al manifest.

El repositorio es público y los logs de sus workflows también, así que un
secreto que llegue a un log queda expuesto al mundo. Este módulo es la última
barrera: se aplica al texto ya formateado, justo antes de emitirlo.

Se reconocen secretos por tres vías, y las tres hacen falta porque ninguna cubre
lo que cubren las otras:

1. **por nombre de variable** —cualquier cosa que se llame ``*_TOKEN``,
   ``*_SECRET`` y compañía—, que es lo que atrapa la configuración;
2. **por forma del valor**, que atrapa una credencial pegada a mano en un
   mensaje aunque no esté en el entorno;
3. **por registro explícito** (``registrar_secreto``), para los secretos que
   nacen durante la ejecución y no tienen ni nombre de variable ni forma
   reconocible.

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
    re.compile(r"\bya29\.[A-Za-z0-9_\-]{10,}\b"),             # access token de Google
    re.compile(r"(?<![A-Za-z0-9_])1//[A-Za-z0-9_\-]{20,}"),   # refresh token de Google
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b"),
    re.compile(r"\bApikey\s+[A-Za-z0-9._\-]{16,}\b"),
)

# Secretos que solo existen durante la ejecución: no vienen del entorno y no
# tienen una forma reconocible, así que ninguna de las dos defensas anteriores
# los cubre. El caso que obliga a esto es el ``device_code`` del flujo de
# dispositivo: es una cadena opaca que Google no documenta con ningún prefijo
# estable, y quien la tenga junto al ``client_secret`` puede reclamar la
# autorización. Se registran en cuanto se obtienen para que el filtro del logger
# los enmascare si alguno llegara a una traza por cualquier vía.
#
# Guardarlos aquí no añade exposición: ya están en memoria del proceso. Lo que
# añade es que el redactor sepa reconocerlos.
_SECRETOS_EN_MEMORIA: set[str] = set()


def registrar_secreto(valor: str) -> None:
    """Marca un valor obtenido en tiempo de ejecución como secreto.

    Se ignoran los valores cortos por el mismo motivo que en el entorno: un
    secreto de tres caracteres enmascararía media traza. No hay forma de
    «desregistrar» uno concreto a propósito —olvidarse de un secreto a medias es
    peor que recordarlo—; ``olvidar_secretos`` los limpia todos y existe para
    que los tests no se contaminen entre sí.
    """
    limpio = (valor or "").strip()
    if len(limpio) >= LONGITUD_MINIMA:
        _SECRETOS_EN_MEMORIA.add(limpio)


def olvidar_secretos() -> None:
    """Vacía el registro de secretos en memoria."""
    _SECRETOS_EN_MEMORIA.clear()


def valores_secretos(entorno: dict[str, str] | None = None) -> list[str]:
    """Devuelve los valores que deben ocultarse: del entorno y de la memoria.

    Se lee el entorno en cada llamada en vez de cachearlo, para que una
    variable definida después del arranque también quede cubierta.
    """
    fuente = os.environ if entorno is None else entorno
    valores = list(_SECRETOS_EN_MEMORIA)
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
