"""El publisher de extremo a extremo, con transporte simulado.

Aquí se comprueba la orquestación completa y, sobre todo, la regla que da
sentido a Gate 7.2: **``UNKNOWN`` nunca se convierte en una subida nueva**.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from app.contracts.models import (
    DesenlaceReconciliacion,
    EstadoPublicacion,
    Privacidad,
    PublishJob,
    TRANSICIONES_PUBLICACION,
    clave_idempotencia,
)
from app.core.errors import TiempoAgotado
from app.adapters.youtube.publisher import MAX_INTENTOS, publicar
from app.adapters.youtube.upload import MULTIPLO_FRAGMENTO
from app.pipeline.publicacion import ResultadoGate

from conftest_g72 import (
    SESSION_URL,
    VIDEO_ID,
    TransporteFalso,
    completado,
    error,
    incompleto,
    jitter_fijo,
    metadata,
    respuesta_perdida,
    sesion_abierta,
    sin_dormir,
    token,
    video_remoto,
)

CONTENIDO = b"MP4-de-prueba-" * 64


def _gate(tmp_path: Path, contenido: bytes = CONTENIDO) -> ResultadoGate:
    destino = tmp_path / "final_video.mp4"
    destino.write_bytes(contenido)
    return ResultadoGate(
        listo=True,
        video_path=destino,
        video_sha256=hashlib.sha256(contenido).hexdigest(),
        total_bytes=len(contenido),
    )


def _publicar(tmp_path, guion, *, meta=None, gate=None, **extra):
    run_id = uuid4()
    transporte = TransporteFalso(guion=guion)
    salida = publicar(
        run_id=run_id,
        metadata=meta or metadata(run_id),
        gate=gate or _gate(tmp_path),
        token=token(),
        transporte=transporte,
        dormir=sin_dormir,
        aleatorio=jitter_fijo,
        **extra,
    )
    return salida, transporte


# ---------------------------------------------------------------------------
# 6. Subida resumable normal -> COMPLETED
# ---------------------------------------------------------------------------


def test_subida_normal_termina_en_completed(tmp_path):
    salida, transporte = _publicar(
        tmp_path, [sesion_abierta(), completado(), video_remoto()]
    )
    assert salida.job.state is EstadoPublicacion.completado
    assert salida.publicado
    assert salida.result is not None
    assert salida.result.video_id == VIDEO_ID
    assert salida.result.status is EstadoPublicacion.completado
    assert salida.result.completed_at is not None
    assert transporte.sesiones_iniciadas == 1


def test_una_subida_normal_anota_la_huella_de_la_sesion(tmp_path):
    """Queda constancia de qué sesión fue, sin revelar el URL."""
    salida, _ = _publicar(tmp_path, [sesion_abierta(), completado(), video_remoto()])
    ref = salida.job.upload_session_ref
    assert ref == hashlib.sha256(SESSION_URL.encode()).hexdigest()
    assert SESSION_URL not in (ref or "")


def test_una_subida_en_varios_fragmentos_llega_a_completed(tmp_path):
    contenido = b"z" * (MULTIPLO_FRAGMENTO * 2 + 500)
    gate = _gate(tmp_path, contenido)
    salida, transporte = _publicar(
        tmp_path,
        [
            sesion_abierta(),
            incompleto(MULTIPLO_FRAGMENTO),
            incompleto(MULTIPLO_FRAGMENTO * 2),
            completado(),
            video_remoto(),
        ],
        gate=gate,
        tamano_fragmento=MULTIPLO_FRAGMENTO,
    )
    assert salida.job.state is EstadoPublicacion.completado
    assert transporte.fragmentos_enviados == 3
    assert transporte.sesiones_iniciadas == 1


# ---------------------------------------------------------------------------
# 7-9. Lo transitorio se reintenta
# ---------------------------------------------------------------------------


def test_un_timeout_antes_de_iniciar_se_reintenta(tmp_path):
    def _timeout(_):
        raise TiempoAgotado("sin respuesta")

    salida, transporte = _publicar(
        tmp_path, [_timeout, sesion_abierta(), completado(), video_remoto()]
    )
    assert salida.job.state is EstadoPublicacion.completado
    # El POST se intentó dos veces —eso es el reintento—, pero el contenido
    # viajó una sola vez: un timeout al abrir sesión no envía bytes.
    assert transporte.sesiones_iniciadas == 2
    assert transporte.fragmentos_enviados == 1


@pytest.mark.parametrize("codigo", [429, 500, 502, 503, 504])
def test_los_codigos_transitorios_se_reintentan(tmp_path, codigo):
    salida, _ = _publicar(
        tmp_path, [error(codigo), sesion_abierta(), completado(), video_remoto()]
    )
    assert salida.job.state is EstadoPublicacion.completado


def test_el_reintento_se_agota_en_tres_intentos(tmp_path):
    salida, transporte = _publicar(tmp_path, [error(503)])
    assert salida.job.state is EstadoPublicacion.fallido
    assert len(transporte.llamadas) == MAX_INTENTOS


# ---------------------------------------------------------------------------
# 10-11. Lo permanente no se reintenta
# ---------------------------------------------------------------------------


def test_un_400_no_se_reintenta(tmp_path):
    salida, transporte = _publicar(tmp_path, [error(400)])
    assert salida.job.state is EstadoPublicacion.fallido
    assert len(transporte.llamadas) == 1, "un error permanente se reintentó"


def test_oauth_invalido_no_se_reintenta(tmp_path):
    salida, transporte = _publicar(tmp_path, [error(401)])
    assert salida.job.state is EstadoPublicacion.fallido
    assert len(transporte.llamadas) == 1
    assert salida.result is not None
    assert salida.result.status is EstadoPublicacion.fallido


def test_alcance_insuficiente_no_se_reintenta(tmp_path):
    salida, transporte = _publicar(
        tmp_path, [error(403, "insufficientPermissions")]
    )
    assert salida.job.state is EstadoPublicacion.fallido
    assert len(transporte.llamadas) == 1


# ---------------------------------------------------------------------------
# 12. Sesión con progreso: se continúa la misma
# ---------------------------------------------------------------------------


def test_una_sesion_con_progreso_se_continua_no_se_recrea(tmp_path):
    contenido = b"z" * (MULTIPLO_FRAGMENTO * 2)
    gate = _gate(tmp_path, contenido)
    salida, transporte = _publicar(
        tmp_path,
        [
            sesion_abierta(),
            respuesta_perdida(final=False, bytes_enviados=0),
            incompleto(MULTIPLO_FRAGMENTO),  # la reconciliación ve progreso
            completado(),                     # se continúa y termina
            video_remoto(),
        ],
        gate=gate,
        tamano_fragmento=MULTIPLO_FRAGMENTO,
    )
    assert salida.job.state is EstadoPublicacion.completado
    assert transporte.sesiones_iniciadas == 1, "se abrió una segunda sesión"


# ---------------------------------------------------------------------------
# 13-14. Respuesta perdida -> reconciliación
# ---------------------------------------------------------------------------


def test_respuesta_perdida_en_fragmento_intermedio_va_a_reconciliacion(tmp_path):
    contenido = b"z" * (MULTIPLO_FRAGMENTO * 2)
    gate = _gate(tmp_path, contenido)
    salida, transporte = _publicar(
        tmp_path,
        [
            sesion_abierta(),
            respuesta_perdida(final=False, bytes_enviados=0),
            incompleto(MULTIPLO_FRAGMENTO),
            completado(),
            video_remoto(),
        ],
        gate=gate,
        tamano_fragmento=MULTIPLO_FRAGMENTO,
    )
    assert salida.job.state is EstadoPublicacion.completado
    assert transporte.sesiones_iniciadas == 1


def test_respuesta_perdida_en_el_ultimo_fragmento_va_a_reconciliacion(tmp_path):
    """El caso del enunciado: PUT final, YouTube acepta, la conexión muere."""
    salida, transporte = _publicar(
        tmp_path,
        [
            sesion_abierta(),
            respuesta_perdida(final=True, bytes_enviados=0),
            completado(),   # la reconciliación encuentra el vídeo
            video_remoto(),
        ],
    )
    assert salida.job.state is EstadoPublicacion.completado
    assert salida.result is not None
    assert salida.result.video_id == VIDEO_ID
    assert transporte.sesiones_iniciadas == 1, "se subió dos veces"


# ---------------------------------------------------------------------------
# 15-16. Confirme o no confirme el proveedor
# ---------------------------------------------------------------------------


def test_si_el_proveedor_confirma_el_video_no_hay_segunda_subida(tmp_path):
    salida, transporte = _publicar(
        tmp_path,
        [sesion_abierta(), respuesta_perdida(final=True), completado(), video_remoto()],
    )
    assert transporte.sesiones_iniciadas == 1
    assert transporte.fragmentos_enviados == 1
    assert salida.job.state is EstadoPublicacion.completado


def test_si_el_proveedor_no_confirma_acaba_en_needs_review(tmp_path):
    salida, transporte = _publicar(
        tmp_path,
        [sesion_abierta(), respuesta_perdida(final=True), error(404)],
    )
    assert salida.job.state is EstadoPublicacion.revision_pendiente
    assert salida.result is not None
    assert salida.result.status is EstadoPublicacion.revision_pendiente
    assert salida.result.video_id is None
    assert salida.result.reconciliation is not None
    assert (
        salida.result.reconciliation.outcome is DesenlaceReconciliacion.desconocido
    )


# ---------------------------------------------------------------------------
# 17. UNKNOWN jamás crea una segunda sesión
# ---------------------------------------------------------------------------


def test_unknown_no_crea_una_segunda_sesion(tmp_path):
    salida, transporte = _publicar(
        tmp_path,
        [sesion_abierta(), respuesta_perdida(final=True), error(404)],
    )
    assert transporte.sesiones_iniciadas == 1
    assert transporte.fragmentos_enviados == 1
    assert salida.job.state is EstadoPublicacion.revision_pendiente


def test_el_needs_review_por_incertidumbre_explica_por_que(tmp_path):
    salida, _ = _publicar(
        tmp_path, [sesion_abierta(), respuesta_perdida(final=True), error(404)]
    )
    assert "duplicado" in (salida.job.reason or "")


# ---------------------------------------------------------------------------
# 18. CONFIRMED_NOT_UPLOADED sí permite una sesión nueva
# ---------------------------------------------------------------------------


def test_si_consta_que_no_se_subio_se_abre_una_sesion_nueva(tmp_path):
    salida, transporte = _publicar(
        tmp_path,
        [
            sesion_abierta(),
            respuesta_perdida(final=False, bytes_enviados=0),
            incompleto(0),        # el proveedor: cero bytes recibidos
            sesion_abierta(),     # se permite abrir otra
            completado(),
            video_remoto(),
        ],
    )
    assert salida.job.state is EstadoPublicacion.completado
    assert transporte.sesiones_iniciadas == 2


# ---------------------------------------------------------------------------
# 19. Integridad al reanudar
# ---------------------------------------------------------------------------


def test_si_el_archivo_cambia_al_reanudar_acaba_en_needs_review(tmp_path):
    """Nunca se continúa una sesión con un archivo distinto."""
    gate = _gate(tmp_path, b"z" * (MULTIPLO_FRAGMENTO * 2))
    ruta = gate.video_path
    assert ruta is not None

    def _progreso_y_sabotaje(_):
        # Entre la reconciliación y la reanudación, el archivo cambia.
        ruta.write_bytes(b"y" * (MULTIPLO_FRAGMENTO * 2))
        from app.adapters.youtube.upload import RespuestaHTTP

        return RespuestaHTTP(
            status=308, headers={"Range": f"bytes=0-{MULTIPLO_FRAGMENTO - 1}"}
        )

    salida, transporte = _publicar(
        tmp_path,
        [
            sesion_abierta(),
            respuesta_perdida(final=False, bytes_enviados=0),
            _progreso_y_sabotaje,
        ],
        gate=gate,
        tamano_fragmento=MULTIPLO_FRAGMENTO,
    )
    assert salida.job.state is EstadoPublicacion.fallido
    assert "INTEGRITY_MISMATCH" in (salida.job.reason or "")
    assert transporte.sesiones_iniciadas == 1


# ---------------------------------------------------------------------------
# 20. Verificación
# ---------------------------------------------------------------------------


def test_una_verificacion_que_no_cuadra_no_llega_a_completed(tmp_path):
    salida, _ = _publicar(
        tmp_path,
        [sesion_abierta(), completado(), video_remoto(title="Otro título")],
    )
    assert salida.job.state is EstadoPublicacion.revision_pendiente
    assert salida.result is not None
    assert salida.result.status is not EstadoPublicacion.completado
    assert salida.result.completed_at is None
    assert salida.result.video_id == VIDEO_ID


def test_una_privacidad_distinta_de_la_pedida_no_llega_a_completed(tmp_path):
    salida, _ = _publicar(
        tmp_path, [sesion_abierta(), completado(), video_remoto(privacy="public")]
    )
    assert salida.job.state is EstadoPublicacion.revision_pendiente
    assert "privacidad" in (salida.job.reason or "")


def test_si_no_se_puede_verificar_no_se_da_por_completada(tmp_path):
    salida, _ = _publicar(tmp_path, [sesion_abierta(), completado(), error(503)])
    assert salida.job.state is EstadoPublicacion.revision_pendiente
    assert salida.result is not None
    assert salida.result.video_id == VIDEO_ID, "el vídeo existe: no es un fallo"
    assert salida.result.completed_at is None


def test_completed_exige_haber_verificado(tmp_path):
    """El HTTP terminando bien no basta: hay que volver a preguntar."""
    _, transporte = _publicar(
        tmp_path, [sesion_abierta(), completado(), video_remoto()]
    )
    lecturas = [ll for ll in transporte.llamadas if ll.metodo == "GET"]
    assert len(lecturas) == 1, "no se verificó contra el proveedor"


# ---------------------------------------------------------------------------
# 21. Privacidad
# ---------------------------------------------------------------------------


def test_la_privacidad_private_se_preserva_de_extremo_a_extremo(tmp_path):
    import json

    salida, transporte = _publicar(
        tmp_path, [sesion_abierta(), completado(), video_remoto()]
    )
    enviado = json.loads(transporte.llamadas[0].cuerpo.decode("utf-8"))
    assert enviado["status"]["privacyStatus"] == "private"
    assert salida.result is not None
    assert salida.result.privacy_status is Privacidad.privado


# ---------------------------------------------------------------------------
# EL TEST CRÍTICO DE ARQUITECTURA
# ---------------------------------------------------------------------------


def test_critico_provider_acepta_cliente_pierde_respuesta_no_hay_segunda_subida(
    tmp_path,
):
    """El test que decide si Gate 7.2 está terminado.

    Escenario:
      1. el proveedor acepta la subida;
      2. el cliente pierde la respuesta;
      3. el publisher no puede confirmarlo;
      4. el estado queda ``UNKNOWN``;
      5. **no se inicia una segunda subida automáticamente**.
    """
    salida, transporte = _publicar(
        tmp_path,
        [
            sesion_abierta(),
            respuesta_perdida(final=True, bytes_enviados=0),
            # El proveedor ya no reconoce la sesión: no se puede saber si aceptó.
            error(410),
        ],
    )

    # 4. El desenlace es desconocido, no «fallido».
    assert salida.result is not None
    assert salida.result.reconciliation is not None
    assert (
        salida.result.reconciliation.outcome is DesenlaceReconciliacion.desconocido
    )

    # 5. No hubo segunda subida: una sola sesión y un solo fragmento.
    assert transporte.sesiones_iniciadas == 1, "se inició una segunda subida"
    assert transporte.fragmentos_enviados == 1, "se reenviaron los bytes"

    # El trabajo queda donde lo tiene que mirar una persona.
    assert salida.job.state is EstadoPublicacion.revision_pendiente
    assert salida.job.reason


def test_critico_tras_reiniciar_el_proceso_no_se_puede_resubir_sin_reconciliar(
    tmp_path,
):
    """La otra mitad: el publisher reinicia y el estado sigue siendo ``UNKNOWN``.

    Un trabajo en ``NEEDS_REVIEW`` no puede volver a ``READY`` ni entrar en
    ``UPLOADING``: la tabla de transiciones lo impide, así que una segunda
    subida automática **no es ni representable**.
    """
    salidas = TRANSICIONES_PUBLICACION[EstadoPublicacion.revision_pendiente]
    assert EstadoPublicacion.listo not in salidas
    assert EstadoPublicacion.subiendo not in salidas

    job = PublishJob(
        run_id=uuid4(),
        state=EstadoPublicacion.revision_pendiente,
        attempt=1,
        idempotency_key=clave_idempotencia(uuid4()),
        reason="no se pudo determinar si la subida llegó a completarse",
    )
    with pytest.raises(ValueError, match="transición no permitida"):
        job.transicionar(EstadoPublicacion.subiendo, metadata=metadata())
    with pytest.raises(ValueError, match="transición no permitida"):
        job.transicionar(EstadoPublicacion.listo)
