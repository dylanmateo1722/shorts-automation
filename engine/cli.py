#!/usr/bin/env python
"""Motor de render propio, sobre FFmpeg. Habla el mismo CLI que MoneyPrinterTurbo.

Por qué existe
--------------

``MPTAdapter`` no ejecuta un motor concreto: ejecuta **un CLI en una ruta**
—``<SHORTS_MPT_DIR>/cli.py`` con ``<SHORTS_MPT_DIR>/.venv/bin/python``— y lee un
objeto JSON de su última línea de stdout. Eso lo hace enchufable, y este archivo
es un motor de verdad enchufado ahí.

MoneyPrinterTurbo sigue siendo una opción válida, pero arrastra dos cosas que
impiden entregar el producto: es un submódulo que hay que vendorizar y, para
conseguir material visual, necesita una credencial de un banco de vídeo externo.
Este motor no necesita ninguna de las dos: recibe el material que la corrida ya
declaró y autorizó por procedencia, y lo combina con la narración usando el
FFmpeg que el proyecto ya empaqueta.

Qué hace, exactamente
---------------------

Lo único que el pipeline le pide al motor: coger el material visual y el audio
de la narración y devolver un vídeo vertical cuya duración sea la del audio. Los
subtítulos y el rótulo **no** son suyos —los quema la etapa de composición—, y
por eso ``--stop-at video`` es el modo en el que se le invoca.

    material(es) ──┐
                   ├── concatena ── ajusta a 9:16 ── recorta/repite ── H.264 + AAC
    narración ─────┘        al largo exacto del audio

No es un mock. Escribe archivos reales con FFmpeg real, y si FFmpeg falla, falla.

Contrato de proceso
-------------------

* Códigos de salida: ``0`` éxito, ``1`` fallo de la tarea, ``2`` argumentos
  inválidos. Son los de MPT, y el adaptador los distingue: un ``2`` es un error
  de entrada nuestro, no un fallo de render.
* Última línea de stdout: ``{"result": {"videos": [...], "combined_videos": [...]}}``
  con rutas absolutas. El adaptador las importa al directorio de la corrida, así
  que aquí se pueden escribir donde convenga.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

RAIZ = Path(__file__).resolve().parent

#: Vertical 1080×1920. El pipeline valida la relación de aspecto en la QA de
#: vídeo, así que no es un detalle cosmético: producir otra cosa la reprueba.
ANCHO = 1080
ALTO = 1920
FPS = 30

#: Duración máxima de un fragmento de material que se dará por bueno. Un material
#: de horas no es un error, pero recortarlo en silencio a 45 s sí lo sería sin
#: decirlo, así que se registra en el log.
TIMEOUT_FFMPEG_S = 900


def binario_ffmpeg() -> str:
    """El FFmpeg a usar, buscado en cuatro sitios y por ese orden.

    El orden importa. El motor corre como subproceso con su propio intérprete,
    así que no puede dar por hecho que ve las dependencias del proyecto: se
    intenta ``imageio-ffmpeg`` porque es la versión que el resto del proyecto
    tiene comprobada, pero no se depende de poder importarlo.

    1. ``SHORTS_FFMPEG``, para fijarlo a mano.
    2. El paquete ``imageio-ffmpeg``, si es importable.
    3. El binario que ese paquete deja en el entorno del proyecto, buscado por
       ruta. Sirve cuando el intérprete del motor no ve ese site-packages.
    4. ``ffmpeg`` en el PATH.
    """
    fijado = os.environ.get("SHORTS_FFMPEG", "").strip()
    if fijado:
        if not Path(fijado).is_file():
            raise RuntimeError(f"SHORTS_FFMPEG apunta a {fijado}, que no existe")
        return fijado

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - se sigue buscando
        pass

    # El paquete instala el binario junto a sí mismo. Se busca desde la raíz del
    # proyecto, que es el padre del directorio del motor.
    for raiz in (RAIZ.parent, RAIZ):
        for candidato in sorted(raiz.glob(".venv/lib/*/site-packages/imageio_ffmpeg/binaries/ffmpeg-*")):
            if candidato.is_file():
                return str(candidato)

    if ruta := shutil.which("ffmpeg"):
        return ruta

    raise RuntimeError(
        "no hay FFmpeg disponible: ni SHORTS_FFMPEG, ni imageio-ffmpeg "
        "importable, ni su binario en el entorno del proyecto, ni ffmpeg en PATH"
    )


def _ejecutar(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=TIMEOUT_FFMPEG_S
    )


_DURACION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")


def duracion_s(ruta: Path) -> float:
    """Duración de un medio, leída de la salida de ``ffmpeg -i``.

    Se usa ``ffmpeg -i`` y no ``ffprobe`` por el mismo motivo que el resto del
    proyecto: el binario empaquetado no trae ffprobe.
    """
    proceso = _ejecutar([binario_ffmpeg(), "-hide_banner", "-i", str(ruta)])
    texto = (proceso.stderr or "") + (proceso.stdout or "")
    encontrado = _DURACION.search(texto)
    if not encontrado:
        raise RuntimeError(f"no se pudo leer la duración de {ruta}")
    horas, minutos, segundos = encontrado.groups()
    return int(horas) * 3600 + int(minutos) * 60 + float(segundos)


def _filtro_vertical() -> str:
    """Lleva cualquier entrada a 1080×1920 sin deformarla.

    Se escala cubriendo el encuadre y se recorta el exceso, en vez de estirar:
    deformar una cara para que quepa es peor que perder unos píxeles del borde.
    """
    return (
        f"scale={ANCHO}:{ALTO}:force_original_aspect_ratio=increase,"
        f"crop={ANCHO}:{ALTO},setsar=1,fps={FPS}"
    )


def combinar(
    materiales: list[Path], audio: Path, destino: Path, *, registro: list[str]
) -> Path:
    """Material + narración → un MP4 vertical del largo exacto del audio.

    El audio manda: si el material es más corto se repite en bucle, y si es más
    largo se corta. Al revés —ajustar el audio al material— cortaría la
    narración a mitad de frase.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    largo_audio = duracion_s(audio)
    registro.append(f"audio: {largo_audio:.2f}s")

    argv = [binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"]

    # Cada material entra en bucle; el bucle no alarga nada por sí solo, porque
    # la duración final la fija -t sobre el audio.
    for material in materiales:
        registro.append(f"material: {material.name} ({duracion_s(material):.2f}s)")
        argv += ["-stream_loop", "-1", "-i", str(material)]
    argv += ["-i", str(audio)]

    indice_audio = len(materiales)
    if len(materiales) == 1:
        cadena = f"[0:v]{_filtro_vertical()}[v]"
    else:
        # Se normaliza cada material antes de concatenar: concatenar tamaños
        # distintos falla, y hacerlo después deformaría el resultado.
        partes = [
            f"[{i}:v]{_filtro_vertical()}[v{i}]" for i in range(len(materiales))
        ]
        entradas = "".join(f"[v{i}]" for i in range(len(materiales)))
        cadena = (
            ";".join(partes)
            + f";{entradas}concat=n={len(materiales)}:v=1:a=0[v]"
        )

    argv += [
        "-filter_complex", cadena,
        "-map", "[v]",
        "-map", f"{indice_audio}:a",
        "-t", f"{largo_audio:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        # +faststart deja el moov al principio: un reproductor web puede empezar
        # sin descargar el archivo entero.
        "-movflags", "+faststart",
        str(destino),
    ]

    proceso = _ejecutar(argv)
    if proceso.returncode != 0:
        raise RuntimeError(
            f"FFmpeg falló con código {proceso.returncode}: "
            f"{(proceso.stderr or '').strip()[:500]}"
        )
    if not destino.is_file() or destino.stat().st_size == 0:
        raise RuntimeError(f"FFmpeg terminó bien pero no escribió {destino}")
    registro.append(f"salida: {destino.name} ({destino.stat().st_size} bytes)")
    return destino


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--task-id")
    parser.add_argument("--video-script")
    parser.add_argument("--custom-audio-file")
    parser.add_argument("--video-source")
    parser.add_argument("--video-materials")
    parser.add_argument("--video-aspect")
    parser.add_argument("--video-fit-mode")
    parser.add_argument("--bgm-type")
    parser.add_argument("--stop-at")
    parser.add_argument("--subtitle-enabled", action="store_true")
    parser.add_argument(
        "--no-subtitle-enabled", dest="subtitle_enabled", action="store_false"
    )
    args, _sobrantes = parser.parse_known_args()

    # --- argumentos: todo lo que se rechaza aquí sale con 2 -----------------
    try:
        tarea = uuid.UUID(str(args.task_id))
    except (ValueError, TypeError):
        print(
            f"error: argument --task-id: task-id must be a valid UUID, "
            f"got {args.task_id!r}",
            file=sys.stderr,
        )
        return 2

    if not args.custom_audio_file:
        print("error: argument --custom-audio-file is required", file=sys.stderr)
        return 2
    audio = Path(args.custom_audio_file)
    if not audio.is_file():
        print(f"error: audio file not found: {audio}", file=sys.stderr)
        return 2

    materiales = [
        Path(m.strip()) for m in (args.video_materials or "").split(",") if m.strip()
    ]
    if not materiales:
        print("error: argument --video-materials is required", file=sys.stderr)
        return 2
    ausentes = [str(m) for m in materiales if not m.is_file()]
    if ausentes:
        print(f"error: material not found: {', '.join(ausentes)}", file=sys.stderr)
        return 2

    if args.video_aspect and args.video_aspect not in ("9:16",):
        print(
            f"error: argument --video-aspect: this engine only renders 9:16, "
            f"got {args.video_aspect!r}",
            file=sys.stderr,
        )
        return 2

    # --- render: de aquí en adelante un fallo es 1, no 2 -------------------
    registro: list[str] = [f"tarea: {tarea}", f"stop-at: {args.stop_at}"]
    salida_tarea = RAIZ / "storage" / "tasks" / str(tarea)
    try:
        combinado = combinar(
            materiales,
            audio,
            salida_tarea / "combined-1.mp4",
            registro=registro,
        )
        # El pipeline distingue el combinado del final. Este motor se invoca con
        # ``--stop-at video``, así que no añade nada al combinado: el final es el
        # mismo vídeo, y la composición es la que quema subtítulos y rótulo.
        final = salida_tarea / "final-1.mp4"
        shutil.copy2(combinado, final)
    except Exception as exc:  # noqa: BLE001 - el contrato exige salir con 1
        print(f"render failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        (salida_tarea).mkdir(parents=True, exist_ok=True)
        (salida_tarea / "engine.log").write_text(
            "\n".join(registro) + f"\nFALLO: {exc}\n", encoding="utf-8"
        )
        return 1

    (salida_tarea / "engine.log").write_text(
        "\n".join(registro) + "\n", encoding="utf-8"
    )

    # Última línea de stdout: el objeto que el adaptador parsea.
    print(
        json.dumps(
            {
                "task_id": str(tarea),
                "engine": "ffmpeg-vertical-1080x1920",
                "result": {
                    "videos": [str(final.absolute())],
                    "combined_videos": [str(combinado.absolute())],
                },
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
