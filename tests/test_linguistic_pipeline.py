"""Pipeline lingüístico: Transcript → Translation → AdaptedScript.

Usa el proveedor falso, que cuenta las llamadas. Eso permite demostrar lo que
más importa en esta etapa: **un artefacto válido evita llamar al modelo**, y
por tanto evita gastar dinero.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest

from app.adapters.llm import ProveedorFalso
from app.config.settings import Settings
from app.contracts.models import AdaptedScript, TipoSeccion, Transcript, Translation
from app.core.manifest import EstadoEtapa, EstadoRun
from app.core.workspace import Workspace
from app.pipeline import core
from app.pipeline.linguistic import construir_pipeline_linguistico

RAIZ = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
TRANSCRIPT = FIXTURES / "transcripts" / "estudio_atencion.json"
RESPUESTAS = json.loads(
    (FIXTURES / "respuestas" / "estudio_atencion.json").read_text("utf-8")
)


@pytest.fixture
def settings_ling(tmp_path) -> Settings:
    """Configuración real de prompts, con runs en un directorio temporal."""
    return Settings(
        raiz_proyecto=RAIZ,
        raiz_runs=tmp_path / "runs",
        llm_provider="fake",
        llm_model="fake-1",
        target_language="es",
    )


@pytest.fixture
def proveedor() -> ProveedorFalso:
    return ProveedorFalso(copy.deepcopy(RESPUESTAS))


def _ejecutar(settings, proveedor, *, run_id=None, objetivo=30.0, forzar=None):
    return core.ejecutar_run(
        run_id or uuid.uuid4(),
        settings,
        forzar=forzar,
        parametros={
            "transcript_path": str(TRANSCRIPT),
            "target_duration_seconds": objetivo,
            "proveedor_llm": proveedor,
        },
        etapas=construir_pipeline_linguistico(),
    )


# --- camino completo -------------------------------------------------------


def test_pipeline_completo(settings_ling, proveedor):
    resultado = _ejecutar(settings_ling, proveedor)

    assert resultado.exito
    assert resultado.manifest.status is EstadoRun.completada
    assert [e.name for e in resultado.manifest.stages] == [
        "ingest_transcript", "translation", "adaptation",
    ]
    assert proveedor.llamadas == 2, "una llamada por etapa de modelo"

    ws = Workspace(settings_ling.raiz_runs, resultado.run_id)
    transcript = ws.leer_artefacto("transcript", Transcript)
    traduccion = ws.leer_artefacto("translation", Translation)
    guion = ws.leer_artefacto("adapted_script", AdaptedScript)

    assert transcript.source_language == "en"
    assert transcript.run_id == resultado.run_id, "el run_id lo pone la corrida"
    assert traduccion.target_language == "es"
    assert traduccion.source_transcript_reference == "transcript"
    assert guion.language == "es"
    assert guion.hook.startswith("¿")
    assert guion.sections[0].kind is TipoSeccion.hook
    assert guion.target_duration_seconds == 30.0
    assert 0 < guion.estimated_duration_seconds < 60
    assert guion.full_text
    assert guion.transformation_notes


def test_las_secciones_quedan_ordenadas(settings_ling, proveedor):
    desordenado = copy.deepcopy(RESPUESTAS)
    desordenado[1]["sections"] = list(reversed(desordenado[1]["sections"]))
    resultado = _ejecutar(settings_ling, ProveedorFalso(desordenado))
    ws = Workspace(settings_ling.raiz_runs, resultado.run_id)
    guion = ws.leer_artefacto("adapted_script", AdaptedScript)
    assert [s.order for s in guion.sections] == sorted(s.order for s in guion.sections)


def test_el_manifest_registra_proveedor_modelo_y_prompt(settings_ling, proveedor):
    resultado = _ejecutar(settings_ling, proveedor)

    for nombre, familia in (("translation", "translation_v1"), ("adaptation", "adaptation_v1")):
        metadata = resultado.manifest.etapa(nombre).metadata
        assert metadata["provider"] == "fake"
        assert metadata["model"] == "fake-1"
        assert metadata["prompt_version"] == familia

    # La etapa de ingesta no invoca a ningún modelo y no inventa procedencia.
    assert resultado.manifest.etapa("ingest_transcript").metadata == {}


# --- coste: idempotencia ---------------------------------------------------


def test_un_artefacto_valido_evita_llamar_al_modelo(settings_ling):
    """Requisito de control de coste: reanudar no debe repetir el gasto."""
    run_id = uuid.uuid4()
    primero = ProveedorFalso(copy.deepcopy(RESPUESTAS))
    assert _ejecutar(settings_ling, primero, run_id=run_id).exito
    assert primero.llamadas == 2

    segundo = ProveedorFalso(copy.deepcopy(RESPUESTAS))
    resultado = _ejecutar(settings_ling, segundo, run_id=run_id)

    assert resultado.exito
    assert segundo.llamadas == 0, "el modelo no debía llamarse en la reanudación"
    assert all(e.status is EstadoEtapa.omitida for e in resultado.manifest.stages)


def test_forzar_una_etapa_vuelve_a_llamar_solo_a_esa(settings_ling):
    run_id = uuid.uuid4()
    _ejecutar(settings_ling, ProveedorFalso(copy.deepcopy(RESPUESTAS)), run_id=run_id)

    segundo = ProveedorFalso([copy.deepcopy(RESPUESTAS[1])])
    resultado = _ejecutar(settings_ling, segundo, run_id=run_id, forzar="adaptation")

    assert resultado.exito
    assert segundo.llamadas == 1
    assert resultado.manifest.etapa("translation").status is EstadoEtapa.omitida
    assert resultado.manifest.etapa("adaptation").status is EstadoEtapa.completada


def test_un_artefacto_corrupto_si_vuelve_a_llamar(settings_ling):
    run_id = uuid.uuid4()
    _ejecutar(settings_ling, ProveedorFalso(copy.deepcopy(RESPUESTAS)), run_id=run_id)
    ws = Workspace(settings_ling.raiz_runs, run_id)
    ws.ruta_artefacto("adapted_script").write_text("{roto", encoding="utf-8")

    segundo = ProveedorFalso([copy.deepcopy(RESPUESTAS[1])])
    assert _ejecutar(settings_ling, segundo, run_id=run_id).exito
    assert segundo.llamadas == 1


# --- fidelidad -------------------------------------------------------------


def test_una_traduccion_que_pierde_una_cifra_falla(settings_ling):
    roto = copy.deepcopy(RESPUESTAS)
    roto[0]["text"] = roto[0]["text"].replace("240 voluntarios", "muchos voluntarios")
    resultado = _ejecutar(settings_ling, ProveedorFalso(roto))

    assert not resultado.exito
    assert resultado.manifest.etapa("translation").status is EstadoEtapa.fallida
    assert resultado.manifest.errors[0]["type"] == "ContenidoInvalido"
    assert resultado.manifest.errors[0]["retryable"] is False


def test_una_traduccion_sin_traducir_falla(settings_ling):
    roto = copy.deepcopy(RESPUESTAS)
    roto[0]["text"] = json.loads(TRANSCRIPT.read_text("utf-8"))["text"]
    assert not _ejecutar(settings_ling, ProveedorFalso(roto)).exito


def test_un_guion_que_inventa_una_cifra_falla(settings_ling):
    roto = copy.deepcopy(RESPUESTAS)
    roto[1]["sections"][2]["text"] += " Participaron 999 centros."
    resultado = _ejecutar(settings_ling, ProveedorFalso(roto))

    assert not resultado.exito
    assert resultado.manifest.etapa("adaptation").status is EstadoEtapa.fallida
    assert resultado.manifest.etapa_completada("translation"), "la etapa previa se conserva"


def test_un_guion_con_signos_rotos_falla(settings_ling):
    roto = copy.deepcopy(RESPUESTAS)
    roto[1]["sections"][0]["text"] = "¿Cinco minutos al día pueden cambiar tu atención."
    roto[1]["hook"] = roto[1]["sections"][0]["text"]
    assert not _ejecutar(settings_ling, ProveedorFalso(roto)).exito


# --- respuestas mal formadas ----------------------------------------------


def test_un_guion_sin_hook_se_rechaza(settings_ling):
    roto = copy.deepcopy(RESPUESTAS)
    for seccion in roto[1]["sections"]:
        seccion["kind"] = "development"
    resultado = _ejecutar(settings_ling, ProveedorFalso(roto))
    assert not resultado.exito
    assert resultado.manifest.errors[-1]["type"] == "RespuestaInvalida"


def test_un_tipo_de_seccion_desconocido_se_rechaza(settings_ling):
    roto = copy.deepcopy(RESPUESTAS)
    roto[1]["sections"][1]["kind"] = "epilogo"
    assert not _ejecutar(settings_ling, ProveedorFalso(roto)).exito


def test_una_traduccion_sin_campo_text_se_rechaza(settings_ling):
    roto = copy.deepcopy(RESPUESTAS)
    del roto[0]["text"]
    resultado = _ejecutar(settings_ling, ProveedorFalso(roto))
    assert not resultado.exito
    assert resultado.manifest.errors[0]["type"] == "RespuestaInvalida"


# --- duración y condensación ----------------------------------------------


def test_un_guion_largo_dispara_la_condensacion(settings_ling):
    largo = copy.deepcopy(RESPUESTAS)
    largo[1]["sections"][2]["text"] = " ".join(["palabra"] * 200) + "."
    # Segunda respuesta de adaptación: ya condensada.
    respuestas = [largo[0], largo[1], copy.deepcopy(RESPUESTAS[1])]
    proveedor = ProveedorFalso(respuestas)

    resultado = _ejecutar(settings_ling, proveedor, objetivo=30.0)

    assert resultado.exito
    assert proveedor.llamadas == 3, "traducción + adaptación + una condensación"
    assert "condense_v1" in proveedor.prompts_recibidos[2]


def test_si_sigue_largo_tras_los_intentos_se_invalida(settings_ling):
    """No se trunca el texto: se declara inválido."""
    largo = copy.deepcopy(RESPUESTAS)
    largo[1]["sections"][2]["text"] = " ".join(["palabra"] * 200) + "."
    proveedor = ProveedorFalso([largo[0], largo[1]])

    resultado = _ejecutar(settings_ling, proveedor, objetivo=30.0)

    assert not resultado.exito
    assert proveedor.llamadas == 1 + 1 + settings_ling.max_condensation_attempts
    error = resultado.manifest.errors[-1]
    assert error["type"] == "ContenidoInvalido"
    assert "no se trunca" in error["message"]


def test_un_guion_corto_es_valido_y_solo_avisa(settings_ling, proveedor):
    """Alargarlo exigiría inventar contenido, así que se acepta."""
    resultado = _ejecutar(settings_ling, proveedor, objetivo=60.0)
    assert resultado.exito


# --- entrada ---------------------------------------------------------------


def test_sin_transcript_falla_con_mensaje_claro(settings_ling, proveedor):
    resultado = core.ejecutar_run(
        uuid.uuid4(), settings_ling,
        parametros={"proveedor_llm": proveedor},
        etapas=construir_pipeline_linguistico(),
    )
    assert not resultado.exito
    assert "--transcript" in resultado.manifest.errors[0]["message"]
    assert proveedor.llamadas == 0


def test_un_transcript_inexistente_falla(settings_ling, proveedor):
    resultado = core.ejecutar_run(
        uuid.uuid4(), settings_ling,
        parametros={"transcript_path": "/no/existe.json", "proveedor_llm": proveedor},
        etapas=construir_pipeline_linguistico(),
    )
    assert not resultado.exito
    assert resultado.manifest.errors[0]["type"] == "EntradaInvalida"


def test_un_transcript_que_no_cumple_el_contrato_falla(settings_ling, proveedor, tmp_path):
    malo = tmp_path / "malo.json"
    malo.write_text(json.dumps({"source_language": "en"}), encoding="utf-8")
    resultado = core.ejecutar_run(
        uuid.uuid4(), settings_ling,
        parametros={"transcript_path": str(malo), "proveedor_llm": proveedor},
        etapas=construir_pipeline_linguistico(),
    )
    assert not resultado.exito
    assert "contrato" in resultado.manifest.errors[0]["message"]


def test_validar_run_revalida_los_artefactos(settings_ling, proveedor):
    resultado = _ejecutar(settings_ling, proveedor)
    etapas = construir_pipeline_linguistico()
    ok, problemas = core.validar_run(resultado.run_id, settings_ling, etapas=etapas)
    assert ok and not problemas


def test_el_proveedor_se_construye_una_sola_vez_por_corrida(
    settings_ling, monkeypatch, tmp_path
):
    """Regresión: construirlo por etapa reiniciaba el cursor del falso.

    La adaptación recibía entonces la respuesta de la traducción y el pipeline
    fallaba solo por CLI, nunca en los tests que inyectaban el proveedor.
    """
    respuestas = tmp_path / "respuestas.json"
    respuestas.write_text(json.dumps(RESPUESTAS), encoding="utf-8")
    monkeypatch.setenv("LLM_FAKE_RESPONSES", str(respuestas))

    resultado = core.ejecutar_run(
        uuid.uuid4(),
        settings_ling,
        parametros={
            "transcript_path": str(TRANSCRIPT),
            "target_duration_seconds": 30.0,
        },
        etapas=construir_pipeline_linguistico(),
    )

    assert resultado.exito, "cada etapa recibió su propia respuesta preparada"
    ws = Workspace(settings_ling.raiz_runs, resultado.run_id)
    assert ws.leer_artefacto("adapted_script", AdaptedScript).hook
