"""Proveedor sobre la API de Chat Completions compatible con OpenAI.

Una sola implementación cubre a la mayoría de proveedores —OpenAI, Moonshot,
DeepSeek, Groq, OpenRouter y otros— porque todos exponen el mismo contrato
HTTP; lo único que cambia es ``base_url`` y el nombre del modelo. Eso es
exactamente lo que Gate 0 pedía: no casarse con un proveedor antes de la
evaluación ciega.

Se usa ``urllib`` de la biblioteca estándar en vez de un SDK: la llamada es un
POST con JSON, y añadir una dependencia por eso acoplaría el proyecto a la
forma de un proveedor concreto.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

from app.adapters.llm.base import ProveedorLLM
from app.core.errors import (
    ConfiguracionInvalida,
    ErrorPipeline,
    ErrorTransitorio,
    LimiteDeTasa,
    RespuestaInvalida,
    TiempoAgotado,
)

RUTA_COMPLETIONS = "/chat/completions"

# Códigos que merecen reintento: el servidor no dijo que la petición esté mal,
# dijo que ahora no puede.
CODIGOS_TRANSITORIOS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: Reintentos ante un fallo transitorio, y base del backoff exponencial.
#:
#: Existen porque sin ellos un solo 429 tira la corrida entera. Contra una API
#: real el límite de tasa no es una anomalía: es lo que pasa cuando dos
#: peticiones caen juntas. Y el coste de rendirse es alto, porque para cuando
#: se llama al modelo ya se ingirió la fuente y el resto del pipeline espera.
MAX_INTENTOS = 4
BACKOFF_BASE_S = 2.0

#: Qué significa cada rechazo que no se reintenta, para no mandar a revisar lo
#: que está bien.
#:
#: Sin esto los cuatro casos llegan como «rechazó la petición con 4xx», y los
#: cuatro tienen remedios distintos e incompatibles: cambiar la clave, pagar,
#: cambiar de modelo o corregir el identificador. El texto sale de la tabla de
#: errores que publica OpenRouter, y es el mismo contrato en los demás
#: proveedores compatibles. El 401 se trata aparte porque su mensaje nombra la
#: variable de entorno del proveedor concreto, y eso no cabe en un texto fijo.
MOTIVOS_PERMANENTES = {
    402: (
        "la cuenta o la clave no tienen saldo; en un modelo gratuito suele "
        "significar que se agotó su cupo diario"
    ),
    403: (
        "petición prohibida: permisos insuficientes, o el modelo moderó la "
        "entrada y la marcó"
    ),
    404: (
        "el recurso no existe; lo habitual es que LLM_MODEL no sea un "
        "identificador válido de este proveedor"
    ),
}


class ProveedorOpenAICompatible(ProveedorLLM):
    """Cliente de Chat Completions para cualquier proveedor compatible."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        modelo: str,
        nombre: str = "openai_compatible",
        timeout_s: int = 120,
        modo_json: bool = True,
        temperatura: float = 0.3,
        max_intentos: int = MAX_INTENTOS,
        max_tokens: int = 4000,
        cabeceras_extra: dict[str, str] | None = None,
        dormir=None,
    ) -> None:
        if not base_url:
            raise ConfiguracionInvalida("falta LLM_BASE_URL para el proveedor LLM")
        if not modelo:
            raise ConfiguracionInvalida("falta LLM_MODEL para el proveedor LLM")
        if not api_key:
            raise ConfiguracionInvalida(
                f"falta {_variable_credencial(nombre)} en el entorno; la "
                f"credencial nunca se lee del código ni del manifest"
            )
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.modelo = modelo
        self.nombre = nombre
        self.timeout_s = timeout_s
        self.modo_json = modo_json
        self.temperatura = temperatura
        self.max_intentos = max(1, max_intentos)
        #: Presupuesto por defecto. Las etapas llaman sin indicarlo, así que si
        #: se quedara fijo en el código no habría forma de subirlo sin tocarlo.
        self.max_tokens = max_tokens
        #: Cabeceras propias del proveedor. Nunca llevan credenciales: la
        #: autorización va en su sitio y se añade aparte.
        self.cabeceras_extra = dict(cabeceras_extra or {})
        # Se guarda como atributo y no como valor por defecto del parámetro: un
        # valor por defecto se evalúa al importar, y entonces un test que
        # sustituya ``time.sleep`` no tendría efecto y esperaría de verdad.
        self._dormir = dormir or time.sleep

    def _cuerpo(self, prompt: str, max_tokens: int) -> dict:
        cuerpo = {
            "model": self.modelo,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperatura,
            "max_tokens": max_tokens,
        }
        if self.modo_json:
            # Cuando el proveedor lo soporta, evita que el modelo envuelva la
            # respuesta en prosa. La validación estricta se hace igualmente.
            cuerpo["response_format"] = {"type": "json_object"}
        return cuerpo

    def generar_json(self, prompt: str, *, max_tokens: int | None = None) -> dict:
        """Llama al modelo, reintentando solo lo que tiene sentido reintentar.

        Un 400 no se arregla repitiéndolo; un 429 o un 503 sí. Rendirse ante el
        segundo tira una corrida en la que ya se ingirió la fuente.
        """
        presupuesto = max_tokens or self.max_tokens
        ultimo: ErrorPipeline | None = None
        for intento in range(1, self.max_intentos + 1):
            try:
                return self._una_llamada(prompt, presupuesto)
            except (LimiteDeTasa, ErrorTransitorio, TiempoAgotado) as exc:
                ultimo = exc
                if intento == self.max_intentos:
                    break
                espera = BACKOFF_BASE_S * (2 ** (intento - 1))
                pedida = getattr(exc, "espera_s", None)
                if isinstance(pedida, (int, float)) and pedida > 0:
                    espera = max(espera, float(pedida))
                self._dormir(espera)
        raise ultimo

    def _una_llamada(self, prompt: str, max_tokens: int) -> dict:
        peticion = urllib.request.Request(
            f"{self.base_url}{RUTA_COMPLETIONS}",
            data=json.dumps(self._cuerpo(prompt, max_tokens)).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                **self.cabeceras_extra,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(peticion, timeout=self.timeout_s) as respuesta:
                bruto = respuesta.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # El cuerpo del error puede repetir la credencial: no se propaga.
            if exc.code == 429:
                limite = LimiteDeTasa(
                    f"el proveedor {self.nombre} aplicó límite de tasa (429)"
                )
                # Muchos proveedores dicen cuánto esperar. Respetarlo es más
                # eficaz —y más educado— que insistir con nuestro propio ritmo.
                limite.espera_s = _retry_after(exc)
                raise limite from exc
            if exc.code in CODIGOS_TRANSITORIOS:
                raise ErrorTransitorio(
                    f"el proveedor {self.nombre} respondió {exc.code}"
                ) from exc
            if exc.code == 401:
                # Se nombra la variable concreta porque decir solo «401» manda a
                # revisar también el modelo y la base_url, que no tienen nada
                # que ver. La clase no cambia: sigue siendo un rechazo
                # permanente, y por tanto no se reintenta.
                raise RespuestaInvalida(
                    f"el proveedor {self.nombre} rechazó la credencial (401): "
                    f"revisa {_variable_credencial(self.nombre)}. No se "
                    f"reintenta, porque una clave inválida sigue siéndolo"
                ) from exc
            if motivo := MOTIVOS_PERMANENTES.get(exc.code):
                raise RespuestaInvalida(
                    f"el proveedor {self.nombre} rechazó la petición con "
                    f"{exc.code}: {motivo}"
                ) from exc
            raise RespuestaInvalida(
                f"el proveedor {self.nombre} rechazó la petición con {exc.code}"
            ) from exc
        except socket.timeout as exc:
            raise TiempoAgotado(
                f"el proveedor {self.nombre} no respondió en {self.timeout_s}s"
            ) from exc
        except urllib.error.URLError as exc:
            raise ErrorTransitorio(
                f"no se pudo contactar con el proveedor {self.nombre}"
            ) from exc

        return self.interpretar_json(self._extraer_contenido(bruto))

    @staticmethod
    def _extraer_contenido(bruto: str) -> str:
        """Saca el texto del mensaje de la envoltura de Chat Completions.

        Antes de devolverlo comprueba ``finish_reason``. Si el modelo se quedó
        sin presupuesto de tokens, el JSON llega cortado a media llave y el
        error genérico diría «no es JSON válido», que manda a buscar el
        problema al sitio equivocado: el JSON está mal porque falta, no porque
        el modelo lo escribiera mal.
        """
        try:
            datos = json.loads(bruto)
            eleccion = datos["choices"][0]
            contenido = eleccion["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RespuestaInvalida(
                f"la envoltura de la respuesta no tiene la forma esperada: {exc}"
            ) from exc

        if eleccion.get("finish_reason") == "length":
            raise RespuestaInvalida(
                "el modelo agotó el presupuesto de tokens y la respuesta llegó "
                "cortada; sube LLM_MAX_TOKENS o acorta la fuente con "
                "--max-caracteres"
            )
        return contenido


def _variable_credencial(nombre: str) -> str:
    """La variable de entorno donde ese proveedor espera su clave.

    Se nombra la que el proveedor entrega, no una genérica: quien saca la clave
    de OpenRouter la tiene como ``OPENROUTER_API_KEY``, y mandarle a otra
    variable es mandarle a buscar lo que ya tiene.
    """
    if nombre == "openrouter":
        return "OPENROUTER_API_KEY (o LLM_API_KEY)"
    return "LLM_API_KEY"


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """Los segundos que el proveedor pide esperar, si los pide en segundos."""
    try:
        bruto = (exc.headers or {}).get("Retry-After")
    except Exception:  # noqa: BLE001 - unas cabeceras raras no rompen la llamada
        return None
    if not bruto:
        return None
    try:
        return max(0.0, float(str(bruto).strip()))
    except ValueError:
        # También admite una fecha HTTP; no se interpreta, se usa el backoff.
        return None
