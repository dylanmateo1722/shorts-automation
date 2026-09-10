"""Motor falso que imita el contrato de CLI de MoneyPrinterTurbo.

No es un mock: es un proceso real que acepta los mismos flags, escribe un
archivo de salida real y respeta los mismos códigos de salida (0 éxito,
1 fallo de tarea, 2 error de argumentos). Permite probar el manejo real de
subprocess, rutas, parseo de JSON y mapeo de errores del adaptador sin
ejecutar un render de noventa segundos.

El comportamiento se elige con la variable de entorno ``FAKE_MPT_MODE``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

RAIZ = Path(__file__).resolve().parent


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
    parser.add_argument("--no-subtitle-enabled", dest="subtitle_enabled",
                        action="store_false")
    args, sobrantes = parser.parse_known_args()

    modo = os.environ.get("FAKE_MPT_MODE", "ok")

    # El motor real exige que --task-id sea un UUID y sale con 2 si no lo es.
    try:
        uuid.UUID(str(args.task_id))
    except (ValueError, TypeError):
        print(f"error: argument --task-id: task-id must be a valid UUID, "
              f"got {args.task_id!r}", file=sys.stderr)
        return 2

    # Se registran los argumentos recibidos para que el test pueda afirmarlos.
    registro = RAIZ / "ultima_invocacion.json"
    registro.write_text(json.dumps({
        "task_id": args.task_id,
        "video_script": args.video_script,
        "custom_audio_file": args.custom_audio_file,
        "video_source": args.video_source,
        "video_materials": (args.video_materials or "").split(","),
        "video_aspect": args.video_aspect,
        "video_fit_mode": args.video_fit_mode,
        "bgm_type": args.bgm_type,
        "stop_at": args.stop_at,
        "subtitle_enabled": args.subtitle_enabled,
        "sobrantes": sobrantes,
    }, indent=2), encoding="utf-8")

    if modo == "argerror":
        print("error: argument --video-aspect: invalid choice", file=sys.stderr)
        return 2

    if modo == "fail":
        print("Traceback (most recent call last):\nRuntimeError: render falló",
              file=sys.stderr)
        return 1

    if modo == "secreto":
        # Fuga simulada, para comprobar que el adaptador redacta el log.
        print(f"config cargada: api_key={os.environ.get('DEMO_API_KEY', '')}",
              file=sys.stderr)

    if modo == "nojson":
        print("esto no es json")
        return 0

    destino = RAIZ / "storage" / "tasks" / str(args.task_id)
    destino.mkdir(parents=True, exist_ok=True)
    final = destino / "final-1.mp4"

    if modo == "sinvideo":
        print(json.dumps({"task_id": args.task_id, "result": {"videos": []}}))
        return 0

    fixture = os.environ.get("FAKE_MPT_FIXTURE")
    if not fixture or not Path(fixture).is_file():
        print("falta FAKE_MPT_FIXTURE", file=sys.stderr)
        return 1
    shutil.copy2(fixture, final)

    print(json.dumps({
        "task_id": args.task_id,
        "result": {
            "videos": [str(final)],
            "combined_videos": [str(final)],
            "audio_duration": 1.0,
            "failed_stage": None,
        },
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
