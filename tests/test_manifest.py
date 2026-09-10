"""Manifest: creación, actualización, persistencia y recarga."""

from __future__ import annotations

import json
import uuid

import pytest

from app.core.errors import ArtefactoCorrupto
from app.core.manifest import EstadoEtapa, EstadoRun, Manifest


def test_creacion_y_persistencia(tmp_path):
    rid = uuid.uuid4()
    manifest = Manifest(run_id=rid, config={"log_level": "INFO"}, versions={"app": "0.1.0"})
    ruta = manifest.guardar(tmp_path)

    assert ruta.is_file()
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    assert datos["run_id"] == str(rid)
    assert datos["status"] == EstadoRun.creada.value
    assert datos["schema_version"] == "1"


def test_recarga_conserva_el_estado(tmp_path):
    rid = uuid.uuid4()
    manifest = Manifest(run_id=rid)
    entrada = manifest.registrar_etapa("render")
    entrada.status = EstadoEtapa.completada
    entrada.duration_s = 12.5
    manifest.registrar_artefacto("render_result", "render_result.json", 701)
    manifest.guardar(tmp_path)

    recargado = Manifest.cargar(tmp_path)
    assert recargado.etapa_completada("render")
    assert recargado.etapa("render").duration_s == 12.5
    assert recargado.artifacts["render_result"].bytes == 701


def test_registrar_etapa_es_idempotente(tmp_path):
    manifest = Manifest(run_id=uuid.uuid4())
    primera = manifest.registrar_etapa("render")
    segunda = manifest.registrar_etapa("render")
    assert primera is segunda
    assert len(manifest.stages) == 1


def test_etapa_omitida_cuenta_como_completada():
    """Reanudar no debe re-ejecutar lo que ya se omitió por ser válido."""
    manifest = Manifest(run_id=uuid.uuid4())
    manifest.registrar_etapa("render").status = EstadoEtapa.omitida
    assert manifest.etapa_completada("render")


def test_etapa_fallida_no_cuenta_como_completada():
    manifest = Manifest(run_id=uuid.uuid4())
    manifest.registrar_etapa("render").status = EstadoEtapa.fallida
    assert not manifest.etapa_completada("render")


def test_manifest_de_otra_ejecucion_se_rechaza(tmp_path):
    """Evita mezclar dos corridas en el mismo directorio."""
    Manifest(run_id=uuid.uuid4()).guardar(tmp_path)
    with pytest.raises(ArtefactoCorrupto):
        Manifest.cargar_o_crear(tmp_path, uuid.uuid4(), config={}, versions={})


def test_manifest_corrupto_se_reemplaza_por_uno_nuevo(tmp_path):
    (tmp_path / "manifest.json").write_text("{roto", encoding="utf-8")
    rid = uuid.uuid4()
    manifest, origen = Manifest.cargar_o_crear(tmp_path, rid, config={}, versions={})
    assert origen == "creado"
    assert manifest.run_id == rid


def test_escritura_atomica_no_deja_temporales(tmp_path):
    Manifest(run_id=uuid.uuid4()).guardar(tmp_path)
    assert not list(tmp_path.glob("*.tmp"))
