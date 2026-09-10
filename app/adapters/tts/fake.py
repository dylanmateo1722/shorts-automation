"""Proveedor de TTS falso: determinista, sin red y sin credenciales.

Es el que usan los tests y CI. No simula el audio: lo genera de verdad con
FFmpeg, de modo que la duración se mide sobre un archivo real y el camino de
medición se ejercita igual que con el proveedor de verdad.

Reproduce a propósito las tres conductas de Edge TTS que complican el
subtitulado, comprobadas en Gate 0.5:

1. el texto del evento llega **sin puntuación**;
2. un número se agrupa con la palabra siguiente en un solo evento
   (``"73 %"``, ``"3 segundos"``);
3. el audio **sigue sonando después del último evento**.

Sin las tres, un test con proveedor falso daría verde sobre un problema que el
proveedor real sí tiene.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.adapters.media import binario_ffmpeg
from app.adapters.tts.base import (
    ConfiguracionVoz,
    LimiteTemporal,
    ProveedorTTS,
    ResultadoSintesis,
)
from app.contracts.models import AdaptedScript
from app.core.errors import AudioInvalido, DependenciaAusente, TiempoAgotado
from app.subtitles.segmenter import PUNTUACION_IGNORABLE

#: Palabras por minuto con las que reparte los tiempos. Es la medida de Gate
#: 0.5 para que la duración del audio falso sea del mismo orden que la
#: estimación del guion, no porque el falso locute nada.
PALABRAS_POR_MINUTO = 119

#: Cola de audio tras el último evento. Los 0,96 s medidos con
#: ``es-CR-JuanNeural`` en Gate 0.5, para que los tests vean la cola que el
#: proveedor real deja.
COLA_S = 0.96

#: Silencio antes de la primera palabra, como el que deja Edge TTS.
ENTRADA_S = 0.10

#: Separación entre eventos consecutivos.
SEPARACION_S = 0.04

# Propiedades del audio que genera. Coinciden con las del MP3 de Edge TTS para
# que el artefacto del falso sea indistinguible en forma del real.
FRECUENCIA_HZ = 24_000
CANALES = 1


def _tokenizar(texto: str) -> list[str]:
    """Parte el guion como lo haría el TTS: sin puntuación y agrupando números.

    El resultado imita la forma de los eventos, no el texto del guion. Los
    subtítulos nunca se construyen desde aquí.
    """
    crudos = []
    for palabra in texto.split():
        limpia = "".join(c for c in palabra if c not in PUNTUACION_IGNORABLE).strip()
        if limpia:
            crudos.append(limpia)

    agrupados: list[str] = []
    i = 0
    while i < len(crudos):
        actual = crudos[i]
        # Un número suelto viaja con el token siguiente, como hace Edge TTS.
        if actual.isdigit() and i + 1 < len(crudos):
            agrupados.append(f"{actual} {crudos[i + 1]}")
            i += 2
            continue
        agrupados.append(actual)
        i += 1
    return agrupados


class ProveedorTTSFalso(ProveedorTTS):
    """Síntesis determinista con audio real y tiempos derivados del texto."""

    nombre = "fake"
    motor = "fake-tts-1"
    formato = "mp3"

    def __init__(self, *, palabras_por_minuto: int = PALABRAS_POR_MINUTO) -> None:
        self.palabras_por_minuto = palabras_por_minuto
        #: Cuántas veces se ha llamado. Permite a un test demostrar que la
        #: idempotencia evita una segunda síntesis.
        self.llamadas = 0

    # --- tiempos -----------------------------------------------------------

    def _limites(self, texto: str) -> list[LimiteTemporal]:
        tokens = _tokenizar(texto)
        if not tokens:
            raise AudioInvalido(
                "el guion no contiene ninguna palabra que narrar", stage="voice"
            )

        palabras = len(texto.split())
        habla_s = palabras / self.palabras_por_minuto * 60
        # El tiempo de habla se reparte en proporción a la longitud del token,
        # descontando las separaciones.
        separaciones = SEPARACION_S * max(0, len(tokens) - 1)
        util_s = max(habla_s - separaciones, 0.05 * len(tokens))
        total_caracteres = sum(len(t) for t in tokens)

        limites: list[LimiteTemporal] = []
        reloj = ENTRADA_S
        for token in tokens:
            duracion = max(util_s * len(token) / total_caracteres, 0.05)
            limites.append(LimiteTemporal(inicio_s=reloj, duracion_s=duracion, texto=token))
            reloj += duracion + SEPARACION_S
        return limites

    # --- audio -------------------------------------------------------------

    def _generar_audio(self, destino: Path, duracion_s: float) -> int:
        destino.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency=220:duration={duracion_s:.3f}",
            "-ar", str(FRECUENCIA_HZ), "-ac", str(CANALES),
            "-c:a", "libmp3lame", str(destino),
        ]
        try:
            proceso = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        except FileNotFoundError as exc:
            raise DependenciaAusente(
                f"no se pudo ejecutar FFmpeg para el audio falso: {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TiempoAgotado("FFmpeg no terminó generando el audio falso") from exc
        if proceso.returncode != 0:
            raise AudioInvalido(
                f"FFmpeg falló generando el audio falso: "
                f"{(proceso.stderr or '').strip()[-300:]}",
                stage="voice",
            )
        return destino.stat().st_size

    # --- interfaz ----------------------------------------------------------

    def sintetizar(
        self, guion: AdaptedScript, destino: Path, config: ConfiguracionVoz
    ) -> ResultadoSintesis:
        self.llamadas += 1
        limites = self._limites(guion.full_text)
        # El audio dura más que el último evento: es la cola que deja el
        # proveedor real y que obliga a medir el archivo.
        bytes_audio = self._generar_audio(destino, limites[-1].fin_s + COLA_S)
        return ResultadoSintesis(
            audio_path=destino,
            limites=limites,
            voz=config.voz,
            formato=self.formato,
            bytes_audio=bytes_audio,
            avisos=[],
        )
