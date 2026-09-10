"""Síntesis de narración en español con Edge TTS.

Se llama a la librería directamente en lugar de delegar el TTS en
MoneyPrinterTurbo, porque necesitamos los eventos ``WordBoundary`` para
construir nuestros propios subtítulos (decisión D4). Si el TTS lo hiciera MPT,
esos tiempos quedarían dentro de su proceso.

Edge TTS no requiere ninguna credencial.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import edge_tts

VOZ_POR_DEFECTO = "es-CR-JuanNeural"
TICKS_POR_SEGUNDO = 10_000_000


@dataclass
class ResultadoTTS:
    """Narración sintetizada junto con sus tiempos por palabra."""

    audio_path: Path
    eventos: list[dict]
    proveedor: str
    voz: str
    bytes_audio: int
    fin_ultimo_evento_s: float
    # Duración real del archivo. Edge TTS deja una cola de audio después del
    # último WordBoundary (~0,9 s medidos con es-CR-JuanNeural), así que el
    # fin del último evento NO sirve como duración del audio.
    duracion_real_s: float = 0.0
    fallback_usado: bool = False
    incidencias: list[str] = field(default_factory=list)


async def _sintetizar(texto: str, voz: str, destino: Path) -> tuple[list[dict], int]:
    comunicador = edge_tts.Communicate(texto, voz, boundary="WordBoundary")
    eventos: list[dict] = []
    bytes_audio = 0
    with destino.open("wb") as salida:
        async for chunk in comunicador.stream():
            tipo = chunk.get("type")
            if tipo == "audio":
                datos = chunk["data"]
                salida.write(datos)
                bytes_audio += len(datos)
            elif tipo in ("WordBoundary", "SentenceBoundary"):
                eventos.append(
                    {
                        "type": tipo,
                        "offset": chunk["offset"],
                        "duration": chunk["duration"],
                        "text": chunk.get("text", ""),
                    }
                )
    return eventos, bytes_audio


def sintetizar(texto: str, destino: Path, voz: str = VOZ_POR_DEFECTO) -> ResultadoTTS:
    """Genera el MP3 de narración y devuelve sus eventos de tiempo.

    Raises:
        RuntimeError: si el servicio no devuelve audio o no devuelve tiempos.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    eventos, bytes_audio = asyncio.run(_sintetizar(texto, voz, destino))

    if bytes_audio == 0:
        raise RuntimeError("Edge TTS no devolvió audio")
    palabras = [e for e in eventos if e["type"] == "WordBoundary"]
    if not palabras:
        raise RuntimeError("Edge TTS no devolvió eventos WordBoundary")

    from .qa import duracion_media

    ultimo = palabras[-1]
    return ResultadoTTS(
        audio_path=destino,
        eventos=eventos,
        proveedor="edge-tts",
        voz=voz,
        bytes_audio=bytes_audio,
        fin_ultimo_evento_s=(ultimo["offset"] + ultimo["duration"]) / TICKS_POR_SEGUNDO,
        duracion_real_s=duracion_media(destino),
    )
