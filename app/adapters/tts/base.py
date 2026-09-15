"""Interfaz del proveedor de síntesis de voz.

El pipeline no conoce Edge TTS ni ningún otro proveedor: solo esta interfaz.
Cambiar de proveedor es escribir otra implementación, no tocar las etapas.

Qué devuelve y qué no
---------------------

``sintetizar`` devuelve el audio escrito y los tiempos que el proveedor haya
entregado, nada más. **No** construye el ``VoiceAsset``: hacerlo obligaría a
cada proveedor a conocer el directorio de la corrida, la huella del guion y la
medición del archivo, que son asuntos del pipeline. El proveedor sintetiza; el
pipeline convierte eso en contrato.

Tampoco devuelve la duración del audio. Se mide después sobre el archivo, y esa
separación es deliberada: Gate 0.5 comprobó que el último tiempo que reporta
Edge TTS queda casi un segundo antes del final real del MP3, así que un
proveedor no es fuente fiable de la duración de su propia salida.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from app.contracts.models import AdaptedScript

# Valores por defecto de la librería de Edge TTS, no inventados aquí. Se usan
# como neutros porque son los que aplica el servicio si no se indica nada.
RITMO_NEUTRO = "+0%"
TONO_NEUTRO = "+0Hz"


@dataclass(frozen=True)
class ConfiguracionVoz:
    """Qué voz usar y con qué ajustes.

    ``ritmo`` y ``tono`` se expresan como los acepta el proveedor (``"+10%"``,
    ``"-5Hz"``). No se traducen a una escala propia: inventar una unidad
    intermedia obligaría a mapearla de vuelta en cada proveedor.
    """

    voz: str
    ritmo: str = RITMO_NEUTRO
    tono: str = TONO_NEUTRO


@dataclass(frozen=True)
class LimiteTemporal:
    """Un tiempo por palabra tal como lo entrega el proveedor, ya en segundos.

    Es la forma neutra: cada proveedor traduce sus unidades aquí —Edge TTS
    cuenta en intervalos de 100 ns— para que el pipeline no las conozca.

    El texto viene del proveedor y **no** sirve como texto de subtítulo: llega
    sin puntuación y puede agrupar varios tokens del guion.
    """

    inicio_s: float
    duracion_s: float
    texto: str

    @property
    def fin_s(self) -> float:
        return self.inicio_s + self.duracion_s


@dataclass
class ResultadoSintesis:
    """Lo que un proveedor entrega: un archivo de audio y sus tiempos."""

    audio_path: Path
    limites: list[LimiteTemporal]
    voz: str
    formato: str
    bytes_audio: int
    # Incidencias no fatales del proveedor, para el log y el manifest.
    avisos: list[str] = field(default_factory=list)

    @property
    def fin_ultimo_limite_s(self) -> float:
        return max((lim.fin_s for lim in self.limites), default=0.0)


class ProveedorTTS(ABC):
    """Un proveedor capaz de narrar un guion y devolver sus tiempos."""

    #: Nombre corto del proveedor, tal como se registra en los artefactos.
    nombre: str = "desconocido"
    #: Motor de síntesis concreto. Un TTS no tiene "modelo" como un LLM.
    motor: str = "desconocido"
    #: Extensión y formato del audio que produce.
    formato: str = "mp3"

    @abstractmethod
    def sintetizar(
        self, guion: AdaptedScript, destino: Path, config: ConfiguracionVoz
    ) -> ResultadoSintesis:
        """Narra ``guion.full_text`` en ``destino`` y devuelve sus tiempos.

        Raises:
            ProveedorNoDisponible: la conexión falló o el servicio no responde.
            TiempoAgotado: el proveedor no terminó a tiempo.
            LimiteDeTasa: el proveedor rechazó la petición por volumen.
            AudioInvalido: el proveedor respondió sin audio utilizable.
            ConfiguracionInvalida: la voz o los ajustes no son válidos.
        """
