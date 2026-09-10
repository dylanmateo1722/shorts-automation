"""Abstracción de TTS: construcción, configuración y taxonomía de errores.

Ninguno de estos tests toca la red. El proveedor real se ejercita en
``test_tts_real.py``, aparte y marcado, para que quede claro cuál es cuál.
"""

from __future__ import annotations

import uuid

import pytest

from app.adapters.tts import (
    RITMO_NEUTRO,
    TONO_NEUTRO,
    ConfiguracionVoz,
    ProveedorEdgeTTS,
    ProveedorTTS,
    ProveedorTTSFalso,
    configuracion_voz,
    construir_proveedor_tts,
)
from app.adapters.tts import fake as modulo_falso
from app.adapters.media import inspeccionar_audio
from app.config.settings import Settings
from app.core.errors import (
    AudioInvalido,
    ConfiguracionInvalida,
    ProveedorNoDisponible,
    TiempoAgotado,
)
from tests.conftest import GUION_REFERENCIA, construir_guion


@pytest.fixture
def guion():
    return construir_guion(uuid.uuid4())


# ---------------------------------------------------------------------------
# Construcción desde configuración
# ---------------------------------------------------------------------------


def test_edge_es_el_proveedor_por_defecto(tmp_path):
    """Es el único validado y no requiere credencial. No se cambia en G3."""
    settings = Settings(raiz_proyecto=tmp_path)
    assert settings.tts_provider == "edge"
    assert settings.tts_voice == "es-CR-JuanNeural"
    assert isinstance(construir_proveedor_tts(settings), ProveedorEdgeTTS)


def test_se_puede_pedir_el_proveedor_falso(tmp_path):
    settings = Settings(raiz_proyecto=tmp_path, tts_provider="fake")
    assert isinstance(construir_proveedor_tts(settings), ProveedorTTSFalso)


def test_elevenlabs_se_declara_no_implementado(tmp_path):
    """No se finge que existe un fallback: se dice que no está escrito."""
    settings = Settings(raiz_proyecto=tmp_path, tts_provider="elevenlabs")
    with pytest.raises(ConfiguracionInvalida, match="NO está implementado"):
        construir_proveedor_tts(settings)


def test_no_hay_conmutacion_automatica_a_otro_proveedor(tmp_path):
    """Pedir el fallback falla; no cae en silencio sobre Edge TTS."""
    settings = Settings(raiz_proyecto=tmp_path, tts_provider="elevenlabs")
    with pytest.raises(ConfiguracionInvalida) as excinfo:
        construir_proveedor_tts(settings)
    assert "edge" in str(excinfo.value)  # lista lo que sí existe


def test_un_proveedor_desconocido_nombra_los_admitidos(tmp_path):
    settings = Settings(raiz_proyecto=tmp_path, tts_provider="inventado")
    with pytest.raises(ConfiguracionInvalida, match="desconocido"):
        construir_proveedor_tts(settings)


def test_falta_de_voz_se_detecta_antes_de_sintetizar(tmp_path):
    settings = Settings(raiz_proyecto=tmp_path, tts_voice="")
    with pytest.raises(ConfiguracionInvalida, match="TTS_VOICE"):
        settings.verificar_tts()


def test_la_configuracion_llega_desde_el_entorno(monkeypatch, tmp_path):
    monkeypatch.setenv("SHORTS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("TTS_PROVIDER", "fake")
    monkeypatch.setenv("TTS_VOICE", "es-MX-DaliaNeural")
    monkeypatch.setenv("TTS_RATE", "+10%")
    monkeypatch.setenv("TTS_PITCH", "-5Hz")

    settings = Settings.desde_entorno()
    config = configuracion_voz(settings)
    assert (config.voz, config.ritmo, config.tono) == ("es-MX-DaliaNeural", "+10%", "-5Hz")


def test_sin_ritmo_ni_tono_se_usan_los_neutros(tmp_path):
    config = configuracion_voz(Settings(raiz_proyecto=tmp_path))
    assert config.ritmo == RITMO_NEUTRO == "+0%"
    assert config.tono == TONO_NEUTRO == "+0Hz"


def test_la_configuracion_de_voz_llega_al_manifest(tmp_path):
    publico = Settings(raiz_proyecto=tmp_path, tts_voice="es-ES-AlvaroNeural").publico()
    assert publico["tts_provider"] == "edge"
    assert publico["tts_voice"] == "es-ES-AlvaroNeural"


# ---------------------------------------------------------------------------
# El pipeline no conoce el proveedor
# ---------------------------------------------------------------------------


def test_ambos_proveedores_cumplen_la_misma_interfaz():
    for clase in (ProveedorEdgeTTS, ProveedorTTSFalso):
        assert issubclass(clase, ProveedorTTS)
        assert clase.nombre and clase.motor and clase.formato


def test_el_pipeline_no_importa_ningun_proveedor_concreto():
    """Cambiar de proveedor debe ser escribir otra clase, no tocar etapas."""
    import ast
    from pathlib import Path

    arbol = ast.parse(Path("app/pipeline/voice.py").read_text(encoding="utf-8"))
    importados = {
        nodo.module or ""
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.ImportFrom)
    } | {
        alias.name
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Import)
        for alias in nodo.names
    }
    concretos = {"edge_tts", "app.adapters.tts.edge", "app.adapters.tts.fake"}
    assert not importados & concretos, importados & concretos


