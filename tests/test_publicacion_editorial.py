"""Gate 7.4-B: publicación privada de contenido editorial real.

Lo que esta capa añade no es la subida —eso lo dejó Gate 7.2 y lo ejercitó Gate
7.3— sino **qué se publica y con qué respaldo**. Los tests de este archivo
existen para que ninguna de esas dos cosas pueda quedar implícita:

* La metadata se declara en un fichero y se valida antes de tocar la red. Una
  privacidad distinta de ``private`` o una declaración de medios sintéticos
  ausente hacen que la solicitud **no llegue a existir como objeto**.
* La aprobación editorial y legal la declara una persona en otro fichero, y vale
  para **esta** corrida, **este** vídeo y **este** material. Cambiar cualquiera
  de los tres la invalida.
* Nadie edita a mano el artefacto que produjo la QA.

Los dos casos que dan sentido al resto:

* ``test_una_solicitud_no_privada_no_abre_ninguna_sesion`` y
  ``test_una_aprobacion_que_no_aprueba_no_abre_ninguna_sesion``: no basta con que
  el objeto falle, hay que demostrar que el transporte **no se usó**.
* ``test_publicar_no_es_una_etapa_del_stage_runner``: la publicación no puede
  alcanzarse con un rerun, porque un rerun de una publicación es una segunda
  subida.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.adapters.youtube.upload import iniciar_sesion
from app.config.provenance import VERSION_POLITICA
from app.contracts.models import (
    MAX_BYTES_DESCRIPCION,
    MAX_CARACTERES_ETIQUETAS,
    MAX_CARACTERES_TITULO,
    AprobacionEditorial,
    BaseLicencia,
    ClaseFuente,
    ComprobacionQA,
    DivulgacionIA,
    EstadoDivulgacionIA,
    EstadoEvaluacion,
    EstadoPublicacion,
    EstadoQA,
    EstadoRender,
    EvaluacionEditorialLegal,
    Evidence,
    LicenseDecision,
    MetadataEditorial,
    NivelQA,
    Privacidad,
    ProvenanceLedger,
    PublishMetadata,
    QAResult,
    RenderJob,
    RenderResult,
    SourceAsset,
    TipoEvidencia,
    TipoMedio,
    longitud_etiquetas,
)
from app.core.errors import EntradaInvalida
from app.pipeline import publicacion_editorial as pe

from conftest_g72 import (
    SESSION_URL,
    ACCESS_TOKEN,
    TransporteFalso,
    VIDEO_ID,
    completado,
    sesion_abierta,
    token,
    video_remoto,
)

TITULO = "LOS SANTOS FILES — prueba editorial"
DESCRIPCION = "Microdocumental vertical de prueba. No se publica nada de verdad."

RUTA_VIDEO = "final/short.mp4"
RUTA_INTERMEDIO = "render/final.mp4"
CONTENIDO_FINAL = b"MP4-final-compuesto-" * 64
CONTENIDO_INTERMEDIO = b"MP4-intermedio-" * 64

#: Las entradas del render. El MP4 final no está aquí: es el producto.
ENTRADAS = {
    "narration": "voice/narration.mp3",
    "material": "material/aportado.mp4",
    "subtitles": "subs/subtitles.ass",
    "overlay": "overlay/overlay.ass",
}


# ---------------------------------------------------------------------------
# Andamiaje: una corrida en disco con la forma que espera la puerta
# ---------------------------------------------------------------------------


def _divulgacion() -> DivulgacionIA:
    return DivulgacionIA(
        status=EstadoDivulgacionIA.no_requerida,
        reason="narración sintética declarada aparte; no se afirma nada más",
        decided_by="tests",
    )


def _metadata_editorial(run_id, **extra) -> MetadataEditorial:
    campos = dict(
        run_id=run_id,
        title=TITULO,
        description=DESCRIPCION,
        made_for_kids=False,
        ai_disclosure=_divulgacion(),
        contains_synthetic_media=True,
    )
    campos.update(extra)
    return MetadataEditorial(**campos)


def _aprobacion(run_id, video_sha256, materials, **extra) -> AprobacionEditorial:
    campos = dict(
        run_id=run_id,
        status=EstadoEvaluacion.aprobado_por_humano,
        approved_by="dylan",
        reason="revisado a mano: material propio y narración declarada",
        video_sha256=video_sha256,
        materials=list(materials),
    )
    campos.update(extra)
    return AprobacionEditorial(**campos)


def _escribir_entradas(directorio: Path) -> dict[str, str]:
    huellas = {}
    for papel, ruta in ENTRADAS.items():
        destino = directorio / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        contenido = f"contenido-de-{papel}".encode() * 32
        destino.write_bytes(contenido)
        huellas[papel] = hashlib.sha256(contenido).hexdigest()
    return huellas


def _escribir_video(directorio: Path, contenido: bytes = CONTENIDO_FINAL) -> str:
    destino = directorio / RUTA_VIDEO
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(contenido)
    intermedio = directorio / RUTA_INTERMEDIO
    intermedio.parent.mkdir(parents=True, exist_ok=True)
    intermedio.write_bytes(CONTENIDO_INTERMEDIO)
    return hashlib.sha256(contenido).hexdigest()


def _render(run_id, sha) -> RenderResult:
    return RenderResult(
        run_id=run_id,
        status=EstadoRender.exito,
        exit_code=0,
        output_path=RUTA_VIDEO,
        combined_path=RUTA_INTERMEDIO,
        sha256=sha,
        renderer="ffmpeg-libass-burn-in",
    )


def _job(run_id) -> RenderJob:
    return RenderJob(
        run_id=run_id,
        task_id=run_id,
        script="guion de prueba",
        audio_path=ENTRADAS["narration"],
        materials=[ENTRADAS["material"]],
        subtitle_path=ENTRADAS["subtitles"],
        overlay_path=ENTRADAS["overlay"],
        output_path=RUTA_VIDEO,
    )


def _qa(run_id, *, editorial: EvaluacionEditorialLegal | None = None) -> QAResult:
    return QAResult(
        run_id=run_id,
        status=EstadoQA.aprobado,
        technical_qa_ok=True,
        checks=[
            ComprobacionQA(
                name="resolucion", ok=True, level=NivelQA.error, detail="1080x1920"
            )
        ],
        transformation_artifact="transformation_set.json",
        provenance_artifact="provenance_ledger.json",
        ready_for_render=True,
        **({"editorial_legal_assessment": editorial} if editorial else {}),
    )


def _ledger(run_id, huellas: dict[str, str]) -> ProvenanceLedger:
    fuentes, decisiones, permitidos = [], [], []
    for papel, ruta in ENTRADAS.items():
        evidencia = Evidence(
            evidence_id=f"ev-{papel}",
            kind=TipoEvidencia.registro_propiedad,
            reference=f"artefacto que generó {papel}",
            description="recurso declarado en esta corrida",
            issued_by="editorial",
        )
        fuentes.append(
            SourceAsset(
                run_id=run_id,
                asset_id=papel,
                media_kind=TipoMedio.video if papel == "material" else TipoMedio.otro,
                origin="aportado y declarado a mano",
                local_path=ruta,
                sha256=huellas.get(papel),
                evidence=[evidencia],
            )
        )
        decisiones.append(
            LicenseDecision(
                run_id=run_id,
                asset_id=papel,
                decision=ClaseFuente.render_permitido,
                basis=BaseLicencia.propia,
                evidence_ids=[f"ev-{papel}"],
                reason="material editorial decidido a mano",
                decided_by="tests",
                policy_version=VERSION_POLITICA,
            )
        )
        permitidos.append(ruta)
    return ProvenanceLedger(
        run_id=run_id, sources=fuentes, decisions=decisiones, render_assets=permitidos
    )


@pytest.fixture
def corrida(tmp_path) -> dict:
    """Una corrida en disco lista para publicar, con sus cuatro artefactos."""
    run_id = uuid4()
    directorio = tmp_path / str(run_id)
    directorio.mkdir(parents=True)

    huellas = _escribir_entradas(directorio)
    sha = _escribir_video(directorio)

    from app.core.workspace import Workspace

    ws = Workspace.en_directorio(directorio, run_id)
    ws.escribir_artefacto(pe.ARTEFACTO_RENDER_RESULT, _render(run_id, sha))
    ws.escribir_artefacto(pe.ARTEFACTO_RENDER_JOB, _job(run_id))
    ws.escribir_artefacto(pe.ARTEFACTO_QA, _qa(run_id))
    ws.escribir_artefacto(pe.ARTEFACTO_PROCEDENCIA, _ledger(run_id, huellas))

    return {
        "run_id": run_id,
        "dir": directorio,
        "sha": sha,
        "materials": [ENTRADAS["material"]],
        "ws": ws,
    }


def _declarar(corrida, *, metadata=None, aprobacion=None) -> tuple[Path, Path]:
    """Escribe los dos ficheros declarativos y devuelve sus rutas.

    Van **fuera** del directorio de la corrida a propósito: son documentos que
    escribe una persona, no artefactos que produzca el pipeline.
    """
    base = corrida["dir"].parent / "declaraciones"
    base.mkdir(exist_ok=True)
    meta = metadata if metadata is not None else _metadata_editorial(corrida["run_id"])
    apro = (
        aprobacion
        if aprobacion is not None
        else _aprobacion(corrida["run_id"], corrida["sha"], corrida["materials"])
    )
    ruta_meta = base / "metadata.json"
    ruta_apro = base / "aprobacion.json"
    ruta_meta.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    ruta_apro.write_text(apro.model_dump_json(indent=2), encoding="utf-8")
    return ruta_meta, ruta_apro


def _preparar(corrida, **kwargs):
    ruta_meta, ruta_apro = _declarar(corrida, **kwargs)
    return pe.preparar(
        run_id=corrida["run_id"],
        directorio_corrida=corrida["dir"],
        ruta_metadata=ruta_meta,
        ruta_aprobacion=ruta_apro,
    )


def _cuerpo_enviado(metadata: PublishMetadata) -> dict:
    """El cuerpo que el proveedor recibe de verdad, leído del transporte.

    Se abre una sesión contra el transporte falso en vez de llamar a la función
    que construye el JSON: lo que importa no es lo que devuelve un ayudante
    interno, sino lo que sale por el cable.
    """
    transporte = TransporteFalso(guion=[sesion_abierta()])
    iniciar_sesion(
        token(),
        metadata,
        run_id=metadata.run_id,
        attempt=1,
        total_bytes=len(CONTENIDO_FINAL),
        video_sha256="a" * 64,
        transporte=transporte,
    )
    return json.loads(transporte.llamadas[0].cuerpo.decode("utf-8"))


def _guion_feliz() -> TransporteFalso:
    """Abrir sesión, subir el único fragmento, y verificar el vídeo remoto."""
    return TransporteFalso(
        [
            sesion_abierta(),
            completado(),
            video_remoto(title=TITULO, description=DESCRIPCION, privacy="private"),
        ]
    )


# ---------------------------------------------------------------------------
# 1. D5 — los límites que la documentación de YouTube fija, y solo esos
# ---------------------------------------------------------------------------


def test_el_titulo_admite_exactamente_cien_caracteres():
    run_id = uuid4()
    assert MAX_CARACTERES_TITULO == 100
    _metadata_editorial(run_id, title="t" * MAX_CARACTERES_TITULO)
    with pytest.raises(ValidationError):
        _metadata_editorial(run_id, title="t" * (MAX_CARACTERES_TITULO + 1))


def test_la_descripcion_se_mide_en_bytes_y_no_en_caracteres():
    """El límite documentado son 5000 **bytes**, y ahí está la trampa.

    Una descripción de 2600 caracteres acentuados cabe de sobra en 5000
    caracteres y no cabe en 5000 bytes. Medirla en caracteres dejaría pasar una
    metadata que YouTube rechaza con un 400 a mitad de la subida.
    """
    run_id = uuid4()
    assert MAX_BYTES_DESCRIPCION == 5000

    justo = "a" * MAX_BYTES_DESCRIPCION
    _metadata_editorial(run_id, description=justo)
    with pytest.raises(ValidationError):
        _metadata_editorial(run_id, description="a" * (MAX_BYTES_DESCRIPCION + 1))

    # 2600 caracteres, 5200 bytes en UTF-8.
    multibyte = "é" * 2600
    assert len(multibyte) < MAX_BYTES_DESCRIPCION
    assert len(multibyte.encode("utf-8")) > MAX_BYTES_DESCRIPCION
    with pytest.raises(ValidationError):
        _metadata_editorial(run_id, description=multibyte)


def test_las_etiquetas_se_cuentan_como_documenta_la_api():
    """El ejemplo de la propia documentación: ``Foo-Baz`` 7, ``Foo Baz`` 9."""
    assert longitud_etiquetas(["Foo-Baz"]) == 7
    assert longitud_etiquetas(["Foo Baz"]) == 9
    # Las comas entre elementos cuentan: 7 + 1 + 9.
    assert longitud_etiquetas(["Foo-Baz", "Foo Baz"]) == 17
    assert longitud_etiquetas([]) == 0


def test_las_etiquetas_admiten_exactamente_quinientos_caracteres():
    run_id = uuid4()
    assert MAX_CARACTERES_ETIQUETAS == 500

    # Diez etiquetas sin espacios de 49 + 9 comas = 499. Una letra más, 500.
    justas = ["x" * 49] * 10
    assert longitud_etiquetas(justas) == 499
    _metadata_editorial(run_id, tags=justas)

    al_limite = ["x" * 50] + ["x" * 49] * 9
    assert longitud_etiquetas(al_limite) == 500
    _metadata_editorial(run_id, tags=al_limite)

    with pytest.raises(ValidationError):
        _metadata_editorial(run_id, tags=["x" * 51] + ["x" * 49] * 9)


def test_una_etiqueta_con_espacios_gasta_dos_caracteres_mas():
    """Dos listas de la misma longitud: una pasa y la otra no, por un espacio.

    La única diferencia entre las dos es que la última etiqueta lleva un espacio
    en medio. Mide los mismos 49 caracteres, pero YouTube la cuenta como si
    estuviera entre comillas, y esos dos caracteres la sacan del límite. Contar
    las etiquetas sumando longitudes dejaría pasar la segunda.
    """
    run_id = uuid4()
    base = ["x" * 49] * 9

    sin_espacio = base + ["x" * 49]
    assert longitud_etiquetas(sin_espacio) == 500 - 1
    _metadata_editorial(run_id, tags=sin_espacio)

    con_espacio = base + ["x" * 24 + " " + "x" * 24]
    assert len(con_espacio[-1]) == len(sin_espacio[-1]) == 49
    assert longitud_etiquetas(con_espacio) == 501
    with pytest.raises(ValidationError):
        _metadata_editorial(run_id, tags=con_espacio)


def test_no_se_inventa_una_lista_de_categorias():
    """Lo único comprobable en local es que no esté en blanco.

    Qué identificadores son válidos depende de la región y lo dice
    ``videoCategories.list``. Hardcodear una lista rechazaría categorías
    legítimas, así que un ``category_id`` cualquiera no vacío se acepta.
    """
    run_id = uuid4()
    _metadata_editorial(run_id, category_id=None)
    _metadata_editorial(run_id, category_id="22")
    _metadata_editorial(run_id, category_id="99999")
    with pytest.raises(ValidationError):
        _metadata_editorial(run_id, category_id="")
    with pytest.raises(ValidationError):
        _metadata_editorial(run_id, category_id="   ")


def test_no_se_impone_bcp_47_al_idioma():
    """La documentación no le fija formato a ``defaultLanguage``.

    Exigir BCP-47 sería imponer una regla nuestra como si fuera de YouTube. Lo
    único que no puede ser es estar en blanco.
    """
    run_id = uuid4()
    for valor in ("es", "es-CR", "es-419", "pt-BR"):
        assert _metadata_editorial(run_id, language=valor).language == valor
    with pytest.raises(ValidationError):
        _metadata_editorial(run_id, language="")


# ---------------------------------------------------------------------------
# 2. D7 — privacidad: solo private, y rechazado por contrato
# ---------------------------------------------------------------------------


def test_la_metadata_editorial_solo_admite_private():
    run_id = uuid4()
    assert _metadata_editorial(run_id).privacy_status is Privacidad.privado

    for prohibida in (Privacidad.publico, Privacidad.no_listado):
        with pytest.raises(ValidationError):
            _metadata_editorial(run_id, privacy_status=prohibida)
    # También por su valor crudo, que es como viene de un fichero JSON.
    for prohibida in ("public", "unlisted"):
        with pytest.raises(ValidationError):
            _metadata_editorial(run_id, privacy_status=prohibida)


def test_la_privacidad_de_la_base_sigue_siendo_representable():
    """``Privacidad`` y ``PublishMetadata`` no cambian: el enum describe lo que
    es representable, y otros flujos la construyen con otros valores. La
    restricción de Gate 7.4-B vive en ``MetadataEditorial``, no en la base."""
    base = PublishMetadata(
        run_id=uuid4(),
        title=TITULO,
        privacy_status=Privacidad.no_listado,
        made_for_kids=False,
        ai_disclosure=_divulgacion(),
    )
    assert base.privacy_status is Privacidad.no_listado


def test_una_solicitud_no_privada_no_abre_ninguna_sesion(corrida):
    """No basta con que el objeto falle: el transporte no se usa."""
    transporte = _guion_feliz()
    base = corrida["dir"].parent / "declaraciones"
    base.mkdir(exist_ok=True)
    # Se escribe el JSON a mano porque el objeto prohibido no se puede construir.
    crudo = json.loads(_metadata_editorial(corrida["run_id"]).model_dump_json())
    crudo["privacy_status"] = "public"
    ruta_meta = base / "metadata.json"
    ruta_meta.write_text(json.dumps(crudo), encoding="utf-8")
    ruta_apro = base / "aprobacion.json"
    ruta_apro.write_text(
        _aprobacion(
            corrida["run_id"], corrida["sha"], corrida["materials"]
        ).model_dump_json(),
        encoding="utf-8",
    )

    with pytest.raises(EntradaInvalida) as exc:
        pe.preparar(
            run_id=corrida["run_id"],
            directorio_corrida=corrida["dir"],
            ruta_metadata=ruta_meta,
            ruta_aprobacion=ruta_apro,
        )

    assert "contrato" in str(exc.value)
    assert transporte.llamadas == [], "se contactó con el proveedor"
    assert transporte.sesiones_iniciadas == 0


# ---------------------------------------------------------------------------
# 3. D4 — containsSyntheticMedia: declarado, nunca inferido
# ---------------------------------------------------------------------------


def test_la_metadata_editorial_exige_declarar_los_medios_sinteticos():
    run_id = uuid4()
    campos = dict(
        run_id=run_id,
        title=TITULO,
        made_for_kids=False,
        ai_disclosure=_divulgacion(),
    )
    with pytest.raises(ValidationError):
        MetadataEditorial(**campos)
    assert MetadataEditorial(**campos, contains_synthetic_media=False) is not None


def test_no_declarado_y_declarado_falso_no_son_lo_mismo():
    """``None`` no se envía; ``False`` se envía como declaración explícita."""
    run_id = uuid4()
    sin_declarar = PublishMetadata(
        run_id=run_id,
        title=TITULO,
        made_for_kids=False,
        ai_disclosure=_divulgacion(),
    )
    assert sin_declarar.contains_synthetic_media is None
    cuerpo = _cuerpo_enviado(sin_declarar)
    assert "containsSyntheticMedia" not in cuerpo["status"]

    declarado_falso = _metadata_editorial(run_id, contains_synthetic_media=False)
    cuerpo = _cuerpo_enviado(declarado_falso)
    assert cuerpo["status"]["containsSyntheticMedia"] is False


def test_el_cuerpo_enviado_es_exactamente_el_declarado():
    """El cuerpo completo de ``videos.insert``, campo por campo."""
    run_id = uuid4()
    metadata = _metadata_editorial(
        run_id,
        tags=["gta", "los santos"],
        category_id="22",
        language="es-CR",
        contains_synthetic_media=True,
    )
    assert _cuerpo_enviado(metadata) == {
        "snippet": {
            "title": TITULO,
            "description": DESCRIPCION,
            "defaultLanguage": "es-CR",
            "tags": ["gta", "los santos"],
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }


def test_los_medios_sinteticos_no_se_derivan_de_la_divulgacion_de_ia():
    """Las dos declaraciones son independientes y el contrato no las relaciona."""
    run_id = uuid4()
    for estado in (
        EstadoDivulgacionIA.requerida,
        EstadoDivulgacionIA.no_requerida,
    ):
        divulgacion = DivulgacionIA(
            status=estado, reason="decidido a mano", decided_by="tests"
        )
        for declarado in (True, False):
            metadata = _metadata_editorial(
                run_id,
                ai_disclosure=divulgacion,
                contains_synthetic_media=declarado,
            )
            cuerpo = _cuerpo_enviado(metadata)
            assert cuerpo["status"]["containsSyntheticMedia"] is declarado


def test_ni_made_for_kids_ni_la_divulgacion_tienen_valor_por_defecto():
    """Las tres decisiones humanas se declaran o la metadata no existe."""
    run_id = uuid4()
    for ausente in ("made_for_kids", "ai_disclosure", "contains_synthetic_media"):
        campos = dict(
            run_id=run_id,
            title=TITULO,
            made_for_kids=False,
            ai_disclosure=_divulgacion(),
            contains_synthetic_media=True,
        )
        campos.pop(ausente)
        with pytest.raises(ValidationError):
            MetadataEditorial(**campos)


# ---------------------------------------------------------------------------
# 4. D3 — la aprobación editorial y legal humana
# ---------------------------------------------------------------------------


def test_la_aprobacion_no_tiene_estado_por_defecto():
    with pytest.raises(ValidationError):
        AprobacionEditorial(
            run_id=uuid4(),
            approved_by="dylan",
            reason="revisado",
            video_sha256="a" * 64,
            materials=["material/aportado.mp4"],
        )


def test_solo_approved_by_human_aprueba():
    run_id, sha = uuid4(), "a" * 64
    rutas = ["material/aportado.mp4"]
    assert _aprobacion(run_id, sha, rutas).aprobado
    for otro in (
        EstadoEvaluacion.no_evaluado,
        EstadoEvaluacion.rechazado_por_humano,
    ):
        assert not _aprobacion(run_id, sha, rutas, status=otro).aprobado


def test_la_aprobacion_exige_autor_motivo_huella_y_material():
    run_id = uuid4()
    completa = dict(
        run_id=run_id,
        status=EstadoEvaluacion.aprobado_por_humano,
        approved_by="dylan",
        reason="revisado",
        video_sha256="a" * 64,
        materials=["material/aportado.mp4"],
    )
    for campo in ("approved_by", "reason", "video_sha256", "materials"):
        campos = dict(completa)
        campos.pop(campo)
        with pytest.raises(ValidationError):
            AprobacionEditorial(**campos)
    # Y ninguno puede quedar vacío.
    for campo, vacio in (
        ("approved_by", " "),
        ("reason", ""),
        ("materials", []),
    ):
        with pytest.raises(ValidationError):
            AprobacionEditorial(**{**completa, campo: vacio})


def test_sin_aprobacion_no_hay_publicacion(corrida):
    motivos = pe.motivos_editoriales(
        None,
        render_result=_render(corrida["run_id"], corrida["sha"]),
        render_job=_job(corrida["run_id"]),
        qa_result=_qa(corrida["run_id"]),
    )
    assert any("no hay aprobación editorial" in m for m in motivos)


def test_una_aprobacion_no_aprobada_bloquea(corrida):
    for estado in (
        EstadoEvaluacion.no_evaluado,
        EstadoEvaluacion.rechazado_por_humano,
    ):
        preparacion = _preparar(
            corrida,
            aprobacion=_aprobacion(
                corrida["run_id"],
                corrida["sha"],
                corrida["materials"],
                status=estado,
            ),
        )
        assert not preparacion.gate.listo
        assert any(
            "aprobación editorial está en" in m for m in preparacion.gate.motivos
        )


def test_la_aprobacion_se_ata_al_video_por_su_huella(corrida):
    """Aprobar un vídeo no aprueba el siguiente que se renderice."""
    preparacion = _preparar(
        corrida,
        aprobacion=_aprobacion(
            corrida["run_id"], "b" * 64, corrida["materials"]
        ),
    )
    assert not preparacion.gate.listo
    assert any("es de otro vídeo" in m for m in preparacion.gate.motivos)


def test_la_aprobacion_se_ata_al_material(corrida):
    """Cambiar el material invalida la aprobación aunque el resto sea igual."""
    preparacion = _preparar(
        corrida,
        aprobacion=_aprobacion(
            corrida["run_id"], corrida["sha"], ["material/otro.mp4"]
        ),
    )
    assert not preparacion.gate.listo
    assert any("es de otro material" in m for m in preparacion.gate.motivos)


def test_un_rechazo_registrado_en_la_qa_gana_sobre_la_aprobacion(corrida):
    """Una persona que rechazó no queda anulada por otro documento."""
    rechazo = EvaluacionEditorialLegal(
        status=EstadoEvaluacion.rechazado_por_humano, assessed_by="dylan"
    )
    corrida["ws"].escribir_artefacto(
        pe.ARTEFACTO_QA, _qa(corrida["run_id"], editorial=rechazo)
    )
    preparacion = _preparar(corrida)

    assert not preparacion.gate.listo
    assert any("rechazo editorial humano" in m for m in preparacion.gate.motivos)


def test_la_qa_no_se_modifica_al_preparar(corrida):
    """La decisión humana va aparte: el artefacto de la QA queda intacto."""
    ruta = corrida["dir"] / f"{pe.ARTEFACTO_QA}.json"
    antes = ruta.read_bytes()
    preparacion = _preparar(corrida)

    assert preparacion.gate.listo
    assert ruta.read_bytes() == antes
    qa = corrida["ws"].leer_artefacto(pe.ARTEFACTO_QA, QAResult)
    assert qa.editorial_legal_assessment.status is EstadoEvaluacion.no_evaluado


def test_la_aprobacion_declara_que_no_hubo_umbral_automatico():
    aprobacion = _aprobacion(uuid4(), "a" * 64, ["material/aportado.mp4"])
    assert aprobacion.automated_similarity_threshold_applied is False
    with pytest.raises(ValidationError):
        AprobacionEditorial(
            run_id=uuid4(),
            status=EstadoEvaluacion.aprobado_por_humano,
            approved_by="dylan",
            reason="revisado",
            video_sha256="a" * 64,
            materials=["material/aportado.mp4"],
            automated_similarity_threshold_applied=True,
        )


# ---------------------------------------------------------------------------
# 5. Las declaraciones pertenecen a esta corrida
# ---------------------------------------------------------------------------


def test_una_metadata_de_otra_corrida_se_rechaza(corrida):
    with pytest.raises(EntradaInvalida) as exc:
        _preparar(corrida, metadata=_metadata_editorial(uuid4()))
    assert "corrida" in str(exc.value)


def test_una_aprobacion_de_otra_corrida_se_rechaza(corrida):
    with pytest.raises(EntradaInvalida) as exc:
        _preparar(
            corrida,
            aprobacion=_aprobacion(uuid4(), corrida["sha"], corrida["materials"]),
        )
    assert "corrida" in str(exc.value)


def test_un_fichero_que_falta_lo_dice(corrida, tmp_path):
    ruta_meta, ruta_apro = _declarar(corrida)
    ruta_apro.unlink()
    with pytest.raises(EntradaInvalida) as exc:
        pe.preparar(
            run_id=corrida["run_id"],
            directorio_corrida=corrida["dir"],
            ruta_metadata=ruta_meta,
            ruta_aprobacion=ruta_apro,
        )
    assert "no existe el fichero" in str(exc.value)


def test_un_json_invalido_lo_dice(corrida):
    ruta_meta, ruta_apro = _declarar(corrida)
    ruta_meta.write_text("{esto no es json", encoding="utf-8")
    with pytest.raises(EntradaInvalida) as exc:
        pe.preparar(
            run_id=corrida["run_id"],
            directorio_corrida=corrida["dir"],
            ruta_metadata=ruta_meta,
            ruta_aprobacion=ruta_apro,
        )
    assert "no cumple su contrato" in str(exc.value)


# ---------------------------------------------------------------------------
# 6. La puerta editorial se compone, no se reescribe
# ---------------------------------------------------------------------------


def test_la_puerta_editorial_sigue_exigiendo_lo_de_la_puerta_base(corrida):
    """Un recurso de entrada que desaparece bloquea igual que antes."""
    (corrida["dir"] / ENTRADAS["material"]).unlink()
    preparacion = _preparar(corrida)

    assert not preparacion.gate.listo
    # El motivo viene de la puerta de Gate 7.2, que se llamó y no se reimplementó.
    assert any("procedencia" in m.lower() or "no existe" in m for m in
               preparacion.gate.motivos)


def test_los_motivos_tecnicos_y_editoriales_se_acumulan(corrida):
    """Se informan todos, no el primero: arreglarlos de uno en uno costaría una
    corrida por motivo."""
    (corrida["dir"] / ENTRADAS["material"]).unlink()
    preparacion = _preparar(
        corrida,
        aprobacion=_aprobacion(
            corrida["run_id"],
            corrida["sha"],
            corrida["materials"],
            status=EstadoEvaluacion.no_evaluado,
        ),
    )
    assert len(preparacion.gate.motivos) >= 2
    assert any("aprobación editorial" in m for m in preparacion.gate.motivos)


def test_un_gate_no_satisfecho_nunca_trae_el_video(corrida):
    """``ResultadoGate`` valida en la construcción; el compuesto lo comprueba."""
    preparacion = _preparar(
        corrida,
        aprobacion=_aprobacion(
            corrida["run_id"],
            corrida["sha"],
            corrida["materials"],
            status=EstadoEvaluacion.no_evaluado,
        ),
    )
    assert preparacion.gate.video_path is None
    assert preparacion.gate.video_sha256 is None
    assert preparacion.gate.total_bytes == 0


# ---------------------------------------------------------------------------
# 7. D1 — los artefactos pertenecen a la corrida, y publicar no es una etapa
# ---------------------------------------------------------------------------


def test_los_artefactos_viven_bajo_publication(corrida):
    preparacion = _preparar(corrida)
    assert preparacion.gate.listo

    directorio = corrida["dir"] / pe.DIRECTORIO_PUBLICACION
    assert (directorio / "publish_metadata.json").is_file()
    assert (directorio / "editorial_approval.json").is_file()
    assert (directorio / "publish_job.json").is_file()
    # Todavía no se ha publicado: no hay resultado que escribir.
    assert not (directorio / "publish_result.json").exists()


def test_los_artefactos_se_escriben_tambien_cuando_la_puerta_rechaza(corrida):
    """Que un intento no publicara no lo hace menos digno de quedar registrado."""
    _preparar(
        corrida,
        aprobacion=_aprobacion(
            corrida["run_id"],
            corrida["sha"],
            corrida["materials"],
            status=EstadoEvaluacion.rechazado_por_humano,
        ),
    )
    directorio = corrida["dir"] / pe.DIRECTORIO_PUBLICACION
    aprobacion = json.loads(
        (directorio / "editorial_approval.json").read_text(encoding="utf-8")
    )
    assert aprobacion["status"] == EstadoEvaluacion.rechazado_por_humano.value
    job = json.loads((directorio / "publish_job.json").read_text(encoding="utf-8"))
    assert job["state"] == EstadoPublicacion.no_listo.value


def test_publicar_no_es_una_etapa_del_stage_runner():
    """El invariante que impide la segunda subida por un rerun.

    Si publicar fuera una etapa, ``run --force`` podría re-ejecutarla, y
    re-ejecutar una publicación es subir otra vez. Se comprueba sobre todos los
    pipelines que el CLI ofrece: ninguno declara una etapa que produzca los
    artefactos de publicación.
    """
    from app.__main__ import PIPELINES

    artefactos_de_publicacion = {
        pe.ARTEFACTO_METADATA,
        pe.ARTEFACTO_APROBACION,
        pe.ARTEFACTO_JOB,
        pe.ARTEFACTO_RESULTADO,
    }
    for nombre, construir in PIPELINES.items():
        producidos = {etapa.artefacto for etapa in construir()}
        assert not (producidos & artefactos_de_publicacion), (
            f"el pipeline {nombre!r} declara una etapa de publicación"
        )
        assert "publication" not in {e.nombre for e in construir()}


def test_el_subcomando_publish_no_acepta_force():
    """``--force`` nombra una etapa, y publicar no es una etapa."""
    from app.__main__ import _construir_parser

    parser = _construir_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["publish", str(uuid4()), "--metadata", "m.json",
             "--approval", "a.json", "--force", "publication"]
        )


def test_el_cli_exige_la_confirmacion_exacta():
    from app.__main__ import CONFIRMACION, _construir_parser

    assert CONFIRMACION == "SUBIR"
    parser = _construir_parser()
    args = parser.parse_args(
        ["publish", str(uuid4()), "--metadata", "m.json", "--approval", "a.json"]
    )
    assert args.confirmar is None, "publicar no puede ser el comportamiento por defecto"


# ---------------------------------------------------------------------------
# 8. Integración con transporte falso: se publica, y se registra
# ---------------------------------------------------------------------------


def test_una_publicacion_completa_deja_job_y_resultado(corrida):
    preparacion = _preparar(corrida)
    transporte = _guion_feliz()

    salida = pe.publicar_preparada(
        preparacion, token=token(), transporte=transporte
    )

    assert salida.publicado
    assert salida.job.state is EstadoPublicacion.completado
    assert salida.result is not None
    assert salida.result.video_id == VIDEO_ID
    assert salida.result.privacy_status is Privacidad.privado
    assert transporte.sesiones_iniciadas == 1, "se abrió más de una sesión"

    directorio = corrida["dir"] / pe.DIRECTORIO_PUBLICACION
    job = json.loads((directorio / "publish_job.json").read_text(encoding="utf-8"))
    assert job["state"] == EstadoPublicacion.completado.value
    resultado = json.loads(
        (directorio / "publish_result.json").read_text(encoding="utf-8")
    )
    assert resultado["video_id"] == VIDEO_ID
    assert resultado["video_sha256"] == corrida["sha"]


def test_el_cuerpo_que_recibe_el_proveedor_es_el_declarado(corrida):
    """La declaración editorial llega tal cual a ``videos.insert``."""
    preparacion = _preparar(
        corrida,
        metadata=_metadata_editorial(
            corrida["run_id"], contains_synthetic_media=True
        ),
    )
    transporte = _guion_feliz()
    pe.publicar_preparada(preparacion, token=token(), transporte=transporte)

    apertura = next(
        ll for ll in transporte.llamadas
        if ll.metodo == "POST" and "uploadType=resumable" in ll.url
    )
    enviado = json.loads(apertura.cuerpo)
    assert enviado["status"]["privacyStatus"] == "private"
    assert enviado["status"]["containsSyntheticMedia"] is True
    assert enviado["status"]["selfDeclaredMadeForKids"] is False
    assert enviado["snippet"]["title"] == TITULO


def test_una_aprobacion_que_no_aprueba_no_abre_ninguna_sesion(corrida):
    """El invariante central: sin APPROVED_BY_HUMAN no hay trato con el proveedor."""
    preparacion = _preparar(
        corrida,
        aprobacion=_aprobacion(
            corrida["run_id"],
            corrida["sha"],
            corrida["materials"],
            status=EstadoEvaluacion.no_evaluado,
        ),
    )
    transporte = _guion_feliz()

    with pytest.raises(EntradaInvalida):
        pe.publicar_preparada(preparacion, token=token(), transporte=transporte)

    assert transporte.llamadas == []
    assert transporte.sesiones_iniciadas == 0
    assert not (
        corrida["dir"] / pe.DIRECTORIO_PUBLICACION / "publish_result.json"
    ).exists()


def test_ningun_artefacto_lleva_la_url_de_sesion_ni_el_token(corrida):
    """``session_url`` no se persiste. Ni ella, ni el token, ni una cabecera."""
    preparacion = _preparar(corrida)
    pe.publicar_preparada(
        preparacion, token=token(), transporte=_guion_feliz()
    )

    directorio = corrida["dir"] / pe.DIRECTORIO_PUBLICACION
    escritos = sorted(directorio.glob("*.json"))
    assert escritos, "no se escribió ningún artefacto"

    prohibidos = (
        SESSION_URL,
        "upload_id",
        ACCESS_TOKEN,
        "Authorization",
        "Bearer",
        "client_secret",
        "refresh_token",
    )
    for ruta in escritos:
        texto = ruta.read_text(encoding="utf-8")
        for secreto in prohibidos:
            assert secreto not in texto, f"{ruta.name} contiene {secreto!r}"

    # Y la referencia que sí consta es un SHA-256, del que no se reconstruye nada.
    job = json.loads((directorio / "publish_job.json").read_text(encoding="utf-8"))
    referencia = job["upload_session_ref"]
    assert referencia and len(referencia) == 64
    assert referencia == hashlib.sha256(SESSION_URL.encode("utf-8")).hexdigest()


def test_publicar_preparada_se_niega_si_la_puerta_no_esta_satisfecha(corrida):
    """Y con un error del dominio, no con un ``assert`` que ``-O`` borraría."""
    (corrida["dir"] / RUTA_VIDEO).unlink()
    preparacion = _preparar(corrida)
    transporte = _guion_feliz()

    with pytest.raises(EntradaInvalida) as exc:
        pe.publicar_preparada(preparacion, token=token(), transporte=transporte)

    assert "no está satisfecha" in str(exc.value)
    assert transporte.llamadas == []


# ---------------------------------------------------------------------------
# 9. E2E local sin red: material real, render, QA, puerta y publicación
# ---------------------------------------------------------------------------

RUTA_MATERIAL_REAL = "material/aportado.mp4"
ASSET_REAL = "material-editorial"


def _material_real_declarado(ws) -> str:
    """Copia el MP4 que generó el pipeline a la ruta del material aportado.

    Se reutiliza un archivo que FFmpeg produjo de verdad: la prueba va sobre el
    cableado, no sobre el códec.
    """
    from app.pipeline import render

    destino = ws.ruta(RUTA_MATERIAL_REAL)
    destino.parent.mkdir(parents=True, exist_ok=True)
    generado = ws.leer_artefacto(render.ARTEFACTO_MATERIAL, RenderResult)
    shutil.copyfile(ws.ruta(generado.output_path), destino)
    huella = hashlib.sha256(destino.read_bytes()).hexdigest()

    from app.pipeline import transformation

    ledger = ws.leer_artefacto(transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger)
    evidencia = Evidence(
        evidence_id="ev-material-real",
        kind=TipoEvidencia.registro_propiedad,
        reference="captura propia aportada a la corrida",
        description="material visual editorial de esta pieza",
        issued_by="editorial",
    )
    fuente = SourceAsset(
        run_id=ledger.run_id,
        asset_id=ASSET_REAL,
        media_kind=TipoMedio.video,
        origin="aportado a mano por quien edita",
        local_path=RUTA_MATERIAL_REAL,
        sha256=huella,
        evidence=[evidencia],
    )
    decision = LicenseDecision(
        run_id=ledger.run_id,
        asset_id=ASSET_REAL,
        decision=ClaseFuente.render_permitido,
        basis=BaseLicencia.propia,
        evidence_ids=["ev-material-real"],
        reason="material editorial aportado y decidido a mano",
        decided_by="tests",
        policy_version=VERSION_POLITICA,
    )
    datos = ledger.model_dump()
    datos["sources"] = [*datos["sources"], fuente.model_dump()]
    datos["decisions"] = [*datos["decisions"], decision.model_dump()]
    datos["render_assets"] = [*datos["render_assets"], RUTA_MATERIAL_REAL]
    ws.escribir_artefacto(
        transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger.model_validate(datos)
    )
    return huella


def test_e2e_local_sin_red_de_material_real_a_publish_result(
    settings_e2e, workspace_e2e, tmp_path
):
    """El recorrido completo de Gate 7.4-B, sin una sola conexión.

    metadata declarada + aprobación humana + material real autorizado y
    seleccionado + render_job + QA + puerta + PublishJob + transporte falso +
    publish_job.json + publish_result.json.
    """
    from app.core.manifest import Manifest
    from app.core.stage_runner import StageRunner
    from app.pipeline import qa_video, render, transformation
    from app.pipeline.stages import ETAPA_RENDER

    ws = workspace_e2e
    runner = StageRunner(ws, Manifest.cargar(ws.dir), settings_e2e)

    # --- 1. Material real, declarado y autorizado -------------------------
    huella_material = _material_real_declarado(ws)
    runner.producidos.pop(transformation.ARTEFACTO_PROCEDENCIA, None)

    # --- 2. Seleccionado explícitamente. Autorizar no es seleccionar ------
    runner.parametros[transformation.CLAVE_MATERIAL_SELECCIONADO] = ASSET_REAL
    job = runner.ejecutar(render.ETAPA_JOB, forzar=True).artefacto
    assert job.materials == [RUTA_MATERIAL_REAL]

    # --- 3. Render y composición reales -----------------------------------
    runner.ejecutar(ETAPA_RENDER, forzar=True)
    final = runner.ejecutar(render.ETAPA_COMPOSICION, forzar=True).artefacto
    assert final.status is EstadoRender.exito
    archivo = ws.ruta(final.output_path)
    assert final.sha256 == hashlib.sha256(archivo.read_bytes()).hexdigest()

    # --- 4. QA técnica del vídeo ------------------------------------------
    qa = runner.ejecutar(qa_video.ETAPA_QA_VIDEO).artefacto
    assert qa.technical_qa_ok
    # La QA no juzga lo editorial, y sigue sin hacerlo.
    assert qa.editorial_legal_assessment.status is EstadoEvaluacion.no_evaluado

    # --- 5. Las dos declaraciones -----------------------------------------
    declaraciones = tmp_path / "declaraciones_e2e"
    declaraciones.mkdir()
    ruta_meta = declaraciones / "metadata.json"
    ruta_meta.write_text(
        _metadata_editorial(ws.run_id).model_dump_json(indent=2), encoding="utf-8"
    )
    ruta_apro = declaraciones / "aprobacion.json"
    ruta_apro.write_text(
        _aprobacion(ws.run_id, final.sha256, job.materials).model_dump_json(indent=2),
        encoding="utf-8",
    )

    # --- 6. La puerta -----------------------------------------------------
    preparacion = pe.preparar(
        run_id=ws.run_id,
        directorio_corrida=ws.dir,
        ruta_metadata=ruta_meta,
        ruta_aprobacion=ruta_apro,
    )
    assert preparacion.gate.listo, preparacion.gate.motivos
    assert preparacion.gate.video_sha256 == final.sha256
    assert preparacion.job.state is EstadoPublicacion.no_listo

    # --- 7. La publicación, contra el transporte falso ---------------------
    transporte = _guion_feliz()
    salida = pe.publicar_preparada(
        preparacion, token=token(), transporte=transporte
    )

    assert salida.publicado
    assert salida.job.state is EstadoPublicacion.completado
    assert salida.job.attempt == 1
    assert transporte.sesiones_iniciadas == 1

    # --- 8. Los artefactos, en la corrida ---------------------------------
    directorio = ws.dir / pe.DIRECTORIO_PUBLICACION
    job_escrito = json.loads(
        (directorio / "publish_job.json").read_text(encoding="utf-8")
    )
    resultado_escrito = json.loads(
        (directorio / "publish_result.json").read_text(encoding="utf-8")
    )
    assert job_escrito["state"] == EstadoPublicacion.completado.value
    assert resultado_escrito["privacy_status"] == "private"
    assert resultado_escrito["video_sha256"] == final.sha256

    # --- 9. La trazabilidad se sostiene de punta a punta ------------------
    aprobacion_escrita = json.loads(
        (directorio / "editorial_approval.json").read_text(encoding="utf-8")
    )
    assert aprobacion_escrita["video_sha256"] == final.sha256
    assert aprobacion_escrita["materials"] == [RUTA_MATERIAL_REAL]
    ledger = ws.leer_artefacto(transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger)
    assert ledger.fuente(ASSET_REAL).sha256 == huella_material
    assert ledger.fuente_de(job.materials[0]).asset_id == ASSET_REAL

    # --- 10. Y no se filtró nada -----------------------------------------
    for ruta in directorio.glob("*.json"):
        texto = ruta.read_text(encoding="utf-8")
        assert SESSION_URL not in texto
        assert ACCESS_TOKEN not in texto
