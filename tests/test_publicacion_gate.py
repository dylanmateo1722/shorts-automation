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

import pytest

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
    RenderJob,
    RenderResult,
    SourceAsset,
    TipoEvidencia,
    TipoMedio,
)
from app.config.provenance import VERSION_POLITICA
from app.adapters.youtube.publisher import publicar
from app.pipeline.publicacion import evaluar_precondiciones

from conftest_g72 import TransporteFalso, metadata, token

#: La forma **real** del artefacto que produce la etapa de composición. Los dos
#: campos no son intercambiables y el fixture lo refleja:
#:
#:   output_path    el vídeo compuesto, con subtítulos y rótulo. Es el publicable.
#:   combined_path  el intermedio crudo del motor, solo referenciado.
#:
#: El fixture anterior ponía el vídeo en ``combined_path`` y dejaba
#: ``output_path`` vacío, así que reproducía la misma inversión que el gate
#: tenía y por eso no podía detectarla.
RUTA_VIDEO = "final/short.mp4"
RUTA_INTERMEDIO = "render/final.mp4"

#: Contenido distinto en cada archivo: si el gate cogiera el intermedio, su
#: huella no coincidiría y el test lo diría.
CONTENIDO_FINAL = b"MP4-final-compuesto-" * 64
CONTENIDO_INTERMEDIO = b"MP4-intermedio-del-motor-" * 64


def _escribir_video(directorio: Path, contenido: bytes = CONTENIDO_FINAL) -> str:
    """Escribe el compuesto **y** el intermedio, como hace una corrida real."""
    destino = directorio / RUTA_VIDEO
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(contenido)

    intermedio = directorio / RUTA_INTERMEDIO
    intermedio.parent.mkdir(parents=True, exist_ok=True)
    intermedio.write_bytes(CONTENIDO_INTERMEDIO)

    return hashlib.sha256(contenido).hexdigest()


