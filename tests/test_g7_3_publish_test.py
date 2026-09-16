"""El punto de entrada de Gate 7.3, comprobado sin tocar la red.

Lo que aquí importa no es el protocolo —ese ya lo cubre Gate 7.2— sino que el
script no pueda saltarse ninguna de sus propias precondiciones: el alcance, la
privacidad, el tamaño mínimo para que la prueba del resumable pruebe algo, y la
puerta de publicación.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from uuid import uuid4

import pytest

RAIZ = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "g7_3_publish_test", RAIZ / "scripts" / "g7_3_publish_test.py"
)
g73 = importlib.util.module_from_spec(_spec)
sys.modules["g7_3_publish_test"] = g73
_spec.loader.exec_module(g73)

from app.adapters.youtube.auth import ComprobacionAuth, ResultadoAuth  # noqa: E402
from app.adapters.youtube.upload import (  # noqa: E402
    MULTIPLO_FRAGMENTO,
    URL_SUBIDA,
    RespuestaHTTP,
)
from app.contracts.models import (  # noqa: E402
    EstadoDivulgacionIA,
    Privacidad,
)

SCOPE_GESTION = "https://www.googleapis.com/auth/youtube"
SCOPE_SUBIDA = "https://www.googleapis.com/auth/youtube.upload"


# ---------------------------------------------------------------------------
# Metadata de la prueba
# ---------------------------------------------------------------------------


def test_la_metadata_es_privada():
    meta = g73.construir_metadata(uuid4())
    assert meta.privacy_status is Privacidad.privado


def test_la_metadata_declara_todo_lo_que_no_tiene_valor_por_defecto():
    meta = g73.construir_metadata(uuid4())
    assert meta.made_for_kids is False
    assert meta.category_id is None
    assert meta.language == "es"
    assert meta.ai_disclosure.status is EstadoDivulgacionIA.no_requerida
    assert meta.ai_disclosure.decided_by == "human"


def test_el_motivo_de_la_divulgacion_dice_por_que_no_hace_falta():
    """Las cinco afirmaciones que sostienen la decisión, explícitas."""
    razon = g73.construir_metadata(uuid4()).ai_disclosure.reason.lower()
    for afirmacion in (
        "visuales generados no realistas",
        "narración",
        "sintética",
        "persona real",
        "acontecimiento real",
        "lugar real",
        "fotorrealista",
    ):
        assert afirmacion in razon, afirmacion


def test_la_decision_de_divulgacion_no_se_presenta_como_default_global():
    razon = g73.construir_metadata(uuid4()).ai_disclosure.reason.lower()
    assert "solo a este fixture" in razon
    assert "no es un valor por defecto" in razon


def test_el_titulo_y_la_descripcion_identifican_la_prueba(tmp_path):
    """Criterio Q: hay que poder encontrar el vídeo y borrarlo."""
    run_id = uuid4()
    meta = g73.construir_metadata(run_id)
    assert "G7.3" in meta.title
    assert str(run_id) in meta.description
    assert "privad" in meta.description.lower()


def test_la_metadata_valida_contra_el_contrato():
    meta = g73.construir_metadata(uuid4())
    assert meta.permite_publicacion_automatica


# ---------------------------------------------------------------------------
# Precondiciones que el script se impone
# ---------------------------------------------------------------------------


def test_se_rechaza_una_privacidad_que_no_sea_privada():
    meta = g73.construir_metadata(uuid4())
    for publica in (Privacidad.publico, Privacidad.no_listado):
        alterada = meta.model_copy(update={"privacy_status": publica})
        with pytest.raises(g73.PruebaAbortada, match="solo publica en privado"):
            g73.exigir_privacidad_privada(alterada)


def test_la_privacidad_privada_pasa():
    g73.exigir_privacidad_privada(g73.construir_metadata(uuid4()))


def test_un_video_que_cabe_en_un_fragmento_aborta_la_prueba():
    """Sin al menos dos PUT, la prueba del resumable no probaría el resumable."""
    with pytest.raises(g73.PruebaAbortada, match="al menos dos PUT"):
        g73.exigir_tamano_para_varios_fragmentos(MULTIPLO_FRAGMENTO)


def test_un_video_mayor_que_el_fragmento_pasa():
    g73.exigir_tamano_para_varios_fragmentos(MULTIPLO_FRAGMENTO + 1)


def test_el_fragmento_es_el_minimo_del_protocolo():
    assert g73.TAMANO_FRAGMENTO == MULTIPLO_FRAGMENTO == 256 * 1024
    assert g73.TAMANO_FRAGMENTO % MULTIPLO_FRAGMENTO == 0


# ---------------------------------------------------------------------------
# Preflight: el alcance se comprueba por igualdad exacta
# ---------------------------------------------------------------------------


def _comprobacion(resultado, *, scope: str, canal="UC123", esperado="UC123"):
    return ComprobacionAuth(
        resultado=resultado,
        detalle="prueba",
        channel_id=canal,
        expected_channel_id=esperado,
        scope_concedido=scope,
    )


def _preflight_con(monkeypatch, comprobacion):
    monkeypatch.setattr(g73, "comprobar_autenticacion", lambda *a, **k: comprobacion)
    return g73.preflight(object())


def test_el_preflight_acepta_el_alcance_requerido(monkeypatch):
    informe = _preflight_con(
        monkeypatch, _comprobacion(ResultadoAuth.autenticado, scope=SCOPE_GESTION)
    )
    assert informe["scope_ok"] is True
    assert informe["required_scope"] == g73.SCOPE_REQUERIDO


def test_el_alcance_de_subida_no_basta_para_esta_prueba(monkeypatch):
    """``…/youtube.upload`` **contiene** ``…/youtube`` como texto y no sirve."""
    with pytest.raises(g73.PruebaAbortada, match="alcance concedido no incluye"):
        _preflight_con(
            monkeypatch, _comprobacion(ResultadoAuth.autenticado, scope=SCOPE_SUBIDA)
        )


def test_el_alcance_se_compara_como_lista_no_como_subcadena(monkeypatch):
    """Un token con varios alcances vale si el requerido está entre ellos."""
    informe = _preflight_con(
        monkeypatch,
        _comprobacion(
            ResultadoAuth.autenticado,
            scope=f"{SCOPE_SUBIDA} {SCOPE_GESTION} https://www.googleapis.com/auth/userinfo.email",
        ),
    )
    assert informe["scope_ok"] is True
    assert SCOPE_GESTION in informe["granted_scopes"]


def test_sin_alcance_declarado_se_aborta(monkeypatch):
    with pytest.raises(g73.PruebaAbortada, match="alcance concedido no incluye"):
        _preflight_con(
            monkeypatch, _comprobacion(ResultadoAuth.autenticado, scope="")
        )


@pytest.mark.parametrize(
    "resultado",
    [
        ResultadoAuth.credenciales_ausentes,
        ResultadoAuth.autorizacion_invalida,
        ResultadoAuth.alcance_insuficiente,
    ],
)
def test_un_auth_check_que_no_autentica_aborta(monkeypatch, resultado):
    with pytest.raises(g73.PruebaAbortada, match="no autenticó"):
        _preflight_con(monkeypatch, _comprobacion(resultado, scope=SCOPE_GESTION))


def test_un_canal_distinto_del_esperado_aborta(monkeypatch):
    """``WRONG_CHANNEL`` lo para ya la primera puerta, antes de mirar el canal."""
    with pytest.raises(g73.PruebaAbortada, match="WRONG_CHANNEL"):
        _preflight_con(
            monkeypatch,
            _comprobacion(
                ResultadoAuth.canal_incorrecto,
                scope=SCOPE_GESTION,
                canal="UC-otro",
                esperado="UC123",
            ),
        )


def test_el_informe_del_preflight_no_lleva_credenciales(monkeypatch):
    informe = _preflight_con(
        monkeypatch, _comprobacion(ResultadoAuth.autenticado, scope=SCOPE_GESTION)
    )
    import json

    texto = json.dumps(informe).lower()
    for prohibido in ("refresh_token", "client_secret", "access_token", "bearer", "ya29"):
        assert prohibido not in texto, prohibido


# ---------------------------------------------------------------------------
# Transporte instrumentado
# ---------------------------------------------------------------------------


class _InternoFalso:
    def __init__(self):
        self.vistas = []

    def peticion(self, metodo, url, *, headers, cuerpo=None, timeout_s=30):
        self.vistas.append((metodo, url))
        return RespuestaHTTP(status=200, headers={"Location": "https://secreto/x"})


def test_el_transporte_cuenta_sesiones_y_fragmentos():
    interno = _InternoFalso()
    t = g73.TransporteContado(interno=interno)

    t.peticion("POST", f"{URL_SUBIDA}?uploadType=resumable", headers={}, cuerpo=b"{}")
    t.peticion("PUT", "https://secreto/x", headers={}, cuerpo=b"a" * 100)
    t.peticion("PUT", "https://secreto/x", headers={}, cuerpo=b"b" * 100)
    t.peticion("PUT", "https://secreto/x", headers={}, cuerpo=b"")
    t.peticion("GET", "https://www.googleapis.com/youtube/v3/videos?id=x", headers={})

    resumen = t.resumen()
    assert resumen["session_post_count"] == 1
    assert resumen["chunk_put_count"] == 2
    assert resumen["progress_query_count"] == 1
    assert resumen["read_count"] == 1
    assert resumen["bytes_uploaded"] == 200


def test_la_traza_del_transporte_no_guarda_urls_ni_cabeceras():
    """El URL de sesión es secreto operativo: no puede acabar en un artifact."""
    import json

    interno = _InternoFalso()
    t = g73.TransporteContado(interno=interno)
    t.peticion(
        "PUT",
        "https://upload.example/session?upload_id=SECRETO-123",
        headers={"Authorization": "Bearer ya29.TOKEN"},
        cuerpo=b"datos",
    )
    texto = json.dumps(t.resumen())
    assert "SECRETO-123" not in texto
    assert "upload_id" not in texto
    assert "ya29" not in texto
    assert "Authorization" not in texto
    assert "Bearer" not in texto


def test_el_transporte_no_altera_la_respuesta():
    interno = _InternoFalso()
    t = g73.TransporteContado(interno=interno)
    respuesta = t.peticion("GET", "https://x/y", headers={})
    assert respuesta.status == 200
    assert interno.vistas == [("GET", "https://x/y")]


# ---------------------------------------------------------------------------
# El script no reimplementa el publisher
# ---------------------------------------------------------------------------


def test_el_script_no_duplica_el_protocolo():
    """El resumable, la reconciliación y la máquina de estados viven en G7.2."""
    fuente = (RAIZ / "scripts" / "g7_3_publish_test.py").read_text(encoding="utf-8")
    for prohibido in (
        "Content-Range",
        "iniciar_sesion(",
        "subir_fragmento(",
        "consultar_progreso(",
        "reconciliar(",
        "transicionar(",
        "videos.insert",
    ):
        assert prohibido not in fuente, f"el script reimplementa {prohibido}"


def test_el_script_usa_el_publisher_de_g72():
    fuente = (RAIZ / "scripts" / "g7_3_publish_test.py").read_text(encoding="utf-8")
    assert "from app.adapters.youtube.publisher import publicar" in fuente
    assert "from app.pipeline.publicacion import evaluar_precondiciones" in fuente


def test_el_script_no_puede_publicar_en_publico():
    fuente = (RAIZ / "scripts" / "g7_3_publish_test.py").read_text(encoding="utf-8")
    assert "Privacidad.publico" not in fuente
    assert "Privacidad.no_listado" not in fuente


def test_el_script_no_borra_videos_ni_pide_processing_details():
    fuente = (RAIZ / "scripts" / "g7_3_publish_test.py").read_text(encoding="utf-8")
    assert "videos.delete" not in fuente
    assert "processingDetails" not in fuente


# ---------------------------------------------------------------------------
# El workflow: lo que no puede cambiar sin que alguien se entere
#
# Las comprobaciones son textuales a propósito. Hacerlas con un parser de YAML
# exigiría PyYAML, que no está en `requirements-dev.txt`: los tests se
# omitirían en CI y el workflow quedaría sin vigilancia justo donde más
# importa, que es el sitio desde el que se sube un vídeo real.
# ---------------------------------------------------------------------------

WORKFLOW = RAIZ / ".github" / "workflows" / "youtube-publish-test.yml"


def _texto() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _lineas_de_codigo() -> list[str]:
    """Las líneas que no son comentario: la prosa puede nombrar lo prohibido."""
    return [
        linea
        for linea in _texto().splitlines()
        if not linea.strip().startswith("#")
    ]


def test_el_workflow_existe():
    assert WORKFLOW.is_file()
    assert "publish-test:" in _texto()


def test_el_workflow_solo_se_lanza_a_mano():
    """Subir un vídeo real no puede dispararlo un push ni un cron."""
    codigo = _lineas_de_codigo()
    disparadores = [
        linea.strip().rstrip(":")
        for linea in codigo
        if re.fullmatch(r"  [a-z_]+:", linea)
    ]
    assert "workflow_dispatch" in disparadores
    for prohibido in ("push", "pull_request", "schedule", "workflow_run"):
        assert prohibido not in disparadores, prohibido


def test_el_workflow_exige_la_confirmacion_literal():
    texto = _texto()
    assert "confirmacion:" in texto
    assert "required: true" in texto
    assert '!= "SUBIR"' in texto
    # Y la comprobación es el primer paso, antes de cualquier otra cosa.
    pasos = re.findall(r"^      - name: (.+)$", texto, re.M)
    assert pasos, "no se reconocieron los pasos"
    assert "Confirmación" in pasos[0], pasos[0]


def test_el_workflow_tiene_permisos_minimos():
    assert re.search(r"^permissions:\n  contents: read$", _texto(), re.M)


def test_los_secrets_solo_llegan_a_los_pasos_que_los_necesitan():
    """Dos pasos: el preflight y la subida. Ninguno más."""
    texto = _texto()
    assert texto.count("secrets.YOUTUBE_CLIENT_ID") == 2
    assert texto.count("secrets.YOUTUBE_CLIENT_SECRET") == 2
    assert texto.count("secrets.YOUTUBE_REFRESH_TOKEN") == 2
    assert texto.count("secrets.EXPECTED_YOUTUBE_CHANNEL_ID") == 2


def test_el_workflow_no_crea_secrets_nuevos():
    usados = set(re.findall(r"secrets\.([A-Z_]+)", _texto()))
    assert usados == {
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "EXPECTED_YOUTUBE_CHANNEL_ID",
    }, usados


def test_el_workflow_no_traza_el_entorno():
    """``set -x`` volcaría las variables, secrets incluidos, al log."""
    for linea in _lineas_de_codigo():
        despojada = linea.strip()
        assert "set -x" not in despojada, linea
        assert "printenv" not in despojada, linea
        # El comando `env` vuelca todas las variables. La clave YAML `env:`,
        # que es donde se declaran, no tiene nada que ver.
        assert despojada != "env", linea
        assert not despojada.startswith("env |"), linea
        assert not despojada.startswith("env >"), linea


def test_el_workflow_usa_set_mas_x_en_los_pasos_sensibles():
    assert _texto().count("set +x") >= 2


def test_el_workflow_no_reintenta_por_su_cuenta():
    """Un reintento automático sobre una subida incierta la duplicaría."""
    codigo = "\n".join(_lineas_de_codigo()).lower()
    for prohibido in ("retry", "continue-on-error: true", "max_attempts"):
        assert prohibido not in codigo, prohibido


def test_el_workflow_no_cancela_una_subida_en_curso():
    """Cancelarla a mitad dejaría justo la incertidumbre que el Gate evita."""
    assert "cancel-in-progress: false" in _texto()


def test_el_workflow_comprueba_el_alcance_por_igualdad_exacta():
    texto = _texto()
    assert "REQUERIDO not in concedidos" in texto
    assert 'REQUERIDO = "https://www.googleapis.com/auth/youtube"' in texto
    assert "scope_granted" in texto


def test_el_workflow_exige_un_solo_post_y_dos_fragmentos():
    """Los criterios N y E, comprobados por el propio workflow."""
    texto = _texto()
    assert 'transporte.get("session_post_count") == 1' in texto
    assert '(transporte.get("chunk_put_count") or 0) >= 2' in texto
    assert 'resultado.get("privacy_status") == "private"' in texto


def test_el_workflow_busca_secretos_en_los_artifacts():
    texto = _texto()
    for prohibido in ("refresh_token", "client_secret", "access_token", "session_url"):
        assert prohibido in texto, prohibido


def test_el_workflow_avisa_de_revisar_a_mano_antes_de_repetir():
    texto = _texto()
    assert "NO vuelvas a lanzar" in texto
    assert "duplicar" in texto


def test_el_workflow_no_publica_en_publico():
    codigo = "\n".join(_lineas_de_codigo())
    assert '"public"' not in codigo
    assert '"unlisted"' not in codigo
