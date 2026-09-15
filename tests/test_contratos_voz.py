"""Contratos de voz y subtítulos: validación, serialización y regresiones.

Los casos de esta suite son la matriz que fija el contrato de Gate 3. Cada
rechazo está aquí porque un artefacto que lo incumpliera desincronizaría el
video o lo haría irreproducible en otra máquina.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.contracts.models import (
    SCHEMA_VERSION,
    SCHEMA_VERSION_VOZ,
    SubtitleAsset,
    SubtitleCue,
    VoiceAsset,
    WordBoundary,
    WordBoundaryAsset,
    hash_guion,
)

GUION = "¿Sabías que el 73 % se abandona antes de los 3 segundos? ¡Increíble!"
HUELLA = hash_guion(GUION)


def _voz(**extra) -> VoiceAsset:
    base = dict(
        run_id=uuid.uuid4(), language="es", provider="edge-tts",
        engine="edge-tts-neural", voice="es-CR-JuanNeural",
        audio_path="voice/narration.mp3", audio_format="mp3",
        audio_duration_seconds=30.29, sample_rate_hz=24000, channels=1,
        source_script_sha256=HUELLA,
    )
    base.update(extra)
    return VoiceAsset(**base)


def _limite(**extra) -> WordBoundary:
    base = dict(index=0, start_seconds=0.1, duration_seconds=0.57, text="Sabías")
    base.update(extra)
    return WordBoundary(**base)


def _limites(**extra) -> WordBoundaryAsset:
    base = dict(
        run_id=uuid.uuid4(), provider="edge-tts", engine="edge-tts-neural",
        voice="es-CR-JuanNeural", audio_path="voice/narration.mp3",
        audio_duration_seconds=30.29,
        boundaries=[
            _limite(index=0, start_seconds=0.10),
            _limite(index=1, start_seconds=0.69, text="que"),
        ],
        source_script_sha256=HUELLA,
    )
    base.update(extra)
    return WordBoundaryAsset(**base)


def _cue(**extra) -> SubtitleCue:
    base = dict(index=1, start_seconds=0.0, end_seconds=2.34, text="¿Sabías que?")
    base.update(extra)
    return SubtitleCue(**base)


def _subtitulos(**extra) -> SubtitleAsset:
    cues = extra.pop("cues", [_cue()])
    base = dict(
        run_id=uuid.uuid4(), language="es", source_script_sha256=HUELLA,
        word_boundary_artifact="word_boundaries.json",
        srt_path="subtitles/subtitles.srt", ass_path="subtitles/subtitles.ass",
        cue_count=len(cues), duration_seconds=30.29,
        max_lines=2, target_chars_per_line=32, cues=cues,
    )
    base.update(extra)
    return SubtitleAsset(**base)


# ---------------------------------------------------------------------------
# Campos comunes
# ---------------------------------------------------------------------------


def test_los_artefactos_de_voz_declaran_su_propia_version_de_schema():
    """Gate 3 usa "1.0" sin reescribir la de los artefactos ya persistidos."""
    assert _voz().schema_version == SCHEMA_VERSION_VOZ == "1.0"
    assert _limites().schema_version == SCHEMA_VERSION_VOZ
    assert _subtitulos().schema_version == SCHEMA_VERSION_VOZ
    assert SCHEMA_VERSION == "1"


def test_created_at_es_consciente_de_zona():
    assert _voz().created_at.tzinfo is not None


def test_run_id_invalido_es_rechazado():
    with pytest.raises(ValidationError):
        _voz(run_id="no-es-un-uuid")


def test_campo_desconocido_es_rechazado():
    with pytest.raises(ValidationError):
        _voz(bitrate=128_000)


# ---------------------------------------------------------------------------
# VoiceAsset
# ---------------------------------------------------------------------------


def test_voice_asset_valido():
    voz = _voz()
    assert voz.audio_duration_seconds == 30.29
    assert voz.channels == 1


@pytest.mark.parametrize("duracion", [0.0, -1.0])
def test_voice_asset_rechaza_duracion_no_positiva(duracion):
    with pytest.raises(ValidationError):
        _voz(audio_duration_seconds=duracion)


@pytest.mark.parametrize(
    "ruta",
    [
        "/home/runner/work/runs/x/voice/narration.mp3",
        "/Users/dylan/project/runs/voice.mp3",
        "C:\\Users\\dylan\\voice.mp3",
        "\\\\servidor\\runs\\voice.mp3",
    ],
)
def test_voice_asset_rechaza_ruta_absoluta(ruta):
    """Una ruta absoluta deja el artefacto inservible en otra máquina."""
    with pytest.raises(ValidationError):
        _voz(audio_path=ruta)


@pytest.mark.parametrize("campo", ["voice", "provider", "engine", "language", "audio_format"])
def test_voice_asset_rechaza_campos_de_texto_vacios(campo):
    with pytest.raises(ValidationError):
        _voz(**{campo: ""})


@pytest.mark.parametrize(
    "hash_malo",
    ["", "abc", HUELLA.upper(), HUELLA[:-1], HUELLA + "0", "z" * 64],
)
def test_voice_asset_rechaza_hash_invalido(hash_malo):
    with pytest.raises(ValidationError):
        _voz(source_script_sha256=hash_malo)


def test_voice_asset_rechaza_frecuencia_y_canales_imposibles():
    with pytest.raises(ValidationError):
        _voz(sample_rate_hz=0)
    with pytest.raises(ValidationError):
        _voz(channels=0)


def test_voice_asset_no_guarda_el_texto_del_guion():
    """Solo la huella: duplicar el guion crearía dos fuentes de verdad."""
    serializado = _voz().model_dump_json()
    assert "Sabías" not in serializado
    assert HUELLA in serializado


# ---------------------------------------------------------------------------
# WordBoundary
# ---------------------------------------------------------------------------


def test_word_boundary_valido_y_fin_derivado():
    limite = _limite(start_seconds=1.5, duration_seconds=0.5)
    assert limite.end_seconds == 2.0
    # end_seconds no se persiste: no puede discrepar de start + duration.
    assert "end_seconds" not in limite.model_dump()


def test_word_boundary_rechaza_inicio_negativo():
    with pytest.raises(ValidationError):
        _limite(start_seconds=-0.1)


def test_word_boundary_rechaza_duracion_cero():
    with pytest.raises(ValidationError):
        _limite(duration_seconds=0.0)


@pytest.mark.parametrize("texto", ["", "   ", "\n"])
def test_word_boundary_rechaza_texto_vacio(texto):
    with pytest.raises(ValidationError):
        _limite(text=texto)


def test_word_boundary_rechaza_index_negativo():
    with pytest.raises(ValidationError):
        _limite(index=-1)


# ---------------------------------------------------------------------------
# WordBoundaryAsset
# ---------------------------------------------------------------------------


def test_word_boundary_asset_valido():
    assert len(_limites().boundaries) == 2


def test_word_boundary_asset_rechaza_index_duplicado():
    with pytest.raises(ValidationError, match="duplicado"):
        _limites(boundaries=[_limite(index=0), _limite(index=0, start_seconds=0.7)])


def test_word_boundary_asset_rechaza_numeracion_que_no_empieza_en_cero():
    with pytest.raises(ValidationError, match="empieza en 0"):
        _limites(boundaries=[_limite(index=1)])


def test_word_boundary_asset_rechaza_orden_invertido():
    with pytest.raises(ValidationError, match="desordenados"):
        _limites(
            boundaries=[
                _limite(index=0, start_seconds=0.1),
                _limite(index=2, start_seconds=0.5),
                _limite(index=1, start_seconds=0.9),
            ]
        )


def test_word_boundary_asset_rechaza_tiempos_que_retroceden():
    with pytest.raises(ValidationError, match="antes que"):
        _limites(
            boundaries=[
                _limite(index=0, start_seconds=2.0),
                _limite(index=1, start_seconds=1.0),
            ]
        )


def test_word_boundary_asset_rechaza_duracion_de_audio_invalida():
    with pytest.raises(ValidationError):
        _limites(audio_duration_seconds=0.0)


def test_word_boundary_asset_rechaza_ruta_absoluta():
    with pytest.raises(ValidationError):
        _limites(audio_path="/tmp/narration.mp3")


def test_la_cola_de_audio_tras_el_ultimo_boundary_es_valida():
    """Regresión de Gate 0.5: Edge TTS deja ~0,96 s de audio después del último
    WordBoundary. Un contrato que exigiera cobertura total rechazaría la salida
    real del proveedor."""
    limites = _limites(
        boundaries=[_limite(index=0, start_seconds=0.10, duration_seconds=0.57)],
        audio_duration_seconds=6.77,
    )
    ultimo = limites.boundaries[-1]
    assert ultimo.end_seconds < limites.audio_duration_seconds


# ---------------------------------------------------------------------------
# SubtitleCue
# ---------------------------------------------------------------------------


def test_subtitle_cue_valido():
    assert _cue().end_seconds > _cue().start_seconds


def test_subtitle_cue_rechaza_index_cero():
    """El índice es el del bloque SRT y empieza en 1."""
    with pytest.raises(ValidationError):
        _cue(index=0)


def test_subtitle_cue_rechaza_tiempo_negativo():
    with pytest.raises(ValidationError):
        _cue(start_seconds=-0.5)


@pytest.mark.parametrize("fin", [2.0, 1.0])
def test_subtitle_cue_rechaza_fin_no_posterior_al_inicio(fin):
    with pytest.raises(ValidationError):
        _cue(start_seconds=2.0, end_seconds=fin)


def test_subtitle_cue_rechaza_texto_vacio():
    with pytest.raises(ValidationError):
        _cue(text="  ")


def test_subtitle_cue_admite_salto_de_linea():
    """El reparto en dos líneas viaja en el texto, que es lo que el SRT escribe."""
    cue = _cue(text="¿Sabías que\nesto cambió todo?")
    assert cue.text.split("\n") == ["¿Sabías que", "esto cambió todo?"]


# ---------------------------------------------------------------------------
# SubtitleAsset
# ---------------------------------------------------------------------------


def test_subtitle_asset_valido():
    asset = _subtitulos()
    assert asset.cue_count == len(asset.cues) == 1


def test_subtitle_asset_rechaza_cue_count_que_no_cuadra():
    with pytest.raises(ValidationError, match="cue_count"):
        _subtitulos(cues=[_cue()], cue_count=5)


def test_subtitle_asset_rechaza_solapamiento():
    with pytest.raises(ValidationError, match="aún no ha terminado"):
        _subtitulos(
            cues=[
                _cue(index=1, start_seconds=0.0, end_seconds=2.0),
                _cue(index=2, start_seconds=1.5, end_seconds=3.0),
            ]
        )


def test_subtitle_asset_admite_cues_contiguos():
    """Que un cue empiece justo cuando acaba el anterior no es solapamiento."""
    asset = _subtitulos(
        cues=[
            _cue(index=1, start_seconds=0.0, end_seconds=2.0),
            _cue(index=2, start_seconds=2.0, end_seconds=3.0),
        ]
    )
    assert asset.cue_count == 2


def test_subtitle_asset_rechaza_indices_no_crecientes():
    with pytest.raises(ValidationError, match="no crecientes"):
        _subtitulos(
            cues=[
                _cue(index=1, start_seconds=0.0, end_seconds=1.0),
                _cue(index=1, start_seconds=1.0, end_seconds=2.0),
            ]
        )


def test_subtitle_asset_rechaza_numeracion_que_no_empieza_en_uno():
    with pytest.raises(ValidationError, match="empieza en 1"):
        _subtitulos(cues=[_cue(index=2)])


@pytest.mark.parametrize("campo", ["srt_path", "ass_path", "word_boundary_artifact"])
def test_subtitle_asset_rechaza_rutas_absolutas(campo):
    with pytest.raises(ValidationError):
        _subtitulos(**{campo: "/tmp/subtitulos.srt"})


def test_subtitle_asset_rechaza_duracion_no_positiva():
    with pytest.raises(ValidationError):
        _subtitulos(duration_seconds=0.0)


@pytest.mark.parametrize("lineas", [1, 3, 4])
def test_subtitle_asset_rechaza_mas_o_menos_de_dos_lineas(lineas):
    """El contrato fija dos líneas: un artefacto no puede declarar otra regla."""
    with pytest.raises(ValidationError, match="max_lines"):
        _subtitulos(max_lines=lineas)


@pytest.mark.parametrize("caracteres", [20, 42])
def test_subtitle_asset_rechaza_otro_objetivo_de_caracteres(caracteres):
    with pytest.raises(ValidationError, match="target_chars_per_line"):
        _subtitulos(target_chars_per_line=caracteres)


def test_subtitle_asset_admite_cero_cues_si_lo_declara():
    assert _subtitulos(cues=[], duration_seconds=1.0).cue_count == 0


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def test_el_hash_ignora_los_espacios_de_los_extremos():
    assert hash_guion(f"  {GUION}\n") == hash_guion(GUION)


def test_el_hash_distingue_guiones_distintos():
    assert hash_guion(GUION) != hash_guion(GUION.replace("73", "74"))


def test_el_hash_no_normaliza_mas_alla_de_los_extremos():
    """Quitar acentos o puntuación haría que dos guiones distintos coincidieran."""
    assert hash_guion("¿Qué?") != hash_guion("Que")
    assert hash_guion("a  b") != hash_guion("a b")


def test_el_hash_es_sha256_hexadecimal_minuscula():
    huella = hash_guion(GUION)
    assert len(huella) == 64
    assert huella == huella.lower()
    int(huella, 16)  # lanza si no es hexadecimal


# ---------------------------------------------------------------------------
# Serialización
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "constructor, modelo",
    [(_voz, VoiceAsset), (_limites, WordBoundaryAsset), (_subtitulos, SubtitleAsset)],
)
def test_ida_y_vuelta_por_json(constructor, modelo):
    original = constructor()
    assert modelo.model_validate_json(original.model_dump_json()) == original


def test_la_ida_y_vuelta_conserva_uuid_y_fecha():
    original = _voz()
    copia = VoiceAsset.model_validate_json(original.model_dump_json())
    assert copia.run_id == original.run_id
    assert copia.created_at == original.created_at
    assert copia.created_at.tzinfo is not None


def test_la_ida_y_vuelta_conserva_la_precision_de_los_tiempos():
    cues = [_cue(index=1, start_seconds=0.0, end_seconds=2.3456789)]
    original = _subtitulos(cues=cues)
    copia = SubtitleAsset.model_validate_json(original.model_dump_json())
    assert copia.cues[0].end_seconds == 2.3456789


def test_los_tiempos_se_guardan_en_segundos_no_como_marca_srt():
    """La marca 00:00:02,340 pertenece al serializador, no al artefacto."""
    serializado = _subtitulos().model_dump_json()
    assert "00:00:02,340" not in serializado
    assert '"end_seconds":2.34' in serializado.replace(" ", "")


def test_el_artefacto_no_lleva_campos_en_milisegundos():
    """Contratos pequeños: nada de start_ms, end_ms ni duration_ms."""
    campos = set(SubtitleCue.model_fields) | set(WordBoundary.model_fields)
    assert not any(campo.endswith("_ms") for campo in campos)
    assert "end_seconds" not in WordBoundary.model_fields
