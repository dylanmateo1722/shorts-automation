"""Contratos de procedencia, transformación editorial y QA técnica.

El caso central de esta suite es el bloqueo de ``reference_only``: que la
restricción viva en el contrato y no en una convención significa que un recurso
de referencia **no se puede representar** como utilizable en el render, ni por
descuido ni a propósito.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.contracts.models import (
    NOTA_EDITORIAL,
    SCHEMA_VERSION_TRANSFORMACION,
    TRANSFORMACIONES_OBLIGATORIAS,
    AssetProvenance,
    BaseLicencia,
    ClaseFuente,
    ComprobacionQA,
    EstadoEvaluacion,
    EstadoQA,
    EstadoValidacion,
    EvaluacionEditorialLegal,
    NivelQA,
    OverlaySpec,
    ProvenanceLedger,
    QAResult,
    TipoTransformacion,
    TransformationElement,
    TransformationSet,
)

PROPIO = "voice/narration.mp3"
REFERENCIA = "reference/fuente.mp4"


def _propio(**extra) -> AssetProvenance:
    base = dict(
        asset_path=PROPIO,
        source_class=ClaseFuente.render_permitido,
        basis=BaseLicencia.propia,
        evidence_ref="generado por este pipeline",
        description="narración propia",
    )
    base.update(extra)
    return AssetProvenance(**base)


def _referencia(**extra) -> AssetProvenance:
    base = dict(
        asset_path=REFERENCIA,
        source_class=ClaseFuente.solo_referencia,
        basis=BaseLicencia.ninguna,
        evidence_ref="consultado para el análisis temático",
        description="material de referencia",
    )
    base.update(extra)
    return AssetProvenance(**base)


def _ledger(**extra) -> ProvenanceLedger:
    base = dict(run_id=uuid.uuid4(), assets=[_propio()], render_assets=[PROPIO])
    base.update(extra)
    return ProvenanceLedger(**base)


def _elementos(tipos=None) -> list[TransformationElement]:
    return [
        TransformationElement(
            kind=tipo,
            rationale=f"elemento {tipo.value}",
            asset_ref=f"assets/{tipo.value}.json",
            validation_status=EstadoValidacion.validado,
        )
        for tipo in (tipos if tipos is not None else TipoTransformacion)
    ]


def _checks(errores: int = 0, avisos: int = 0) -> list[ComprobacionQA]:
    checks = [ComprobacionQA(name="ok", ok=True, detail="bien")]
    checks += [
        ComprobacionQA(name=f"e{i}", ok=False, detail="mal", level=NivelQA.error)
        for i in range(errores)
    ]
    checks += [
        ComprobacionQA(name=f"w{i}", ok=False, detail="ojo", level=NivelQA.aviso)
        for i in range(avisos)
    ]
    return checks


def _qa(**extra) -> QAResult:
    checks = extra.pop("checks", _checks())
    fallos = [c for c in checks if not c.ok and c.level is NivelQA.error]
    avisos = [c for c in checks if not c.ok and c.level is NivelQA.aviso]
    base = dict(
        run_id=uuid.uuid4(),
        status=(
            EstadoQA.reprobado
            if fallos
            else (EstadoQA.aprobado_con_avisos if avisos else EstadoQA.aprobado)
        ),
        technical_qa_ok=not fallos,
        checks=checks,
        transformation_artifact="transformation_set.json",
        provenance_artifact="provenance_ledger.json",
        ready_for_render=not fallos,
    )
    base.update(extra)
    return QAResult(**base)


# ---------------------------------------------------------------------------
# Campos comunes
# ---------------------------------------------------------------------------


def test_los_artefactos_de_transformacion_declaran_su_version():
    assert _ledger().schema_version == SCHEMA_VERSION_TRANSFORMACION == "1.0"
    assert TransformationSet(run_id=uuid.uuid4()).schema_version == "1.0"
    assert _qa().schema_version == "1.0"


def test_run_id_invalido_es_rechazado():
    with pytest.raises(ValidationError):
        _ledger(run_id="no-es-un-uuid")


def test_campo_desconocido_es_rechazado():
    with pytest.raises(ValidationError):
        _ledger(similarity_score=0.4)


# ---------------------------------------------------------------------------
# Procedencia
# ---------------------------------------------------------------------------


def test_las_dos_clases_de_procedencia_existen():
    assert ClaseFuente.render_permitido.value == "render_allowed"
    assert ClaseFuente.solo_referencia.value == "reference_only"


def test_un_recurso_render_allowed_se_acepta():
    ledger = _ledger()
    assert ledger.permite_render(PROPIO)
    assert ledger.clase(PROPIO) is ClaseFuente.render_permitido
    assert ledger.render_assets == [PROPIO]


def test_un_recurso_reference_only_queda_bloqueado_por_el_contrato():
    """El caso que da sentido a toda la capa.

    No es que una etapa se acuerde de comprobarlo: el artefacto **no valida** si
    un recurso de referencia aparece entre los utilizables para el render.
    """
    with pytest.raises(ValidationError, match="reference_only"):
        _ledger(assets=[_propio(), _referencia()], render_assets=[PROPIO, REFERENCIA])


def test_un_recurso_reference_only_puede_existir_en_el_run():
    """Puede estar registrado; lo que no puede es usarse en el render."""
    ledger = _ledger(assets=[_propio(), _referencia()], render_assets=[PROPIO])
    assert ledger.clase(REFERENCIA) is ClaseFuente.solo_referencia
    assert not ledger.permite_render(REFERENCIA)
    assert REFERENCIA not in ledger.render_assets


def test_reference_only_no_se_convierte_en_render_allowed_solo_por_listarlo():
    """No hay promoción automática: hay que cambiar su clase declarada."""
    with pytest.raises(ValidationError):
        _ledger(assets=[_referencia()], render_assets=[REFERENCIA])


def test_un_recurso_sin_declarar_no_puede_usarse_en_el_render():
    with pytest.raises(ValidationError, match="sin procedencia declarada"):
        _ledger(assets=[_propio()], render_assets=[PROPIO, "overlay/overlay.ass"])


def test_la_restriccion_no_depende_del_nombre_del_archivo():
    """Un recurso llamado "propio" sigue bloqueado si se declaró de referencia."""
    enganoso = _referencia(asset_path="voice/narration_propia_original.mp3")
    with pytest.raises(ValidationError, match="reference_only"):
        _ledger(assets=[enganoso], render_assets=[enganoso.asset_path])


def test_un_recurso_declarado_dos_veces_es_rechazado():
    with pytest.raises(ValidationError, match="dos veces"):
        _ledger(assets=[_propio(), _propio(description="otra cosa")])


def test_la_procedencia_rechaza_rutas_absolutas():
    with pytest.raises(ValidationError):
        _propio(asset_path="/home/runner/work/runs/voice/narration.mp3")
    with pytest.raises(ValidationError):
        _ledger(render_assets=["/tmp/narration.mp3"])


def test_la_procedencia_exige_evidencia_y_descripcion():
    with pytest.raises(ValidationError):
        _propio(evidence_ref="   ")
    with pytest.raises(ValidationError):
        _propio(description="")


def test_un_ledger_vacio_es_valido_pero_no_permite_nada():
    ledger = _ledger(assets=[], render_assets=[])
    assert ledger.clase(PROPIO) is None
    assert not ledger.permite_render(PROPIO)


@pytest.mark.parametrize("base", list(BaseLicencia))
def test_cualquier_base_de_licencia_es_representable(base):
    """G4 registra la procedencia; no resuelve la cuestión legal."""
    ledger = _ledger(assets=[_propio(basis=base)], render_assets=[PROPIO])
    assert ledger.assets[0].basis is base


# ---------------------------------------------------------------------------
# Elementos de transformación
# ---------------------------------------------------------------------------


def test_los_cinco_tipos_obligatorios_estan_definidos():
    assert {t.value for t in TRANSFORMACIONES_OBLIGATORIAS} == {
        "own_narration",
        "restructured_script",
        "added_context",
        "own_subtitles",
        "visual_overlay",
    }


def test_un_conjunto_completo_no_tiene_faltantes():
    conjunto = TransformationSet(run_id=uuid.uuid4(), elements=_elementos())
    assert conjunto.faltantes == set()
    assert len(conjunto.tipos) == 5


def test_un_conjunto_incompleto_declara_lo_que_falta():
    """El contrato permite persistirlo: que falte algo es un hallazgo de QA."""
    parciales = [TipoTransformacion.narracion_propia, TipoTransformacion.subtitulos_propios]
    conjunto = TransformationSet(run_id=uuid.uuid4(), elements=_elementos(parciales))
    assert {t.value for t in conjunto.faltantes} == {
        "restructured_script",
        "added_context",
        "visual_overlay",
    }


def test_un_elemento_duplicado_es_rechazado():
    elementos = _elementos([TipoTransformacion.narracion_propia])
    with pytest.raises(ValidationError, match="duplicado"):
        TransformationSet(run_id=uuid.uuid4(), elements=elementos * 2)


def test_un_elemento_exige_referencia_a_un_recurso():
    """Sin referencia, el elemento sería indistinguible de transformation=true."""
    with pytest.raises(ValidationError):
        TransformationElement(
            kind=TipoTransformacion.narracion_propia, rationale="propia"
        )


def test_un_elemento_rechaza_una_referencia_absoluta():
    with pytest.raises(ValidationError):
        TransformationElement(
            kind=TipoTransformacion.overlay_visual,
            rationale="rótulo",
            asset_ref="/tmp/overlay.ass",
        )


def test_un_elemento_exige_descripcion():
    with pytest.raises(ValidationError):
        TransformationElement(
            kind=TipoTransformacion.overlay_visual, rationale="  ", asset_ref="a.ass"
        )


def test_el_estado_de_validacion_por_defecto_es_pendiente():
    elemento = TransformationElement(
        kind=TipoTransformacion.contexto_agregado, rationale="x", asset_ref="a.json"
    )
    assert elemento.validation_status is EstadoValidacion.pendiente


def test_el_contrato_no_guarda_puntuacion_de_transformacion():
    """No hay porcentaje, ni similitud, ni umbral: no caben en el contrato."""
    campos = set(TransformationElement.model_fields) | set(TransformationSet.model_fields)
    prohibidos = {"similarity", "similarity_score", "percentage", "transformation_score"}
    assert not campos & prohibidos


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------


def _overlay(**extra) -> OverlaySpec:
    base = dict(
        run_id=uuid.uuid4(),
        overlay_path="overlay/overlay.ass",
        overlay_format="ass",
        text="¿Cinco minutos\nal día?",
        start_seconds=0.0,
        end_seconds=3.0,
        source_text_reference="adapted_script.json",
    )
    base.update(extra)
    return OverlaySpec(**base)


def test_el_overlay_valido_lleva_texto_tiempos_y_origen():
    overlay = _overlay()
    assert overlay.end_seconds > overlay.start_seconds
    assert overlay.source_text_reference == "adapted_script.json"


@pytest.mark.parametrize("fin", [0.0, -1.0, 0.5])
def test_el_overlay_rechaza_tiempos_imposibles(fin):
    with pytest.raises(ValidationError):
        _overlay(start_seconds=0.5, end_seconds=fin)


def test_el_overlay_rechaza_una_ruta_absoluta():
    with pytest.raises(ValidationError):
        _overlay(overlay_path="/tmp/overlay.ass")


def test_el_overlay_rechaza_texto_vacio():
    with pytest.raises(ValidationError):
        _overlay(text="   ")


# ---------------------------------------------------------------------------
# QAResult
# ---------------------------------------------------------------------------


def test_qa_pass_sin_errores_ni_avisos():
    informe = _qa()
    assert informe.status is EstadoQA.aprobado
    assert informe.technical_qa_ok is True
    assert informe.errors == [] and informe.warnings == []
    assert informe.ready_for_render is True


def test_qa_fail_con_un_error():
    informe = _qa(checks=_checks(errores=1))
    assert informe.status is EstadoQA.reprobado
    assert informe.technical_qa_ok is False
    assert len(informe.errors) == 1
    assert informe.ready_for_render is False


def test_los_avisos_no_convierten_la_qa_en_fail():
    """Un aviso es información. Ascenderlo a error sería cambiar el veredicto."""
    informe = _qa(checks=_checks(avisos=3))
    assert informe.status is EstadoQA.aprobado_con_avisos
    assert informe.technical_qa_ok is True
    assert len(informe.warnings) == 3
    assert informe.ready_for_render is True


def test_un_estado_que_no_cuadra_con_las_comprobaciones_es_rechazado():
    with pytest.raises(ValidationError, match="el estado declarado"):
        _qa(checks=_checks(errores=1), status=EstadoQA.aprobado, technical_qa_ok=True)


def test_no_se_puede_declarar_ok_con_errores():
    with pytest.raises(ValidationError, match="technical_qa_ok"):
        _qa(checks=_checks(errores=1), status=EstadoQA.reprobado, technical_qa_ok=True)


def test_no_se_puede_declarar_listo_para_el_render_con_errores():
    with pytest.raises(ValidationError, match="lista para el render"):
        _qa(checks=_checks(errores=1), ready_for_render=True)


def test_la_qa_tecnica_no_se_mezcla_con_el_juicio_editorial():
    """Lo normal es QA técnica en verde y juicio editorial sin evaluar."""
    informe = _qa()
    assert informe.technical_qa_ok is True
    assert informe.editorial_legal_assessment.status is EstadoEvaluacion.no_evaluado


def test_el_juicio_editorial_nunca_afirma_haber_aplicado_un_umbral():
    """El contrato rechaza el valor contrario: no existe tal umbral."""
    with pytest.raises(ValidationError):
        EvaluacionEditorialLegal(automated_similarity_threshold_applied=True)


def test_el_juicio_editorial_trae_la_advertencia_por_escrito():
    evaluacion = EvaluacionEditorialLegal()
    assert evaluacion.note == NOTA_EDITORIAL
    assert "no se automatiza" in evaluacion.note
    assert evaluacion.assessed_by is None


def test_una_persona_puede_registrar_su_decision_editorial():
    evaluacion = EvaluacionEditorialLegal(
        status=EstadoEvaluacion.aprobado_por_humano, assessed_by="revisión editorial"
    )
    assert evaluacion.status.value == "APPROVED_BY_HUMAN"
    assert evaluacion.automated_similarity_threshold_applied is False


def test_el_informe_referencia_los_artefactos_en_vez_de_copiarlos():
    """Una segunda copia de los elementos acabaría discrepando de la primera."""
    assert "transformation_elements" not in QAResult.model_fields
    assert "transformation_artifact" in QAResult.model_fields


def test_el_informe_rechaza_rutas_absolutas():
    with pytest.raises(ValidationError):
        _qa(transformation_artifact="/tmp/transformation_set.json")


def test_el_informe_distingue_el_nivel_de_cada_comprobacion():
    informe = _qa(checks=_checks(errores=1, avisos=2))
    assert [c.level for c in informe.errors] == [NivelQA.error]
    assert [c.level for c in informe.warnings] == [NivelQA.aviso, NivelQA.aviso]
    assert len(informe.checks) == 4


def test_el_resumen_etiqueta_fallos_y_avisos_por_separado():
    informe = _qa(checks=_checks(errores=1, avisos=1))
    resumen = informe.resumen()
    assert "FALLA" in resumen and "AVISO" in resumen


# ---------------------------------------------------------------------------
# Serialización
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "constructor, modelo",
    [
        (_ledger, ProvenanceLedger),
        (_overlay, OverlaySpec),
        (_qa, QAResult),
        (lambda: TransformationSet(run_id=uuid.uuid4(), elements=_elementos()), TransformationSet),
    ],
)
def test_ida_y_vuelta_por_json(constructor, modelo):
    original = constructor()
    assert modelo.model_validate_json(original.model_dump_json()) == original


def test_el_bloqueo_sobrevive_a_la_ida_y_vuelta():
    """Un ledger manipulado a mano en el JSON tampoco se acepta al releerlo."""
    import json

    ledger = _ledger(assets=[_propio(), _referencia()], render_assets=[PROPIO])
    datos = json.loads(ledger.model_dump_json())
    datos["render_assets"].append(REFERENCIA)

    with pytest.raises(ValidationError, match="reference_only"):
        ProvenanceLedger.model_validate(datos)


def test_los_enums_se_serializan_por_valor():
    serializado = _ledger().model_dump_json()
    assert '"render_allowed"' in serializado
    assert '"own"' in serializado
