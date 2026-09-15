"""Idempotencia: artefacto válido se omite, artefacto inválido se rehace."""

from __future__ import annotations


import pytest

from app.contracts.models import Artefacto
from app.core.errors import ArtefactoCorrupto, ErrorPermanente
from app.core.manifest import EstadoEtapa, Manifest
from app.core.stage_runner import ContextoEtapa, Etapa, StageRunner


class ArtefactoPrueba(Artefacto):
    valor: int


@pytest.fixture
def runner(workspace):
    manifest = Manifest(run_id=workspace.run_id)
    return StageRunner(workspace, manifest, settings=None)


def _etapa(contador: list, *, validar=None) -> Etapa:
    def ejecutar(ctx: ContextoEtapa) -> ArtefactoPrueba:
        contador.append(1)
        return ArtefactoPrueba(run_id=ctx.workspace.run_id, valor=len(contador))

    return Etapa(
        nombre="prueba", artefacto="prueba", modelo=ArtefactoPrueba,
        ejecutar=ejecutar, validar=validar,
    )


def test_primera_ejecucion_produce_el_artefacto(runner, workspace):
    llamadas = []
    resultado = runner.ejecutar(_etapa(llamadas))

    assert not resultado.omitida
    assert len(llamadas) == 1
    assert workspace.existe("prueba")
    assert runner.manifest.etapa("prueba").status is EstadoEtapa.completada


def test_artefacto_valido_se_omite(runner):
    llamadas = []
    etapa = _etapa(llamadas)
    runner.ejecutar(etapa)
    resultado = runner.ejecutar(etapa)

    assert resultado.omitida
    assert len(llamadas) == 1, "la etapa no debía volver a ejecutarse"
    assert runner.manifest.etapa("prueba").status is EstadoEtapa.omitida


def test_artefacto_con_json_corrupto_se_rehace(runner, workspace):
    llamadas = []
    etapa = _etapa(llamadas)
    runner.ejecutar(etapa)
    workspace.ruta_artefacto("prueba").write_text("{roto", encoding="utf-8")

    resultado = runner.ejecutar(etapa)
    assert not resultado.omitida
    assert len(llamadas) == 2


def test_artefacto_que_no_cumple_el_contrato_se_rehace(runner, workspace):
    llamadas = []
    etapa = _etapa(llamadas)
    runner.ejecutar(etapa)
    # JSON válido pero sin el campo obligatorio: no cumple el contrato.
    workspace.ruta_artefacto("prueba").write_text(
        '{"schema_version":"1","run_id":"%s","created_at":"2026-01-01T00:00:00Z"}'
        % workspace.run_id,
        encoding="utf-8",
    )
    resultado = runner.ejecutar(etapa)
    assert not resultado.omitida
    assert len(llamadas) == 2


def test_validacion_propia_de_la_etapa_puede_invalidar(runner):
    """El JSON valida, pero el archivo al que apunta desapareció."""
    llamadas = []

    def validar(artefacto, ws):
        raise ArtefactoCorrupto("falta el archivo referenciado")

    etapa = _etapa(llamadas, validar=validar)
    runner.ejecutar(etapa)
    resultado = runner.ejecutar(etapa)
    assert not resultado.omitida
    assert len(llamadas) == 2


def test_forzar_re_ejecuta_aunque_el_artefacto_sea_valido(runner):
    llamadas = []
    etapa = _etapa(llamadas)
    runner.ejecutar(etapa)
    resultado = runner.ejecutar(etapa, forzar=True)
    assert not resultado.omitida
    assert len(llamadas) == 2


def test_fallo_registra_error_tipado_y_no_escribe_artefacto(runner, workspace):
    def ejecutar(ctx):
        raise ErrorPermanente("entrada inválida", stage="prueba")

    etapa = Etapa(
        nombre="prueba", artefacto="prueba", modelo=ArtefactoPrueba, ejecutar=ejecutar
    )
    with pytest.raises(ErrorPermanente):
        runner.ejecutar(etapa)

    assert not workspace.existe("prueba"), "un fallo no debe dejar artefacto reutilizable"
    entrada = runner.manifest.etapa("prueba")
    assert entrada.status is EstadoEtapa.fallida
    assert entrada.error["category"] == "permanente"
    assert entrada.error["retryable"] is False
    assert runner.manifest.errors


def test_el_artefacto_producido_queda_disponible_para_la_siguiente_etapa(runner):
    llamadas = []
    runner.ejecutar(_etapa(llamadas))
    recibido = {}

    def ejecutar(ctx: ContextoEtapa):
        recibido.update(ctx.previos)
        return ArtefactoPrueba(run_id=ctx.workspace.run_id, valor=99)

    runner.ejecutar(
        Etapa(nombre="segunda", artefacto="segunda", modelo=ArtefactoPrueba,
              ejecutar=ejecutar)
    )
    assert "prueba" in recibido
