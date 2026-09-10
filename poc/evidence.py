"""Registro de evidencia de la corrida.

Gate 0.5 exige evidencia, no afirmaciones: duración por etapa, uso de CPU,
espacio, tamaños de artefactos, versiones y el commit exacto de MPT.

Nunca se registran secretos ni se vuelca el entorno completo.
"""

from __future__ import annotations

import json
import platform
import resource
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Evidencia:
    """Acumula las mediciones de una corrida identificada por ``run_id``."""

    run_id: str
    etapas: list[dict] = field(default_factory=list)
    entorno: dict = field(default_factory=dict)
    artefactos: list[dict] = field(default_factory=list)
    errores: list[dict] = field(default_factory=list)
    resultados: dict = field(default_factory=dict)
    synthetic_media: dict = field(default_factory=dict)

    @contextmanager
    def etapa(self, nombre: str):
        """Mide duración y CPU de hijos de una etapa del pipeline."""
        inicio = time.monotonic()
        cpu_inicio = resource.getrusage(resource.RUSAGE_CHILDREN)
        registro = {"nombre": nombre, "ok": True}
        try:
            yield registro
        except Exception as exc:
            registro["ok"] = False
            registro["error"] = f"{type(exc).__name__}: {exc}"
            self.errores.append({"etapa": nombre, "error": registro["error"]})
            raise
        finally:
            cpu_fin = resource.getrusage(resource.RUSAGE_CHILDREN)
            registro["duracion_s"] = round(time.monotonic() - inicio, 3)
            registro["cpu_hijos_s"] = round(
                (cpu_fin.ru_utime - cpu_inicio.ru_utime)
                + (cpu_fin.ru_stime - cpu_inicio.ru_stime),
                3,
            )
            registro["max_rss_hijos_mb"] = round(cpu_fin.ru_maxrss / 1024, 1)
            self.etapas.append(registro)

    def registrar_artefacto(self, nombre: str, path: Path) -> None:
        self.artefactos.append(
            {
                "nombre": nombre,
                "path": str(path),
                "existe": path.exists(),
                "bytes": path.stat().st_size if path.exists() else 0,
            }
        )

    def capturar_entorno(self, raiz_mpt: Path, paquetes: dict[str, str]) -> None:
        """Registra versiones y el commit exacto de MPT. Sin secretos."""
        uso = shutil.disk_usage(Path.cwd())
        commit = ""
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=raiz_mpt,
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
        except Exception:
            commit = "desconocido"
        self.entorno = {
            "python": platform.python_version(),
            "plataforma": platform.platform(),
            "cpu_logicas": __import__("os").cpu_count(),
            "disco_total_gb": round(uso.total / 1e9, 2),
            "disco_libre_gb": round(uso.free / 1e9, 2),
            "mpt_commit": commit,
            "paquetes": paquetes,
        }

    def volcar(self, destino: Path) -> Path:
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "entorno": self.entorno,
                    "etapas": self.etapas,
                    "artefactos": self.artefactos,
                    "resultados": self.resultados,
                    "synthetic_media": self.synthetic_media,
                    "errores": self.errores,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return destino
