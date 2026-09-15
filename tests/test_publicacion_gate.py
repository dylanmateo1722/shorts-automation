"""La puerta previa: qué impide que YouTube se contacte siquiera.

Estos tests comprueban el requisito más fuerte del Gate en su parte defensiva:
cuando una precondición no se cumple, **no hay trato con el proveedor**. Por eso
varios de ellos afirman sobre el transporte que no se usó, y no solo sobre el
estado resultante.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from app.contracts.models import (
    BaseLicencia,
    ClaseFuente,
    ComprobacionQA,
    EstadoDivulgacionIA,
    EstadoPublicacion,
    EstadoQA,
    EstadoRender,
    Evidence,
    LicenseDecision,
    NivelQA,
    ProvenanceLedger,
    QAResult,
    RenderResult,
    SourceAsset,
    TipoEvidencia,
    TipoMedio,
)
from app.config.provenance import VERSION_POLITICA
from app.adapters.youtube.publisher import publicar
from app.pipeline.publicacion import evaluar_precondiciones

from conftest_g72 import TransporteFalso, metadata, token

RUTA_VIDEO = "final_video.mp4"


def _escribir_video(directorio: Path, contenido: bytes = b"MP4-de-prueba" * 100) -> str:
    destino = directorio / RUTA_VIDEO
    destino.write_bytes(contenido)
    return hashlib.sha256(contenido).hexdigest()


def _render(run_id, sha: str | None, *, status=EstadoRender.exito) -> RenderResult:
    return RenderResult(
        run_id=run_id,
        status=status,
        exit_code=0 if status is EstadoRender.exito else 1,
        combined_path=RUTA_VIDEO,
        sha256=sha,
        renderer="ffmpeg-composition",
    )


def _qa(run_id, *, ok: bool = True) -> QAResult:
    comprobacion = ComprobacionQA(
        name="resolucion", ok=ok, level=NivelQA.error, detail="1080x1920"
    )
    return QAResult(
        run_id=run_id,
        status=EstadoQA.aprobado if ok else EstadoQA.reprobado,
        technical_qa_ok=ok,
        checks=[comprobacion],
        transformation_artifact="transformation_set.json",
        provenance_artifact="provenance_ledger.json",
        ready_for_render=ok,
    )


def _ledger(run_id, *, clase: ClaseFuente = ClaseFuente.render_permitido) -> ProvenanceLedger:
    evidencia = Evidence(
        evidence_id="ev-1",
        kind=TipoEvidencia.registro_propiedad,
        reference="artefacto de la corrida",
        description="el pipeline generó este vídeo",
        issued_by="pipeline",
    )
    fuente = SourceAsset(
        run_id=run_id,
        asset_id="video-final",
        media_kind=TipoMedio.video,
        origin="pipeline",
        local_path=RUTA_VIDEO,
        evidence=[evidencia],
    )
    decision = LicenseDecision(
        run_id=run_id,
        asset_id="video-final",
        decision=clase,
        basis=BaseLicencia.propia,
        evidence_ids=["ev-1"] if clase is ClaseFuente.render_permitido else [],
        reason="generado por el pipeline",
        decided_by="tests",
        policy_version=VERSION_POLITICA,
    )
    return ProvenanceLedger(
        run_id=run_id,
        sources=[fuente],
        decisions=[decision],
        render_assets=[RUTA_VIDEO] if clase is ClaseFuente.render_permitido else [],
    )


def _evaluar(tmp_path: Path, **sustituciones):
    run_id = sustituciones.pop("run_id", uuid4())
    sha = sustituciones.pop("sha", None)
    if sha is None:
        sha = _escribir_video(tmp_path)
    args = dict(
        render_result=_render(run_id, sha),
        qa_result=_qa(run_id),
        ledger=_ledger(run_id),
        metadata=metadata(run_id),
        directorio_corrida=tmp_path,
    )
    args.update(sustituciones)
    return run_id, evaluar_precondiciones(**args)


# ---------------------------------------------------------------------------
# 1. RenderResult inválido -> NOT_READY
# ---------------------------------------------------------------------------


def test_sin_render_result_no_esta_listo(tmp_path):
    _, gate = _evaluar(tmp_path, render_result=None)
    assert not gate.listo
    assert any("RenderResult" in m for m in gate.motivos)


def test_render_fallido_no_esta_listo(tmp_path):
    run_id = uuid4()
    sha = _escribir_video(tmp_path)
    _, gate = _evaluar(
        tmp_path,
        run_id=run_id,
        sha=sha,
        render_result=_render(run_id, sha, status=EstadoRender.fallo),
    )
    assert not gate.listo
    assert any("no tuvo éxito" in m for m in gate.motivos)


# ---------------------------------------------------------------------------
# 2. QA failure -> no se contacta con YouTube
# ---------------------------------------------------------------------------


def test_qa_reprobada_no_esta_lista(tmp_path):
    run_id = uuid4()
    sha = _escribir_video(tmp_path)
    _, gate = _evaluar(tmp_path, run_id=run_id, sha=sha, qa_result=_qa(run_id, ok=False))
    assert not gate.listo
    assert any("QA técnica" in m for m in gate.motivos)


def test_qa_reprobada_no_produce_ninguna_llamada(tmp_path):
    """El requisito literal: con la QA en rojo, YouTube no se toca."""
    run_id = uuid4()
    sha = _escribir_video(tmp_path)
    _, gate = _evaluar(tmp_path, run_id=run_id, sha=sha, qa_result=_qa(run_id, ok=False))
    transporte = TransporteFalso()

    salida = publicar(
        run_id=run_id,
        metadata=metadata(run_id),
        gate=gate,
        token=token(),
        transporte=transporte,
    )

    assert transporte.llamadas == [], "se habló con YouTube pese a la QA fallida"
    assert salida.job.state is EstadoPublicacion.no_listo
    assert salida.result is None


# ---------------------------------------------------------------------------
# 3. RENDER_ALLOWED obligatorio
# ---------------------------------------------------------------------------


def test_recurso_de_referencia_no_puede_publicarse(tmp_path):
    run_id = uuid4()
    sha = _escribir_video(tmp_path)
    _, gate = _evaluar(
        tmp_path,
        run_id=run_id,
        sha=sha,
        ledger=_ledger(run_id, clase=ClaseFuente.solo_referencia),
    )
    assert not gate.listo
    assert any("no autoriza el render" in m for m in gate.motivos)


def test_sin_ledger_no_esta_listo(tmp_path):
    _, gate = _evaluar(tmp_path, ledger=None)
    assert not gate.listo
    assert any("ProvenanceLedger" in m for m in gate.motivos)


def test_recurso_de_referencia_no_produce_ninguna_llamada(tmp_path):
    run_id = uuid4()
    sha = _escribir_video(tmp_path)
    _, gate = _evaluar(
        tmp_path,
        run_id=run_id,
        sha=sha,
        ledger=_ledger(run_id, clase=ClaseFuente.solo_referencia),
    )
    transporte = TransporteFalso()
    publicar(
        run_id=run_id,
        metadata=metadata(run_id),
        gate=gate,
        token=token(),
        transporte=transporte,
    )
    assert transporte.llamadas == []


# ---------------------------------------------------------------------------
# 4. MP4 inexistente -> no hay subida
# ---------------------------------------------------------------------------


def test_mp4_inexistente_no_esta_listo(tmp_path):
    run_id = uuid4()
    # No se escribe el archivo: el artefacto declara una ruta que no existe.
    _, gate = _evaluar(tmp_path, run_id=run_id, sha="0" * 64)
    assert not gate.listo
    assert any("no existe en disco" in m for m in gate.motivos)


def test_mp4_inexistente_no_produce_ninguna_llamada(tmp_path):
    run_id = uuid4()
    _, gate = _evaluar(tmp_path, run_id=run_id, sha="0" * 64)
    transporte = TransporteFalso()
    publicar(
        run_id=run_id,
        metadata=metadata(run_id),
        gate=gate,
        token=token(),
        transporte=transporte,
    )
    assert transporte.llamadas == []


def test_mp4_vacio_no_esta_listo(tmp_path):
    run_id = uuid4()
    (tmp_path / RUTA_VIDEO).write_bytes(b"")
    _, gate = _evaluar(tmp_path, run_id=run_id, sha=hashlib.sha256(b"").hexdigest())
    assert not gate.listo
    assert any("vacío" in m for m in gate.motivos)


# ---------------------------------------------------------------------------
# 5. Integridad
# ---------------------------------------------------------------------------


def test_integrity_mismatch_bloquea_la_publicacion(tmp_path):
    """El archivo en disco no es el que se validó."""
    run_id = uuid4()
    _escribir_video(tmp_path)
    _, gate = _evaluar(tmp_path, run_id=run_id, sha="f" * 64)
    assert not gate.listo
    assert any("INTEGRITY_MISMATCH" in m for m in gate.motivos)


def test_sin_sha_registrado_no_hay_contra_que_comprobar(tmp_path):
    run_id = uuid4()
    _escribir_video(tmp_path)
    _, gate = _evaluar(
        tmp_path, run_id=run_id, sha=None, render_result=_render(run_id, None)
    )
    assert not gate.listo
    assert any("SHA-256" in m for m in gate.motivos)


# ---------------------------------------------------------------------------
# 6. Divulgación de IA
# ---------------------------------------------------------------------------


def test_disclosure_que_exige_verificacion_bloquea(tmp_path):
    from app.contracts.models import DivulgacionIA

    run_id = uuid4()
    sha = _escribir_video(tmp_path)
    meta = metadata(
        run_id,
        ai_disclosure=DivulgacionIA(
            status=EstadoDivulgacionIA.requiere_verificacion_actual,
            reason="la norma aplicable cambió y hay que mirarlo",
            decided_by="tests",
        ),
    )
    _, gate = _evaluar(tmp_path, run_id=run_id, sha=sha, metadata=meta)
    assert not gate.listo
    assert any("requires_current_verification" in m for m in gate.motivos)


def test_disclosure_bloqueante_no_produce_ninguna_llamada(tmp_path):
    from app.contracts.models import DivulgacionIA

    run_id = uuid4()
    sha = _escribir_video(tmp_path)
    meta = metadata(
        run_id,
        ai_disclosure=DivulgacionIA(
            status=EstadoDivulgacionIA.requiere_verificacion_actual,
            reason="pendiente de comprobar",
            decided_by="tests",
        ),
    )
    _, gate = _evaluar(tmp_path, run_id=run_id, sha=sha, metadata=meta)
    transporte = TransporteFalso()
    publicar(
        run_id=run_id, metadata=meta, gate=gate, token=token(), transporte=transporte
    )
    assert transporte.llamadas == []


# ---------------------------------------------------------------------------
# Camino feliz de la puerta
# ---------------------------------------------------------------------------


def test_todo_en_orden_deja_pasar(tmp_path):
    run_id = uuid4()
    sha = _escribir_video(tmp_path)
    _, gate = _evaluar(tmp_path, run_id=run_id, sha=sha)
    assert gate.listo, gate.motivos
    assert gate.video_path is not None and gate.video_path.is_file()
    assert gate.video_sha256 == sha
    assert gate.total_bytes > 0


def test_se_informan_todas_las_causas_no_solo_la_primera(tmp_path):
    """Arreglarlas de una en una obligaría a una corrida por cada."""
    run_id = uuid4()
    _, gate = _evaluar(
        tmp_path,
        run_id=run_id,
        sha="0" * 64,
        qa_result=_qa(run_id, ok=False),
        ledger=None,
    )
    assert not gate.listo
    assert len(gate.motivos) >= 3
