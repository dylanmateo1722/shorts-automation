"""Inspección de archivos de medios con FFmpeg.

Se usa ``ffmpeg -i`` y no ``ffprobe`` porque el binario que empaqueta
``imageio-ffmpeg`` no incluye ffprobe, y no queremos exigir un FFmpeg de
sistema para poder ejecutar en un runner limpio.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import AudioInvalido, DependenciaAusente, TiempoAgotado


def binario_ffmpeg() -> str:
    """Resuelve el binario de FFmpeg: primero el del sistema, luego el empaquetado."""
    del_sistema = shutil.which("ffmpeg")
    if del_sistema:
        return del_sistema
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise DependenciaAusente(
            "no hay FFmpeg en el PATH ni el paquete imageio-ffmpeg instalado"
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def binario_ffprobe() -> str | None:
    """Devuelve ffprobe si está disponible, o None.

    El FFmpeg que empaqueta ``imageio-ffmpeg`` **no incluye ffprobe**, así que
    no se puede exigir: el pipeline debe funcionar en un entorno que solo tenga
    el binario empaquetado. Cuando existe se prefiere, porque devuelve JSON en
    vez de un texto pensado para humanos, y ``InfoMedia.leido_con`` deja escrito
    cuál se usó en lugar de dejarlo a la imaginación.
    """
    return shutil.which("ffprobe")


@dataclass(frozen=True)
class InfoMedia:
    duracion_s: float
    ancho: int
    alto: int
    fps: float
    codec_video: str
    codec_audio: str
    formato_pixel: str = ""
    audio_sample_rate_hz: int = 0
    #: Herramienta con la que se leyeron estos valores: "ffprobe" o "ffmpeg".
    leido_con: str = "ffmpeg"

    @property
    def tiene_audio(self) -> bool:
        return bool(self.codec_audio)


def _fraccion(texto: str) -> float:
    """Convierte "30/1" o "30000/1001" en un número de cuadros por segundo."""
    try:
        if "/" in texto:
            num, den = texto.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        return float(texto)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _inspeccionar_con_ffprobe(path: Path, binario: str, timeout_s: int) -> InfoMedia:
    """Lee las propiedades del archivo del JSON de ffprobe."""
    argv = [
        binario, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        proceso = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise TiempoAgotado(f"ffprobe no respondió al inspeccionar {path}") from exc
    if proceso.returncode != 0:
        raise AudioInvalido(
            f"ffprobe no pudo abrir {path}: {(proceso.stderr or '').strip()[-300:]}"
        )
    try:
        datos = json.loads(proceso.stdout or "{}")
    except ValueError as exc:
        raise AudioInvalido(f"ffprobe devolvió un JSON ilegible para {path}: {exc}") from exc

    flujos = datos.get("streams") or []
    video = next((f for f in flujos if f.get("codec_type") == "video"), {})
    audio = next((f for f in flujos if f.get("codec_type") == "audio"), {})
    duracion = _fraccion(str(datos.get("format", {}).get("duration") or 0))
    if not duracion:
        duracion = _fraccion(str(video.get("duration") or 0))

    return InfoMedia(
        duracion_s=duracion,
        ancho=int(video.get("width") or 0),
        alto=int(video.get("height") or 0),
        fps=_fraccion(str(video.get("avg_frame_rate") or video.get("r_frame_rate") or 0)),
        codec_video=str(video.get("codec_name") or ""),
        codec_audio=str(audio.get("codec_name") or ""),
        formato_pixel=str(video.get("pix_fmt") or ""),
        audio_sample_rate_hz=int(audio.get("sample_rate") or 0),
        leido_con="ffprobe",
    )


def inspeccionar(path: Path, *, timeout_s: int = 300, preferir_ffprobe: bool = True) -> InfoMedia:
    """Lee resolución, fps, duración, códecs y formato de píxel de un archivo.

    Prefiere ffprobe cuando está disponible; si no, interpreta la salida de
    ``ffmpeg -i``, que es el mecanismo validado en Gate 0.5 y el único que
    funciona con el binario empaquetado.
    """
    if preferir_ffprobe and (ffprobe := binario_ffprobe()):
        return _inspeccionar_con_ffprobe(Path(path), ffprobe, timeout_s)
    try:
        proceso = subprocess.run(
            [binario_ffmpeg(), "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise TiempoAgotado(f"FFmpeg no respondió al inspeccionar {path}") from exc

    salida = (proceso.stderr or "") + (proceso.stdout or "")

    duracion = 0.0
    if m := re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", salida):
        duracion = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    ancho = alto = 0
    codec_video = ""
    if m := re.search(r"Stream #\d+:\d+.*?: Video: (\w+).*?, (\d+)x(\d+)", salida, re.S):
        codec_video, ancho, alto = m.group(1), int(m.group(2)), int(m.group(3))

    fps = 0.0
    if m := re.search(r"(\d+(?:\.\d+)?) fps", salida):
        fps = float(m.group(1))

    formato_pixel = ""
    if m := re.search(
        r"Stream #\d+:\d+.*?: Video: \w+.*?, (yuv\w+|rgb\w+|bgr\w+|gray\w*|nv\d+)", salida, re.S
    ):
        formato_pixel = m.group(1)

    codec_audio, sample_rate = "", 0
    if m := re.search(r"Stream #\d+:\d+.*?: Audio: (\w+).*?, (\d+) Hz", salida, re.S):
        codec_audio, sample_rate = m.group(1), int(m.group(2))
    elif m := re.search(r"Stream #\d+:\d+.*?: Audio: (\w+)", salida):
        codec_audio = m.group(1)

    return InfoMedia(
        duracion_s=duracion, ancho=ancho, alto=alto, fps=fps,
        codec_video=codec_video, codec_audio=codec_audio,
        formato_pixel=formato_pixel, audio_sample_rate_hz=sample_rate,
        leido_con="ffmpeg",
    )


def sha256_archivo(path: Path, *, bloque: int = 1 << 20) -> str:
    """Huella del archivo, leída por bloques para no cargarlo entero en memoria."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as archivo:
        for trozo in iter(lambda: archivo.read(bloque), b""):
            digest.update(trozo)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

