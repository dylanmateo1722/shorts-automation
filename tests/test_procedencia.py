"""Procedencia: fuente, evidencia, decisión y la puerta que protege el render.

Los 41 casos que Gate 6 exige, en el orden en que los enumera, con el número
delante del nombre para que la correspondencia sea comprobable y no haya que
creerse que están todos.

Lo que esta suite demuestra —y lo que no
----------------------------------------

Demuestra que el sistema no renderiza material que su política no autoriza, y
que cuando lo rechaza **el motor no llega a ejecutarse**. Eso es una propiedad
técnica, verificable aquí.

No demuestra legalidad. ``render_allowed`` significa que la procedencia
registrada satisface la política, no que el material esté libre de
reclamaciones. Ningún test de este archivo afirma lo segundo, porque ningún test
podría.
"""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from app.config.provenance import (
    VERSION_POLITICA,
    ArchivoDeclaraciones,
    DeclaracionFuente,
    decidir,
    declarar_referencia,
    evaluar,
)
from app.contracts.models import (
    BaseLicencia,
    ClaseFuente,
    Evidence,
    LicenseDecision,
    ProvenanceLedger,
    RenderResult,
    SourceAsset,
    TipoEvidencia,
    TipoMedio,
)
from app.core.errors import ArtefactoCorrupto, EntradaInvalida, ProcedenciaInvalida
from app.core.manifest import Manifest
from app.core.stage_runner import StageRunner
from app.pipeline import render, transformation
from app.pipeline.stages import ARTEFACTO_JOB, ARTEFACTO_RESULTADO
from tests.conftest import reclasificar, sin_decision, sin_fuente

RUTA_LOG_MOTOR = "render/mpt.log"


# ---------------------------------------------------------------------------
# Constructores
# ---------------------------------------------------------------------------


def _ev(kind: TipoEvidencia, eid: str = "ev-1", **extra) -> Evidence:
    base = dict(
        evidence_id=eid,
        kind=kind,
        reference="https://example.org/respaldo",
        description="respaldo declarado para esta fuente",
    )
    base.update(extra)
    return Evidence(**base)


def _fuente(**extra) -> SourceAsset:
    base = dict(
        run_id=uuid.uuid4(),
        asset_id="fuente-1",
        media_kind=TipoMedio.video,
        origin="archivo declarado a mano para la prueba",
    )
    base.update(extra)
    return SourceAsset(**base)


def _propia(**extra) -> SourceAsset:
    base = dict(evidence=[_ev(TipoEvidencia.registro_propiedad)])
    base.update(extra)
    return _fuente(**base)


def _ledger(fuentes, decisiones, render_assets=(), **extra) -> ProvenanceLedger:
    base = dict(
        run_id=uuid.uuid4(),
        sources=list(fuentes),
        decisions=list(decisiones),
        render_assets=list(render_assets),
    )
    base.update(extra)
    return ProvenanceLedger(**base)


def _decision(fuente: SourceAsset, **extra) -> LicenseDecision:
    base = dict(
        run_id=uuid.uuid4(),
        asset_id=fuente.asset_id,
        decision=ClaseFuente.render_permitido,
        basis=BaseLicencia.propia,
        evidence_ids=[e.evidence_id for e in fuente.evidence],
        reason="decisión construida por el test",
        policy_version=VERSION_POLITICA,
    )
    base.update(extra)
    return LicenseDecision(**base)


# ---------------------------------------------------------------------------
# SourceAsset (1-6)
# ---------------------------------------------------------------------------


def test_01_un_asset_valido_se_representa_completo():
    fuente = _fuente(
        local_path="external/clip.mp4",
        source_url="https://example.org/clip",
        sha256="a" * 64,
        evidence=[_ev(TipoEvidencia.pagina_licencia)],
        license_id="CC-BY-4.0",
        attribution_required=True,
        attribution_text="Autora Ejemplo (CC BY 4.0)",
        attribution_source="https://example.org/clip",
        commercial_use_allowed=True,
    )
    assert fuente.asset_id == "fuente-1"
    assert fuente.media_kind is TipoMedio.video
    assert fuente.evidencia("ev-1") is not None
    assert fuente.evidencia("no-existe") is None


def test_02_una_clasificacion_invalida_es_rechazada():
    """La clase es un enum: un estado que nadie definió no se puede escribir."""
    with pytest.raises(ValidationError):
        _decision(_propia(), decision="probablemente_si")


def test_03_una_base_invalida_es_rechazada():
    with pytest.raises(ValidationError):
        _decision(_propia(), basis="me_lo_dijo_un_amigo")


