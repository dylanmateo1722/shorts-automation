"""run_id: es también el --task-id de MoneyPrinterTurbo, que exige UUID."""

from __future__ import annotations

import uuid

import pytest

from app.core.errors import EntradaInvalida
from app.core.run_id import nuevo_run_id, parsear_run_id


def test_uuid_valido_se_acepta():
    valor = str(uuid.uuid4())
    assert str(parsear_run_id(valor)) == valor


def test_uuid_ya_parseado_pasa_sin_cambios():
    generado = nuevo_run_id()
    assert parsear_run_id(generado) is generado


@pytest.mark.parametrize(
    "invalido",
    ["", "no-es-uuid", "12345", "34435690391-1", None, "poc-run"],
)
def test_valores_invalidos_se_rechazan(invalido):
    with pytest.raises(EntradaInvalida):
        parsear_run_id(invalido)


def test_el_error_es_permanente_y_no_reintentable():
    """Reintentar un run_id inválido produciría exactamente el mismo fallo."""
    with pytest.raises(EntradaInvalida) as exc:
        parsear_run_id("no-es-uuid")
    assert exc.value.categoria == "permanente"
    assert exc.value.retryable is False


def test_nuevo_run_id_es_unico():
    assert nuevo_run_id() != nuevo_run_id()
