"""Proveedor de TTS sobre Edge TTS.

Se llama a la librería directamente en lugar de delegar el TTS en
MoneyPrinterTurbo, porque necesitamos los eventos ``WordBoundary`` para
construir nuestros propios subtítulos (decisión D4). Si el TTS lo hiciera el
motor de render, esos tiempos quedarían dentro de su proceso.

Edge TTS no requiere ninguna credencial. Esta es la única implementación de
``ProveedorTTS`` que habla con un servicio externo; la lógica de síntesis es la
que se validó en Gate 0.5 y aquí solo se le pone la interfaz delante.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.adapters.tts.base import (
    ConfiguracionVoz,
    LimiteTemporal,
    ProveedorTTS,
    ResultadoSintesis,
)
from app.contracts.models import AdaptedScript
from app.core.errors import (
    AudioInvalido,
    ConfiguracionInvalida,
    ProveedorNoDisponible,
    TiempoAgotado,
)

#: Voz por defecto. Medida en Gate 0.5 (119 palabras por minuto). No se afirma
#: que sea mejor que otra: es la única sobre la que tenemos medición.
VOZ_POR_DEFECTO = "es-CR-JuanNeural"

#: Edge TTS mide en intervalos de 100 ns.
TICKS_POR_SEGUNDO = 10_000_000


class ProveedorEdgeTTS(ProveedorTTS):
    """Síntesis con el servicio de Edge TTS, sin credenciales."""

    nombre = "edge-tts"
    motor = "edge-tts-neural"
    formato = "mp3"

    def __init__(self, *, timeout_s: int = 120) -> None:
        self.timeout_s = timeout_s

    # --- síntesis ----------------------------------------------------------

    async def _sintetizar(
        self, texto: str, config: ConfiguracionVoz, destino: Path
    ) -> tuple[list[dict], int]:
        import edge_tts

        comunicador = edge_tts.Communicate(
            texto,
            config.voz,
            rate=config.ritmo,
            pitch=config.tono,
            boundary="WordBoundary",
        )
        eventos: list[dict] = []
        bytes_audio = 0
        with destino.open("wb") as salida:
            async for chunk in comunicador.stream():
                tipo = chunk.get("type")
                if tipo == "audio":
                    datos = chunk["data"]
                    salida.write(datos)
                    bytes_audio += len(datos)
                elif tipo == "WordBoundary":
                    eventos.append(
                        {
                            "offset": chunk["offset"],
                            "duration": chunk["duration"],
                            "text": chunk.get("text", ""),
                        }
                    )
        return eventos, bytes_audio

    def sintetizar(
        self, guion: AdaptedScript, destino: Path, config: ConfiguracionVoz
    ) -> ResultadoSintesis:
        """Narra el guion y devuelve los ``WordBoundary`` ya en segundos."""
        import edge_tts.exceptions as excepciones_edge

        destino.parent.mkdir(parents=True, exist_ok=True)
        try:
            eventos, bytes_audio = asyncio.run(
                asyncio.wait_for(
                    self._sintetizar(guion.full_text, config, destino),
                    timeout=self.timeout_s,
                )
            )
        except asyncio.TimeoutError as exc:
            raise TiempoAgotado(
                f"Edge TTS no terminó en {self.timeout_s}s con la voz {config.voz!r}",
                stage="voice",
            ) from exc
        except excepciones_edge.NoAudioReceived as exc:
            raise AudioInvalido(
                f"Edge TTS no devolvió audio con la voz {config.voz!r}", stage="voice"
            ) from exc
        except excepciones_edge.EdgeTTSException as exc:
            # Engloba WebSocketError, UnexpectedResponse y UnknownResponse: el
            # servicio respondió algo que no esperamos o no respondió. Todas son
            # transitorias, no un problema del guion.
            raise ProveedorNoDisponible(
                f"Edge TTS falló: {type(exc).__name__}", stage="voice"
            ) from exc
        except ValueError as exc:
            # La librería valida voz, ritmo y tono con ValueError.
            raise ConfiguracionInvalida(
                f"Edge TTS rechazó la configuración de voz "
                f"(voz={config.voz!r}, ritmo={config.ritmo!r}, tono={config.tono!r}): {exc}",
                stage="voice",
            ) from exc
        except OSError as exc:
            raise ProveedorNoDisponible(
                f"no se pudo hablar con Edge TTS: {exc}", stage="voice"
            ) from exc

        if bytes_audio == 0:
            raise AudioInvalido("Edge TTS no devolvió audio", stage="voice")

        limites = [
            LimiteTemporal(
                inicio_s=evento["offset"] / TICKS_POR_SEGUNDO,
                duracion_s=evento["duration"] / TICKS_POR_SEGUNDO,
                texto=evento["text"],
            )
            for evento in eventos
            if str(evento.get("text") or "").strip() and evento["duration"] > 0
        ]
        if not limites:
            raise AudioInvalido(
                "Edge TTS devolvió audio pero ningún WordBoundary utilizable; "
                "sin tiempos no se pueden sincronizar subtítulos",
                stage="voice",
            )

        descartados = len(eventos) - len(limites)
        avisos = (
            [f"{descartados} evento(s) de Edge TTS sin texto o sin duración"]
            if descartados
            else []
        )
        return ResultadoSintesis(
            audio_path=destino,
            limites=limites,
            voz=config.voz,
            formato=self.formato,
            bytes_audio=bytes_audio,
            avisos=avisos,
        )
