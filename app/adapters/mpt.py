"""Adaptador de MoneyPrinterTurbo.

**Único punto de contacto con el motor.** Ningún otro módulo conoce su ruta,
su intérprete, sus flags ni el formato de su salida. Si una versión futura
cambia cualquiera de esas cosas, el arreglo vive aquí.

Se invoca por CLI y no por su API HTTP porque ``--custom-audio-file`` no
funciona por HTTP: MPT exige que el audio ya esté dentro del directorio de la
tarea y no expone endpoint para subirlo ahí.

Contrato: ``render(RenderJob) -> RenderResult``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from app.adapters.media import inspeccionar
from app.config.mpt import escribir_config
from app.config.settings import Settings
from app.contracts.models import EstadoRender, RenderJob, RenderResult
from app.core.errors import EntradaInvalida, TiempoAgotado
from app.core.logging import log_evento
from app.core.workspace import Workspace

# Nombre del archivo donde se vuelca la salida completa del motor. No entra en
# el JSON del resultado: los logs pueden ser largos y el resultado debe
# permanecer legible y seguro.
ARCHIVO_LOG = "render/mpt.log"

# El motor escribe en su propio storage/tasks/<task_id>/, fuera del directorio
# de la corrida. La salida se copia dentro para que el artefacto guarde una
# ruta relativa y la corrida sea autocontenida e interpretable en otra máquina.
DESTINO_FINAL = "render/final.mp4"
DESTINO_COMBINADO = "render/combined.mp4"

# Longitud máxima del mensaje de error que se guarda en el RenderResult.
MAX_ERROR = 500


class MPTAdapter:
    """Ejecuta MoneyPrinterTurbo y traduce su salida a contratos propios."""

    def __init__(self, settings: Settings, workspace: Workspace) -> None:
        self.settings = settings
        self.workspace = workspace

    # --- construcción del comando -----------------------------------------

    def construir_argv(self, job: RenderJob) -> list[str]:
        """Arma los argumentos del CLI del motor.

        Las rutas del ``RenderJob`` son relativas al directorio de la corrida;
        aquí se resuelven a absolutas porque el subproceso se ejecuta con el
        directorio de MPT como cwd.
        """
        audio = self.workspace.ruta(job.audio_path).absolute()
        materiales = [str(self.workspace.ruta(m).absolute()) for m in job.materials]
        argv = [
            str(self.settings.python_mpt),
            str(self.settings.cli_mpt),
            "--task-id", str(job.task_id),
            "--video-script", job.script,
            "--custom-audio-file", str(audio),
            "--video-source", "local",
            "--video-materials", ",".join(materiales),
            "--video-aspect", job.aspect.value,
            "--video-fit-mode", job.fit_mode.value,
            "--bgm-type", job.bgm_type,
            "--stop-at", "video",
        ]
        # Los subtítulos son nuestros: el motor renderiza sin ellos.
        argv.append("--subtitle-enabled" if job.subtitles_enabled else "--no-subtitle-enabled")
        return argv

    # --- validación de la entrada -----------------------------------------

    def _validar_job(self, job: RenderJob) -> None:
        if job.task_id != self.workspace.run_id:
            raise EntradaInvalida(
                f"el task_id del RenderJob ({job.task_id}) debe coincidir con el "
                f"run_id de la ejecución ({self.workspace.run_id})",
                stage="render",
            )
        audio = self.workspace.ruta(job.audio_path)
        if not audio.is_file():
            raise EntradaInvalida(f"no existe el audio {job.audio_path!r}", stage="render")
        for material in job.materials:
            if not self.workspace.ruta(material).is_file():
                raise EntradaInvalida(
                    f"no existe el material {material!r}", stage="render"
                )

    # --- ejecución ---------------------------------------------------------

    def render(self, job: RenderJob) -> RenderResult:
        """Ejecuta el motor y devuelve un resultado tipado y validado."""
        self.settings.verificar_mpt()
        self._validar_job(job)
        escribir_config(self.settings.raiz_mpt, log_level=self.settings.log_level)

        argv = self.construir_argv(job)
        try:
            proceso = subprocess.run(
                argv, cwd=self.settings.raiz_mpt, capture_output=True,
                text=True, timeout=self.settings.mpt_timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise TiempoAgotado(
                f"MoneyPrinterTurbo excedió {self.settings.mpt_timeout_s}s",
                stage="render",
            ) from exc

        self._volcar_log(proceso.stdout, proceso.stderr)

        if proceso.returncode == 2:
            # El motor rechazó los argumentos antes de empezar: es un error de
            # entrada, no un fallo de render.
            raise EntradaInvalida(
                f"MoneyPrinterTurbo rechazó los argumentos: {self._resumir(proceso.stderr)}",
                stage="render",
            )

        if proceso.returncode != 0:
            return RenderResult(
                run_id=self.workspace.run_id,
                status=EstadoRender.fallo,
                exit_code=proceso.returncode,
                error=self._resumir(proceso.stderr),
                engine_commit=self._commit_motor(),
            )

        datos = self._parsear_salida(proceso.stdout)
        resultado = datos.get("result", {})
        videos = resultado.get("videos") or []
        if not videos:
            return RenderResult(
                run_id=self.workspace.run_id,
                status=EstadoRender.fallo,
                exit_code=0,
                error="el motor terminó con éxito pero no devolvió ningún video",
                engine_commit=self._commit_motor(),
            )

        salida = self._importar(Path(videos[0]), DESTINO_FINAL)
        info = inspeccionar(salida)
        combinados = resultado.get("combined_videos") or []
        combinado = (
            self._importar(Path(combinados[0]), DESTINO_COMBINADO) if combinados else None
        )
        return RenderResult(
            run_id=self.workspace.run_id,
            status=EstadoRender.exito,
            exit_code=0,
            output_path=self.workspace.relativa(salida),
            combined_path=self.workspace.relativa(combinado) if combinado else None,
            duration_s=info.duracion_s,
            file_size_bytes=salida.stat().st_size if salida.is_file() else None,
            width=info.ancho,
            height=info.alto,
            fps=info.fps,
            video_codec=info.codec_video,
            audio_codec=info.codec_audio,
            failed_stage=resultado.get("failed_stage"),
            engine_commit=self._commit_motor(),
        )

    # --- utilidades --------------------------------------------------------

    def _importar(self, origen: Path, destino_relativo: str) -> Path:
        """Copia una salida del motor al directorio de la corrida.

        Sin esto, el artefacto guardaría una ruta absoluta al storage interno
        del motor y dejaría de ser válido en otra máquina o en un runner.
        """
        destino = self.workspace.ruta(destino_relativo)
        destino.parent.mkdir(parents=True, exist_ok=True)
        if origen.resolve() != destino.resolve():
            shutil.copy2(origen, destino)
        return destino

    def _parsear_salida(self, stdout: str) -> dict:
        """El motor imprime un objeto JSON como última línea de stdout."""
        lineas = [l for l in (stdout or "").strip().splitlines() if l.strip()]
        if not lineas:
            raise EntradaInvalida("el motor no devolvió salida en stdout", stage="render")
        try:
            return json.loads(lineas[-1])
        except ValueError as exc:
            raise EntradaInvalida(
                f"la salida del motor no es JSON interpretable: {exc}", stage="render"
            ) from exc

    def _volcar_log(self, stdout: str | None, stderr: str | None) -> Path:
        """Guarda la salida completa en disco, fuera del resultado."""
        destino = self.workspace.ruta(ARCHIVO_LOG)
        destino.parent.mkdir(parents=True, exist_ok=True)
        from app.core.redaction import redactar

        destino.write_text(
            redactar(f"--- stdout ---\n{stdout or ''}\n--- stderr ---\n{stderr or ''}\n"),
            encoding="utf-8",
        )
        log_evento(self.workspace.run_id, "render", "engine_log", path=ARCHIVO_LOG)
        return destino

    @staticmethod
    def _resumir(texto: str | None) -> str:
        """Recorta y sanea un mensaje del motor para guardarlo en el resultado."""
        from app.core.redaction import redactar

        limpio = redactar((texto or "").strip())
        if len(limpio) <= MAX_ERROR:
            return limpio
        return limpio[-MAX_ERROR:]

    def _commit_motor(self) -> str | None:
        """Commit exacto del motor, para que el resultado sea reproducible."""
        try:
            proceso = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=self.settings.raiz_mpt,
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proceso.stdout.strip() or None
