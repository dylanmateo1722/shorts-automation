"""Adaptador de MoneyPrinterTurbo.

Se ejecuta contra un motor falso que respeta el contrato real del CLI: mismos
flags, mismo JSON en stdout y mismos códigos de salida. El subproceso, el
manejo de rutas y el parseo son los de producción; lo único sustituido es el
render, que tardaría noventa segundos.
"""

from __future__ import annotations

import uuid

import pytest

from app.adapters.mpt import MPTAdapter
from app.contracts.models import EstadoRender, ModoAjuste, RelacionAspecto, RenderJob
from app.core.errors import EjecutableAusente, EntradaInvalida
from tests.conftest import ultima_invocacion


@pytest.fixture
def job(workspace, entrada_controlada) -> RenderJob:
    return RenderJob(
        run_id=workspace.run_id,
        task_id=workspace.run_id,
        script="guion de prueba",
        audio_path=workspace.relativa(entrada_controlada["audio"]),
        materials=[workspace.relativa(entrada_controlada["material"])],
    )


@pytest.fixture
def adaptador(settings_falsos, workspace) -> MPTAdapter:
    return MPTAdapter(settings_falsos, workspace)


# --- construcción del comando ---------------------------------------------


def test_argv_lleva_el_task_id_de_la_corrida(adaptador, job, workspace):
    argv = adaptador.construir_argv(job)
    assert "--task-id" in argv
    assert argv[argv.index("--task-id") + 1] == str(workspace.run_id)


def test_argv_convierte_rutas_relativas_en_absolutas(adaptador, job, workspace):
    """El subproceso corre con el directorio del motor como cwd."""
    argv = adaptador.construir_argv(job)
    audio = argv[argv.index("--custom-audio-file") + 1]
    materiales = argv[argv.index("--video-materials") + 1]
    assert audio.startswith("/") and audio.endswith("audio.mp3")
    assert materiales.startswith("/")
    assert str(workspace.dir) in audio


def test_argv_desactiva_los_subtitulos_del_motor(adaptador, job):
    """Los subtítulos son nuestros; el motor debe renderizar sin ellos."""
    argv = adaptador.construir_argv(job)
    assert "--no-subtitle-enabled" in argv
    assert "--subtitle-enabled" not in argv


def test_argv_respeta_aspecto_y_ajuste(adaptador, workspace, entrada_controlada):
    job = RenderJob(
        run_id=workspace.run_id, task_id=workspace.run_id, script="x",
        audio_path=workspace.relativa(entrada_controlada["audio"]),
        materials=[workspace.relativa(entrada_controlada["material"])],
        aspect=RelacionAspecto.vertical, fit_mode=ModoAjuste.contener,
    )
    argv = adaptador.construir_argv(job)
    assert argv[argv.index("--video-aspect") + 1] == "9:16"
    assert argv[argv.index("--video-fit-mode") + 1] == "contain"


# --- validación de la entrada ---------------------------------------------


def test_task_id_distinto_del_run_id_se_rechaza(adaptador, workspace, entrada_controlada):
    job = RenderJob(
        run_id=workspace.run_id, task_id=uuid.uuid4(), script="x",
        audio_path=workspace.relativa(entrada_controlada["audio"]),
        materials=[workspace.relativa(entrada_controlada["material"])],
    )
    with pytest.raises(EntradaInvalida, match="task_id"):
        adaptador.render(job)


def test_audio_inexistente_se_rechaza_antes_de_ejecutar(adaptador, job):
    job.audio_path = "input/no_existe.mp3"
    with pytest.raises(EntradaInvalida, match="audio"):
        adaptador.render(job)


def test_material_inexistente_se_rechaza(adaptador, job):
    job.materials = ["input/no_existe.mp4"]
    with pytest.raises(EntradaInvalida, match="material"):
        adaptador.render(job)


def test_motor_ausente_es_error_de_infraestructura(settings_falsos, workspace, job):
    (settings_falsos.raiz_mpt / "cli.py").unlink()
    with pytest.raises(EjecutableAusente, match="setup_mpt"):
        MPTAdapter(settings_falsos, workspace).render(job)


# --- ejecución -------------------------------------------------------------


def test_render_exitoso_devuelve_resultado_tipado(adaptador, job, workspace):
    resultado = adaptador.render(job)

    assert resultado.status is EstadoRender.exito
    assert resultado.exit_code == 0
    assert resultado.run_id == workspace.run_id
    assert resultado.width == 1080 and resultado.height == 1920
    assert resultado.fps == 30.0
    assert resultado.file_size_bytes and resultado.file_size_bytes > 0
    assert resultado.engine_commit is None or isinstance(resultado.engine_commit, str)


def test_la_salida_se_importa_al_directorio_de_la_corrida(adaptador, job, workspace):
    """Sin esto el artefacto guardaría una ruta absoluta al storage del motor."""
    resultado = adaptador.render(job)

    assert resultado.output_path == "render/final.mp4"
    assert not resultado.output_path.startswith("/")
    assert workspace.ruta(resultado.output_path).is_file()


def test_el_motor_recibe_exactamente_lo_que_pedimos(adaptador, job, settings_falsos, workspace):
    adaptador.render(job)
    recibido = ultima_invocacion(settings_falsos.raiz_mpt)

    assert recibido["task_id"] == str(workspace.run_id)
    assert recibido["video_source"] == "local"
    assert recibido["stop_at"] == "video"
    assert recibido["bgm_type"] == "none"
    assert recibido["subtitle_enabled"] is False
    assert recibido["sobrantes"] == [], "el adaptador envió flags que el motor no conoce"


def test_exit_2_se_traduce_a_entrada_invalida(adaptador, job, monkeypatch):
    """Código 2 es rechazo de argumentos, no un fallo de render."""
    monkeypatch.setenv("FAKE_MPT_MODE", "argerror")
    with pytest.raises(EntradaInvalida):
        adaptador.render(job)


def test_exit_1_devuelve_resultado_fallido_y_no_lanza(adaptador, job, monkeypatch):
    """El adaptador cumple su contrato: devuelve RenderResult con el error."""
    monkeypatch.setenv("FAKE_MPT_MODE", "fail")
    resultado = adaptador.render(job)

    assert resultado.status is EstadoRender.fallo
    assert resultado.exit_code == 1
    assert resultado.error and "render falló" in resultado.error
    assert resultado.output_path is None


def test_stdout_sin_json_es_entrada_invalida(adaptador, job, monkeypatch):
    monkeypatch.setenv("FAKE_MPT_MODE", "nojson")
    with pytest.raises(EntradaInvalida, match="JSON"):
        adaptador.render(job)


def test_exito_sin_videos_se_reporta_como_fallo(adaptador, job, monkeypatch):
    monkeypatch.setenv("FAKE_MPT_MODE", "sinvideo")
    resultado = adaptador.render(job)
    assert resultado.status is EstadoRender.fallo
    assert "no devolvió ningún video" in resultado.error


def test_el_log_del_motor_va_a_disco_y_no_al_resultado(adaptador, job, workspace, monkeypatch):
    monkeypatch.setenv("FAKE_MPT_MODE", "fail")
    resultado = adaptador.render(job)

    log = workspace.ruta("render/mpt.log")
    assert log.is_file()
    assert "--- stderr ---" in log.read_text(encoding="utf-8")
    assert len(resultado.error) <= 500
