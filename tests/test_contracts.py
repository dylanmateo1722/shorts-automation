"""Contratos: validación, serialización y campos obligatorios."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.contracts.models import (
    SCHEMA_VERSION,
    ClaseFuente,
    EstadoRender,
    ProcedenciaIA,
    RenderJob,
    RenderResult,
    Transcript,
    Translation,
)


def _job(**extra) -> RenderJob:
    rid = extra.pop("run_id", uuid.uuid4())
    base = dict(
        run_id=rid, task_id=rid, script="guion", audio_path="input/audio.mp3",
        materials=["input/material.mp4"],
    )
    base.update(extra)
    return RenderJob(**base)


def test_artefacto_lleva_run_id_created_at_y_schema_version():
    job = _job()
    assert job.schema_version == SCHEMA_VERSION
    assert job.created_at.tzinfo is not None
    assert isinstance(job.run_id, uuid.UUID)


def test_campos_obligatorios_faltantes_son_rechazados():
    with pytest.raises(ValidationError):
        RenderJob(run_id=uuid.uuid4())  # faltan script, audio_path y materials


def test_campo_desconocido_es_rechazado():
    """extra=forbid: un campo que nadie declaró es una etapa escribiendo mal."""
    with pytest.raises(ValidationError):
        _job(campo_inventado="x")


def test_script_vacio_es_rechazado():
    with pytest.raises(ValidationError):
        _job(script="")


def test_materials_vacio_es_rechazado():
    with pytest.raises(ValidationError):
        _job(materials=[])


def test_serializacion_ida_y_vuelta():
    job = _job()
    recuperado = RenderJob.model_validate_json(job.model_dump_json())
    assert recuperado == job


def test_enum_se_serializa_por_valor():
    resultado = RenderResult(
        run_id=uuid.uuid4(), status=EstadoRender.exito, exit_code=0
    )
    assert '"success"' in resultado.model_dump_json()
    assert ClaseFuente.solo_referencia.value == "reference_only"


def test_artefacto_de_ia_registra_procedencia():
    """Sin proveedor, modelo y versión de prompt la salida no es auditable."""
    rid = uuid.uuid4()
    traduccion = Translation(
        run_id=rid, source_ref="transcript", language="es", segments=[],
        full_text="hola",
        provenance=ProcedenciaIA(provider="x", model="y", prompt_version="v1"),
    )
    recuperada = Translation.model_validate_json(traduccion.model_dump_json())
    assert recuperada.provenance is not None
    assert recuperada.provenance.prompt_version == "v1"


def test_procedencia_es_opcional_en_artefactos_no_generados():
    Transcript(run_id=uuid.uuid4(), language="en", segments=[])