def test_04_un_hash_invalido_es_rechazado():
    with pytest.raises(ValidationError):
        _fuente(local_path="a/b.mp4", sha256="no-es-un-sha256")
    with pytest.raises(ValidationError):
        _fuente(local_path="a/b.mp4", sha256="A" * 64)  # mayúsculas
    with pytest.raises(ValidationError):
        _fuente(local_path="a/b.mp4", sha256="a" * 63)
    # Y una huella sin archivo al que corresponda no describe nada.
    with pytest.raises(ValidationError, match="exige local_path"):
        _fuente(sha256="a" * 64)


@pytest.mark.parametrize(
    "ruta", ["/tmp/clip.mp4", "C:\\clip.mp4", "\\\\servidor\\clip.mp4"]
)
def test_05_un_path_absoluto_es_rechazado(ruta):
    with pytest.raises(ValidationError):
        _fuente(local_path=ruta)


def test_06_una_fuente_declarada_que_no_existe_se_rechaza_en_la_entrada(
    runner_g4, tmp_path
):
    """Declarar un archivo que no está no es una fuente: es un error de entrada."""
    archivo = tmp_path / "fuentes.json"
    archivo.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "basis": "own",
                        "asset": {
                            "asset_id": "externa:fantasma",
                            "media_kind": "video",
                            "origin": "declarada pero ausente",
                            "local_path": "external/no-esta.mp4",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    runner_g4.parametros[transformation.CLAVE_DECLARACIONES] = str(archivo)

    with pytest.raises(EntradaInvalida, match="no existe en la corrida"):
        runner_g4.ejecutar(transformation.ETAPA_PROCEDENCIA, forzar=True)


# ---------------------------------------------------------------------------
# LicenseDecision (7-17)
# ---------------------------------------------------------------------------


def test_07_render_allowed_con_base_propia():
    veredicto = evaluar(_propia(), BaseLicencia.propia)
    assert veredicto.decision is ClaseFuente.render_permitido
    assert veredicto.evidence_ids == ("ev-1",)


def test_08_render_allowed_con_licencia():
    fuente = _fuente(
        evidence=[_ev(TipoEvidencia.pagina_licencia)],
        license_id="licencia-comercial-123",
        commercial_use_allowed=True,
    )
    assert evaluar(fuente, BaseLicencia.licenciada).decision is ClaseFuente.render_permitido


def test_08b_una_licencia_que_no_consta_si_cubre_el_uso_queda_pendiente():
    """``licensed`` no implica uso comercial, y no se supone que lo implique."""
    fuente = _fuente(evidence=[_ev(TipoEvidencia.pagina_licencia)], license_id="x-1")
    veredicto = evaluar(fuente, BaseLicencia.licenciada)
    assert veredicto.decision is ClaseFuente.revision_pendiente
    assert "no implica uso comercial" in veredicto.reason


def test_09_render_allowed_con_cc_by():
    fuente = _fuente(
        evidence=[_ev(TipoEvidencia.registro_licencia_cc)],
        license_id="CC-BY-4.0",
        attribution_required=True,
        attribution_text="Autora Ejemplo (CC BY 4.0)",
    )
    assert evaluar(fuente, BaseLicencia.cc_by).decision is ClaseFuente.render_permitido


def test_09b_creative_commons_sin_licencia_concreta_queda_pendiente():
    """La familia CC no dice nada: hay licencias CC que prohíben este uso."""
    fuente = _fuente(evidence=[_ev(TipoEvidencia.registro_licencia_cc)])
    veredicto = evaluar(fuente, BaseLicencia.cc_by)
    assert veredicto.decision is ClaseFuente.revision_pendiente
    assert "sin licencia concreta" in veredicto.reason


def test_10_render_allowed_con_dominio_publico():
    fuente = _fuente(evidence=[_ev(TipoEvidencia.registro_dominio_publico)])
    assert (
        evaluar(fuente, BaseLicencia.dominio_publico).decision
        is ClaseFuente.render_permitido
    )


def test_10b_una_afirmacion_del_usuario_no_es_un_registro_de_dominio_publico():
    """Decir «esto es de dominio público» no es evidencia de que lo sea."""
    fuente = _fuente(notes="el solicitante afirma que es de dominio público")
    assert (
        evaluar(fuente, BaseLicencia.dominio_publico).decision
        is ClaseFuente.revision_pendiente
    )


def test_11_render_allowed_con_permiso_y_evidencia():
    fuente = _fuente(
        evidence=[
            _ev(
                TipoEvidencia.documento_permiso,
                reference="correo-2026-02-11#id-9931",
                description="autorización escrita para este uso",
                issued_by="Autora Ejemplo",
            )
        ]
    )
    veredicto = evaluar(fuente, BaseLicencia.permiso)
    assert veredicto.decision is ClaseFuente.render_permitido
    assert veredicto.evidence_ids == ("ev-1",)


def test_12_base_desconocida_queda_pendiente_de_revision():
    veredicto = evaluar(_propia(), BaseLicencia.desconocida)
    assert veredicto.decision is ClaseFuente.revision_pendiente
    assert not veredicto.permite_render


def test_13_uso_legitimo_alegado_queda_pendiente_de_revision():
    """No hay evaluador de uso legítimo, y alegar no es concluir.

    Ni siquiera con evidencia: la evidencia no convierte una afirmación jurídica
    en una decisión que este sistema pueda tomar.
    """
    fuente = _fuente(evidence=[_ev(TipoEvidencia.pagina_licencia)])
    veredicto = evaluar(fuente, BaseLicencia.uso_legitimo_alegado)
    assert veredicto.decision is ClaseFuente.revision_pendiente
    assert "no evalúa uso legítimo" in veredicto.reason


def test_14_render_allowed_con_base_desconocida_es_rechazado():
    with pytest.raises(ValidationError, match="decisión humana"):
        _decision(_propia(), basis=BaseLicencia.desconocida)


def test_15_render_allowed_con_uso_legitimo_alegado_es_rechazado():
    with pytest.raises(ValidationError, match="decisión humana"):
        _decision(_propia(), basis=BaseLicencia.uso_legitimo_alegado)


def test_16_un_permiso_sin_evidencia_no_autoriza():
    """``user_claimed_permission=true`` no es una base de procedencia."""
    veredicto = evaluar(_fuente(notes="el solicitante dice tener permiso"), BaseLicencia.permiso)
    assert veredicto.decision is ClaseFuente.revision_pendiente
    assert "sin evidencia" in veredicto.reason

    # Y construir la decisión a mano, sin evidencia, tampoco cuela.
    with pytest.raises(ValidationError, match="no es una base de procedencia"):
        _decision(_fuente(), basis=BaseLicencia.permiso, evidence_ids=[])


def test_17_una_licencia_incompatible_con_el_uso_bloquea():
    fuente = _fuente(
        evidence=[_ev(TipoEvidencia.pagina_licencia)],
        license_id="CC-BY-NC-4.0",
        commercial_use_allowed=False,
    )
    veredicto = evaluar(fuente, BaseLicencia.licenciada)
    assert veredicto.decision is ClaseFuente.bloqueado
    assert not veredicto.permite_render


# ---------------------------------------------------------------------------
# Evidence (18-21)
# ---------------------------------------------------------------------------


def test_18_una_evidencia_valida_respalda_la_decision():
    fuente = _propia()
    decision = decidir(fuente, BaseLicencia.propia, run_id=uuid.uuid4())
    ledger = _ledger([fuente], [decision])

    assert decision.evidence_ids == ["ev-1"]
    assert ledger.fuente("fuente-1").evidencia("ev-1").kind is TipoEvidencia.registro_propiedad


def test_19_sin_evidencia_no_se_decide_a_favor():
    veredicto = evaluar(_fuente(), BaseLicencia.propia)
    assert veredicto.decision is ClaseFuente.revision_pendiente
    assert veredicto.evidence_ids == ()


def test_20_una_decision_no_puede_invocar_evidencia_que_la_fuente_no_registra():
    fuente = _propia()
    with pytest.raises(ValidationError, match="que la fuente no registra"):
        _ledger([fuente], [_decision(fuente, evidence_ids=["ev-inventada"])])


def test_21_evidencia_contradictoria_no_respalda_la_base():
    """Apuntar a una licencia no demuestra que exista un permiso escrito."""
    fuente = _fuente(evidence=[_ev(TipoEvidencia.pagina_licencia)])
    veredicto = evaluar(fuente, BaseLicencia.permiso)
    assert veredicto.decision is ClaseFuente.revision_pendiente
    assert "permission_document" in veredicto.reason


def test_21b_dos_evidencias_con_el_mismo_identificador_son_rechazadas():
    with pytest.raises(ValidationError, match="duplicada"):
        _fuente(
            evidence=[
                _ev(TipoEvidencia.pagina_licencia, "ev-1"),
                _ev(TipoEvidencia.documento_permiso, "ev-1"),
            ]
        )


def test_21c_una_evidencia_sin_clasificar_no_respalda_ninguna_base():
    """``other`` registra que hay algo; no dice qué demuestra."""
    fuente = _fuente(evidence=[_ev(TipoEvidencia.otra)])
    for base in (
        BaseLicencia.propia,
        BaseLicencia.licenciada,
        BaseLicencia.cc_by,
        BaseLicencia.dominio_publico,
        BaseLicencia.permiso,
    ):
        assert evaluar(fuente, base).decision is ClaseFuente.revision_pendiente, base


# ---------------------------------------------------------------------------
# Attribution (22-24)
# ---------------------------------------------------------------------------


def test_22_una_atribucion_exigida_se_registra_con_su_fuente():
    fuente = _fuente(
        evidence=[_ev(TipoEvidencia.registro_licencia_cc)],
        license_id="CC-BY-4.0",
        attribution_required=True,
        attribution_text="Autora Ejemplo (CC BY 4.0)",
        attribution_source="https://example.org/clip",
    )
    ledger = _ledger([fuente], [decidir(fuente, BaseLicencia.cc_by, run_id=uuid.uuid4())])

    registrada = ledger.fuente("fuente-1")
    assert registrada.attribution_required is True
    assert registrada.attribution_text == "Autora Ejemplo (CC BY 4.0)"
    assert registrada.attribution_source == "https://example.org/clip"


def test_23_con_la_atribucion_presente_la_politica_autoriza():
    fuente = _fuente(
        evidence=[_ev(TipoEvidencia.registro_licencia_cc)],
        license_id="CC-BY-4.0",
        attribution_required=True,
        attribution_text="Autora Ejemplo (CC BY 4.0)",
    )
    assert evaluar(fuente, BaseLicencia.cc_by).decision is ClaseFuente.render_permitido


def test_24_una_atribucion_exigida_sin_texto_queda_pendiente_y_no_se_puede_autorizar():
    """Política: ``needs_review``, y el ledger se niega a autorizarla igualmente.

    El texto no se inventa. Que el estado sea representable es deliberado —es el
    de quien sabe que hace falta crédito y aún no lo ha escrito—, y lo que no se
    puede es renderizar con él.
    """
    fuente = _fuente(
        evidence=[_ev(TipoEvidencia.registro_licencia_cc)],
        license_id="CC-BY-4.0",
        attribution_required=True,
    )
    veredicto = evaluar(fuente, BaseLicencia.cc_by)
    assert veredicto.decision is ClaseFuente.revision_pendiente
    assert "no consta el texto" in veredicto.reason

    with pytest.raises(ValidationError, match="no hay texto con el que atribuir"):
        _ledger([fuente], [_decision(fuente, basis=BaseLicencia.cc_by)])


# ---------------------------------------------------------------------------
# Ledger (25-30)
# ---------------------------------------------------------------------------


def test_25_el_ledger_registra_la_fuente_y_se_puede_consultar():
    fuente = _propia(local_path="external/clip.mp4")
    ledger = _ledger([fuente], [_decision(fuente)], ["external/clip.mp4"])

    assert ledger.fuente("fuente-1") == fuente
    assert ledger.fuente_de("external/clip.mp4") == fuente
    assert ledger.fuente("no-existe") is None


def test_26_el_ledger_registra_la_decision_y_la_enlaza_con_su_fuente():
    fuente = _propia(local_path="external/clip.mp4")
    ledger = _ledger([fuente], [_decision(fuente)], ["external/clip.mp4"])

    decision = ledger.decision("fuente-1")
    assert decision.decision is ClaseFuente.render_permitido
    assert decision.basis is BaseLicencia.propia
    assert ledger.clase("external/clip.mp4") is ClaseFuente.render_permitido
    assert ledger.por_clase(ClaseFuente.render_permitido) == [fuente]
    assert ledger.rutas_por_clase(ClaseFuente.render_permitido) == ["external/clip.mp4"]


def test_27_una_fuente_sin_decision_no_es_utilizable():
    """Sin decisión no hay clase, y sin clase no se entra al render."""
    fuente = _propia(local_path="external/clip.mp4")
    ledger = _ledger([fuente], [])

    assert ledger.sin_decidir() == ["fuente-1"]
    assert ledger.clase("external/clip.mp4") is None
    assert not ledger.permite_render("external/clip.mp4")
    with pytest.raises(ValidationError, match="sin ninguna decisión de licencia"):
        _ledger([fuente], [], ["external/clip.mp4"])


def test_28_una_decision_sobre_una_fuente_que_no_existe_es_rechazada():
    with pytest.raises(ValidationError, match="no corresponde a ninguna fuente"):
        _ledger([], [_decision(_propia())])


def test_28b_dos_decisiones_sobre_la_misma_fuente_son_rechazadas():
    fuente = _propia()
    with pytest.raises(ValidationError, match="dos decisiones"):
        _ledger(
            [fuente],
            [
                _decision(fuente),
                _decision(fuente, decision=ClaseFuente.bloqueado, evidence_ids=[]),
            ],
        )


def test_29_un_hash_que_no_corresponde_al_archivo_se_detecta(runner_g4, workspace_voz):
    """Política de integridad: se rechaza, no se degrada a ``needs_review``.

    La decisión se tomó sobre un contenido concreto. Si el archivo cambió, la
    decisión ya no habla de lo que hay en disco, y dejarla como «pendiente de
    revisión» la haría parecer aplicable.
    """
    runner_g4.ejecutar(transformation.ETAPA_PROCEDENCIA)
    ledger = workspace_voz.leer_artefacto(
        transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger
    )
    ruta = ledger.render_assets[0]
    original = workspace_voz.ruta(ruta).read_bytes()
    workspace_voz.ruta(ruta).write_bytes(original + b"contenido sustituido")

    with pytest.raises(ArtefactoCorrupto, match="cambió"):
        transformation.validar_procedencia(ledger, workspace_voz)

    with pytest.raises(ProcedenciaInvalida, match="cambió desde que se autorizó"):
        render.verificar_procedencia(
            workspace_voz, ledger, [ruta], etapa="render_job"
        )


def test_30_cada_decision_registra_la_version_de_politica_que_la_produjo():
    fuente = _propia()
    decision = decidir(fuente, BaseLicencia.propia, run_id=uuid.uuid4())
    ledger = _ledger([fuente], [decision])

    assert decision.policy_version == VERSION_POLITICA == "provenance_policy_v1"
    assert ledger.versiones_de_politica() == {VERSION_POLITICA}
    assert decision.decided_at is not None
    assert decision.decided_by == "provenance_policy"


def test_30b_una_referencia_se_declara_y_no_la_decide_la_politica():
    """``reference_only`` es una declaración sobre el uso, no un veredicto."""
    fuente = _fuente(local_path="reference/fuente.txt")
    decision = declarar_referencia(fuente, run_id=uuid.uuid4())

    assert decision.decision is ClaseFuente.solo_referencia
    assert decision.basis is BaseLicencia.desconocida
    assert decision.decided_by == "declaration"
    assert decision.policy_version == VERSION_POLITICA
    ledger = _ledger([fuente], [decision])
    assert not ledger.permite_render("reference/fuente.txt")


def test_30c_el_ledger_referencia_la_fuente_en_vez_de_copiarla():
    """La decisión apunta por identificador; no duplica el contenido de la fuente."""
    fuente = _propia(local_path="external/clip.mp4", sha256="b" * 64)
    decision = _decision(fuente)
    datos = json.loads(_ledger([fuente], [decision], ["external/clip.mp4"]).model_dump_json())

    assert set(datos["decisions"][0]) == {
        "schema_version", "run_id", "created_at", "asset_id", "decision", "basis",
        "evidence_ids", "reason", "policy_version", "decided_at", "decided_by",
    }
    assert "local_path" not in datos["decisions"][0]
    assert "sha256" not in datos["decisions"][0]


# ---------------------------------------------------------------------------
# Integración con el render (31-36)
#
# Lo que hay que demostrar no es que el job falle: es que MPT no se ejecuta. El
# motor falso escribe su log siempre que arranca, en éxito y en fallo, así que la
# ausencia de runs/<run>/render/ es la prueba.
# ---------------------------------------------------------------------------


def _motor_no_se_ejecuto(ws) -> None:
    assert not ws.ruta(RUTA_LOG_MOTOR).exists(), "el motor se ejecutó pese al bloqueo"
    assert not (ws.dir / "render").exists(), "el motor dejó su directorio"
    assert not ws.existe(ARTEFACTO_JOB)
    assert not ws.existe(ARTEFACTO_RESULTADO)


def test_31_con_todo_autorizado_el_motor_se_ejecuta(runner_render, workspace_e2e):
    from app.pipeline.stages import ETAPA_RENDER

    runner_render.ejecutar(render.ETAPA_JOB)
    resultado = runner_render.ejecutar(ETAPA_RENDER).artefacto

    assert workspace_e2e.ruta(RUTA_LOG_MOTOR).is_file(), "el motor no se ejecutó"
    assert resultado.output_path
    assert workspace_e2e.ruta(resultado.output_path).is_file()


@pytest.mark.parametrize(
    "clase",
    [ClaseFuente.solo_referencia, ClaseFuente.revision_pendiente, ClaseFuente.bloqueado],
    ids=["32_reference_only", "33_needs_review", "34_blocked"],
)
def test_32_33_34_una_clase_que_no_autoriza_impide_el_render(
    runner_render, workspace_e2e, clase
):
    material = workspace_e2e.leer_artefacto(
        render.ARTEFACTO_MATERIAL, RenderResult
    ).output_path
    nuevo = reclasificar(
        _leer_ledger(workspace_e2e), material, clase, basis=BaseLicencia.desconocida
    )
    _escribir_ledger(runner_render, nuevo)

    assert not workspace_e2e.ruta(RUTA_LOG_MOTOR).exists(), "el motor ya había corrido"
    with pytest.raises(ProcedenciaInvalida, match=clase.value):
        runner_render.ejecutar(render.ETAPA_JOB, forzar=True)
    _motor_no_se_ejecuto(workspace_e2e)


def test_35_un_asset_no_declarado_impide_el_render(runner_render, workspace_e2e):
    material = workspace_e2e.leer_artefacto(
        render.ARTEFACTO_MATERIAL, RenderResult
    ).output_path
    _escribir_ledger(runner_render, sin_fuente(_leer_ledger(workspace_e2e), material))

    with pytest.raises(ProcedenciaInvalida, match="sin procedencia declarada"):
        runner_render.ejecutar(render.ETAPA_JOB, forzar=True)
    _motor_no_se_ejecuto(workspace_e2e)


def test_35b_un_asset_registrado_pero_sin_decidir_impide_el_render(
    runner_render, workspace_e2e
):
    material = workspace_e2e.leer_artefacto(
        render.ARTEFACTO_MATERIAL, RenderResult
    ).output_path
    _escribir_ledger(runner_render, sin_decision(_leer_ledger(workspace_e2e), material))

    with pytest.raises(ProcedenciaInvalida, match="sin ninguna decisión de licencia"):
        runner_render.ejecutar(render.ETAPA_JOB, forzar=True)
    _motor_no_se_ejecuto(workspace_e2e)


def test_36_una_decision_inconsistente_impide_el_render(runner_render, workspace_e2e):
    """Un ledger con una decisión contradictoria no se puede ni leer.

    Se edita el JSON a mano, que es como llegaría una incoherencia así: nadie
    puede construirla en memoria porque el contrato la rechaza. Al releerlo, la
    etapa no obtiene ledger, y sin ledger no hay autorización que invocar.
    """
    ruta = workspace_e2e.ruta_artefacto(transformation.ARTEFACTO_PROCEDENCIA)
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    datos["decisions"][0]["basis"] = "unknown"  # sigue diciendo render_allowed
    ruta.write_text(json.dumps(datos), encoding="utf-8")
    runner_render.producidos.pop(transformation.ARTEFACTO_PROCEDENCIA, None)

    with pytest.raises(ArtefactoCorrupto):
        runner_render.ejecutar(render.ETAPA_JOB, forzar=True)
    _motor_no_se_ejecuto(workspace_e2e)


def test_36b_un_hash_que_cambio_impide_el_render_sin_llegar_al_motor(
    runner_render, workspace_e2e
):
    material = workspace_e2e.leer_artefacto(
        render.ARTEFACTO_MATERIAL, RenderResult
    ).output_path
    archivo = workspace_e2e.ruta(material)
    archivo.write_bytes(archivo.read_bytes() + b"\x00sustituido")

    with pytest.raises(ProcedenciaInvalida, match="cambió desde que se autorizó"):
        runner_render.ejecutar(render.ETAPA_JOB, forzar=True)
    _motor_no_se_ejecuto(workspace_e2e)


def test_36c_conocer_la_url_de_un_short_ajeno_no_lo_autoriza():
    """No existe ninguna regla «URL de YouTube → render_allowed»."""
    fuente = _fuente(
        asset_id="externo:short-viral",
        source_url="https://www.youtube.com/shorts/ejemplo",
        origin="encontrado online",
    )
    for base in list(BaseLicencia):
        veredicto = evaluar(fuente, base)
        assert veredicto.decision is not ClaseFuente.render_permitido, base


# ---------------------------------------------------------------------------
# Idempotencia (37-41)
# ---------------------------------------------------------------------------


def test_37_una_segunda_corrida_no_duplica_el_ledger(runner_g4, workspace_voz):
    primero = runner_g4.ejecutar(transformation.ETAPA_PROCEDENCIA)
    segundo = runner_g4.ejecutar(transformation.ETAPA_PROCEDENCIA)

    assert not primero.omitida
    assert segundo.omitida, "el ledger válido debería reutilizarse"

    ledger = workspace_voz.leer_artefacto(
        transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger
    )
    ids = [f.asset_id for f in ledger.sources]
    assert len(ids) == len(set(ids)), ids
    assert len(ledger.decisions) == len(ledger.sources)


def test_38_una_segunda_corrida_no_vuelve_a_renderizar(runner_render, workspace_e2e):
    from app.pipeline.stages import ETAPA_RENDER

    runner_render.ejecutar(render.ETAPA_JOB)
    runner_render.ejecutar(ETAPA_RENDER)
    huella = workspace_e2e.ruta(RUTA_LOG_MOTOR).read_bytes()

    assert runner_render.ejecutar(render.ETAPA_JOB).omitida
    assert runner_render.ejecutar(ETAPA_RENDER).omitida
    assert workspace_e2e.ruta(RUTA_LOG_MOTOR).read_bytes() == huella


def test_39_un_cambio_de_hash_invalida_el_ledger(runner_g4, workspace_voz):
    runner_g4.ejecutar(transformation.ETAPA_PROCEDENCIA)
    ledger = workspace_voz.leer_artefacto(
        transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger
    )
    ruta = ledger.render_assets[0]
    archivo = workspace_voz.ruta(ruta)
    archivo.write_bytes(archivo.read_bytes() + b"otro contenido")

    assert not runner_g4.ejecutar(transformation.ETAPA_PROCEDENCIA).omitida

    # Y el ledger nuevo describe el archivo nuevo.
    nuevo = workspace_voz.leer_artefacto(
        transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger
    )
    assert nuevo.fuente_de(ruta).sha256 != ledger.fuente_de(ruta).sha256


def test_40_un_cambio_de_decision_invalida_el_job(runner_render, workspace_e2e):
    """Perder la autorización invalida un job ya construido, no solo uno nuevo."""
    runner_render.ejecutar(render.ETAPA_JOB)
    assert runner_render.ejecutar(render.ETAPA_JOB).omitida

    material = workspace_e2e.leer_artefacto(
        render.ARTEFACTO_MATERIAL, RenderResult
    ).output_path
    _escribir_ledger(
        runner_render,
        reclasificar(
            _leer_ledger(workspace_e2e),
            material,
            ClaseFuente.bloqueado,
            basis=BaseLicencia.desconocida,
        ),
    )

    with pytest.raises(ProcedenciaInvalida, match="blocked"):
        runner_render.ejecutar(render.ETAPA_JOB)


def test_41_un_cambio_de_version_de_politica_invalida_el_ledger(
    runner_g4, workspace_voz, monkeypatch
):
    """Una política nueva no puede dar por buenas las decisiones de la anterior."""
    runner_g4.ejecutar(transformation.ETAPA_PROCEDENCIA)
    assert runner_g4.ejecutar(transformation.ETAPA_PROCEDENCIA).omitida

    monkeypatch.setattr(transformation, "VERSION_POLITICA", "provenance_policy_v2")
    resultado = runner_g4.ejecutar(transformation.ETAPA_PROCEDENCIA)
    assert not resultado.omitida, "la política cambió y el ledger se reutilizó"


def test_41b_el_ledger_detecta_decisiones_de_otra_politica():
    fuente = _propia()
    ledger = _ledger(
        [fuente], [_decision(fuente, policy_version="provenance_policy_v0")]
    )
    assert ledger.versiones_de_politica() == {"provenance_policy_v0"}
    assert ledger.versiones_de_politica() != {VERSION_POLITICA}


# ---------------------------------------------------------------------------
# Declaración de fuentes externas
# ---------------------------------------------------------------------------


def test_una_declaracion_no_puede_fijar_los_campos_de_la_corrida():
    """El archivo de entrada no decide de qué corrida es una fuente."""
    declaracion = DeclaracionFuente(
        basis=BaseLicencia.propia,
        asset={
            "run_id": str(uuid.uuid4()),
            "asset_id": "externa:1",
            "media_kind": "video",
            "origin": "x",
        },
    )
    with pytest.raises(ValueError, match="los pone la corrida"):
        declaracion.fuente(uuid.uuid4())


def test_una_declaracion_con_un_campo_inventado_es_rechazada():
    declaracion = DeclaracionFuente(
        basis=BaseLicencia.propia,
        asset={
            "asset_id": "externa:1",
            "media_kind": "video",
            "origin": "x",
            "allowed": True,
        },
    )
    with pytest.raises(ValidationError):
        declaracion.fuente(uuid.uuid4())


def test_el_archivo_de_declaraciones_se_lee_y_la_politica_decide(tmp_path):
    archivo = tmp_path / "fuentes.json"
    archivo.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "basis": "cc_by",
                        "asset": {
                            "asset_id": "externa:cc",
                            "media_kind": "video",
                            "origin": "archivo del autor",
                            "license_id": "CC-BY-4.0",
                            "attribution_required": True,
                            "attribution_text": "Autora Ejemplo (CC BY 4.0)",
                            "evidence": [
                                {
                                    "evidence_id": "ev-cc",
                                    "kind": "cc_license_record",
                                    "reference": "https://creativecommons.org/licenses/by/4.0/",
                                    "description": "licencia declarada",
                                }
                            ],
                        },
                    },
                    {
                        "basis": "fair_use_claim",
                        "asset": {
                            "asset_id": "externa:alegada",
                            "media_kind": "video",
                            "origin": "fragmento ajeno",
                        },
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    declaraciones = ArchivoDeclaraciones.leer(archivo)
    run_id = uuid.uuid4()
    clases = {
        d.asset["asset_id"]: decidir(d.fuente(run_id), d.basis, run_id=run_id).decision
        for d in declaraciones.sources
    }
    assert clases == {
        "externa:cc": ClaseFuente.render_permitido,
        "externa:alegada": ClaseFuente.revision_pendiente,
    }


def test_una_fuente_externa_autorizada_no_entra_sola_en_el_render(
    runner_g4, workspace_voz, tmp_path
):
    """Autorizada no es lo mismo que utilizada.

    La política puede permitir un recurso ajeno y el render seguir sin usarlo:
    ``render_assets`` dice qué consume esta corrida, no qué sería elegible.
    """
    destino = workspace_voz.ruta("external/clip.txt")
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("clip cedido", encoding="utf-8")

    archivo = tmp_path / "fuentes.json"
    archivo.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "basis": "public_domain",
                        "asset": {
                            "asset_id": "externa:pd",
                            "media_kind": "video",
                            "origin": "archivo público",
                            "local_path": "external/clip.txt",
                            "evidence": [
                                {
                                    "evidence_id": "ev-pd",
                                    "kind": "public_domain_record",
                                    "reference": "registro-publico#1931",
                                    "description": "registro de dominio público",
                                }
                            ],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    runner_g4.parametros[transformation.CLAVE_DECLARACIONES] = str(archivo)
    ledger = runner_g4.ejecutar(transformation.ETAPA_PROCEDENCIA, forzar=True).artefacto

    assert ledger.clase("external/clip.txt") is ClaseFuente.render_permitido
    assert "external/clip.txt" not in ledger.render_assets
    # Y se le calcula la huella, que es lo que ancla la decisión al contenido.
    assert ledger.fuente("externa:pd").sha256 is not None


def test_el_ledger_no_guarda_secretos(runner_g4, workspace_voz, monkeypatch):
    """Una evidencia registra dónde mirar, nunca la credencial para mirarlo."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-no-debe-salir-de-aqui")
    runner_g4.ejecutar(transformation.ETAPA_PROCEDENCIA, forzar=True)

    contenido = workspace_voz.ruta_artefacto(
        transformation.ARTEFACTO_PROCEDENCIA
    ).read_text(encoding="utf-8")
    assert "sk-no-debe-salir-de-aqui" not in contenido
    for prohibido in ("api_key", "token", "bearer", "cookie", "password", "secret"):
        assert prohibido not in contenido.lower(), prohibido


# ---------------------------------------------------------------------------
# Ayudas sobre el ledger persistido
# ---------------------------------------------------------------------------


def _leer_ledger(ws) -> ProvenanceLedger:
    return ws.leer_artefacto(transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger)


def _escribir_ledger(runner: StageRunner, nuevo: ProvenanceLedger) -> None:
    runner.workspace.escribir_artefacto(transformation.ARTEFACTO_PROCEDENCIA, nuevo)
    runner.producidos.pop(transformation.ARTEFACTO_PROCEDENCIA, None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner_g4(workspace_voz, settings_voz) -> StageRunner:
    """Corrida con voz y overlay hechos, lista para la etapa de procedencia."""
    from app.adapters.tts import ProveedorTTSFalso
    from app.pipeline import voice

    manifest, _ = Manifest.cargar_o_crear(
        workspace_voz.dir, workspace_voz.run_id,
        config=settings_voz.publico(), versions={},
    )
    runner = StageRunner(
        workspace_voz, manifest, settings_voz,
        {voice.CLAVE_PROVEEDOR_TTS: ProveedorTTSFalso()},
    )
    for etapa in voice.construir_pipeline_voz():
        runner.ejecutar(etapa)
    runner.ejecutar(transformation.ETAPA_OVERLAY)
    return runner


@pytest.fixture
def runner_render(settings_e2e, workspace_e2e) -> StageRunner:
    """Corrida con todo lo anterior hecho y el render por delante."""
    return StageRunner(workspace_e2e, Manifest.cargar(workspace_e2e.dir), settings_e2e)
