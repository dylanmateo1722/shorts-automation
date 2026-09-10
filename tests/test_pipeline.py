"""Pipeline mínimo: orden de etapas, éxito, fallo y reanudación."""

from __future__ import annotations

import uuid

import pytest

from app.contracts.models import EstadoRender, RenderJob, RenderResult
from app.core.manifest import EstadoEtapa, EstadoRun, Manifest
from app.core.workspace import Workspace
from app.pipeline import core, stages


@pytest.fixture(autouse=True)
def entrada_breve(monkeypatch):
    """Acorta la entrada controlada: los tests prueban orquestación, no duración."""
    monkeypatch.setattr(stages, "DURACION_AUDIO_S", 1)
    monkeypatch.setattr(stages, "DURACION_MATERIAL_S", 1)


def test_orden_de_las_etapas():
    assert [e.nombre for e in core.construir_pipeline()] == ["prepare_input", "render"]


def test_cada_etapa_declara_su_artefacto_y_su_contrato():
    etapas = core.construir_pipeline()
    assert etapas[0].artefacto == "render_job" and etapas[0].modelo is RenderJob
    assert etapas[1].artefacto == "render_result" and etapas[1].modelo is RenderResult


def test_ejecucion_exitosa(settings_falsos):
    run_id = uuid.uuid4()
    resultado = core.ejecutar_run(run_id, settings_falsos)

    assert resultado.exito
    assert resultado.manifest.status is EstadoRun.completada
    assert [e.name for e in resultado.manifest.stages] == ["prepare_input", "render"]
    assert all(e.status is EstadoEtapa.completada for e in resultado.manifest.stages)

    ws = Workspace(settings_falsos.raiz_runs, run_id)
    render = ws.leer_artefacto("render_result", RenderResult)
    assert render.status is EstadoRender.exito
    assert ws.ruta(render.output_path).is_file()


def test_el_manifest_registra_versiones_y_config(settings_falsos):
    resultado = core.ejecutar_run(uuid.uuid4(), settings_falsos)
    assert resultado.manifest.versions["app"]
    assert resultado.manifest.versions["python"]
    assert resultado.manifest.config["mpt_timeout_s"] == 120


def test_fallo_del_motor_detiene_el_pipeline(settings_falsos, monkeypatch):
    monkeypatch.setenv("FAKE_MPT_MODE", "fail")
    run_id = uuid.uuid4()
    resultado = core.ejecutar_run(run_id, settings_falsos)

    assert not resultado.exito
    assert resultado.manifest.status is EstadoRun.fallida
    assert resultado.manifest.etapa("render").status is EstadoEtapa.fallida
    assert resultado.manifest.errors
    assert resultado.manifest.errors[0]["category"] == "permanente"

    ws = Workspace(settings_falsos.raiz_runs, run_id)
    assert not ws.existe("render_result"), "un fallo no debe dejar artefacto reutilizable"
    # La etapa previa sí completó: reanudar no debería repetirla.
    assert resultado.manifest.etapa_completada("prepare_input")


def test_reanudacion_omite_lo_ya_hecho(settings_falsos):
    run_id = uuid.uuid4()
    core.ejecutar_run(run_id, settings_falsos)
    segunda = core.ejecutar_run(run_id, settings_falsos)

    assert segunda.exito
    assert all(e.status is EstadoEtapa.omitida for e in segunda.manifest.stages)


def test_reanudacion_tras_fallo_solo_repite_la_etapa_fallida(settings_falsos, monkeypatch):
    run_id = uuid.uuid4()
    monkeypatch.setenv("FAKE_MPT_MODE", "fail")
    assert not core.ejecutar_run(run_id, settings_falsos).exito

    monkeypatch.setenv("FAKE_MPT_MODE", "ok")
    segunda = core.ejecutar_run(run_id, settings_falsos)

    assert segunda.exito
    assert segunda.manifest.etapa("prepare_input").status is EstadoEtapa.omitida
    assert segunda.manifest.etapa("render").status is EstadoEtapa.completada


def test_forzar_una_etapa_la_repite(settings_falsos):
    run_id = uuid.uuid4()
    core.ejecutar_run(run_id, settings_falsos)
    tercera = core.ejecutar_run(run_id, settings_falsos, forzar="render")

    assert tercera.manifest.etapa("prepare_input").status is EstadoEtapa.omitida
    assert tercera.manifest.etapa("render").status is EstadoEtapa.completada


def test_validar_run_detecta_artefacto_ausente(settings_falsos):
    run_id = uuid.uuid4()
    core.ejecutar_run(run_id, settings_falsos)
    ws = Workspace(settings_falsos.raiz_runs, run_id)

    ok, problemas = core.validar_run(run_id, settings_falsos)
    assert ok and not problemas

    ws.ruta("render/final.mp4").unlink()
    ok, problemas = core.validar_run(run_id, settings_falsos)
    assert not ok
    assert any("final.mp4" in p for p in problemas)


def test_validar_run_inexistente_no_revienta(settings_falsos):
    ok, problemas = core.validar_run(uuid.uuid4(), settings_falsos)
    assert not ok and problemas


def test_el_manifest_persiste_entre_ejecuciones(settings_falsos):
    run_id = uuid.uuid4()
    core.ejecutar_run(run_id, settings_falsos)
    ws = Workspace(settings_falsos.raiz_runs, run_id)

    en_disco = Manifest.cargar(ws.dir)
    assert en_disco.run_id == run_id
    assert en_disco.status is EstadoRun.completada
    assert "render_result" in en_disco.artifacts