# FFmpeg nombra la disposición de canales, no su número. Se traducen las que
# emite; cualquier otra cae en la forma genérica "N channels".
CANALES_POR_DISPOSICION = {
    "mono": 1,
    "stereo": 2,
    "quad": 4,
    "5.1": 6,
    "7.1": 8,
}


@dataclass(frozen=True)
class InfoAudio:
    """Propiedades **medidas** sobre un archivo de audio.

    Ninguna se estima: todas salen de lo que FFmpeg lee del contenedor. La
    duración de aquí es la que vale como final del audio, no la suma de los
    tiempos que reporte un TTS.
    """

    duracion_s: float
    formato: str
    codec: str
    sample_rate_hz: int
    canales: int
    bytes: int


def _canales(texto: str) -> int:
    if texto in CANALES_POR_DISPOSICION:
        return CANALES_POR_DISPOSICION[texto]
    if m := re.match(r"(\d+)(?:\.\d+)? channels?", texto):
        return int(m.group(1))
    return 0


def inspeccionar_audio(path: Path, *, timeout_s: int = 300) -> InfoAudio:
    """Lee duración, formato, códec, frecuencia y canales de un audio.

    Raises:
        AudioInvalido: si el archivo no existe, está vacío o FFmpeg no
            reconoce en él una pista de audio utilizable.
    """
    ruta = Path(path)
    if not ruta.is_file():
        raise AudioInvalido(f"no existe el audio {ruta}")
    tamano = ruta.stat().st_size
    if tamano == 0:
        raise AudioInvalido(f"el audio {ruta} está vacío")

    try:
        proceso = subprocess.run(
            [binario_ffmpeg(), "-hide_banner", "-i", str(ruta)],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise TiempoAgotado(f"FFmpeg no respondió al inspeccionar {ruta}") from exc

    salida = (proceso.stderr or "") + (proceso.stdout or "")

    duracion = 0.0
    if m := re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", salida):
        duracion = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    formato = ""
    if m := re.search(r"Input #0,\s*([^,]+),", salida):
        formato = m.group(1).strip()

    codec, sample_rate, canales = "", 0, 0
    if m := re.search(
        r"Stream #\d+:\d+.*?: Audio: (\w+).*?, (\d+) Hz, ([^,]+),", salida
    ):
        codec, sample_rate, canales = m.group(1), int(m.group(2)), _canales(m.group(3).strip())

    if not codec:
        raise AudioInvalido(f"FFmpeg no encontró una pista de audio en {ruta}")
    if duracion <= 0:
        raise AudioInvalido(f"el audio {ruta} declara una duración de {duracion}s")
    if sample_rate <= 0 or canales <= 0:
        raise AudioInvalido(
            f"el audio {ruta} no declara frecuencia o canales utilizables "
            f"({sample_rate} Hz, {canales} canal(es))"
        )

    return InfoAudio(
        duracion_s=duracion, formato=formato, codec=codec,
        sample_rate_hz=sample_rate, canales=canales, bytes=tamano,
    )


def comprobar_decodificable(path: Path, *, timeout_s: int = 300) -> None:
    """Decodifica el audio entero y descarta la salida.

    Un archivo puede tener cabecera válida y cuerpo truncado: eso solo se ve
    decodificándolo. Es barato para una narración de decenas de segundos.

    Raises:
        AudioInvalido: si FFmpeg no consigue decodificarlo.
    """
    try:
        proceso = subprocess.run(
            [binario_ffmpeg(), "-hide_banner", "-loglevel", "error",
             "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise TiempoAgotado(f"FFmpeg no respondió al decodificar {path}") from exc
    if proceso.returncode != 0:
        raise AudioInvalido(
            f"el audio {path} no se pudo decodificar: "
            f"{(proceso.stderr or '').strip()[-300:]}"
        )
