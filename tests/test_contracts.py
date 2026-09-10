"""Contratos: validación, serialización y campos obligatorios."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.contracts.models import (
    SCHEMA_VERSION,
    AdaptedScript,
    ClaseFuente,
    EstadoRender,
    ProcedenciaIA,
    RenderJob,
    RenderResult,
    SeccionGuion,
    SegmentoTexto,
    TipoSeccion,
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
        run_id=rid, source_language="en", target_language="es",
        source_transcript_reference="transcript", text="hola",
        provenance=ProcedenciaIA(provider="x", model="y", prompt_version="v1"),
    )
    recuperada = Translation.model_validate_json(traduccion.model_dump_json())
    assert recuperada.prompt_version == "v1"
    assert recuperada.provider == "x" and recuperada.model == "y"


def test_la_procedencia_es_obligatoria_en_artefactos_generados():
    """Una traducción sin procedencia no sería reproducible ni auditable."""
    with pytest.raises(ValidationError):
        Translation(
            run_id=uuid.uuid4(), source_language="en", target_language="es",
            source_transcript_reference="transcript", text="hola",
        )


def test_procedencia_es_opcional_en_artefactos_no_generados():
    """Un transcript puede venir de una fuente que no es un modelo."""
    t = Transcript(run_id=uuid.uuid4(), source_language="en", text="hello there")
    assert t.provenance is None


# ---------------------------------------------------------------------------
# Contratos lingüísticos (Gate 2)
# ---------------------------------------------------------------------------


def _procedencia() -> ProcedenciaIA:
    return ProcedenciaIA(provider="fake", model="fake-1", prompt_version="v1")


def test_transcript_valido_sin_marcas_temporales():
    """Una transcripción puede llegar sin tiempos y seguir siendo válida."""
    t = Transcript(
        run_id=uuid.uuid4(), source_language="en", text="Hello there friend.",
        segments=[SegmentoTexto(text="Hello there friend.")],
    )
    assert not t.tiene_tiempos
    assert t.segments[0].start_s is None


def test_transcript_conserva_las_marcas_cuando_existen():
    t = Transcript(
        run_id=uuid.uuid4(), source_language="en", text="Hola.",
        segments=[SegmentoTexto(text="Hola.", start_s=0.0, end_s=1.2)],
    )
    assert t.tiene_tiempos


def test_transcript_sin_texto_es_rechazado():
    with pytest.raises(ValidationError):
        Transcript(run_id=uuid.uuid4(), source_language="en", text="")


def test_translation_ida_y_vuelta():
    original = Translation(
        run_id=uuid.uuid4(), source_language="en", target_language="es",
        source_transcript_reference="transcript", text="Hola mundo.",
        segments=[SegmentoTexto(text="Hola mundo.")], provenance=_procedencia(),
    )
    recuperada = Translation.model_validate_json(original.model_dump_json())
    assert recuperada == original
    assert recuperada.schema_version == SCHEMA_VERSION


def test_adapted_script_ida_y_vuelta():
    rid = uuid.uuid4()
    original = AdaptedScript(
        run_id=rid, language="es", hook="¿Empezamos?",
        sections=[SeccionGuion(kind=TipoSeccion.hook, text="¿Empezamos?", order=0)],
        full_text="¿Empezamos?", target_duration_seconds=30.0,
        estimated_duration_seconds=1.0, provenance=_procedencia(),
    )
    recuperado = AdaptedScript.model_validate_json(original.model_dump_json())
    assert recuperado == original
    assert recuperado.provider == "fake" and recuperado.prompt_version == "v1"


def test_adapted_script_exige_al_menos_una_seccion():
    with pytest.raises(ValidationError):
        AdaptedScript(
            run_id=uuid.uuid4(), language="es", hook="h", sections=[], full_text="h",
            target_duration_seconds=30.0, estimated_duration_seconds=1.0,
            provenance=_procedencia(),
        )


def test_adapted_script_exige_duracion_objetivo_positiva():
    with pytest.raises(ValidationError):
        AdaptedScript(
            run_id=uuid.uuid4(), language="es", hook="h",
            sections=[SeccionGuion(kind=TipoSeccion.hook, text="h", order=0)],
            full_text="h", target_duration_seconds=0.0, estimated_duration_seconds=1.0,
            provenance=_procedencia(),
        )


def test_los_tipos_de_seccion_son_los_acordados():
    assert {t.value for t in TipoSeccion} == {
        "hook", "context", "development", "payoff", "cta"
    }