# ---------------------------------------------------------------------------
# Proveedor falso
# ---------------------------------------------------------------------------


def test_el_falso_produce_audio_real_y_medible(tmp_path, guion):
    proveedor = ProveedorTTSFalso()
    destino = tmp_path / "voz" / "narracion.mp3"
    resultado = proveedor.sintetizar(guion, destino, ConfiguracionVoz(voz="es-CR-JuanNeural"))

    assert destino.is_file() and destino.stat().st_size > 0
    info = inspeccionar_audio(destino)
    assert info.duracion_s > 0
    assert info.codec == "mp3" and info.sample_rate_hz == 24_000 and info.canales == 1
    assert resultado.bytes_audio == destino.stat().st_size


def test_el_falso_es_determinista(tmp_path, guion):
    uno = ProveedorTTSFalso().sintetizar(
        guion, tmp_path / "a.mp3", ConfiguracionVoz(voz="v")
    )
    otro = ProveedorTTSFalso().sintetizar(
        guion, tmp_path / "b.mp3", ConfiguracionVoz(voz="v")
    )
    assert [(x.inicio_s, x.duracion_s, x.texto) for x in uno.limites] == [
        (x.inicio_s, x.duracion_s, x.texto) for x in otro.limites
    ]


def test_el_falso_deja_cola_de_audio_como_el_real(tmp_path, guion):
    """Sin cola, un test con el falso daría verde sobre el defecto de G0.5."""
    proveedor = ProveedorTTSFalso()
    destino = tmp_path / "narracion.mp3"
    resultado = proveedor.sintetizar(guion, destino, ConfiguracionVoz(voz="v"))
    duracion_real = inspeccionar_audio(destino).duracion_s
    assert duracion_real > resultado.fin_ultimo_limite_s
    assert duracion_real - resultado.fin_ultimo_limite_s > 0.5


def test_el_falso_emite_los_tiempos_sin_puntuacion(tmp_path, guion):
    """Como Edge TTS: el texto del evento no trae ¿ ¡ ni comas."""
    resultado = ProveedorTTSFalso().sintetizar(
        guion, tmp_path / "n.mp3", ConfiguracionVoz(voz="v")
    )
    textos = [x.texto for x in resultado.limites]
    assert "Sabías" in textos
    assert not any(any(c in t for c in "¿¡?!,.") for t in textos)


def test_el_falso_agrupa_el_numero_con_la_palabra_siguiente():
    assert "73 %" in modulo_falso._tokenizar(GUION_REFERENCIA)
    assert "3 segundos" in modulo_falso._tokenizar(GUION_REFERENCIA)


def test_el_falso_cuenta_sus_llamadas(tmp_path, guion):
    """Permite demostrar que la idempotencia evita una segunda síntesis."""
    proveedor = ProveedorTTSFalso()
    assert proveedor.llamadas == 0
    proveedor.sintetizar(guion, tmp_path / "n.mp3", ConfiguracionVoz(voz="v"))
    assert proveedor.llamadas == 1


def test_un_guion_sin_palabras_es_un_error_de_audio(tmp_path):
    guion = construir_guion(uuid.uuid4(), texto="...")
    with pytest.raises(AudioInvalido):
        ProveedorTTSFalso().sintetizar(guion, tmp_path / "n.mp3", ConfiguracionVoz(voz="v"))


def test_los_tiempos_del_falso_son_monotonos(tmp_path, guion):
    resultado = ProveedorTTSFalso().sintetizar(
        guion, tmp_path / "n.mp3", ConfiguracionVoz(voz="v")
    )
    for anterior, siguiente in zip(resultado.limites, resultado.limites[1:]):
        assert siguiente.inicio_s >= anterior.fin_s
        assert anterior.duracion_s > 0


