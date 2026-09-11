"""Taxonomía de errores del pipeline.

Se distinguen tres categorías porque cada una exige una respuesta distinta:

* **Transitorio** — puede reintentarse. Timeouts, 429, 5xx, red inestable.
* **Permanente** — reintentarlo repite el mismo fallo y gasta dinero.
  Configuración inválida, licencia denegada, entrada inválida, artefacto
  corrupto, QA reprobado.
* **Infraestructura** — el entorno no está en condiciones de ejecutar.
  Ejecutable ausente, dependencia faltante, disco lleno, permisos.

Gate 1 solo establece el contrato. La política de reintentos por etapa se
implementa en Gates posteriores.
"""

from __future__ import annotations


class ErrorPipeline(Exception):
    """Base de los errores del pipeline."""

    categoria = "desconocida"
    retryable = False

    def __init__(self, mensaje: str, *, stage: str | None = None) -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.stage = stage

    def a_dict(self) -> dict:
        """Representación serializable para el manifest."""
        return {
            "type": type(self).__name__,
            "category": self.categoria,
            "retryable": self.retryable,
            "stage": self.stage,
            "message": self.mensaje,
        }


class ErrorTransitorio(ErrorPipeline):
    categoria = "transitorio"
    retryable = True


class ErrorPermanente(ErrorPipeline):
    categoria = "permanente"
    retryable = False


class ErrorInfraestructura(ErrorPipeline):
    categoria = "infraestructura"
    retryable = False


# --- Transitorios ----------------------------------------------------------


class TiempoAgotado(ErrorTransitorio):
    """La operación excedió su tiempo máximo."""


class LimiteDeTasa(ErrorTransitorio):
    """El servicio remoto respondió con un límite de tasa."""


class ProveedorNoDisponible(ErrorTransitorio):
    """No se pudo hablar con el proveedor: conexión caída o servicio fuera.

    Es transitorio a propósito. Un TTS que no responde ahora suele responder
    dentro de un minuto, y confundirlo con un error de contenido llevaría a
    descartar un guion perfectamente válido.
    """


# --- Permanentes -----------------------------------------------------------


class EntradaInvalida(ErrorPermanente):
    """Los datos de entrada no cumplen el contrato."""


class ConfiguracionInvalida(ErrorPermanente):
    """Falta configuración obligatoria o tiene un valor imposible."""


class ArtefactoCorrupto(ErrorPermanente):
    """Un artefacto existe pero no puede leerse o no valida."""


class RespuestaInvalida(ErrorPermanente):
    """El proveedor respondió, pero la respuesta no es utilizable.

    JSON mal formado, esquema incorrecto o contenido incompleto. Reintentarlo
    a ciegas, como si fuera un fallo de red, suele repetir el mismo resultado
    y cuesta dinero.
    """


class ContenidoInvalido(ErrorPermanente):
    """La respuesta es estructuralmente válida pero no cumple el contenido.

    Idioma equivocado, cifras alteradas, datos inventados o duración
    imposible. Es un problema de contenido, no de transporte.
    """


class AudioInvalido(ErrorPermanente):
    """El audio generado no sirve: no existe, está vacío, no dura o no decodifica.

    Se separa de los errores del proveedor porque aquí el proveedor sí
    respondió: lo que falla es el archivo resultante, y reintentar la llamada
    no lo arregla por sí solo.
    """


class AlineacionInvalida(ErrorPermanente):
    """Los tiempos del TTS no pueden alinearse con el guion.

    Faltan palabras, sobran incompatibles o la correspondencia es ambigua.
    Nunca se adivina en silencio: un subtítulo desincronizado es peor que un
    fallo visible.
    """


class SubtituloInvalido(ErrorPermanente):
    """Los cues generados incumplen una regla que invalida el subtitulado."""


class AutorizacionInvalida(ErrorPermanente):
    """La autorización OAuth no sirve: revocada, caducada o de alcance corto.

    Es permanente **a propósito**. Reintentar una autorización revocada la deja
    igual de revocada, y hacerlo en bucle solo gasta cuota y esconde que lo que
    hace falta es que una persona repita el consentimiento.

    Se separa de ``ConfiguracionInvalida`` porque el remedio es otro: allí falta
    una variable, aquí la variable está y lo que ya no vale es lo que contiene.
    """


class LicenciaDenegada(ErrorPermanente):
    """La fuente no puede usarse. No es un fallo: es una decisión."""


class ProcedenciaInvalida(ErrorPermanente):
    """Se intentó usar en el render un recurso que no lo permite.

    Caso principal: un recurso marcado ``reference_only``. Informó el tema o el
    análisis, pero su material no puede aparecer en la pieza, y convertirlo en
    asset de render no es un descuido recuperable sino una decisión que nadie
    tomó.
    """


class TransformacionIncompleta(ErrorPermanente):
    """Falta algún elemento editorial propio obligatorio.

    No es un aviso: una corrida sin todos sus elementos registrados no puede
    declararse lista para el render, porque no hay evidencia de qué es propio.
    """


class ComposicionFallida(ErrorPermanente):
    """FFmpeg no pudo componer el vídeo final.

    Se distingue del fallo del motor de render a propósito: si el motor produjo
    su vídeo y lo que falla es la composición, repetir el render costaría
    minutos sin arreglar nada.
    """


class QAFallido(ErrorPermanente):
    """El resultado no pasó los controles técnicos."""


# --- Infraestructura -------------------------------------------------------


class EjecutableAusente(ErrorInfraestructura):
    """Falta un binario o un entorno necesario para ejecutar."""


class DependenciaAusente(ErrorInfraestructura):
    """Falta una dependencia del sistema."""


class EspacioInsuficiente(ErrorInfraestructura):
    """No hay espacio en disco para continuar."""


class PermisoDenegado(ErrorInfraestructura):
    """El proceso no tiene permisos sobre una ruta o recurso."""
