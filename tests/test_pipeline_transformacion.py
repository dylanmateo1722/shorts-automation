"""Etapas de transformación y QA técnica sobre el orquestador.

Se ejecutan con el proveedor de TTS falso, que genera audio real con FFmpeg: la
cadena que se prueba aquí es la misma que corre con Edge TTS, sin salir a la
red.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.adapters.tts import ProveedorTTSFalso
from app.contracts.models import (
    AdaptedScript,
    ClaseFuente,
    EstadoQA,
    OverlaySpec,
    ProvenanceLedger,
    QAResult,
    SubtitleAsset,
    TipoTransformacion,
    TransformationSet,
    VoiceAsset,
)
from app.core.errors import ProcedenciaInvalida
from app.core.manifest import EstadoEtapa, Manifest
from app.core.stage_runner import StageRunner
from app.pipeline import qa as modulo_qa
from app.pipeline import transformation, voice
from tests.conftest import construir_guion


@pytest.fixture
def proveedor() -> ProveedorTTSFalso:
    return ProveedorTTSFalso()


@pytest.fixture
def runner(workspace_voz, settings_voz, proveedor) -> StageRunner:
    """Corrida con las etapas de voz ya ejecutadas y las de G4 por delante."""
    manifest, _ = Manifest.cargar_o_crear(
        workspace_voz.dir, workspace_voz.run_id,
        config=settings_voz.publico(), versions={},
    )
    runner = StageRunner(
        workspace_voz, manifest, settings_voz,
        {voice.CLAVE_PROVEEDOR_TTS: proveedor},
    )
    for etapa in voice.construir_pipeline_voz():
        runner.ejecutar(etapa)
    return runner


def _etapas_g4():
    return [
        transformation.ETAPA_OVERLAY,
        transformation.ETAPA_PROCEDENCIA,
        transformation.ETAPA_TRANSFORMACION,
        modulo_qa.ETAPA_QA,
    ]


def _ejecutar_g4(runner: StageRunner):
    return [runner.ejecutar(etapa) for etapa in _etapas_g4()]


def _con_referencia(runner: StageRunner, ruta: str = "reference/fuente.txt") -> str:
    """Crea un recurso de referencia y lo declara en los parámetros."""
    destino = runner.workspace.ruta(ruta)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("material consultado", encoding="utf-8")
    runner.parametros[transformation.CLAVE_REFERENCIAS] = [ruta]
    return ruta


# ---------------------------------------------------------------------------
# Cadena completa
# ---------------------------------------------------------------------------


def test_del_subtitulo_al_informe_de_qa(runner, workspace_voz):
    overlay, ledger, conjunto, informe = (r.artefacto for r in _ejecutar_g4(runner))

    assert isinstance(overlay, OverlaySpec)
    assert isinstance(ledger, ProvenanceLedger)
    assert isinstance(conjunto, TransformationSet)
    assert isinstance(informe, QAResult)
    assert workspace_voz.ruta(overlay.overlay_path).is_file()
    assert informe.technical_qa_ok is True


def test_se_persisten_los_cuatro_artefactos(runner, workspace_voz):
    _ejecutar_g4(runner)
    for nombre, modelo in (
        (transformation.ARTEFACTO_OVERLAY, OverlaySpec),
        (transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger),
        (transformation.ARTEFACTO_TRANSFORMACION, TransformationSet),
        (modulo_qa.ARTEFACTO_QA, QAResult),
    ):
        assert workspace_voz.leer_artefacto(nombre, modelo).run_id == workspace_voz.run_id


def test_todas_las_rutas_guardadas_son_relativas(runner, workspace_voz):
    _ejecutar_g4(runner)
    overlay = workspace_voz.leer_artefacto(transformation.ARTEFACTO_OVERLAY, OverlaySpec)
    ledger = workspace_voz.leer_artefacto(
        transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger
    )
    conjunto = workspace_voz.leer_artefacto(
        transformation.ARTEFACTO_TRANSFORMACION, TransformationSet
    )

    rutas = (
        [overlay.overlay_path, overlay.source_text_reference]
        + [a.asset_path for a in ledger.assets]
        + [e.asset_ref for e in conjunto.elements]
    )
    for ruta in rutas:
        assert not ruta.startswith("/")
        assert str(workspace_voz.dir) not in ruta
        assert workspace_voz.ruta(ruta).is_file(), ruta


# ---------------------------------------------------------------------------
# Overlay propio
# ---------------------------------------------------------------------------


def test_el_overlay_sale_del_gancho_del_guion_propio(runner, workspace_voz):
    overlay = runner.ejecutar(transformation.ETAPA_OVERLAY).artefacto
    guion = workspace_voz.leer_artefacto(voice.ARTEFACTO_GUION, AdaptedScript)
    palabras_overlay = set(overlay.text.split())
    assert palabras_overlay <= set(guion.hook.split())
    assert overlay.source_text_reference == f"{voice.ARTEFACTO_GUION}.json"


def test_el_overlay_usa_centesimas_como_exige_el_formato_ass(runner, workspace_voz):
    """Misma regresión de Gate 0.5: libass no lee milisegundos."""
    overlay = runner.ejecutar(transformation.ETAPA_OVERLAY).artefacto
    contenido = workspace_voz.ruta(overlay.overlay_path).read_text(encoding="utf-8")
    dialogos = [x for x in contenido.splitlines() if x.startswith("Dialogue:")]
    assert dialogos
    for linea in dialogos:
        for marca in linea.split(",")[1:3]:
            assert len(marca.split(".")[-1]) == 2, marca


def test_el_overlay_no_dura_mas_que_la_narracion(runner, workspace_voz):
    overlay = runner.ejecutar(transformation.ETAPA_OVERLAY).artefacto
    voz = workspace_voz.leer_artefacto(voice.ARTEFACTO_VOZ, VoiceAsset)
    assert overlay.end_seconds <= voz.audio_duration_seconds


# ---------------------------------------------------------------------------
# Procedencia
# ---------------------------------------------------------------------------


def test_los_recursos_propios_quedan_como_render_allowed(runner, workspace_voz):
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    ledger = runner.ejecutar(transformation.ETAPA_PROCEDENCIA).artefacto

    voz = workspace_voz.leer_artefacto(voice.ARTEFACTO_VOZ, VoiceAsset)
    subtitulos = workspace_voz.leer_artefacto(voice.ARTEFACTO_SUBTITULOS, SubtitleAsset)
    for ruta in (voz.audio_path, subtitulos.srt_path, subtitulos.ass_path):
        assert ledger.permite_render(ruta), ruta
    assert all(a.basis.value == "own" for a in ledger.assets)


def test_un_recurso_de_referencia_se_registra_y_queda_fuera_del_render(runner):
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    ruta = _con_referencia(runner)
    ledger = runner.ejecutar(transformation.ETAPA_PROCEDENCIA).artefacto

    assert ledger.clase(ruta) is ClaseFuente.solo_referencia
    assert not ledger.permite_render(ruta)
    assert ruta not in ledger.render_assets


def test_un_recurso_de_referencia_nunca_llega_a_los_elementos(runner):
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    ruta = _con_referencia(runner)
    runner.ejecutar(transformation.ETAPA_PROCEDENCIA)
    conjunto = runner.ejecutar(transformation.ETAPA_TRANSFORMACION).artefacto

    assert all(e.asset_ref != ruta for e in conjunto.elements)


def test_un_recurso_de_referencia_absoluto_se_rechaza_en_la_entrada(runner):
    from app.core.errors import EntradaInvalida

    runner.ejecutar(transformation.ETAPA_OVERLAY)
    runner.parametros[transformation.CLAVE_REFERENCIAS] = ["/etc/passwd"]
    with pytest.raises(EntradaInvalida, match="absoluta"):
        runner.ejecutar(transformation.ETAPA_PROCEDENCIA)


def test_declarar_como_render_un_recurso_inexistente_es_un_error(runner, workspace_voz):
    """Si el audio desapareció, la procedencia no puede autorizarlo."""
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    voz = workspace_voz.leer_artefacto(voice.ARTEFACTO_VOZ, VoiceAsset)
    workspace_voz.ruta(voz.audio_path).unlink()

    with pytest.raises(ProcedenciaInvalida, match="no existe"):
        runner.ejecutar(transformation.ETAPA_PROCEDENCIA)


def test_un_elemento_sobre_material_de_referencia_se_bloquea(runner, workspace_voz):
    """Se fuerza el caso: el ledger degrada el audio a referencia."""
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    runner.ejecutar(transformation.ETAPA_PROCEDENCIA)

    ledger = workspace_voz.leer_artefacto(
        transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger
    )
    voz = workspace_voz.leer_artefacto(voice.ARTEFACTO_VOZ, VoiceAsset)
    degradado = ledger.model_copy(
        update={
            "assets": [
                a.model_copy(update={"source_class": ClaseFuente.solo_referencia})
                if a.asset_path == voz.audio_path
                else a
                for a in ledger.assets
            ],
            "render_assets": [r for r in ledger.render_assets if r != voz.audio_path],
        }
    )
    workspace_voz.escribir_artefacto(transformation.ARTEFACTO_PROCEDENCIA, degradado)
    runner.producidos.pop(transformation.ARTEFACTO_PROCEDENCIA, None)

    with pytest.raises(ProcedenciaInvalida, match="reference_only"):
        runner.ejecutar(transformation.ETAPA_TRANSFORMACION)


# ---------------------------------------------------------------------------
# Elementos de transformación
# ---------------------------------------------------------------------------


def test_se_registran_los_cinco_elementos_obligatorios(runner):
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    runner.ejecutar(transformation.ETAPA_PROCEDENCIA)
    conjunto = runner.ejecutar(transformation.ETAPA_TRANSFORMACION).artefacto

    assert conjunto.faltantes == set()
    assert conjunto.tipos == set(TipoTransformacion)


def test_cada_elemento_apunta_a_un_recurso_que_existe(runner, workspace_voz):
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    runner.ejecutar(transformation.ETAPA_PROCEDENCIA)
    conjunto = runner.ejecutar(transformation.ETAPA_TRANSFORMACION).artefacto

    for elemento in conjunto.elements:
        assert workspace_voz.ruta(elemento.asset_ref).is_file(), elemento.kind


def test_la_narracion_propia_referencia_el_audio_de_gate_3(runner, workspace_voz):
    """No se vuelve a sintetizar: se referencia lo que ya produjo Gate 3."""
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    runner.ejecutar(transformation.ETAPA_PROCEDENCIA)
    conjunto = runner.ejecutar(transformation.ETAPA_TRANSFORMACION).artefacto

    voz = workspace_voz.leer_artefacto(voice.ARTEFACTO_VOZ, VoiceAsset)
    elemento = next(
        e for e in conjunto.elements if e.kind is TipoTransformacion.narracion_propia
    )
    assert elemento.asset_ref == voz.audio_path


def test_los_subtitulos_propios_referencian_el_srt_de_gate_3(runner, workspace_voz):
    """No hay un segundo sistema de subtítulos."""
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    runner.ejecutar(transformation.ETAPA_PROCEDENCIA)
    conjunto = runner.ejecutar(transformation.ETAPA_TRANSFORMACION).artefacto

    subtitulos = workspace_voz.leer_artefacto(voice.ARTEFACTO_SUBTITULOS, SubtitleAsset)
    elemento = next(
        e for e in conjunto.elements if e.kind is TipoTransformacion.subtitulos_propios
    )
    assert elemento.asset_ref == subtitulos.srt_path


def test_el_guion_reestructurado_referencia_el_adapted_script(runner):
    """Sin crear una segunda copia del guion."""
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    runner.ejecutar(transformation.ETAPA_PROCEDENCIA)
    conjunto = runner.ejecutar(transformation.ETAPA_TRANSFORMACION).artefacto

    elemento = next(
        e for e in conjunto.elements if e.kind is TipoTransformacion.guion_reestructurado
    )
    assert elemento.asset_ref == f"{voice.ARTEFACTO_GUION}.json"


def test_sin_notas_de_adaptacion_el_contexto_se_declara_ausente(
    settings_voz, proveedor, tmp_path
):
    """Ausencia justificada, no un dato inventado para rellenar el campo."""
    from app.core.workspace import Workspace

    ws = Workspace(settings_voz.raiz_runs, uuid.uuid4())
    ws.crear()
    guion = construir_guion(ws.run_id)
    ws.escribir_artefacto(
        voice.ARTEFACTO_GUION, guion.model_copy(update={"transformation_notes": []})
    )
    manifest, _ = Manifest.cargar_o_crear(
        ws.dir, ws.run_id, config=settings_voz.publico(), versions={}
    )
    runner = StageRunner(
        ws, manifest, settings_voz, {voice.CLAVE_PROVEEDOR_TTS: proveedor}
    )
    for etapa in voice.construir_pipeline_voz():
        runner.ejecutar(etapa)
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    runner.ejecutar(transformation.ETAPA_PROCEDENCIA)
    conjunto = runner.ejecutar(transformation.ETAPA_TRANSFORMACION).artefacto

    elemento = next(
        e for e in conjunto.elements if e.kind is TipoTransformacion.contexto_agregado
    )
    assert "ausencia declarada" in elemento.rationale
    assert "inventar" in elemento.rationale
    assert elemento.validation_status.value == "pending"


# ---------------------------------------------------------------------------
# QA técnica
# ---------------------------------------------------------------------------


def test_la_qa_pasa_sobre_una_corrida_completa(runner):
    informe = _ejecutar_g4(runner)[-1].artefacto
    assert informe.technical_qa_ok is True
    assert informe.status in (EstadoQA.aprobado, EstadoQA.aprobado_con_avisos)
    assert informe.errors == [], informe.resumen()
    assert informe.ready_for_render is True


def test_la_qa_ejecuta_comprobaciones_de_todos_los_artefactos(runner):
    informe = _ejecutar_g4(runner)[-1].artefacto
    nombres = " ".join(c.name for c in informe.checks)
    for prefijo in (
        "run:", "adapted_script:", "voice_asset:", "word_boundaries:",
        "subtitle_asset:", "duraciones:", "overlay:", "procedencia:",
        "transformación:",
    ):
        assert prefijo in nombres, prefijo


def test_la_qa_deja_el_juicio_editorial_sin_evaluar(runner):
    informe = _ejecutar_g4(runner)[-1].artefacto
    assert informe.editorial_legal_assessment.status.value == "NOT_ASSESSED"
    assert informe.editorial_legal_assessment.automated_similarity_threshold_applied is False


def test_la_cola_de_audio_es_un_aviso_y_no_tumba_la_qa(runner):
    """El proveedor falso deja cola igual que Edge TTS: debe avisar, no fallar."""
    informe = _ejecutar_g4(runner)[-1].artefacto
    avisos = [c.name for c in informe.warnings]
    assert any("cola de audio" in nombre for nombre in avisos)
    assert informe.technical_qa_ok is True


def test_la_qa_falla_si_falta_el_audio(runner, workspace_voz):
    _ejecutar_g4(runner)
    voz = workspace_voz.leer_artefacto(voice.ARTEFACTO_VOZ, VoiceAsset)
    workspace_voz.ruta(voz.audio_path).unlink()

    informe = modulo_qa.evaluar(workspace_voz)
    assert informe.technical_qa_ok is False
    assert informe.status is EstadoQA.reprobado
    assert any("el audio existe" in c.name for c in informe.errors)
    assert informe.ready_for_render is False


def test_la_qa_falla_si_la_huella_del_guion_no_coincide(runner, workspace_voz):
    """Audio de otro guion: la QA lo ve aunque los archivos estén todos."""
    _ejecutar_g4(runner)
    ws = workspace_voz
    ws.escribir_artefacto(
        voice.ARTEFACTO_GUION,
        construir_guion(ws.run_id, texto="Un guion completamente distinto del narrado."),
    )

    informe = modulo_qa.evaluar(ws)
    assert informe.technical_qa_ok is False
    fallos = " ".join(c.name for c in informe.errors)
    assert "huella del guion" in fallos


def test_la_qa_falla_si_un_artefacto_rompe_su_contrato(runner, workspace_voz):
    """Un SRT con cues solapados no pasa ni el contrato ni la QA."""
    _ejecutar_g4(runner)
    subtitulos = workspace_voz.leer_artefacto(voice.ARTEFACTO_SUBTITULOS, SubtitleAsset)
    datos = json.loads(
        workspace_voz.ruta_artefacto(voice.ARTEFACTO_SUBTITULOS).read_text(encoding="utf-8")
    )
    # Se solapa el segundo cue con el primero escribiendo el JSON a mano.
    datos["cues"][1]["start_seconds"] = datos["cues"][0]["start_seconds"]
    workspace_voz.ruta_artefacto(voice.ARTEFACTO_SUBTITULOS).write_text(
        json.dumps(datos), encoding="utf-8"
    )
    assert subtitulos.cue_count > 1

    informe = modulo_qa.evaluar(workspace_voz)
    assert informe.technical_qa_ok is False
    detalles = " ".join(c.detail for c in informe.errors)
    assert "aún no ha terminado" in detalles


def test_la_qa_falla_si_falta_un_elemento_obligatorio(runner, workspace_voz):
    _ejecutar_g4(runner)
    conjunto = workspace_voz.leer_artefacto(
        transformation.ARTEFACTO_TRANSFORMACION, TransformationSet
    )
    incompleto = conjunto.model_copy(update={"elements": conjunto.elements[:3]})
    workspace_voz.escribir_artefacto(transformation.ARTEFACTO_TRANSFORMACION, incompleto)

    informe = modulo_qa.evaluar(workspace_voz)
    assert informe.technical_qa_ok is False
    fallo = next(c for c in informe.errors if "cinco elementos" in c.name)
    assert "faltan" in fallo.detail


def test_la_qa_avisa_de_los_recursos_de_referencia_sin_tumbar_la_corrida(runner):
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    ruta = _con_referencia(runner)
    runner.ejecutar(transformation.ETAPA_PROCEDENCIA)
    runner.ejecutar(transformation.ETAPA_TRANSFORMACION)
    informe = runner.ejecutar(modulo_qa.ETAPA_QA).artefacto

    aviso = next(c for c in informe.warnings if "solo de referencia" in c.name)
    assert ruta in aviso.detail
    assert "bloqueados para el render" in aviso.detail
    assert informe.technical_qa_ok is True


def test_la_qa_comprueba_que_la_duracion_registrada_es_la_del_archivo(runner, workspace_voz):
    _ejecutar_g4(runner)
    voz = workspace_voz.leer_artefacto(voice.ARTEFACTO_VOZ, VoiceAsset)
    workspace_voz.escribir_artefacto(
        voice.ARTEFACTO_VOZ,
        voz.model_copy(update={"audio_duration_seconds": voz.audio_duration_seconds + 12}),
    )

    informe = modulo_qa.evaluar(workspace_voz)
    assert informe.technical_qa_ok is False
    assert any("la duración registrada es la del archivo" in c.name for c in informe.errors)


def test_la_qa_no_usa_la_duracion_estimada_como_duracion_real(runner, workspace_voz):
    """La referencia del final del vídeo es el audio medido."""
    _ejecutar_g4(runner)
    voz = workspace_voz.leer_artefacto(voice.ARTEFACTO_VOZ, VoiceAsset)
    subtitulos = workspace_voz.leer_artefacto(voice.ARTEFACTO_SUBTITULOS, SubtitleAsset)
    informe = modulo_qa.evaluar(workspace_voz)

    comprobacion = next(
        c for c in informe.checks if "los subtítulos usan la duración real" in c.name
    )
    assert comprobacion.ok
    assert subtitulos.duration_seconds == voz.audio_duration_seconds


def test_la_divergencia_de_duracion_es_un_aviso(runner, workspace_voz):
    from app.contracts.models import AdaptedScript

    _ejecutar_g4(runner)
    guion = workspace_voz.leer_artefacto(voice.ARTEFACTO_GUION, AdaptedScript)
    workspace_voz.escribir_artefacto(
        voice.ARTEFACTO_GUION,
        guion.model_copy(update={"estimated_duration_seconds": 2.0}),
    )

    informe = modulo_qa.evaluar(workspace_voz)
    assert any("estimada frente a real" in c.name for c in informe.warnings)
    # Sigue siendo un aviso: no convierte la corrida en inválida.
    assert not any("estimada frente a real" in c.name for c in informe.errors)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_el_manifest_registra_las_cuatro_etapas(runner):
    _ejecutar_g4(runner)
    for nombre in ("overlay", "provenance", "transformation", "technical_qa"):
        entrada = runner.manifest.etapa(nombre)
        assert entrada is not None, nombre
        assert entrada.status is EstadoEtapa.completada
        assert entrada.started_at and entrada.finished_at


def test_el_manifest_registra_el_veredicto_de_la_qa(runner):
    _ejecutar_g4(runner)
    metadata = runner.manifest.etapa("technical_qa").metadata
    assert metadata["technical_qa_ok"] is True
    assert metadata["editorial_legal_assessment"] == "NOT_ASSESSED"
    assert metadata["ready_for_render"] is True
    assert metadata["checks"] > 0


def test_el_manifest_registra_los_recursos_bloqueados(runner):
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    _con_referencia(runner)
    runner.ejecutar(transformation.ETAPA_PROCEDENCIA)

    metadata = runner.manifest.etapa("provenance").metadata
    assert metadata["reference_only"] == 1
    assert metadata["render_allowed"] == 4


def test_el_manifest_no_guarda_secretos(runner, workspace_voz, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-no-debe-salir-de-aqui")
    _ejecutar_g4(runner)
    runner.manifest.guardar(workspace_voz.dir)

    contenido = (workspace_voz.dir / "manifest.json").read_text(encoding="utf-8")
    assert "sk-no-debe-salir-de-aqui" not in contenido
    assert "api_key" not in contenido.lower()


# ---------------------------------------------------------------------------
# Idempotencia e invalidación
# ---------------------------------------------------------------------------


def test_las_cuatro_etapas_se_omiten_si_sus_artefactos_valen(runner):
    _ejecutar_g4(runner)
    segundos = _ejecutar_g4(runner)
    assert all(r.omitida for r in segundos)


def test_el_overlay_no_se_reescribe_si_ya_vale(runner, workspace_voz):
    overlay = runner.ejecutar(transformation.ETAPA_OVERLAY).artefacto
    marca = workspace_voz.ruta(overlay.overlay_path).stat().st_mtime_ns

    assert runner.ejecutar(transformation.ETAPA_OVERLAY).omitida
    assert workspace_voz.ruta(overlay.overlay_path).stat().st_mtime_ns == marca


def test_si_desaparece_el_overlay_se_vuelve_a_generar(runner, workspace_voz):
    overlay = runner.ejecutar(transformation.ETAPA_OVERLAY).artefacto
    workspace_voz.ruta(overlay.overlay_path).unlink()

    assert not runner.ejecutar(transformation.ETAPA_OVERLAY).omitida
    assert workspace_voz.ruta(overlay.overlay_path).is_file()


def test_cambiar_el_gancho_invalida_el_overlay(runner, workspace_voz):
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    from app.contracts.models import AdaptedScript

    guion = workspace_voz.leer_artefacto(voice.ARTEFACTO_GUION, AdaptedScript)
    workspace_voz.escribir_artefacto(
        voice.ARTEFACTO_GUION, guion.model_copy(update={"hook": "Otro gancho distinto"})
    )
    runner.producidos.pop(voice.ARTEFACTO_GUION, None)

    assert not runner.ejecutar(transformation.ETAPA_OVERLAY).omitida


def test_declarar_una_referencia_nueva_invalida_el_ledger(runner):
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    runner.ejecutar(transformation.ETAPA_PROCEDENCIA)

    _con_referencia(runner)
    resultado = runner.ejecutar(transformation.ETAPA_PROCEDENCIA)
    assert not resultado.omitida
    assert len(resultado.artefacto.assets) == 5


def test_regenerar_el_ledger_invalida_la_transformacion(runner):
    _ejecutar_g4(runner)
    _con_referencia(runner)
    runner.ejecutar(transformation.ETAPA_PROCEDENCIA)

    assert not runner.ejecutar(transformation.ETAPA_TRANSFORMACION).omitida


def test_regenerar_cualquier_dependencia_invalida_la_qa(runner):
    _ejecutar_g4(runner)
    assert runner.ejecutar(modulo_qa.ETAPA_QA).omitida

    _con_referencia(runner)
    runner.ejecutar(transformation.ETAPA_PROCEDENCIA)
    assert not runner.ejecutar(modulo_qa.ETAPA_QA).omitida


def test_un_artefacto_corrupto_se_rehace(runner, workspace_voz):
    _ejecutar_g4(runner)
    workspace_voz.ruta_artefacto(transformation.ARTEFACTO_PROCEDENCIA).write_text(
        "{", encoding="utf-8"
    )
    assert not runner.ejecutar(transformation.ETAPA_PROCEDENCIA).omitida