# ---------------------------------------------------------------------------
# Taxonomía de errores del proveedor
# ---------------------------------------------------------------------------


class _EdgeQueFalla(ProveedorEdgeTTS):
    """Edge TTS con la llamada al servicio sustituida por un fallo concreto."""

    def __init__(self, excepcion: Exception) -> None:
        super().__init__(timeout_s=1)
        self._excepcion = excepcion

    async def _sintetizar(self, texto, config, destino):
        raise self._excepcion


def _fallar_con(excepcion, tmp_path, guion):
    proveedor = _EdgeQueFalla(excepcion)
    return proveedor.sintetizar(guion, tmp_path / "n.mp3", ConfiguracionVoz(voz="v"))


def test_una_caida_del_servicio_es_transitoria(tmp_path, guion):
    import edge_tts.exceptions as edge

    with pytest.raises(ProveedorNoDisponible) as excinfo:
        _fallar_con(edge.WebSocketError("caído"), tmp_path, guion)
    assert excinfo.value.retryable is True
    assert excinfo.value.categoria == "transitorio"


def test_una_respuesta_inesperada_del_servicio_es_transitoria(tmp_path, guion):
    import edge_tts.exceptions as edge

    with pytest.raises(ProveedorNoDisponible):
        _fallar_con(edge.UnexpectedResponse("raro"), tmp_path, guion)


def test_un_fallo_de_red_es_transitorio(tmp_path, guion):
    with pytest.raises(ProveedorNoDisponible):
        _fallar_con(OSError("conexión rechazada"), tmp_path, guion)


def test_quedarse_sin_audio_es_un_error_de_audio(tmp_path, guion):
    import edge_tts.exceptions as edge

    with pytest.raises(AudioInvalido) as excinfo:
        _fallar_con(edge.NoAudioReceived("nada"), tmp_path, guion)
    assert excinfo.value.retryable is False


def test_una_voz_invalida_es_un_error_de_configuracion(tmp_path, guion):
    """Reintentarlo repetiría el mismo fallo y gastaría tiempo."""
    with pytest.raises(ConfiguracionInvalida) as excinfo:
        _fallar_con(ValueError("voz desconocida"), tmp_path, guion)
    assert excinfo.value.categoria == "permanente"


def test_el_timeout_se_distingue_de_los_demas_fallos(tmp_path, guion):
    import asyncio

    class _EdgeLento(ProveedorEdgeTTS):
        async def _sintetizar(self, texto, config, destino):
            await asyncio.sleep(5)
            return [], 0

    proveedor = _EdgeLento(timeout_s=1)
    with pytest.raises(TiempoAgotado):
        proveedor.sintetizar(guion, tmp_path / "n.mp3", ConfiguracionVoz(voz="v"))


def test_audio_sin_tiempos_no_se_acepta(tmp_path, guion):
    """Sin WordBoundary no hay forma de sincronizar: es un fallo, no un aviso."""

    class _EdgeSinTiempos(ProveedorEdgeTTS):
        async def _sintetizar(self, texto, config, destino):
            destino.write_bytes(b"\x00" * 128)
            return [], 128

    with pytest.raises(AudioInvalido, match="WordBoundary"):
        _EdgeSinTiempos().sintetizar(guion, tmp_path / "n.mp3", ConfiguracionVoz(voz="v"))


def test_los_ticks_de_edge_tts_se_traducen_a_segundos(tmp_path, guion):
    """El pipeline nunca ve unidades de 100 ns."""

    class _EdgeConTiempos(ProveedorEdgeTTS):
        async def _sintetizar(self, texto, config, destino):
            destino.write_bytes(b"\x00" * 128)
            return [{"offset": 10_000_000, "duration": 5_000_000, "text": "hola"}], 128

    resultado = _EdgeConTiempos().sintetizar(
        guion, tmp_path / "n.mp3", ConfiguracionVoz(voz="v")
    )
    assert resultado.limites[0].inicio_s == 1.0
    assert resultado.limites[0].duracion_s == 0.5
    assert resultado.limites[0].fin_s == 1.5


# ---------------------------------------------------------------------------
# Seguridad
# ---------------------------------------------------------------------------


def test_el_resultado_del_proveedor_no_lleva_credenciales(tmp_path, guion):
    """Edge TTS no usa ninguna, y el contrato tampoco tiene dónde guardarla."""
    from dataclasses import fields

    from app.adapters.tts.base import ResultadoSintesis

    nombres = {f.name for f in fields(ResultadoSintesis)}
    assert not nombres & {"api_key", "token", "headers", "authorization"}
