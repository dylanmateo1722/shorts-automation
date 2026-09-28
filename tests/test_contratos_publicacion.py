"""Contratos de publicación: intención, estado operacional y resultado.

Gate 7 formaliza los contratos y la máquina de estados. Aquí **no** se prueba
ningún cliente de YouTube, ningún OAuth y ningún upload: no existen todavía. Lo
que se prueba es que los estados imposibles no se puedan representar.

Los dos casos que dan sentido al resto:

* ``test_una_respuesta_perdida_no_autoriza_subir_otra_vez``: si el upload pudo
  haber terminado y la respuesta se perdió, la máquina no deja volver a subir a
  ciegas. Es la única protección real que tenemos, porque **YouTube no ofrece
  ninguna clave de idempotencia**.
* ``test_una_divulgacion_que_exige_verificacion_bloquea_la_subida``: el estado
  ``requires_current_verification`` no se puede saltar entrando en ``UPLOADING``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.contracts.models import (
    ESTADOS_CON_INTENTO,
    ESTADOS_DE_RESULTADO,
    ESTADOS_TERMINALES,
    SCHEMA_VERSION_PUBLICACION,
    TRANSICIONES_PUBLICACION,
    DivulgacionIA,
    EstadoDivulgacionIA,
    EstadoPublicacion,
    Privacidad,
    PublishJob,
    PublishMetadata,
    PublishResult,
    clave_idempotencia,
    exigir_transicion,
    transicion_valida,
)

E = EstadoPublicacion

AHORA = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
VIDEO_ID = "dQw4w9WgXcQ"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
SHA = "a" * 64


# ---------------------------------------------------------------------------
# Constructores
# ---------------------------------------------------------------------------


def _divulgacion(**extra) -> DivulgacionIA:
    base = dict(
        status=EstadoDivulgacionIA.no_requerida,
        reason="la pieza no incluye material sintético que la norma obligue a declarar",
        decided_by="editor@example.org",
        decided_at=AHORA,
    )
    base.update(extra)
    return DivulgacionIA(**base)


def _metadata(**extra) -> PublishMetadata:
    base = dict(
        run_id=uuid.uuid4(),
        title="Por qué el 73 % abandona antes de tres segundos",
        description="Un Short sobre atención.",
        tags=["atención", "shorts"],
        category_id="27",
        privacy_status=Privacidad.privado,
        language="es",
        made_for_kids=False,
        ai_disclosure=_divulgacion(),
    )
    base.update(extra)
    return PublishMetadata(**base)


def _job(**extra) -> PublishJob:
    rid = extra.pop("run_id", uuid.uuid4())
    base = dict(
        run_id=rid,
        idempotency_key=clave_idempotencia(rid),
        started_at=AHORA,
        updated_at=AHORA,
    )
    base.update(extra)
    return PublishJob(**base)


def _resultado(**extra) -> PublishResult:
    base = dict(
        run_id=uuid.uuid4(),
        status=E.completado,
        privacy_status=Privacidad.privado,
        video_id=VIDEO_ID,
        url=URL,
        upload_attempts=1,
        uploaded_at=AHORA,
        completed_at=AHORA + timedelta(seconds=30),
        metadata_sha256=SHA,
        video_sha256="b" * 64,
    )
    base.update(extra)
    return PublishResult(**base)


# ---------------------------------------------------------------------------
# Contratos válidos y campos obligatorios
# ---------------------------------------------------------------------------


def test_los_tres_contratos_validan_y_declaran_su_version():
    for artefacto in (_metadata(), _job(), _resultado()):
        # "1.1" lo subió Gate 7.2 al añadir la referencia a la sesión de subida
        # y el resultado de la reconciliación. "1.2" lo sube Gate 7.4-B al añadir
        # la declaración de medios sintéticos. Los dos son aditivos: un artefacto
        # de la versión anterior sigue validando porque los campos nuevos son
        # opcionales en la base.
        assert artefacto.schema_version == SCHEMA_VERSION_PUBLICACION == "1.2"
        assert isinstance(artefacto.run_id, uuid.UUID)
        assert artefacto.created_at.tzinfo is not None


def test_el_run_id_sigue_siendo_un_uuid():
    for constructor in (_metadata, _job, _resultado):
        with pytest.raises(ValidationError):
            constructor(run_id="no-es-un-uuid")


@pytest.mark.parametrize(
    "constructor, campo",
    [
        (_metadata, "title"),
        (_metadata, "made_for_kids"),
        (_metadata, "ai_disclosure"),
        (_job, "idempotency_key"),
        (_resultado, "status"),
        (_resultado, "upload_attempts"),
    ],
)
def test_los_campos_obligatorios_no_tienen_valor_por_defecto(constructor, campo):
    base = constructor().model_dump()
    base.pop(campo)
    with pytest.raises(ValidationError):
        type(constructor()).model_validate(base)


def test_un_campo_desconocido_es_rechazado():
    for constructor in (_metadata, _job, _resultado):
        with pytest.raises(ValidationError):
            constructor(campo_inventado="x")


def test_made_for_kids_no_se_rellena_por_omision():
    """Es una declaración con consecuencias; nadie puede hacerla por omisión."""
    assert PublishMetadata.model_fields["made_for_kids"].is_required()


def test_el_titulo_respeta_el_limite_del_contrato():
    _metadata(title="x" * 100)
    with pytest.raises(ValidationError):
        _metadata(title="x" * 101)
    with pytest.raises(ValidationError):
        _metadata(title="")


def test_ida_y_vuelta_por_json():
    for constructor, modelo in (
        (_metadata, PublishMetadata),
        (_job, PublishJob),
        (_resultado, PublishResult),
    ):
        original = constructor()
        assert modelo.model_validate_json(original.model_dump_json()) == original


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def test_privacy_status_esta_restringido_a_tres_valores():
    assert {p.value for p in Privacidad} == {"private", "unlisted", "public"}
    for valor in ("private", "unlisted", "public"):
        assert _metadata(privacy_status=valor).privacy_status.value == valor
    with pytest.raises(ValidationError):
        _metadata(privacy_status="friends_only")


def test_el_primer_flujo_publica_en_privado():
    """D19: ``private`` por defecto. Publicar en público es decisión posterior."""
    assert _metadata().privacy_status is Privacidad.privado
    assert PublishMetadata.model_fields["privacy_status"].default is Privacidad.privado


def test_ai_disclosure_esta_restringido_a_tres_estados():
    assert {d.value for d in EstadoDivulgacionIA} == {
        "required",
        "not_required",
        "requires_current_verification",
    }
    with pytest.raises(ValidationError):
        _divulgacion(status="probablemente_no")


def test_publish_status_esta_restringido_a_los_ocho_estados():
    assert {e.value for e in EstadoPublicacion} == {
        "NOT_READY",
        "READY",
        "UPLOADING",
        "UPLOADED",
        "VERIFYING",
        "COMPLETED",
        "FAILED",
        "NEEDS_REVIEW",
    }
    with pytest.raises(ValidationError):
        _job(state="CASI_LISTO")


# ---------------------------------------------------------------------------
# Estados e invariantes del trabajo
# ---------------------------------------------------------------------------


def test_un_trabajo_nuevo_empieza_sin_estar_listo_y_sin_intentos():
    job = _job()
    assert job.state is E.no_listo
    assert job.attempt == 0
    assert not job.terminal


def test_los_estados_que_implican_subida_exigen_al_menos_un_intento():
    for estado in ESTADOS_CON_INTENTO:
        extra = {"reason": "motivo"} if estado is E.fallido else {}
        _job(state=estado, attempt=1, **extra)
        with pytest.raises(ValidationError, match="al menos un intento"):
            _job(state=estado, attempt=0, **extra)


def test_los_estados_previos_a_la_subida_no_pueden_llevar_intentos():
    for estado in (E.no_listo, E.listo):
        with pytest.raises(ValidationError, match="previo a cualquier subida"):
            _job(state=estado, attempt=1)


@pytest.mark.parametrize("estado", [E.revision_pendiente, E.fallido])
def test_los_estados_que_piden_intervencion_exigen_un_motivo(estado):
    attempt = 1 if estado in ESTADOS_CON_INTENTO else 0
    with pytest.raises(ValidationError, match="exige un motivo"):
        _job(state=estado, attempt=attempt)
    assert _job(state=estado, attempt=attempt, reason="qué pasó").reason == "qué pasó"


def test_un_trabajo_no_puede_actualizarse_antes_de_empezar():
    with pytest.raises(ValidationError, match="anterior a started_at"):
        _job(started_at=AHORA, updated_at=AHORA - timedelta(seconds=1))


def test_completed_y_failed_son_los_estados_terminales():
    assert ESTADOS_TERMINALES == {E.completado, E.fallido}
    assert _job(state=E.completado, attempt=1).terminal
    assert _job(state=E.fallido, attempt=1, reason="x").terminal
    assert not _job(state=E.revision_pendiente, reason="x").terminal


# ---------------------------------------------------------------------------
# Transiciones válidas
# ---------------------------------------------------------------------------


def test_la_tabla_cubre_los_ocho_estados():
    assert set(TRANSICIONES_PUBLICACION) == set(EstadoPublicacion)


def test_el_recorrido_completo_llega_a_completed():
    metadata = _metadata()
    job = _job()
    esperados = [E.listo, E.subiendo, E.subido, E.verificando, E.completado]

    for destino in esperados:
        job = job.transicionar(destino, metadata=metadata)
        assert job.state is destino

    assert job.attempt == 1, "solo se empezó una subida"
    assert job.terminal


@pytest.mark.parametrize(
    "origen, destino",
    [
        (E.no_listo, E.listo),
        (E.no_listo, E.revision_pendiente),
        (E.listo, E.subiendo),
        (E.listo, E.no_listo),
        (E.listo, E.revision_pendiente),
        (E.subiendo, E.subido),
        (E.subiendo, E.fallido),
        (E.subiendo, E.revision_pendiente),
        (E.subido, E.verificando),
        (E.subido, E.revision_pendiente),
        (E.verificando, E.completado),
        (E.verificando, E.fallido),
        (E.verificando, E.revision_pendiente),
        (E.revision_pendiente, E.verificando),
        (E.revision_pendiente, E.fallido),
    ],
)
def test_las_transiciones_de_la_tabla_se_aceptan(origen, destino):
    assert transicion_valida(origen, destino)
    exigir_transicion(origen, destino, metadata=_metadata())


def test_entrar_en_uploading_cuenta_un_intento_mas():
    metadata = _metadata()
    job = _job().transicionar(E.listo)
    assert job.attempt == 0
    assert job.transicionar(E.subiendo, metadata=metadata).attempt == 1


def test_una_transicion_no_muta_el_trabajo_anterior():
    """Cada estado por el que se pasó sigue siendo evidencia de que se pasó."""
    job = _job()
    nuevo = job.transicionar(E.listo)
    assert job.state is E.no_listo
    assert nuevo.state is E.listo
    assert nuevo.updated_at >= job.updated_at


def test_una_transicion_valida_sigue_validando_el_artefacto():
    """``model_copy`` se salta los validadores; ``transicionar`` no debe."""
    with pytest.raises(ValidationError, match="exige un motivo"):
        _job().transicionar(E.revision_pendiente)


# ---------------------------------------------------------------------------
# Transiciones inválidas
# ---------------------------------------------------------------------------


def _todas_las_transiciones_invalidas():
    for origen in EstadoPublicacion:
        for destino in EstadoPublicacion:
            if destino not in TRANSICIONES_PUBLICACION[origen]:
                yield origen, destino


@pytest.mark.parametrize("origen, destino", list(_todas_las_transiciones_invalidas()))
def test_toda_transicion_fuera_de_la_tabla_se_rechaza(origen, destino):
    """Incluye los saltos arbitrarios y quedarse en el mismo estado."""
    assert not transicion_valida(origen, destino)
    with pytest.raises(ValueError, match="transición no permitida"):
        exigir_transicion(origen, destino, metadata=_metadata())


@pytest.mark.parametrize("destino", [E.subido, E.verificando, E.completado, E.fallido])
def test_no_se_salta_desde_not_ready_al_final(destino):
    with pytest.raises(ValueError, match="transición no permitida"):
        _job().transicionar(destino, metadata=_metadata())


@pytest.mark.parametrize("estado", sorted(ESTADOS_TERMINALES, key=lambda e: e.value))
def test_de_un_estado_terminal_no_se_sale(estado):
    assert TRANSICIONES_PUBLICACION[estado] == frozenset()
    job = _job(state=estado, attempt=1, reason="motivo")
    for destino in EstadoPublicacion:
        with pytest.raises(ValueError, match="transición no permitida"):
            job.transicionar(destino, metadata=_metadata())


def test_un_fallo_determinista_no_se_reintenta_transitando():
    """Retomar un ``FAILED`` es una ejecución nueva, no una transición."""
    fallido = _job(state=E.fallido, attempt=1, reason="la API rechazó la petición")
    for destino in (E.listo, E.subiendo, E.revision_pendiente):
        with pytest.raises(ValueError, match="transición no permitida"):
            fallido.transicionar(destino, metadata=_metadata())


# ---------------------------------------------------------------------------
# Idempotencia y el caso de la respuesta perdida
# ---------------------------------------------------------------------------


def test_la_clave_de_idempotencia_es_determinista_y_propia_del_run():
    rid = uuid.uuid4()
    otro = uuid.uuid4()
    assert clave_idempotencia(rid) == clave_idempotencia(rid)
    assert clave_idempotencia(rid) != clave_idempotencia(otro)
    assert len(clave_idempotencia(rid)) == 64


def test_la_clave_no_se_presenta_como_idempotencia_nativa_de_youtube():
    """Lo que el contrato afirma sobre la clave tiene que ser verdad.

    YouTube no ofrece ninguna clave de idempotencia genérica, así que la
    documentación de la función tiene que decirlo en vez de dejar que alguien lo
    suponga al leer el nombre del campo.
    """
    doc = clave_idempotencia.__doc__ or ""
    assert "YouTube no ofrece" in doc
    assert "no hace absolutamente nada del lado de YouTube" in doc
    assert "reconciliación" in doc


def test_una_respuesta_perdida_no_autoriza_subir_otra_vez():
    """El caso que D18 exige representar.

    El upload empezó y no se sabe si terminó. Desde ``UPLOADING`` la máquina no
    permite volver a ``READY`` —que sería autorizar otra subida a ciegas, con el
    riesgo de publicar el vídeo dos veces—, así que el único camino es
    ``NEEDS_REVIEW`` y, desde ahí, ir a comprobar qué hay en el remoto.
    """
    metadata = _metadata()
    subiendo = _job().transicionar(E.listo).transicionar(E.subiendo, metadata=metadata)

    assert not transicion_valida(E.subiendo, E.listo)
    with pytest.raises(ValueError, match="transición no permitida"):
        subiendo.transicionar(E.listo, metadata=metadata)

    # El camino que sí existe: registrar la ambigüedad y reconciliar.
    pendiente = subiendo.transicionar(
        E.revision_pendiente,
        reason="la respuesta del upload se perdió; no consta si terminó",
    )
    assert pendiente.attempt == 1, "no se cuenta un intento que no se ha hecho"

    reconciliando = pendiente.transicionar(E.verificando)
    assert reconciliando.state is E.verificando
    assert reconciliando.attempt == 1


def test_desde_revision_pendiente_no_se_vuelve_a_subir_sin_comprobar():
    pendiente = _job(state=E.revision_pendiente, attempt=1, reason="ambiguo")
    for destino in (E.listo, E.subiendo, E.subido, E.completado):
        with pytest.raises(ValueError, match="transición no permitida"):
            pendiente.transicionar(destino, metadata=_metadata())


def test_una_segunda_ejecucion_del_mismo_run_reconoce_la_misma_publicacion():
    rid = uuid.uuid4()
    assert _job(run_id=rid).idempotency_key == _job(run_id=rid).idempotency_key


# ---------------------------------------------------------------------------
# Divulgación de IA
# ---------------------------------------------------------------------------


def test_la_divulgacion_registra_motivo_y_autor():
    divulgacion = _divulgacion(status=EstadoDivulgacionIA.requerida)
    assert divulgacion.reason.strip()
    assert divulgacion.decided_by.strip()
    assert divulgacion.decided_at == AHORA


def test_una_divulgacion_sin_motivo_o_sin_autor_es_rechazada():
    """Sin constancia de quién y por qué, sería un valor por defecto disfrazado."""
    with pytest.raises(ValidationError):
        _divulgacion(reason="   ")
    with pytest.raises(ValidationError):
        _divulgacion(decided_by="")


@pytest.mark.parametrize(
    "estado, permite",
    [
        (EstadoDivulgacionIA.no_requerida, True),
        (EstadoDivulgacionIA.requerida, True),
        (EstadoDivulgacionIA.requiere_verificacion_actual, False),
    ],
)
def test_solo_requires_current_verification_bloquea_la_publicacion(estado, permite):
    metadata = _metadata(ai_disclosure=_divulgacion(status=estado))
    assert metadata.permite_publicacion_automatica is permite
    assert metadata.ai_disclosure.permite_publicacion_automatica is permite


def test_una_divulgacion_que_exige_verificacion_bloquea_la_subida():
    bloqueante = _metadata(
        ai_disclosure=_divulgacion(
            status=EstadoDivulgacionIA.requiere_verificacion_actual,
            reason="cambió la norma aplicable; hay que revisar antes de publicar",
        )
    )
    listo = _job().transicionar(E.listo)

    with pytest.raises(ValueError, match="requires_current_verification"):
        listo.transicionar(E.subiendo, metadata=bloqueante)

    # Lo que sí puede hacer es quedar registrado para que alguien lo mire.
    pendiente = listo.transicionar(
        E.revision_pendiente, reason="la divulgación exige verificación actual"
    )
    assert pendiente.state is E.revision_pendiente


def test_no_se_puede_entrar_en_uploading_sin_presentar_la_metadata():
    """Sin metadata no hay forma de comprobar la divulgación, así que no se pasa."""
    listo = _job().transicionar(E.listo)
    with pytest.raises(ValueError, match="sin la metadata"):
        listo.transicionar(E.subiendo)
    with pytest.raises(ValueError, match="sin la metadata"):
        exigir_transicion(E.listo, E.subiendo)


def test_el_tts_no_determina_por_si_solo_que_haga_falta_divulgacion():
    """No hay regla automática: los tres estados son declarables igual.

    Si el sistema dedujera «hay TTS, por tanto required», estaría inventando una
    clasificación que nadie ha hecho.
    """
    for estado in EstadoDivulgacionIA:
        metadata = _metadata(ai_disclosure=_divulgacion(status=estado))
        assert metadata.ai_disclosure.status is estado


# ---------------------------------------------------------------------------
# Resultado: COMPLETED, FAILED y NEEDS_REVIEW
# ---------------------------------------------------------------------------


def test_un_resultado_completed_necesita_video_id():
    with pytest.raises(ValidationError, match="sin video_id"):
        _resultado(status=E.completado, video_id=None, url=None, uploaded_at=None)


def test_un_resultado_completed_registra_cuando_se_completo():
    with pytest.raises(ValidationError, match="registra cuándo se completó"):
        _resultado(status=E.completado, completed_at=None)


def test_un_resultado_failed_no_necesita_video_id():
    fallido = _resultado(
        status=E.fallido, video_id=None, url=None, uploaded_at=None, completed_at=None
    )
    assert fallido.video_id is None
    assert fallido.status is E.fallido


def test_un_resultado_failed_puede_llevar_video_id_si_ya_existia():
    """Un fallo posterior a la subida no borra el identificador que YouTube dio."""
    fallido = _resultado(status=E.fallido, completed_at=None)
    assert fallido.video_id == VIDEO_ID


def test_needs_review_no_afirma_que_el_upload_fallara():
    """Es el estado de lo que hay que ir a comprobar, no un veredicto."""
    sin_id = _resultado(
        status=E.revision_pendiente,
        video_id=None,
        url=None,
        uploaded_at=None,
        completed_at=None,
    )
    con_id = _resultado(status=E.revision_pendiente, completed_at=None)

    assert sin_id.status is con_id.status is E.revision_pendiente
    assert sin_id.video_id is None and con_id.video_id == VIDEO_ID
    assert sin_id.completed_at is None and con_id.completed_at is None


def test_completed_no_significa_publico():
    """La confusión más fácil de cometer, y la que más caro saldría."""
    privado = _resultado(status=E.completado, privacy_status=Privacidad.privado)
    assert privado.status is E.completado
    assert privado.privacy_status is Privacidad.privado


def test_un_resultado_no_puede_estar_en_un_estado_de_trabajo():
    assert ESTADOS_DE_RESULTADO == {
        E.subido, E.verificando, E.completado, E.fallido, E.revision_pendiente,
    }
    for estado in (E.no_listo, E.listo, E.subiendo):
        with pytest.raises(ValidationError, match="describe por dónde va el trabajo"):
            _resultado(status=estado, completed_at=None)


def test_un_completed_at_sin_completed_es_una_contradiccion():
    with pytest.raises(ValidationError, match="solo 'COMPLETED' se ha completado"):
        _resultado(status=E.verificando, completed_at=AHORA)


@pytest.mark.parametrize("estado", [E.subido, E.verificando])
def test_los_estados_posteriores_a_la_subida_exigen_video_id(estado):
    with pytest.raises(ValidationError, match="no hay video_id"):
        _resultado(
            status=estado, video_id=None, url=None, uploaded_at=None, completed_at=None
        )


def test_un_video_id_va_con_su_fecha_y_su_url():
    with pytest.raises(ValidationError, match="sin uploaded_at"):
        _resultado(status=E.revision_pendiente, uploaded_at=None, completed_at=None)
    with pytest.raises(ValidationError, match="sin url"):
        _resultado(status=E.revision_pendiente, url=None, completed_at=None)
    with pytest.raises(ValidationError, match="sin video_id"):
        _resultado(
            status=E.revision_pendiente, video_id=None, url=None, completed_at=None
        )


def test_un_resultado_cuenta_al_menos_un_intento():
    with pytest.raises(ValidationError):
        _resultado(upload_attempts=0)


def test_las_huellas_del_resultado_son_sha256_o_nada():
    assert _resultado(metadata_sha256=None, video_sha256=None).metadata_sha256 is None
    with pytest.raises(ValidationError):
        _resultado(metadata_sha256="no-es-un-sha256")
    with pytest.raises(ValidationError):
        _resultado(video_sha256="A" * 64)


def test_el_contrato_no_inventa_campos_de_la_api_de_youtube():
    """Solo lo que este Gate utiliza. Nada de campos especulativos."""
    assert set(PublishResult.model_fields) == {
        "schema_version", "run_id", "created_at",
        "provider", "video_id", "url", "status", "privacy_status",
        "upload_attempts", "uploaded_at", "completed_at",
        "metadata_sha256", "video_sha256",
        # Gate 7.2. No es un campo de la API de YouTube: es qué averiguamos
        # nosotros cuando el desenlace de una subida no constaba.
        "reconciliation",
    }


# ---------------------------------------------------------------------------
# Separación Metadata / Result / Job
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "campo",
    ["video_id", "youtube_url", "url", "upload_status", "uploaded_at", "completed_at",
     "status", "state", "attempt", "upload_attempts"],
)
def test_la_metadata_no_contiene_datos_de_resultado(campo):
    """D15: la intención se escribe antes de que exista el vídeo."""
    assert campo not in PublishMetadata.model_fields
    with pytest.raises(ValidationError):
        _metadata(**{campo: "x"})


@pytest.mark.parametrize("campo", ["title", "description", "tags", "category_id",
                                   "made_for_kids", "ai_disclosure", "language"])
def test_el_trabajo_no_contiene_metadata_editorial(campo):
    """D18/criterio 14: el trabajo es estado operacional, no intención."""
    assert campo not in PublishJob.model_fields
    with pytest.raises(ValidationError):
        _job(**{campo: "x"})


def test_el_resultado_no_sustituye_a_la_metadata():
    """Comparten ``privacy_status`` porque son cosas distintas: lo pedido y lo
    observado. Verificar consiste precisamente en compararlos."""
    solo_de_metadata = {"title", "description", "tags", "category_id",
                        "made_for_kids", "ai_disclosure", "language"}
    assert solo_de_metadata & set(PublishResult.model_fields) == set()
    assert "privacy_status" in PublishMetadata.model_fields
    assert "privacy_status" in PublishResult.model_fields

    pedido = _metadata(privacy_status=Privacidad.privado)
    observado = _resultado(privacy_status=Privacidad.no_listado)
    assert pedido.privacy_status is not observado.privacy_status


def test_los_tres_contratos_no_comparten_responsabilidad():
    metadata, job, resultado = set(PublishMetadata.model_fields), set(
        PublishJob.model_fields
    ), set(PublishResult.model_fields)
    comunes = {"schema_version", "run_id", "created_at"}

    assert metadata & job == comunes
    assert metadata & resultado == comunes | {"privacy_status"}
    # ``reconciliation`` aparece en los dos desde Gate 7.2, y es la única
    # excepción aprobada a la separación: el trabajo lo lleva porque explica por
    # qué está donde está —un ``NEEDS_REVIEW`` sin ello no diría qué revisar—, y
    # el resultado lo lleva porque es la evidencia de lo que se observó. La
    # aserción sigue siendo exhaustiva: cualquier solapamiento nuevo la rompe.
    assert job & resultado == comunes | {"reconciliation"}


def test_los_tres_contratos_comparten_el_run_id_de_la_corrida():
    """Es lo que permite relacionarlos sin duplicar datos entre ellos."""
    rid = uuid.uuid4()
    assert (
        _metadata(run_id=rid).run_id
        == _job(run_id=rid).run_id
        == _resultado(run_id=rid).run_id
        == rid
    )


# ---------------------------------------------------------------------------
# Seguridad: esta fase no maneja credenciales
# ---------------------------------------------------------------------------


def test_ningun_contrato_de_publicacion_tiene_campos_de_credencial():
    """OAuth es de una fase posterior; aquí no hay dónde guardar un token."""
    prohibidos = (
        "token", "access_token", "refresh_token", "secret", "client_secret",
        "api_key", "apikey", "password", "credential", "cookie", "bearer", "auth",
    )
    for modelo in (PublishMetadata, PublishJob, PublishResult, DivulgacionIA):
        for campo in modelo.model_fields:
            assert not any(p in campo.lower() for p in prohibidos), (
                f"{modelo.__name__}.{campo}"
            )
