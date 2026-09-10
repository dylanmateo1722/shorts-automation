"""Identificador de ejecución.

El ``run_id`` es también el ``--task-id`` de MoneyPrinterTurbo, y MPT lo
rechaza con código de salida 2 si no es un UUID válido. Mantenerlos iguales
hace trazables sus artefactos sin llevar un mapeo aparte, pero obliga a
validar el formato antes de empezar, no a mitad del pipeline.
"""

from __future__ import annotations

import uuid
from uuid import UUID

from app.core.errors import EntradaInvalida


def nuevo_run_id() -> UUID:
    """Genera un identificador de ejecución."""
    return uuid.uuid4()


def parsear_run_id(valor: str | UUID) -> UUID:
    """Valida y normaliza un ``run_id``.

    Raises:
        EntradaInvalida: si el valor no es un UUID. Es un error permanente:
            reintentarlo produciría exactamente el mismo fallo.
    """
    if isinstance(valor, UUID):
        return valor
    try:
        return UUID(str(valor))
    except (ValueError, AttributeError, TypeError) as exc:
        raise EntradaInvalida(
            f"run_id debe ser un UUID válido (MoneyPrinterTurbo lo exige como "
            f"--task-id); recibido {valor!r}",
            stage="run_id",
        ) from exc
