"""Adaptador del CLI de MoneyPrinterTurbo.

MPT se consume como binario externo fijado por SHA (decisión D1). Toda la
superficie de contacto vive en este archivo: si una versión futura cambia sus
flags o su salida, solo hay que tocar aquí.

Se invoca por CLI y no por su API HTTP porque ``--custom-audio-file`` no
funciona por HTTP: MPT exige que el audio ya esté dentro del directorio de la
tarea y no expone ningún endpoint para subirlo ahí.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Configuración mínima que MPT necesita para nuestro camino. Se escribe en
# tiempo de ejecución y nunca se versiona (decisión D2). No contiene secretos:
# el PoC no usa ningún proveedor que requiera credencial.
PLANTILLA_CONFIG = """# Generado por shorts-automation. No editar a mano, no versionar.
log_level = "INFO"
listen_host = "127.0.0.1"
listen_port = 8080

[app]
video_source = "local"
subtitle_provider = ""
tls_verify = true

[ui]
"""


@dataclass
class ResultadoRender:
    """Salida del render de MPT, ya tipada."""

    video_path: Path
    combined_path: Path | None
    audio_duration_s: float
    materiales: list[str]
    exit_code: int
    task_id: str
    etapa_fallida: str | None = None
    error: str | None = None


def escribir_config(raiz_mpt: Path) -> Path:
    """Genera el ``config.toml`` de MPT en tiempo de ejecución."""
    destino = raiz_mpt / "config.toml"
    destino.write_text(PLANTILLA_CONFIG, encoding="utf-8")
    return destino


def construir_argv(
    *,
    guion: str,
    audio: Path,
    materiales: list[Path],
    task_id: str,
    aspecto: str = "9:16",
    fit_mode: str = "cover",
) -> list[str]:
    """Arma los argumentos del CLI de MPT.

    Se desactivan los subtítulos: los genera y los quema nuestra propia capa
    (decisión D4), porque el segmentador de MPT descarta los signos de
    apertura del español y parte por comas.
    """
    return [
        "python", "cli.py",
        "--task-id", task_id,
        "--video-script", guion,
        "--custom-audio-file", str(audio.resolve()),
        "--video-source", "local",
        "--video-materials", ",".join(str(m.resolve()) for m in materiales),
        "--video-aspect", aspecto,
        "--video-fit-mode", fit_mode,
        "--no-subtitle-enabled",
        "--bgm-type", "none",
        "--stop-at", "video",
    ]


def renderizar(
    *,
    raiz_mpt: Path,
    python_mpt: Path,
    guion: str,
    audio: Path,
    materiales: list[Path],
    task_id: str,
    timeout_s: int = 1800,
) -> ResultadoRender:
    """Ejecuta MPT y traduce su salida a un resultado tipado.

    MPT imprime un objeto JSON en stdout y sus logs en stderr, y sale con 0
    (éxito), 1 (fallo de tarea) o 2 (error de argumentos). Ese contrato se
    respeta tal cual, sin reinterpretarlo.
    """
    escribir_config(raiz_mpt)
    argv = construir_argv(
        guion=guion, audio=audio, materiales=materiales, task_id=task_id
    )
    # No se usa resolve(): en un entorno virtual, bin/python es un enlace
    # simbólico al intérprete del sistema, y resolverlo ejecutaría MPT fuera
    # de su venv, sin sus dependencias.
    argv[0] = str(python_mpt.absolute())

    proceso = subprocess.run(
        argv, cwd=raiz_mpt, capture_output=True, text=True, timeout=timeout_s
    )

    if proceso.returncode != 0:
        # Se propaga el error de MPT sin reinterpretarlo.
        cola = (proceso.stderr or "").strip().splitlines()[-5:]
        return ResultadoRender(
            video_path=Path(),
            combined_path=None,
            audio_duration_s=0.0,
            materiales=[],
            exit_code=proceso.returncode,
            task_id=task_id,
            error="\n".join(cola) or "MPT falló sin mensaje en stderr",
        )

    try:
        datos = json.loads(proceso.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"MPT devolvió stdout no interpretable: {exc}") from exc

    resultado = datos.get("result", {})
    videos = resultado.get("videos") or []
    combinados = resultado.get("combined_videos") or []
    if not videos:
        raise RuntimeError("MPT terminó con éxito pero no devolvió ningún video")

    return ResultadoRender(
        video_path=Path(videos[0]),
        combined_path=Path(combinados[0]) if combinados else None,
        audio_duration_s=float(resultado.get("audio_duration") or 0.0),
        materiales=list(resultado.get("materials") or []),
        exit_code=0,
        task_id=datos.get("task_id", task_id),
        etapa_fallida=resultado.get("failed_stage"),
    )
