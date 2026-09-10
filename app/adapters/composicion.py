"""Composición final con FFmpeg: subtítulos y overlay sobre el vídeo del motor.

MoneyPrinterTurbo no acepta un SRT externo, así que renderiza sin subtítulos y
los incrustamos después. El coste es una segunda pasada de codificación de
vídeo; el audio se copia sin recodificar para no degradar la narración.

Todo el texto sobre vídeo pasa por **libass**, nunca por ``drawtext``: el
FFmpeg que empaqueta ``imageio-ffmpeg`` no incluye ese filtro. Comprobado sobre
el binario real en Gate 0.5, y es la razón de que el overlay de Gate 4 sea un
ASS y no una imagen.

Las capas se encadenan en un solo filtro (``ass=…,ass=…``) y en una sola pasada:
componer dos veces recodificaría el vídeo dos veces sin ganar nada.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.adapters.media import binario_ffmpeg
from app.core.errors import ComposicionFallida, DependenciaAusente, TiempoAgotado

#: Calidad de la segunda pasada. Este es el artefacto que se publica, así que
#: se prioriza calidad sobre tamaño.
CRF_POR_DEFECTO = 18
PRESET_POR_DEFECTO = "medium"

#: Nombre de la composición que se registra en el RenderJob. Si algún día se
#: compone de otra forma, el artefacto dirá cuál se usó.
NOMBRE_COMPOSICION = "ffmpeg-libass-burn-in"


def _escapar_para_filtro(ruta: Path) -> str:
    """Escapa una ruta para meterla dentro de la descripción de un filtro.

    Dentro de un filtro, ``:`` separa opciones y ``\\`` escapa, así que una
    ruta sin escapar rompe el grafo o apunta a otro sitio.
    """
    return str(ruta.resolve()).replace("\\", "/").replace(":", r"\:")


def filtro_de_capas(capas: list[Path]) -> str:
    """Construye el filtro que superpone las capas ASS en orden."""
    return ",".join(f"ass='{_escapar_para_filtro(capa)}'" for capa in capas)


@dataclass(frozen=True)
class ResultadoComposicion:
    salida: Path
    capas: list[str]
    filtro: str
    exit_code: int


def componer(
    *,
    entrada: Path,
    capas_ass: list[Path],
    salida: Path,
    crf: int = CRF_POR_DEFECTO,
    preset: str = PRESET_POR_DEFECTO,
    timeout_s: int = 1800,
) -> ResultadoComposicion:
    """Superpone las capas ASS sobre el vídeo y escribe el MP4 final.

    Raises:
        ComposicionFallida: si FFmpeg falla, o si termina bien pero no deja un
            archivo utilizable. Un MP4 truncado es peor que ninguno: parece
            válido hasta que alguien lo reproduce.
        TiempoAgotado: si FFmpeg no termina a tiempo.
        DependenciaAusente: si no hay FFmpeg.
    """
    if not entrada.is_file():
        raise ComposicionFallida(
            f"no existe el vídeo de entrada {entrada}", stage="composition"
        )
    faltantes = [str(c) for c in capas_ass if not c.is_file()]
    if faltantes:
        raise ComposicionFallida(
            f"faltan capas para componer: {faltantes}", stage="composition"
        )
    if not capas_ass:
        raise ComposicionFallida(
            "no hay ninguna capa que componer; el vídeo final sería el del motor",
            stage="composition",
        )

    salida.parent.mkdir(parents=True, exist_ok=True)
    # Se escribe a un temporal y se mueve al final: así un fallo a mitad no deja
    # un MP4 parcial ocupando el sitio del final. La marca va **antes** de la
    # extensión: FFmpeg deduce el contenedor del sufijo, y un ".mp4.parcial" le
    # deja sin muxer que elegir.
    temporal = salida.with_name(f"{salida.stem}.parcial{salida.suffix}")
    filtro = filtro_de_capas(capas_ass)
    argv = [
        binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(entrada.resolve()),
        "-vf", filtro,
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(temporal.resolve()),
    ]

    try:
        proceso = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError as exc:
        raise DependenciaAusente(f"no se pudo ejecutar FFmpeg: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        temporal.unlink(missing_ok=True)
        raise TiempoAgotado(
            f"FFmpeg no terminó la composición en {timeout_s}s", stage="composition"
        ) from exc

    if proceso.returncode != 0:
        temporal.unlink(missing_ok=True)
        raise ComposicionFallida(
            f"FFmpeg salió con código {proceso.returncode}: "
            f"{(proceso.stderr or '').strip()[-400:]}",
            stage="composition",
        )
    if not temporal.is_file() or temporal.stat().st_size == 0:
        temporal.unlink(missing_ok=True)
        raise ComposicionFallida(
            "FFmpeg terminó con éxito pero no dejó ningún archivo utilizable",
            stage="composition",
        )

    temporal.replace(salida)
    return ResultadoComposicion(
        salida=salida,
        capas=[c.name for c in capas_ass],
        filtro=filtro,
        exit_code=proceso.returncode,
    )


# ---------------------------------------------------------------------------
# Inspección visual
# ---------------------------------------------------------------------------


def extraer_frames(
    video: Path, instantes: list[float], destino: Path, *, ancho: int = 405,
    timeout_s: int = 300,
) -> list[Path]:
    """Extrae un fotograma por instante, para poder mirar el vídeo de verdad.

    ffprobe dice que el archivo tiene 1080×1920; no dice si el subtítulo se ve.
    Estos fotogramas existen para que esa diferencia quede a la vista.
    """
    destino.mkdir(parents=True, exist_ok=True)
    ffmpeg = binario_ffmpeg()
    generados: list[Path] = []

    for indice, instante in enumerate(instantes, start=1):
        archivo = destino / f"frame_{indice:02d}_{instante:0.2f}s.png"
        argv = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(instante, 0):.3f}", "-i", str(video.resolve()),
            "-frames:v", "1", "-vf", f"scale={ancho}:-2", str(archivo.resolve()),
        ]
        try:
            proceso = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise TiempoAgotado(
                f"FFmpeg no extrajo el fotograma de {instante}s a tiempo",
                stage="visual_qa",
            ) from exc
        if proceso.returncode == 0 and archivo.is_file() and archivo.stat().st_size > 0:
            generados.append(archivo)

    return generados


def hoja_de_contactos(frames: list[Path], destino: Path, *, timeout_s: int = 300) -> Path | None:
    """Junta los fotogramas en una tira para revisarlos de un vistazo.

    Se apilan en una sola fila con ``hstack``, que solo exige que midan lo
    mismo: es exactamente el caso, porque salen todos de la misma escala.

    Devuelve None si no se pudo generar. Es una comodidad para la revisión
    humana, no una comprobación, así que su ausencia no invalida nada; los
    fotogramas sueltos siguen ahí.
    """
    if not frames:
        return None
    destino.parent.mkdir(parents=True, exist_ok=True)

    argv = [binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"]
    for frame in frames:
        argv += ["-i", str(frame.resolve())]
    entradas = "".join(f"[{i}:v]" for i in range(len(frames)))
    filtro = (
        f"{entradas}hstack=inputs={len(frames)}" if len(frames) > 1 else f"{entradas}null"
    )
    argv += ["-filter_complex", filtro, str(destino.resolve())]

    try:
        proceso = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        return None
    if proceso.returncode != 0 or not destino.is_file() or destino.stat().st_size == 0:
        return None
    return destino
