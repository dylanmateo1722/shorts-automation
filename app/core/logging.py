"""Logging estructurado y seguro.

Cada línea identifica ejecución, etapa y evento, de forma que una corrida se
pueda seguir desde la consola de un móvil o desde el log de un runner:

    12:04:03 | INFO  | run=... stage=render event=start
    12:05:54 | INFO  | run=... stage=render event=success duration_s=111.2

Todo mensaje pasa por el redactor antes de emitirse.
"""

from __future__ import annotations

import logging
import sys
from uuid import UUID

from app.core.redaction import redactar

FORMATO = "%(asctime)s | %(levelname)-5s | %(message)s"
FORMATO_HORA = "%H:%M:%S"
NOMBRE_LOGGER = "shorts"


class _FiltroRedaccion(logging.Filter):
    """Oculta secretos en el mensaje ya interpolado."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redactar(record.getMessage())
        record.args = ()
        return True


def configurar_logging(nivel: int = logging.INFO, *, stream=None) -> logging.Logger:
    """Configura el logger de la aplicación.

    ``force=True`` porque tanto los runners de CI como los notebooks registran
    handlers en el logger raíz antes de que arranque la aplicación; sin él,
    ``basicConfig`` se ignora en silencio y no se ve ninguna traza.

    ``stream`` es ``sys.stdout`` por defecto, como desde Gate 1. Se puede cambiar
    para que un comando cuya salida es un documento legible por máquina no la
    mezcle con las trazas: un JSON con una línea de log delante no es un JSON.
    """
    logging.basicConfig(
        level=nivel,
        format=FORMATO,
        datefmt=FORMATO_HORA,
        stream=stream if stream is not None else sys.stdout,
        force=True,
    )
    logger = logging.getLogger(NOMBRE_LOGGER)
    logger.setLevel(nivel)
    if not any(isinstance(f, _FiltroRedaccion) for f in logger.filters):
        logger.addFilter(_FiltroRedaccion())
    return logger


def obtener_logger() -> logging.Logger:
    logger = logging.getLogger(NOMBRE_LOGGER)
    if not any(isinstance(f, _FiltroRedaccion) for f in logger.filters):
        logger.addFilter(_FiltroRedaccion())
    return logger


def _formatear_campos(campos: dict) -> str:
    partes = []
    for clave, valor in campos.items():
        if valor is None:
            continue
        if isinstance(valor, float):
            valor = f"{valor:.2f}"
        partes.append(f"{clave}={valor}")
    return " ".join(partes)


def log_evento(
    run_id: UUID | str,
    stage: str,
    event: str,
    *,
    nivel: int = logging.INFO,
    **campos,
) -> None:
    """Emite un evento estructurado del pipeline."""
    mensaje = f"run={run_id} stage={stage} event={event}"
    extra = _formatear_campos(campos)
    if extra:
        mensaje = f"{mensaje} {extra}"
    obtener_logger().log(nivel, mensaje)
