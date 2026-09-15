"""Capa de voz: guion adaptado → narración → subtítulos.

Dos etapas, no tres:

* **voice** sintetiza la narración y persiste sus tiempos. Van juntas porque
  Edge TTS entrega audio y ``WordBoundary`` en la misma respuesta: separarlas
  obligaría a sintetizar dos veces o a pasar los tiempos por un canal lateral.
  Produce dos artefactos, ``VoiceAsset`` y ``WordBoundaryAsset``, porque son
  dos contratos distintos aunque nazcan de una sola llamada.
* **subtitles** alinea esos tiempos con el guion y escribe SRT y ASS.

Tres reglas gobiernan este módulo:

1. **La duración del audio se mide sobre el archivo.** Ni la estimación del
   guion ni el final del último ``WordBoundary`` valen: Gate 0.5 comprobó que
   Edge TTS deja casi un segundo de audio después del último evento.
2. **El texto de los subtítulos sale del guion, no del TTS.** Los eventos
   aportan tiempos; el texto con sus ``¿``, ``¡``, comas y acentos está en
   ``AdaptedScript.full_text``.
3. **Nada se adivina.** Un evento que no case con el guion se descarta y se
   cuenta; si se pierden demasiados, la etapa falla en vez de entregar un
   subtitulado desincronizado.

La frontera de Gate 3 termina en ``SubtitleAsset``. Aquí no hay transformación
visual, ni composición, ni render.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

from app.adapters.media import comprobar_decodificable, inspeccionar_audio
from app.adapters.tts import (
    ConfiguracionVoz,
    ProveedorTTS,
    configuracion_voz,
    construir_proveedor_tts,
)
from app.contracts.models import (
    CARACTERES_OBJETIVO_LINEA,
    MAX_LINEAS_SUBTITULO,
    AdaptedScript,
    SubtitleAsset,
    SubtitleCue,
    VoiceAsset,
    WordBoundary,
    WordBoundaryAsset,
    hash_guion,
)
from app.core.errors import (
    AlineacionInvalida,
    ArtefactoCorrupto,
    SubtituloInvalido,
)
from app.core.logging import log_evento
from app.core.stage_runner import ContextoEtapa, Etapa
from app.core.workspace import Workspace
from app.subtitles import reglas, segmenter, serializers

ARTEFACTO_GUION = "adapted_script"
ARTEFACTO_VOZ = "voice_asset"
ARTEFACTO_LIMITES = "word_boundaries"
ARTEFACTO_SUBTITULOS = "subtitle_asset"

# Rutas de los archivos físicos, relativas al directorio de la corrida. Los
# JSON de artefacto se quedan en la raíz, que es la convención de Gates 1 y 2.
RUTA_AUDIO = "voice/narration"
RUTA_SRT = "subtitles/subtitles.srt"
RUTA_ASS = "subtitles/subtitles.ass"

CLAVE_PROVEEDOR_TTS = "proveedor_tts"

#: Proporción de eventos del TTS que puede descartarse en la alineación antes
#: de considerar que los tiempos no corresponden al guion. Un descarte suelto
#: es normal (un número verbalizado); la mitad significa otro guion.
MAX_DESCARTES = 0.25

#: Divergencia entre duración estimada y real a partir de la cual se avisa. No
#: invalida nada: es información para calibrar el estimador más adelante.
AVISO_DIVERGENCIA = 0.20


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Proveedor
# ---------------------------------------------------------------------------


def _proveedor(ctx: ContextoEtapa) -> ProveedorTTS:
    """Obtiene el proveedor de TTS de la corrida, construyéndolo una sola vez.

    Mismo patrón que la capa lingüística: se admite uno inyectado por
    parámetros para que un test pueda contar las llamadas, y si no se construye
    desde la configuración y se guarda en los recursos de la corrida.
    """
    if (inyectado := ctx.parametros.get(CLAVE_PROVEEDOR_TTS)) is not None:
        return inyectado
    if (cacheado := ctx.recursos.get(CLAVE_PROVEEDOR_TTS)) is not None:
        return cacheado
    proveedor = construir_proveedor_tts(ctx.settings)
    ctx.recursos[CLAVE_PROVEEDOR_TTS] = proveedor
    return proveedor


def huella_configuracion(proveedor: ProveedorTTS, config: ConfiguracionVoz) -> str:
    """Huella de lo que determina el audio, aparte del guion.

    Cambiar proveedor, voz, ritmo o tono produce otra narración, así que el
    audio anterior deja de ser reutilizable aunque el archivo siga intacto.
    No contiene ningún secreto: son cuatro valores de configuración pública.
    """
    partes = [proveedor.nombre, proveedor.motor, config.voz, config.ritmo, config.tono]
    return hashlib.sha256("|".join(partes).encode("utf-8")).hexdigest()[:16]


def _guion(ctx: ContextoEtapa) -> AdaptedScript:
    guion = ctx.previos.get(ARTEFACTO_GUION)
    if guion is None:
        guion = ctx.workspace.leer_artefacto(ARTEFACTO_GUION, AdaptedScript)
    return guion


# ---------------------------------------------------------------------------
# Etapa de voz
# ---------------------------------------------------------------------------


def sintetizar_voz(ctx: ContextoEtapa) -> VoiceAsset:
    """Narra el guion, mide el audio y persiste los tiempos por palabra."""
    ws: Workspace = ctx.workspace
    ctx.settings.verificar_tts()

    guion = _guion(ctx)
    proveedor = _proveedor(ctx)
    config = configuracion_voz(ctx.settings)
    huella_guion = hash_guion(guion.full_text)

    destino = ws.ruta(f"{RUTA_AUDIO}.{proveedor.formato}")
    inicio = time.monotonic()
    resultado = proveedor.sintetizar(guion, destino, config)
    duracion_sintesis = time.monotonic() - inicio

    # La duración sale del archivo, nunca del último evento del proveedor.
    comprobar_decodificable(resultado.audio_path)
    info = inspeccionar_audio(resultado.audio_path)
    cola_s = info.duracion_s - resultado.fin_ultimo_limite_s

    ruta_relativa = ws.relativa(resultado.audio_path)
    limites = [
        WordBoundary(
            index=indice,
            start_seconds=limite.inicio_s,
            duration_seconds=limite.duracion_s,
            text=limite.texto,
        )
        for indice, limite in enumerate(resultado.limites)
    ]
    ws.escribir_artefacto(
        ARTEFACTO_LIMITES,
        WordBoundaryAsset(
            run_id=ws.run_id,
            provider=proveedor.nombre,
            engine=proveedor.motor,
            voice=resultado.voz,
            audio_path=ruta_relativa,
            audio_duration_seconds=info.duracion_s,
            boundaries=limites,
            source_script_sha256=huella_guion,
        ),
    )
    ruta_limites = ws.ruta_artefacto(ARTEFACTO_LIMITES)
    ctx.manifest.registrar_artefacto(
        ARTEFACTO_LIMITES, ws.relativa(ruta_limites), ruta_limites.stat().st_size
    )

    estimada = guion.estimated_duration_seconds
    delta = info.duracion_s - estimada
    ratio = info.duracion_s / estimada if estimada > 0 else 0.0

    avisos = list(resultado.avisos)
    if cola_s > 0:
        avisos.append(
            f"el audio sigue {cola_s:.2f}s después del último WordBoundary"
        )
    if estimada > 0 and abs(ratio - 1) > AVISO_DIVERGENCIA:
        avisos.append(
            f"la duración real ({info.duracion_s:.2f}s) diverge un "
            f"{abs(ratio - 1) * 100:.0f}% de la estimada ({estimada:.2f}s)"
        )

    entrada = ctx.manifest.registrar_etapa("voice")
    entrada.metadata = {
        **entrada.metadata,
        "provider": proveedor.nombre,
        "engine": proveedor.motor,
        "voice": resultado.voz,
        "actual_duration_seconds": round(info.duracion_s, 3),
        "estimated_duration_seconds": round(estimada, 3),
        "duration_delta_seconds": round(delta, 3),
        "duration_ratio": round(ratio, 4),
        "audio_tail_seconds": round(cola_s, 3),
        "word_boundaries": len(limites),
        "synthesis_seconds": round(duracion_sintesis, 3),
        "config_fingerprint": huella_configuracion(proveedor, config),
        "warnings": avisos,
    }

    log_evento(
        ws.run_id, "voice", "synthesized",
        provider=proveedor.nombre, voice=resultado.voz,
        boundaries=len(limites), estimated_s=estimada,
        actual_s=info.duracion_s, delta_s=delta, tail_s=cola_s,
        synthesis_s=duracion_sintesis,
    )
    for aviso in avisos:
        log_evento(ws.run_id, "voice", "warning", detail=aviso)

    return VoiceAsset(
        run_id=ws.run_id,
        language=guion.language,
        provider=proveedor.nombre,
        engine=proveedor.motor,
        voice=resultado.voz,
        audio_path=ruta_relativa,
        audio_format=resultado.formato,
        audio_duration_seconds=info.duracion_s,
        sample_rate_hz=info.sample_rate_hz,
        channels=info.canales,
        source_script_sha256=huella_guion,
    )


def validar_voz(artefacto: VoiceAsset, ws: Workspace) -> None:
    """Un audio que desapareció, o unos tiempos ilegibles, obligan a rehacer."""
    audio = ws.ruta(artefacto.audio_path)
    if not audio.is_file() or audio.stat().st_size == 0:
        raise ArtefactoCorrupto(f"falta el audio {artefacto.audio_path!r}")
    # Los WordBoundary son el otro artefacto de esta etapa: sin ellos no hay
    # nada que reutilizar aunque el VoiceAsset valide.
    limites = ws.leer_artefacto(ARTEFACTO_LIMITES, WordBoundaryAsset)
    if limites.source_script_sha256 != artefacto.source_script_sha256:
        raise ArtefactoCorrupto(
            "los WordBoundary corresponden a otro guion que el audio"
        )


def voz_vigente(artefacto: VoiceAsset, ctx: ContextoEtapa) -> None:
    """Comprueba que el audio sigue correspondiendo a la corrida de ahora.

    Cubre los cinco motivos por los que un audio válido deja de servir:
    cambió el guion, el proveedor, la voz, el ritmo o el tono. Los tres
    últimos no caben en el contrato del artefacto, así que su huella vive en el
    manifest.
    """
    guion = _guion(ctx)
    esperado = hash_guion(guion.full_text)
    if artefacto.source_script_sha256 != esperado:
        raise ArtefactoCorrupto(
            "el audio corresponde a otro guion; se vuelve a sintetizar"
        )

    proveedor = _proveedor(ctx)
    config = configuracion_voz(ctx.settings)
    if artefacto.provider != proveedor.nombre or artefacto.voice != config.voz:
        raise ArtefactoCorrupto(
            f"el audio se generó con {artefacto.provider}/{artefacto.voice} y "
            f"ahora se pide {proveedor.nombre}/{config.voz}"
        )

    entrada = ctx.manifest.etapa("voice")
    previa = (entrada.metadata or {}).get("config_fingerprint") if entrada else None
    actual = huella_configuracion(proveedor, config)
    if previa is not None and previa != actual:
        raise ArtefactoCorrupto(
            "cambió la configuración de voz (ritmo o tono); se vuelve a sintetizar"
        )


# ---------------------------------------------------------------------------
# Etapa de subtítulos
# ---------------------------------------------------------------------------


def _eventos(limites: WordBoundaryAsset) -> list[dict]:
    """Traduce los WordBoundary del contrato a la entrada del segmentador.

    El segmentador es el de Gate 0.5 y trabaja en las unidades de Edge TTS.
    Convertir aquí, en un solo sitio, evita reescribir un componente validado.
    """
    return [
        {
            "offset": round(b.start_seconds * segmenter.TICKS_POR_SEGUNDO),
            "duration": round(b.duration_seconds * segmenter.TICKS_POR_SEGUNDO),
            "text": b.text,
        }
        for b in limites.boundaries
    ]


def generar_subtitulos(ctx: ContextoEtapa) -> SubtitleAsset:
    """Alinea los tiempos con el guion y escribe el SRT canónico y el ASS."""
    ws: Workspace = ctx.workspace
    guion = _guion(ctx)
    voz = ctx.previos.get(ARTEFACTO_VOZ) or ws.leer_artefacto(ARTEFACTO_VOZ, VoiceAsset)
    limites = ws.leer_artefacto(ARTEFACTO_LIMITES, WordBoundaryAsset)

    huella_guion = hash_guion(guion.full_text)
    if voz.source_script_sha256 != huella_guion:
        raise AlineacionInvalida(
            "el audio no corresponde al guion de esta corrida", stage="subtitles"
        )
    if not limites.boundaries:
        raise AlineacionInvalida(
            "no hay WordBoundary con los que sincronizar", stage="subtitles"
        )

    eventos = _eventos(limites)
    inicio = time.monotonic()

    # Un descarte suelto es normal —un número verbalizado, por ejemplo—; muchos
    # significan que los tiempos no son de este guion. No se adivina: se falla
    # antes de producir un subtitulado desincronizado.
    _, descartados = segmenter.alinear(guion.full_text, eventos)
    proporcion = descartados / len(limites.boundaries)
    if proporcion > MAX_DESCARTES:
        raise AlineacionInvalida(
            f"{descartados} de {len(limites.boundaries)} tiempos no casan con el "
            f"guion ({proporcion:.0%}); la sincronización no sería fiable",
            stage="subtitles",
        )

    cues, incidencias = segmenter.segmentar(
        guion.full_text,
        eventos,
        max_car_linea=CARACTERES_OBJETIVO_LINEA,
        max_lineas=MAX_LINEAS_SUBTITULO,
    )
    duracion_alineacion = time.monotonic() - inicio

    if not cues:
        raise AlineacionInvalida(
            "la alineación no produjo ningún subtítulo", stage="subtitles"
        )

    validacion = reglas.validar(
        cues,
        voz.audio_duration_seconds,
        guion=guion.full_text,
        max_car_linea=CARACTERES_OBJETIVO_LINEA,
        max_lineas=MAX_LINEAS_SUBTITULO,
    )
    if not validacion.ok:
        raise SubtituloInvalido(
            "los subtítulos incumplen reglas que invalidan el resultado:\n"
            + validacion.resumen(),
            stage="subtitles",
        )

    ruta_srt = ws.ruta(RUTA_SRT)
    ruta_ass = ws.ruta(RUTA_ASS)
    ruta_srt.parent.mkdir(parents=True, exist_ok=True)
    ruta_srt.write_text(serializers.a_srt(cues), encoding="utf-8")
    # El ASS sale de los mismos cues que el SRT, no de volver a leer el SRT:
    # así no puede convertirse en una segunda fuente de verdad.
    ruta_ass.write_text(
        serializers.a_ass(
            cues,
            fuente=ctx.settings.subtitle_font,
            tamano=ctx.settings.subtitle_font_size,
            margen_vertical=ctx.settings.subtitle_margin_v,
            margen_horizontal=ctx.settings.subtitle_margin_h,
        ),
        encoding="utf-8",
    )

    contrato = [
        SubtitleCue(
            index=indice,
            start_seconds=cue.inicio_s,
            end_seconds=cue.fin_s,
            text="\n".join(cue.lineas or [cue.texto]),
        )
        for indice, cue in enumerate(cues, start=1)
    ]

    avisos = list(incidencias) + [
        f"{c['nombre']}: {c['detalle']}" for c in validacion.avisos
    ]
    entrada = ctx.manifest.registrar_etapa("subtitles")
    entrada.metadata = {
        **entrada.metadata,
        "cue_count": len(contrato),
        "duration_seconds": round(voz.audio_duration_seconds, 3),
        "last_cue_end_seconds": round(contrato[-1].end_seconds, 3),
        "discarded_boundaries": descartados,
        "alignment_seconds": round(duracion_alineacion, 3),
        "max_lines": MAX_LINEAS_SUBTITULO,
        "target_chars_per_line": CARACTERES_OBJETIVO_LINEA,
        "warnings": avisos,
    }

    log_evento(
        ws.run_id, "subtitles", "aligned",
        cues=len(contrato), discarded=descartados,
        last_cue_end_s=contrato[-1].end_seconds,
        audio_s=voz.audio_duration_seconds, alignment_s=duracion_alineacion,
    )
    for aviso in avisos:
        log_evento(ws.run_id, "subtitles", "warning", detail=aviso)

    return SubtitleAsset(
        run_id=ws.run_id,
        language=guion.language,
        source_script_sha256=huella_guion,
        word_boundary_artifact=ws.relativa(ws.ruta_artefacto(ARTEFACTO_LIMITES)),
        srt_path=ws.relativa(ruta_srt),
        ass_path=ws.relativa(ruta_ass),
        cue_count=len(contrato),
        # La referencia del final del video es el audio medido, no el último
        # cue ni la estimación del guion.
        duration_seconds=voz.audio_duration_seconds,
        max_lines=MAX_LINEAS_SUBTITULO,
        target_chars_per_line=CARACTERES_OBJETIVO_LINEA,
        cues=contrato,
    )


def validar_subtitulos(artefacto: SubtitleAsset, ws: Workspace) -> None:
    """Unos subtítulos sin sus archivos ya no son reutilizables."""
    for ruta in (artefacto.srt_path, artefacto.ass_path):
        archivo = ws.ruta(ruta)
        if not archivo.is_file() or archivo.stat().st_size == 0:
            raise ArtefactoCorrupto(f"falta el archivo de subtítulos {ruta!r}")
    if not ws.ruta(artefacto.word_boundary_artifact).is_file():
        raise ArtefactoCorrupto(
            f"falta el artefacto de tiempos {artefacto.word_boundary_artifact!r}"
        )


def subtitulos_vigentes(artefacto: SubtitleAsset, ctx: ContextoEtapa) -> None:
    """Unos subtítulos de otro guion, o de otro audio, no valen."""
    guion = _guion(ctx)
    if artefacto.source_script_sha256 != hash_guion(guion.full_text):
        raise ArtefactoCorrupto(
            "los subtítulos corresponden a otro guion; se vuelven a generar"
        )

    voz = ctx.workspace.leer_artefacto(ARTEFACTO_VOZ, VoiceAsset)
    if abs(artefacto.duration_seconds - voz.audio_duration_seconds) > 1e-6:
        raise ArtefactoCorrupto(
            "los subtítulos se calcularon contra otra duración de audio"
        )

    # Comparar duraciones no basta: cambiar de voz produce otros tiempos por
    # palabra aunque el audio dure lo mismo. Que los tiempos sean posteriores a
    # los subtítulos significa que estos ya no salen de ellos.
    limites = ctx.workspace.leer_artefacto(ARTEFACTO_LIMITES, WordBoundaryAsset)
    if limites.created_at > artefacto.created_at:
        raise ArtefactoCorrupto(
            "los tiempos por palabra se regeneraron después de estos subtítulos"
        )


# ---------------------------------------------------------------------------
# Etapas
# ---------------------------------------------------------------------------


ETAPA_VOZ = Etapa(
    nombre="voice",
    artefacto=ARTEFACTO_VOZ,
    modelo=VoiceAsset,
    ejecutar=sintetizar_voz,
    validar=validar_voz,
    vigente=voz_vigente,
)

ETAPA_SUBTITULOS = Etapa(
    nombre="subtitles",
    artefacto=ARTEFACTO_SUBTITULOS,
    modelo=SubtitleAsset,
    ejecutar=generar_subtitulos,
    validar=validar_subtitulos,
    vigente=subtitulos_vigentes,
)


def construir_pipeline_voz() -> list[Etapa]:
    """AdaptedScript → VoiceAsset → SubtitleAsset. Aquí termina Gate 3."""
    return [ETAPA_VOZ, ETAPA_SUBTITULOS]
