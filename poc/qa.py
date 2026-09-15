"""Controles de calidad del MP4 final.

Separación deliberada, exigida por la corrección de D6:

* ``TECHNICAL_QA`` comprueba propiedades medibles del archivo. Es determinista
  y puede bloquear.
* ``EDITORIAL_LEGAL_ASSESSMENT`` **no se automatiza**. Que los elementos de
  transformación estén presentes no significa que la pieza sea jurídicamente
  transformativa, suficientemente original ni monetizable. Ese juicio es
  humano y aquí solo se declara como no evaluado.

Este módulo no implementa ningún umbral de similitud como criterio de
aprobación automática, y no existe para eludir Content ID ni ningún mecanismo
de detección.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .burn_in import binario_ffmpeg


@dataclass
class InfoMedia:
    """Propiedades leídas del contenedor con FFmpeg."""

    duracion_s: float
    ancho: int
    alto: int
    fps: float
    codec_video: str
    tiene_audio: bool
    codec_audio: str
    volumen_medio_db: float | None = None


def _ejecutar_ffmpeg(argv: list[str]) -> str:
    proceso = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    return (proceso.stderr or "") + (proceso.stdout or "")


def inspeccionar(path: Path) -> InfoMedia:
    """Lee resolución, fps, duración y audio del archivo.

    Se usa ``ffmpeg -i`` en vez de ``ffprobe`` porque el binario que empaqueta
    ``imageio-ffmpeg`` no incluye ffprobe, y no queremos exigir un FFmpeg de
    sistema para poder ejecutar en un runner limpio.
    """
    salida = _ejecutar_ffmpeg([binario_ffmpeg(), "-hide_banner", "-i", str(path)])

    duracion = 0.0
    if m := re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", salida):
        h, mnt, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        duracion = h * 3600 + mnt * 60 + s

    ancho = alto = 0
    codec_video = ""
    fps = 0.0
    if m := re.search(r"Stream #\d+:\d+.*?: Video: (\w+).*?, (\d+)x(\d+)", salida, re.S):
        codec_video, ancho, alto = m.group(1), int(m.group(2)), int(m.group(3))
    if m := re.search(r"(\d+(?:\.\d+)?) fps", salida):
        fps = float(m.group(1))

    codec_audio = ""
    if m := re.search(r"Stream #\d+:\d+.*?: Audio: (\w+)", salida):
        codec_audio = m.group(1)

    volumen = None
    if codec_audio:
        det = _ejecutar_ffmpeg(
            [binario_ffmpeg(), "-hide_banner", "-i", str(path),
             "-af", "volumedetect", "-f", "null", "-"]
        )
        if m := re.search(r"mean_volume:\s*(-?\d+\.?\d*) dB", det):
            volumen = float(m.group(1))

    return InfoMedia(
        duracion_s=duracion, ancho=ancho, alto=alto, fps=fps,
        codec_video=codec_video, tiene_audio=bool(codec_audio),
        codec_audio=codec_audio, volumen_medio_db=volumen,
    )


def duracion_media(path: Path) -> float:
    """Duración real del archivo, leída del contenedor."""
    return inspeccionar(path).duracion_s


@dataclass
class ResultadoQA:
    """Informe de QA con las dos capas separadas."""

    technical_qa_ok: bool
    comprobaciones: list[dict]
    info: InfoMedia
    elementos_transformacion: list[dict] = field(default_factory=list)
    editorial_legal_assessment: dict = field(default_factory=dict)

    def resumen(self) -> str:
        return "\n".join(
            f"  [{'OK   ' if c['ok'] else 'FALLA'}] {c['nombre']}: {c['detalle']}"
            for c in self.comprobaciones
        )


def evaluar(
    video: Path,
    duracion_audio_s: float,
    elementos_transformacion: list[dict],
    *,
    ancho_esperado: int = 1080,
    alto_esperado: int = 1920,
    fps_esperado: float = 30.0,
    tolerancia_duracion_s: float = 0.5,
    umbral_silencio_db: float = -60.0,
) -> ResultadoQA:
    """Ejecuta el QA técnico y declara el estado del juicio editorial/legal."""
    info = inspeccionar(video)
    comprobaciones: list[dict] = []

    def añadir(nombre: str, ok: bool, detalle: str) -> None:
        comprobaciones.append({"nombre": nombre, "ok": bool(ok), "detalle": detalle})

    añadir("archivo existe y no está vacío",
           video.exists() and video.stat().st_size > 0,
           f"{video.stat().st_size if video.exists() else 0} bytes")
    añadir("resolución vertical",
           (info.ancho, info.alto) == (ancho_esperado, alto_esperado),
           f"{info.ancho}x{info.alto} (esperado {ancho_esperado}x{alto_esperado})")
    añadir("fps", abs(info.fps - fps_esperado) < 0.5, f"{info.fps} fps")
    añadir("pista de audio presente", info.tiene_audio, info.codec_audio or "sin audio")
    añadir("audio no mudo",
           info.volumen_medio_db is not None and info.volumen_medio_db > umbral_silencio_db,
           f"volumen medio {info.volumen_medio_db} dB" if info.volumen_medio_db is not None
           else "no medido")
    añadir("duración coincide con la narración",
           abs(info.duracion_s - duracion_audio_s) <= tolerancia_duracion_s,
           f"video {info.duracion_s:.2f}s vs audio {duracion_audio_s:.2f}s "
           f"(tolerancia ±{tolerancia_duracion_s}s)")

    tipos = sorted({e["kind"] for e in elementos_transformacion})
    añadir("elementos de transformación registrados",
           bool(tipos), ", ".join(tipos) or "ninguno")

    return ResultadoQA(
        technical_qa_ok=all(c["ok"] for c in comprobaciones),
        comprobaciones=comprobaciones,
        info=info,
        elementos_transformacion=elementos_transformacion,
        editorial_legal_assessment={
            "status": "NOT_ASSESSED",
            "note": (
                "La presencia de elementos de transformación NO implica que el "
                "resultado sea jurídicamente transformativo, suficientemente "
                "original ni monetizable. Este juicio es editorial y legal, "
                "requiere revisión humana y no se automatiza."
            ),
            "automated_similarity_threshold_applied": False,
        },
    )
