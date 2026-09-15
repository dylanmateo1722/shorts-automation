"""El protocolo resumable, comprobado entero sin tocar la red.

Lo que aquí se prueba es el transporte: que los fragmentos salgan con el
``Content-Range`` que el protocolo exige, que un ``308`` se lea como progreso y
no como fallo, y que los errores se clasifiquen en la taxonomía del proyecto.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.contracts.models import UploadSession
from app.core.errors import (
    ErrorPermanente,
    ErrorTransitorio,
    LimiteDeTasa,
    RespuestaInvalida,
)
from app.adapters.youtube.auth import AutorizacionInvalida
from app.adapters.youtube.upload import (
    MULTIPLO_FRAGMENTO,
    RespuestaPerdida,
    SesionDesconocida,
    consultar_progreso,
    huella_metadata,
    iniciar_sesion,
    leer_video,
    subir_fragmento,
    url_publica,
)

from conftest_g72 import (
    SESSION_URL,
    VIDEO_ID,
    TransporteFalso,
    completado,
    error,
    incompleto,
    metadata,
    sesion_abierta,
    token,
    video_remoto,
)

TOTAL = 3 * MULTIPLO_FRAGMENTO


def _sesion(total: int = TOTAL, confirmados: int = 0) -> UploadSession:
    return UploadSession(
        session_url=SESSION_URL,
        run_id=uuid4(),
        attempt=1,
        video_sha256="a" * 64,
        metadata_sha256="b" * 64,
        total_bytes=total,
        bytes_confirmed=confirmados,
    )


# ---------------------------------------------------------------------------
# Iniciar sesión
# ---------------------------------------------------------------------------


def test_iniciar_sesion_devuelve_el_location_como_url_de_sesion():
    transporte = TransporteFalso(guion=[sesion_abierta()])
    sesion = iniciar_sesion(
        token(),
        metadata(),
        run_id=uuid4(),
        attempt=1,
        total_bytes=TOTAL,
        video_sha256="a" * 64,
        transporte=transporte,
    )
    assert sesion.session_url == SESSION_URL
    assert sesion.bytes_confirmed == 0
    assert sesion.total_bytes == TOTAL


def test_iniciar_sesion_declara_el_tamano_y_el_tipo():
    transporte = TransporteFalso(guion=[sesion_abierta()])
    iniciar_sesion(
        token(),
        metadata(),
        run_id=uuid4(),
        attempt=1,
        total_bytes=TOTAL,
        video_sha256="a" * 64,
        transporte=transporte,
    )
    llamada = transporte.llamadas[0]
    assert llamada.metodo == "POST"
    assert "uploadType=resumable" in llamada.url
    assert llamada.headers["X-Upload-Content-Length"] == str(TOTAL)
    assert llamada.headers["X-Upload-Content-Type"] == "video/mp4"


def test_iniciar_sesion_sin_location_es_respuesta_invalida():
    from app.adapters.youtube.upload import RespuestaHTTP

    transporte = TransporteFalso(guion=[lambda _: RespuestaHTTP(status=200)])
    with pytest.raises(RespuestaInvalida):
        iniciar_sesion(
            token(),
            metadata(),
            run_id=uuid4(),
            attempt=1,
            total_bytes=TOTAL,
            video_sha256="a" * 64,
            transporte=transporte,
        )


def test_no_se_abre_sesion_para_un_archivo_vacio():
    with pytest.raises(ErrorPermanente):
        iniciar_sesion(
            token(),
            metadata(),
            run_id=uuid4(),
            attempt=1,
            total_bytes=0,
            video_sha256="a" * 64,
            transporte=TransporteFalso(guion=[sesion_abierta()]),
        )


def test_la_privacidad_solicitada_viaja_en_el_cuerpo():
    """El MVP publica en privado y eso tiene que verse en lo que se envía."""
    import json

    transporte = TransporteFalso(guion=[sesion_abierta()])
    iniciar_sesion(
        token(),
        metadata(),
        run_id=uuid4(),
        attempt=1,
        total_bytes=TOTAL,
        video_sha256="a" * 64,
        transporte=transporte,
    )
    cuerpo = json.loads(transporte.llamadas[0].cuerpo.decode("utf-8"))
    assert cuerpo["status"]["privacyStatus"] == "private"


# ---------------------------------------------------------------------------
# Fragmentos
# ---------------------------------------------------------------------------


def test_fragmento_intermedio_lleva_su_content_range():
    sesion = _sesion()
    transporte = TransporteFalso(guion=[incompleto(MULTIPLO_FRAGMENTO)])
    nueva, estado = subir_fragmento(
        sesion, b"x" * MULTIPLO_FRAGMENTO, offset=0, transporte=transporte
    )
    llamada = transporte.llamadas[0]
    assert llamada.headers["Content-Range"] == f"bytes 0-{MULTIPLO_FRAGMENTO - 1}/{TOTAL}"
    assert not estado.completada
    assert nueva.bytes_confirmed == MULTIPLO_FRAGMENTO


def test_ultimo_fragmento_devuelve_el_video_id():
    sesion = _sesion(total=MULTIPLO_FRAGMENTO)
    transporte = TransporteFalso(guion=[completado()])
    nueva, estado = subir_fragmento(
        sesion, b"x" * MULTIPLO_FRAGMENTO, offset=0, transporte=transporte
    )
    assert estado.completada and estado.video_id == VIDEO_ID
    assert nueva.completa


def test_un_fragmento_intermedio_no_multiplo_se_rechaza():
    """El protocolo lo exige; saltárselo rompe la sesión a mitad."""
    sesion = _sesion()
    with pytest.raises(ErrorPermanente, match="múltiplo"):
        subir_fragmento(
            sesion, b"x" * 1000, offset=0, transporte=TransporteFalso(guion=[incompleto(1000)])
        )


def test_un_fragmento_desalineado_se_rechaza():
    sesion = _sesion(confirmados=MULTIPLO_FRAGMENTO)
    with pytest.raises(ErrorPermanente, match="hueco"):
        subir_fragmento(
            sesion,
            b"x" * MULTIPLO_FRAGMENTO,
            offset=0,
            transporte=TransporteFalso(guion=[incompleto(0)]),
        )


def test_un_fragmento_que_se_pasa_del_final_se_rechaza():
    sesion = _sesion(total=MULTIPLO_FRAGMENTO)
    with pytest.raises(ErrorPermanente):
        subir_fragmento(
            sesion,
            b"x" * (MULTIPLO_FRAGMENTO * 2),
            offset=0,
            transporte=TransporteFalso(guion=[incompleto(0)]),
        )


def test_una_perdida_de_respuesta_se_propaga_con_su_contexto():
    """No es un fallo de subida: es una subida cuyo desenlace no consta."""
    from conftest_g72 import respuesta_perdida

    sesion = _sesion(total=MULTIPLO_FRAGMENTO)
    transporte = TransporteFalso(guion=[respuesta_perdida(final=True)])
    with pytest.raises(RespuestaPerdida) as capturado:
        subir_fragmento(
            sesion, b"x" * MULTIPLO_FRAGMENTO, offset=0, transporte=transporte
        )
    assert capturado.value.fragmento_final is True


def test_un_timeout_al_enviar_se_trata_como_respuesta_perdida():
    """El proveedor pudo haber recibido los bytes; no se puede dar por fallido."""
    from app.core.errors import TiempoAgotado

    def _timeout(_):
        raise TiempoAgotado("sin respuesta")

    sesion = _sesion(total=MULTIPLO_FRAGMENTO)
    with pytest.raises(RespuestaPerdida):
        subir_fragmento(
            sesion,
            b"x" * MULTIPLO_FRAGMENTO,
            offset=0,
            transporte=TransporteFalso(guion=[_timeout]),
        )


# ---------------------------------------------------------------------------
# Consulta de progreso
# ---------------------------------------------------------------------------


def test_consultar_progreso_usa_el_content_range_de_consulta():
    sesion = _sesion()
    transporte = TransporteFalso(guion=[incompleto(MULTIPLO_FRAGMENTO)])
    estado = consultar_progreso(sesion, transporte=transporte)
    assert transporte.llamadas[0].headers["Content-Range"] == f"bytes */{TOTAL}"
    assert estado.bytes_confirmed == MULTIPLO_FRAGMENTO
    assert not estado.completada


def test_consultar_progreso_sin_range_son_cero_bytes():
    estado = consultar_progreso(_sesion(), transporte=TransporteFalso(guion=[incompleto(0)]))
    assert estado.bytes_confirmed == 0


def test_consultar_progreso_de_una_sesion_ya_completada_da_el_id():
    estado = consultar_progreso(_sesion(), transporte=TransporteFalso(guion=[completado()]))
    assert estado.confirmado and estado.video_id == VIDEO_ID


# ---------------------------------------------------------------------------
# Clasificación de errores
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("codigo", [500, 502, 503, 504])
def test_los_5xx_son_transitorios(codigo):
    with pytest.raises(ErrorTransitorio):
        consultar_progreso(_sesion(), transporte=TransporteFalso(guion=[error(codigo)]))


def test_el_429_es_limite_de_tasa():
    with pytest.raises(LimiteDeTasa):
        consultar_progreso(_sesion(), transporte=TransporteFalso(guion=[error(429)]))


@pytest.mark.parametrize("codigo", [404, 410])
def test_una_sesion_muerta_no_dice_si_el_video_existe(codigo):
    """Es permanente sobre la sesión y **no** concluyente sobre el vídeo."""
    with pytest.raises(SesionDesconocida, match="no dice si el vídeo"):
        consultar_progreso(_sesion(), transporte=TransporteFalso(guion=[error(codigo)]))


def test_el_401_es_autorizacion_invalida():
    with pytest.raises(AutorizacionInvalida):
        consultar_progreso(_sesion(), transporte=TransporteFalso(guion=[error(401)]))


def test_el_alcance_insuficiente_nombra_el_remedio():
    with pytest.raises(AutorizacionInvalida, match="alcance"):
        consultar_progreso(
            _sesion(),
            transporte=TransporteFalso(guion=[error(403, "insufficientPermissions")]),
        )


def test_un_400_es_permanente_y_no_transitorio():
    with pytest.raises(RespuestaInvalida) as capturado:
        consultar_progreso(_sesion(), transporte=TransporteFalso(guion=[error(400)]))
    assert not isinstance(capturado.value, ErrorTransitorio)


# ---------------------------------------------------------------------------
# Lectura posterior
# ---------------------------------------------------------------------------


def test_leer_video_devuelve_lo_observado():
    remoto = leer_video(
        token(), VIDEO_ID, transporte=TransporteFalso(guion=[video_remoto()])
    )
    assert remoto.video_id == VIDEO_ID
    assert remoto.privacy_status == "private"


def test_leer_un_video_que_no_esta_es_respuesta_invalida():
    import json

    from app.adapters.youtube.upload import RespuestaHTTP

    vacio = lambda _: RespuestaHTTP(status=200, body=json.dumps({"items": []}).encode())
    with pytest.raises(RespuestaInvalida):
        leer_video(token(), VIDEO_ID, transporte=TransporteFalso(guion=[vacio]))


def test_la_url_publica_se_construye_aqui():
    assert url_publica(VIDEO_ID) == f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_la_huella_de_metadata_cambia_con_el_titulo():
    a = metadata(title="Uno")
    b = metadata(title="Dos")
    assert huella_metadata(a) != huella_metadata(b)
    assert huella_metadata(a) == huella_metadata(metadata(title="Uno"))