def _render(run_id, sha: str | None, *, status=EstadoRender.exito) -> RenderResult:
    """Un ``RenderResult`` con la forma que escribe la etapa de composición."""
    return RenderResult(
        run_id=run_id,
        status=status,
        exit_code=0 if status is EstadoRender.exito else 1,
        output_path=RUTA_VIDEO,
        combined_path=RUTA_INTERMEDIO,
        # La huella registrada es la del compuesto, nunca la del intermedio.
        sha256=sha,
        renderer="ffmpeg-libass-burn-in",
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


#: Las **entradas** del render, que es lo que el ledger clasifica: de dónde
#: viene cada cosa que acaba dentro del vídeo. El MP4 final no está aquí porque
#: es el producto de esa cadena, no un eslabón.
ENTRADAS = {
    "narration": "voice/narration.mp3",
    "material": "render/material.mp4",
    "subtitles": "subs/subtitles.ass",
    "overlay": "overlay/overlay.ass",
}


def _escribir_entradas(directorio: Path) -> dict[str, str]:
    """Escribe los recursos de entrada y devuelve su huella por papel."""
    huellas = {}
    for papel, ruta in ENTRADAS.items():
        destino = directorio / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        contenido = f"contenido-de-{papel}".encode() * 32
        destino.write_bytes(contenido)
        huellas[papel] = hashlib.sha256(contenido).hexdigest()
    return huellas


def _job(run_id) -> RenderJob:
    """El job con la forma real: declara las entradas, no el producto."""
    return RenderJob(
        run_id=run_id,
        task_id=run_id,
        script="guion de prueba",
        audio_path=ENTRADAS["narration"],
        materials=[ENTRADAS["material"]],
        subtitle_path=ENTRADAS["subtitles"],
        overlay_path=ENTRADAS["overlay"],
        # El job también declara dónde debe quedar el compuesto. Es una salida
        # prevista, y por eso `assets_de_render` no la incluye.
        output_path=RUTA_VIDEO,
    )


def _ledger(
    run_id,
    *,
    clase: ClaseFuente = ClaseFuente.render_permitido,
    afectado: str = "material",
    huellas: dict[str, str] | None = None,
) -> ProvenanceLedger:
    """Ledger sobre las entradas del render.

    ``clase`` y ``afectado`` permiten degradar **un** recurso concreto para
    comprobar que basta con uno para bloquear la publicación.
    """
    huellas = huellas or {}
    fuentes = []
    decisiones = []
    permitidos = []

    for papel, ruta in ENTRADAS.items():
        propia = papel != afectado or clase is ClaseFuente.render_permitido
        decision_papel = ClaseFuente.render_permitido if propia else clase

        evidencia = Evidence(
            evidence_id=f"ev-{papel}",
            kind=TipoEvidencia.registro_propiedad,
            reference=f"artefacto que generó {papel}",
            description="recurso producido por el pipeline de esta corrida",
            issued_by="pipeline",
        )
        fuentes.append(
            SourceAsset(
                run_id=run_id,
                asset_id=papel,
                media_kind=TipoMedio.video if papel == "material" else TipoMedio.otro,
                origin="pipeline",
                local_path=ruta,
                sha256=huellas.get(papel),
                evidence=[evidencia],
            )
        )
        decisiones.append(
            LicenseDecision(
                run_id=run_id,
                asset_id=papel,
                decision=decision_papel,
                basis=BaseLicencia.propia,
                evidence_ids=(
                    [f"ev-{papel}"]
                    if decision_papel is ClaseFuente.render_permitido
                    else []
                ),
                reason="generado por el pipeline de esta corrida",
                decided_by="tests",
                policy_version=VERSION_POLITICA,
            )
        )
        if decision_papel is ClaseFuente.render_permitido:
            permitidos.append(ruta)

    return ProvenanceLedger(
        run_id=run_id,
        sources=fuentes,
        decisions=decisiones,
        render_assets=permitidos,
    )


def _evaluar_tal_cual(tmp_path: Path, run_id, sha, ledger, **sustituciones):
    """Evalúa sin regenerar las entradas.

    ``_evaluar`` las reescribe en cada llamada, que es cómodo para el camino
    feliz pero deshace cualquier alteración hecha a propósito. Los tests que
    sabotean un recurso necesitan evaluar el disco tal como lo dejaron.
    """
    args = dict(
        render_result=_render(run_id, sha),
        render_job=_job(run_id),
        qa_result=_qa(run_id),
        ledger=ledger,
        metadata=metadata(run_id),
        directorio_corrida=tmp_path,
    )
    args.update(sustituciones)
    return evaluar_precondiciones(**args)


def _evaluar(tmp_path: Path, **sustituciones):
    run_id = sustituciones.pop("run_id", uuid4())
    sha = sustituciones.pop("sha", None)
    huellas = _escribir_entradas(tmp_path)
    if sha is None:
        sha = _escribir_video(tmp_path)
    args = dict(
        render_result=_render(run_id, sha),
        render_job=_job(run_id),
        qa_result=_qa(run_id),
        ledger=_ledger(run_id, huellas=huellas),
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
    # El mensaje lo da la misma autoridad que guarda la puerta previa al motor.
    assert any("no puede entrar al render" in m for m in gate.motivos)
    assert any("reference_only" in m for m in gate.motivos)


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
    destino = tmp_path / RUTA_VIDEO
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(b"")
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


# ---------------------------------------------------------------------------
# El gate no puede mentir, y no lo protege un assert (corrección 2)
# ---------------------------------------------------------------------------


def test_un_gate_listo_sin_video_no_es_construible():
    from app.core.errors import EntradaInvalida
    from app.pipeline.publicacion import ResultadoGate

    with pytest.raises(EntradaInvalida, match="ruta del vídeo"):
        ResultadoGate(listo=True, video_sha256="a" * 64, total_bytes=10)


def test_un_gate_listo_sin_huella_no_es_construible(tmp_path):
    from app.core.errors import EntradaInvalida
    from app.pipeline.publicacion import ResultadoGate

    destino = tmp_path / "v.mp4"
    destino.write_bytes(b"x")
    with pytest.raises(EntradaInvalida, match="SHA-256"):
        ResultadoGate(listo=True, video_path=destino, total_bytes=1)


def test_un_gate_listo_con_tamano_cero_no_es_construible(tmp_path):
    from app.core.errors import EntradaInvalida
    from app.pipeline.publicacion import ResultadoGate

    destino = tmp_path / "v.mp4"
    destino.write_bytes(b"x")
    with pytest.raises(EntradaInvalida, match="bytes"):
        ResultadoGate(
            listo=True, video_path=destino, video_sha256="a" * 64, total_bytes=0
        )


def test_un_gate_no_listo_sin_motivos_no_es_construible():
    from app.core.errors import EntradaInvalida
    from app.pipeline.publicacion import ResultadoGate

    with pytest.raises(EntradaInvalida, match="por qué"):
        ResultadoGate(listo=False)


def test_un_gate_listo_con_motivos_no_es_construible(tmp_path):
    from app.core.errors import EntradaInvalida
    from app.pipeline.publicacion import ResultadoGate

    destino = tmp_path / "v.mp4"
    destino.write_bytes(b"x")
    with pytest.raises(EntradaInvalida, match="motivos de rechazo"):
        ResultadoGate(
            listo=True,
            motivos=["la QA falló"],
            video_path=destino,
            video_sha256="a" * 64,
            total_bytes=1,
        )


def test_el_publisher_rechaza_un_gate_malformado_sin_tocar_la_red():
    """Aunque alguien esquive el constructor, no se habla con YouTube."""
    from app.core.errors import EntradaInvalida, ErrorPermanente
    from app.pipeline.publicacion import ResultadoGate

    run_id = uuid4()
    valido = ResultadoGate(
        listo=True, video_path=Path("/tmp/no-existe.mp4"),
        video_sha256="a" * 64, total_bytes=10,
    )
    # `object.__setattr__` salta el validador: es la única forma de fabricar el
    # estado imposible, y sirve para comprobar la última puerta del publisher.
    object.__setattr__(valido, "video_path", None)

    transporte = TransporteFalso()
    with pytest.raises(EntradaInvalida) as capturado:
        publicar(
            run_id=run_id,
            metadata=metadata(run_id),
            gate=valido,
            token=token(),
            transporte=transporte,
        )
    assert isinstance(capturado.value, ErrorPermanente), "el error no es del dominio"
    assert transporte.llamadas == [], "se contactó con el proveedor"


def test_ningun_assert_protege_el_gate():
    """Los asserts desaparecen bajo ``python -O``; estas puertas no pueden."""
    import inspect

    from app.adapters.youtube import publisher
    from app.pipeline import publicacion

    for modulo in (publisher, publicacion):
        for linea in inspect.getsource(modulo).splitlines():
            despojada = linea.strip()
            assert not despojada.startswith("assert "), f"{modulo.__name__}: {linea}"


# ---------------------------------------------------------------------------
# Regresión: qué archivo se publica (bugfix previo a G7.3)
#
# El gate prefería ``combined_path``, que en el artefacto real es el intermedio
# crudo del motor: sin subtítulos, sin rótulo y con otra huella. Estos tests
# fijan la semántica sobre la forma real del artefacto de composición.
# ---------------------------------------------------------------------------


def test_el_gate_elige_output_path_no_combined_path(tmp_path):
    """Lo publicable es el compuesto, y el fixture los distingue por contenido."""
    run_id = uuid4()
    sha = _escribir_video(tmp_path)
    _, gate = _evaluar(tmp_path, run_id=run_id, sha=sha)

    assert gate.listo, gate.motivos
    assert gate.video_path is not None
    assert gate.video_path == tmp_path / RUTA_VIDEO
    assert gate.video_path.name == "short.mp4"
    assert "render" not in gate.video_path.parts, "se eligió el intermedio del motor"
    assert gate.video_path.read_bytes() == CONTENIDO_FINAL


def test_el_intermedio_del_motor_nunca_es_el_video_publicable(tmp_path):
    """Aunque exista en disco y sea válido, no es lo que se sube."""
    run_id = uuid4()
    sha = _escribir_video(tmp_path)
    _, gate = _evaluar(tmp_path, run_id=run_id, sha=sha)

    intermedio = tmp_path / RUTA_INTERMEDIO
    assert intermedio.is_file(), "el fixture debe tener los dos archivos"
    assert gate.video_path != intermedio
    assert gate.video_sha256 != hashlib.sha256(CONTENIDO_INTERMEDIO).hexdigest()


def test_el_sha256_se_valida_contra_el_compuesto(tmp_path):
    """La huella registrada es la del compuesto; el gate comprueba ese archivo."""
    run_id = uuid4()
    sha = _escribir_video(tmp_path)
    _, gate = _evaluar(tmp_path, run_id=run_id, sha=sha)

    assert gate.listo, gate.motivos
    assert gate.video_sha256 == sha == hashlib.sha256(CONTENIDO_FINAL).hexdigest()
    assert gate.total_bytes == len(CONTENIDO_FINAL)


def test_la_huella_del_intermedio_no_sirve_para_pasar_el_gate(tmp_path):
    """Si alguien registrara la huella del intermedio, el gate lo rechaza."""
    run_id = uuid4()
    _escribir_video(tmp_path)
    sha_intermedio = hashlib.sha256(CONTENIDO_INTERMEDIO).hexdigest()
    _, gate = _evaluar(tmp_path, run_id=run_id, sha=sha_intermedio)

    assert not gate.listo
    assert any("INTEGRITY_MISMATCH" in m for m in gate.motivos)


def test_sin_output_path_no_hay_nada_que_publicar(tmp_path):
    """El intermedio no es respaldo del compuesto: su ausencia bloquea."""
    run_id = uuid4()
    sha = _escribir_video(tmp_path)
    solo_intermedio = RenderResult(
        run_id=run_id,
        status=EstadoRender.exito,
        exit_code=0,
        output_path=None,
        combined_path=RUTA_INTERMEDIO,
        sha256=sha,
        renderer="ffmpeg-libass-burn-in",
    )
    _, gate = _evaluar(
        tmp_path, run_id=run_id, sha=sha, render_result=solo_intermedio
    )
    assert not gate.listo
    assert any("no declara la ruta" in m for m in gate.motivos)


def test_el_publisher_sube_el_compuesto(tmp_path):
    """De extremo a extremo: lo que viaja en los PUT es el archivo compuesto."""
    from conftest_g72 import completado, sesion_abierta, video_remoto

    run_id = uuid4()
    sha = _escribir_video(tmp_path)
    _, gate = _evaluar(tmp_path, run_id=run_id, sha=sha)
    assert gate.listo, gate.motivos

    transporte = TransporteFalso(
        guion=[sesion_abierta(), completado(), video_remoto()]
    )
    publicar(
        run_id=run_id,
        metadata=metadata(run_id),
        gate=gate,
        token=token(),
        transporte=transporte,
    )

    enviado = b"".join(
        ll.cuerpo or b""
        for ll in transporte.llamadas
        if ll.metodo == "PUT" and ll.cuerpo
    )
    assert enviado == CONTENIDO_FINAL
    assert CONTENIDO_INTERMEDIO not in enviado


def test_el_comportamiento_seguro_no_se_rompe(tmp_path):
    """Las demás puertas siguen cerrando: QA, procedencia e integridad."""
    from app.contracts.models import ClaseFuente

    run_id = uuid4()
    sha = _escribir_video(tmp_path)

    # QA reprobada
    _, g = _evaluar(tmp_path, run_id=run_id, sha=sha, qa_result=_qa(run_id, ok=False))
    assert not g.listo

    # Procedencia que no autoriza
    _, g = _evaluar(
        tmp_path, run_id=run_id, sha=sha,
        ledger=_ledger(run_id, clase=ClaseFuente.solo_referencia),
    )
    assert not g.listo

    # Huella que no cuadra
    _, g = _evaluar(tmp_path, run_id=run_id, sha="f" * 64)
    assert not g.listo
    assert any("INTEGRITY_MISMATCH" in m for m in g.motivos)


# ---------------------------------------------------------------------------
# Regresión del fallo real de G7.3
#
# La primera ejecución contra YouTube murió en la puerta con:
#
#   "la procedencia de 'final/short.mp4' no autoriza el render (sin decisión);
#    solo 'render_allowed' puede publicarse"
#
# La causa no era el ledger: era la pregunta. ``ProvenanceLedger`` clasifica las
# **entradas** del render, y el MP4 final es el **producto** de esa cadena. Un
# producto no lleva decisión de licencia propia, así que preguntar por él
# devuelve «sin decisión», que es la respuesta correcta a una pregunta
# equivocada.
# ---------------------------------------------------------------------------


def test_el_mp4_final_no_necesita_decision_propia(tmp_path):
    """El fallo exacto de G7.3, al revés: ahora la corrida real pasa."""
    run_id = uuid4()
    huellas = _escribir_entradas(tmp_path)
    sha = _escribir_video(tmp_path)
    ledger = _ledger(run_id, huellas=huellas)

    # Premisa del fallo: el ledger NO conoce el MP4 final, y así debe seguir.
    assert ledger.fuente_de(RUTA_VIDEO) is None
    assert ledger.clase(RUTA_VIDEO) is None
    assert RUTA_VIDEO not in ledger.render_assets
    assert not ledger.permite_render(RUTA_VIDEO)

    _, gate = _evaluar(tmp_path, run_id=run_id, sha=sha, ledger=ledger)

    assert gate.listo, gate.motivos
    assert not any("final/short.mp4" in m for m in gate.motivos)
    assert not any("sin decisión" in m for m in gate.motivos)


def test_el_ledger_clasifica_entradas_no_el_producto(tmp_path):
    """La separación, afirmada sobre el fixture con forma de corrida real."""
    run_id = uuid4()
    huellas = _escribir_entradas(tmp_path)
    ledger = _ledger(run_id, huellas=huellas)

    for ruta in ENTRADAS.values():
        assert ledger.permite_render(ruta), ruta
    assert not ledger.permite_render(RUTA_VIDEO)
    assert not ledger.permite_render(RUTA_INTERMEDIO)


def test_la_puerta_pregunta_por_los_assets_del_job(tmp_path):
    """Lo que se comprueba es ``RenderJob.assets_de_render``, no el producto."""
    run_id = uuid4()
    job = _job(run_id)
    assert set(job.assets_de_render) == set(ENTRADAS.values())
    assert RUTA_VIDEO not in job.assets_de_render, "el producto no es una entrada"
    assert RUTA_INTERMEDIO not in job.assets_de_render

    _, gate = _evaluar(tmp_path, run_id=run_id, render_job=job)
    assert gate.listo, gate.motivos


def test_sin_render_job_no_hay_nada_que_comprobar(tmp_path):
    """Omitirlo no puede ser una forma silenciosa de saltarse la procedencia."""
    _, gate = _evaluar(tmp_path, render_job=None)
    assert not gate.listo
    assert any("RenderJob" in m for m in gate.motivos)


# ---------------------------------------------------------------------------
# Negativas: las tres clases que no autorizan siguen bloqueando
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "clase",
    [
        ClaseFuente.solo_referencia,
        ClaseFuente.revision_pendiente,
        ClaseFuente.bloqueado,
    ],
)
@pytest.mark.parametrize("afectado", sorted(ENTRADAS))
def test_una_entrada_no_autorizada_bloquea_la_publicacion(tmp_path, clase, afectado):
    """Basta con que **una** entrada no esté autorizada, sea cual sea."""
    run_id = uuid4()
    huellas = _escribir_entradas(tmp_path)
    sha = _escribir_video(tmp_path)
    _, gate = _evaluar(
        tmp_path,
        run_id=run_id,
        sha=sha,
        ledger=_ledger(run_id, clase=clase, afectado=afectado, huellas=huellas),
    )
    assert not gate.listo, f"{clase.value} sobre {afectado} no bloqueó"
    assert any(clase.value in m for m in gate.motivos), gate.motivos
    assert any(ENTRADAS[afectado] in m for m in gate.motivos), gate.motivos


def test_una_entrada_sin_declarar_bloquea(tmp_path):
    """Un recurso que el ledger no conoce no pasa por ser 'de la casa'."""
    run_id = uuid4()
    huellas = _escribir_entradas(tmp_path)
    sha = _escribir_video(tmp_path)

    intruso = _job(run_id).model_copy(
        update={"materials": ["render/material.mp4", "render/no_declarado.mp4"]}
    )
    (tmp_path / "render/no_declarado.mp4").write_bytes(b"x" * 100)

    _, gate = _evaluar(
        tmp_path, run_id=run_id, sha=sha, render_job=intruso,
        ledger=_ledger(run_id, huellas=huellas),
    )
    assert not gate.listo
    assert any("sin procedencia declarada" in m for m in gate.motivos), gate.motivos


def test_una_entrada_que_cambio_desde_que_se_autorizo_bloquea(tmp_path):
    """La decisión se tomó sobre un contenido concreto; si cambió, no aplica."""
    run_id = uuid4()
    huellas = _escribir_entradas(tmp_path)
    sha = _escribir_video(tmp_path)

    # El archivo se sustituye después de registrarse su huella.
    (tmp_path / ENTRADAS["narration"]).write_bytes(b"otra-narracion" * 32)

    gate = _evaluar_tal_cual(
        tmp_path, run_id, sha, _ledger(run_id, huellas=huellas)
    )
    assert not gate.listo
    assert any("cambió desde que se autorizó" in m for m in gate.motivos), gate.motivos


def test_una_entrada_autorizada_que_no_existe_bloquea(tmp_path):
    """Autorizado no es lo mismo que presente."""
    run_id = uuid4()
    huellas = _escribir_entradas(tmp_path)
    sha = _escribir_video(tmp_path)
    (tmp_path / ENTRADAS["overlay"]).unlink()

    gate = _evaluar_tal_cual(
        tmp_path, run_id, sha, _ledger(run_id, huellas=huellas)
    )
    assert not gate.listo
    assert any("el archivo no existe" in m for m in gate.motivos), gate.motivos


# ---------------------------------------------------------------------------
# La integridad del producto sigue siendo contra output_path
# ---------------------------------------------------------------------------


def test_el_sha256_del_producto_se_sigue_verificando_contra_output_path(tmp_path):
    """La corrección de las entradas no relajó la del producto."""
    run_id = uuid4()
    huellas = _escribir_entradas(tmp_path)
    _escribir_video(tmp_path)

    # Entradas impecables, producto manipulado.
    _, gate = _evaluar(
        tmp_path, run_id=run_id, sha="f" * 64, ledger=_ledger(run_id, huellas=huellas)
    )
    assert not gate.listo
    assert any("INTEGRITY_MISMATCH" in m for m in gate.motivos), gate.motivos


def test_las_dos_integridades_son_independientes(tmp_path):
    """La del producto y la de cada entrada se comprueban por separado."""
    run_id = uuid4()
    huellas = _escribir_entradas(tmp_path)
    sha = _escribir_video(tmp_path)

    # Producto correcto, entrada alterada -> bloquea por la entrada.
    (tmp_path / ENTRADAS["material"]).write_bytes(b"material-sustituido" * 32)
    gate = _evaluar_tal_cual(
        tmp_path, run_id, sha, _ledger(run_id, huellas=huellas)
    )
    assert not gate.listo
    assert any("cambió desde que se autorizó" in m for m in gate.motivos)
    assert not any("INTEGRITY_MISMATCH" in m for m in gate.motivos), (
        "el producto está intacto y no debería figurar como alterado"
    )
