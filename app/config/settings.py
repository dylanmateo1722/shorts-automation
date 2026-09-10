"""Configuración centralizada de la aplicación.

Una sola fuente: variables de entorno, con ``.env`` únicamente para desarrollo
local. Ningún valor sensible se escribe en el manifest ni en los logs: lo que
llega al manifest sale de ``Settings.publico()``, que es una lista explícita de
campos no sensibles, no un volcado del entorno.

La configuración propia se mantiene separada de la de MoneyPrinterTurbo, que
se genera en tiempo de ejecución (ver ``app.config.mpt``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from app.core.errors import ConfiguracionInvalida, EjecutableAusente

# Raíz del proyecto: dos niveles por encima de este archivo.
RAIZ_PROYECTO = Path(__file__).resolve().parent.parent.parent

SUBDIR_MPT = Path("vendor/moneyprinterturbo")
SUBDIR_RUNS = Path("runs")
SUBDIR_PROMPTS = Path("prompts")

# Palabras por minuto de la narración, medidas en Gate 0.5: 60 palabras en
# 30,29 s con la voz es-CR-JuanNeural de Edge TTS. Es una sola muestra con una
# sola voz, así que sirve para estimar y no para prometer: la duración real se
# medirá cuando exista audio.
WPM_POR_DEFECTO = 119


def _entero(nombre: str, por_defecto: int) -> int:
    bruto = os.environ.get(nombre)
    if bruto is None or bruto == "":
        return por_defecto
    try:
        return int(bruto)
    except ValueError as exc:
        raise ConfiguracionInvalida(
            f"{nombre} debe ser un entero; recibido {bruto!r}"
        ) from exc


@dataclass(frozen=True)
class Settings:
    """Configuración efectiva de una ejecución."""

    raiz_proyecto: Path = RAIZ_PROYECTO
    raiz_runs: Path = field(default_factory=lambda: RAIZ_PROYECTO / SUBDIR_RUNS)
    raiz_mpt: Path = field(default_factory=lambda: RAIZ_PROYECTO / SUBDIR_MPT)
    mpt_timeout_s: int = 1800
    log_level: str = "INFO"

    # --- capa lingüística ---
    llm_provider: str = ""
    llm_model: str = ""
    llm_base_url: str = ""
    llm_timeout_s: int = 120
    target_language: str = "es"
    default_wpm: int = WPM_POR_DEFECTO
    translation_prompt_version: str = "v1"
    adaptation_prompt_version: str = "v1"
    # Margen aceptable alrededor de la duración objetivo, en tanto por uno.
    duration_tolerance: float = 0.25
    # Intentos de condensación cuando el guion sale largo. Acotado a propósito:
    # cada intento es una llamada al modelo y cuesta dinero.
    max_condensation_attempts: int = 2

    @classmethod
    def desde_entorno(cls) -> "Settings":
        """Construye la configuración leyendo el entorno."""
        raiz = Path(os.environ.get("SHORTS_PROJECT_ROOT", RAIZ_PROYECTO)).resolve()
        return cls(
            raiz_proyecto=raiz,
            raiz_runs=Path(os.environ.get("SHORTS_RUNS_DIR", raiz / SUBDIR_RUNS)),
            raiz_mpt=Path(os.environ.get("SHORTS_MPT_DIR", raiz / SUBDIR_MPT)),
            mpt_timeout_s=_entero("SHORTS_MPT_TIMEOUT_S", 1800),
            log_level=os.environ.get("SHORTS_LOG_LEVEL", "INFO").upper(),
            llm_provider=os.environ.get("LLM_PROVIDER", "").strip(),
            llm_model=os.environ.get("LLM_MODEL", "").strip(),
            llm_base_url=os.environ.get("LLM_BASE_URL", "").strip(),
            llm_timeout_s=_entero("LLM_TIMEOUT_S", 120),
            target_language=os.environ.get("TARGET_LANGUAGE", "es").strip(),
            default_wpm=_entero("DEFAULT_WPM", WPM_POR_DEFECTO),
            translation_prompt_version=os.environ.get(
                "TRANSLATION_PROMPT_VERSION", "v1"
            ).strip(),
            adaptation_prompt_version=os.environ.get(
                "ADAPTATION_PROMPT_VERSION", "v1"
            ).strip(),
        )

    # --- MoneyPrinterTurbo -------------------------------------------------

    @property
    def python_mpt(self) -> Path:
        """Intérprete del entorno virtual de MPT.

        No se resuelven enlaces simbólicos: ``bin/python`` de un venv apunta al
        intérprete del sistema, y resolverlo ejecutaría MPT fuera de su entorno,
        sin sus dependencias.
        """
        return (self.raiz_mpt / ".venv" / "bin" / "python").absolute()

    @property
    def cli_mpt(self) -> Path:
        return (self.raiz_mpt / "cli.py").absolute()

    def verificar_mpt(self) -> None:
        """Comprueba que el motor está instalado antes de intentar usarlo.

        Raises:
            EjecutableAusente: con la instrucción concreta para arreglarlo.
        """
        if not self.cli_mpt.is_file():
            raise EjecutableAusente(
                f"no se encuentra el CLI de MoneyPrinterTurbo en {self.cli_mpt}; "
                f"ejecuta scripts/setup_mpt.sh"
            )
        if not self.python_mpt.is_file():
            raise EjecutableAusente(
                f"no se encuentra el entorno de MoneyPrinterTurbo en "
                f"{self.python_mpt}; ejecuta scripts/setup_mpt.sh"
            )

    @property
    def raiz_prompts(self) -> Path:
        return self.raiz_proyecto / SUBDIR_PROMPTS

    @property
    def llm_api_key(self) -> str:
        """Credencial del proveedor. **Solo** desde el entorno.

        Es una propiedad y no un campo del dataclass para que no pueda acabar
        por descuido en un ``repr``, en un volcado del manifest ni en un log.
        """
        return os.environ.get("LLM_API_KEY", "")

    def verificar_llm(self) -> None:
        """Comprueba que hay proveedor configurado antes de gastar en llamadas.

        Raises:
            ConfiguracionInvalida: nombrando la variable que falta.
        """
        if not self.llm_provider:
            raise ConfiguracionInvalida(
                "falta LLM_PROVIDER; configúralo en .env o en el entorno"
            )
        if self.default_wpm <= 0:
            raise ConfiguracionInvalida(
                f"DEFAULT_WPM debe ser mayor que cero; recibido {self.default_wpm}"
            )

    # --- manifest ----------------------------------------------------------

    def publico(self) -> dict:
        """Configuración apta para el manifest.

        Lista explícita y cerrada. Las rutas se expresan relativas a la raíz
        del proyecto para que el manifest no dependa de una máquina concreta.
        """
        return {
            "runs_dir": self._relativa(self.raiz_runs),
            "mpt_dir": self._relativa(self.raiz_mpt),
            "mpt_timeout_s": self.mpt_timeout_s,
            "log_level": self.log_level,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "target_language": self.target_language,
            "default_wpm": self.default_wpm,
            "translation_prompt_version": self.translation_prompt_version,
            "adaptation_prompt_version": self.adaptation_prompt_version,
        }

    def _relativa(self, ruta: Path) -> str:
        try:
            return str(Path(ruta).resolve().relative_to(self.raiz_proyecto.resolve()))
        except ValueError:
            return str(ruta)
