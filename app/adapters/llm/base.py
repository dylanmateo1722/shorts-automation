"""Interfaz del proveedor de modelo de lenguaje.

El pipeline no conoce ningún SDK ni ningún proveedor concreto: solo esta
interfaz. Cambiar de proveedor es escribir otra implementación, no tocar las
etapas.

La interfaz es deliberadamente estrecha —una llamada que devuelve JSON— para
que cada proveedor nuevo no tenga que reimplementar carga de prompts, parseo
ni construcción de contratos. Esa lógica vive una sola vez, en la capa
lingüística.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

from app.core.errors import RespuestaInvalida

# Algunos modelos envuelven el JSON en un bloque de código pese a pedirles que
# no lo hagan. Quitarlo es determinista y barato; no quitarlo obliga a
# descartar una respuesta por lo demás correcta.
#
# La valla se busca en cualquier posición, no solo abarcando toda la respuesta:
# un modelo que escriba «Aquí tienes el JSON:» antes del bloque es corriente,
# sobre todo en los modelos gratuitos y cuando no se puede pedir
# ``response_format`` porque el proveedor rechazaría la petición.
_VALLA = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _objeto_mas_externo(texto: str) -> str | None:
    """El primer objeto JSON completo del texto, si lo hay.

    Último recurso cuando el modelo rodea el JSON de prosa y no lo encierra en
    una valla. Se cuentan llaves respetando las cadenas y los escapes: contar a
    secas rompería con una llave dentro de un texto, que en un guion en español
    es perfectamente posible.

    Devuelve ``None`` si no hay ningún objeto equilibrado; no adivina.
    """
    inicio = texto.find("{")
    if inicio < 0:
        return None
    profundidad = 0
    en_cadena = False
    escapado = False
    for posicion in range(inicio, len(texto)):
        caracter = texto[posicion]
        if en_cadena:
            if escapado:
                escapado = False
            elif caracter == "\\":
                escapado = True
            elif caracter == '"':
                en_cadena = False
            continue
        if caracter == '"':
            en_cadena = True
        elif caracter == "{":
            profundidad += 1
        elif caracter == "}":
            profundidad -= 1
            if profundidad == 0:
                return texto[inicio : posicion + 1]
    return None


class ProveedorLLM(ABC):
    """Un proveedor capaz de devolver un objeto JSON a partir de un prompt."""

    #: Nombre corto del proveedor, tal como se registra en los artefactos.
    nombre: str = "desconocido"
    #: Identificador del modelo concreto.
    modelo: str = "desconocido"

    @abstractmethod
    def generar_json(self, prompt: str, *, max_tokens: int = 4000) -> dict:
        """Envía el prompt y devuelve la respuesta ya interpretada como JSON.

        Raises:
            ErrorTransitorio: fallos de transporte (timeout, 429, 5xx).
            RespuestaInvalida: la respuesta no es un objeto JSON utilizable.
        """

    @staticmethod
    def interpretar_json(texto: str) -> dict:
        """Convierte la respuesta del modelo en un diccionario.

        Raises:
            RespuestaInvalida: si no hay JSON, o si no es un objeto.
        """
        if not texto or not texto.strip():
            raise RespuestaInvalida("el proveedor devolvió una respuesta vacía")
        limpio = texto.strip()

        # Tres intentos, del más fiel al más tolerante. El orden importa: si el
        # modelo devolvió JSON limpio no se le toca nada, y solo cuando eso
        # falla se empieza a desenvolver. Al revés se correría el riesgo de
        # extraer un objeto anidado de una respuesta que ya era correcta.
        candidatos = [limpio]
        if valla := _VALLA.search(limpio):
            candidatos.append(valla.group(1))
        if (suelto := _objeto_mas_externo(limpio)) is not None:
            candidatos.append(suelto)

        ultimo: ValueError | None = None
        for candidato in candidatos:
            try:
                datos = json.loads(candidato)
            except ValueError as exc:
                ultimo = exc
                continue
            if not isinstance(datos, dict):
                raise RespuestaInvalida(
                    f"se esperaba un objeto JSON y llegó {type(datos).__name__}"
                )
            return datos

        raise RespuestaInvalida(
            f"la respuesta del proveedor no es JSON válido: {ultimo}"
        )
