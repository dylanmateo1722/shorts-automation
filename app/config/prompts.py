"""Carga de prompts versionados.

Los prompts viven fuera del código de negocio, en ``prompts/<familia>/<versión>.md``.
Un prompt anónimo incrustado en una función no se puede versionar, comparar ni
auditar, y hace imposible saber con qué instrucciones se produjo un artefacto.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.core.errors import ConfiguracionInvalida


def identificador(familia: str, version: str) -> str:
    """Etiqueta que se guarda en el artefacto, por ejemplo ``translation_v1``."""
    return f"{familia}_{version}"


@lru_cache(maxsize=32)
def _leer(ruta: str) -> str:
    return Path(ruta).read_text(encoding="utf-8")


def cargar(raiz_prompts: Path, familia: str, version: str) -> str:
    """Devuelve el texto de un prompt versionado.

    Raises:
        ConfiguracionInvalida: si la versión pedida no existe, nombrando las
            disponibles en vez de fallar con un error de archivo genérico.
    """
    ruta = Path(raiz_prompts) / familia / f"{version}.md"
    if not ruta.is_file():
        directorio = Path(raiz_prompts) / familia
        disponibles = (
            sorted(p.stem for p in directorio.glob("*.md")) if directorio.is_dir() else []
        )
        raise ConfiguracionInvalida(
            f"no existe el prompt {familia}/{version}; "
            f"versiones disponibles: {disponibles or 'ninguna'}"
        )
    return _leer(str(ruta))


def renderizar(plantilla: str, **valores) -> str:
    """Sustituye los marcadores del prompt.

    Se usa ``str.format`` con llaves dobles escapadas en las plantillas, para
    que los ejemplos de JSON del propio prompt sobrevivan a la sustitución.
    """
    try:
        return plantilla.format(**valores)
    except KeyError as exc:
        raise ConfiguracionInvalida(
            f"el prompt espera el marcador {exc} y no se proporcionó"
        ) from exc
