"""QA técnica sobre los artefactos producidos hasta Gate 3.

Qué comprueba
-------------

Propiedades **medibles y deterministas** de lo que hay en disco: que los
artefactos existen y cumplen su contrato, que los archivos a los que apuntan
están ahí, que las huellas del guion coinciden entre audio, tiempos y
subtítulos, que las duraciones son coherentes entre sí, que la procedencia
autoriza cada recurso del render y que los cinco elementos editoriales propios
están registrados con una referencia verificable.

Qué NO comprueba
----------------

Nada sobre el valor editorial, la originalidad suficiente, el cumplimiento de
derechos de autor ni la monetizabilidad. No existe puntuación de similitud, ni
porcentaje de transformación, ni umbral alguno destinado a decidir si un
contenido pasa un sistema de detección. Ese juicio es humano, se declara aparte
y su estado por defecto es ``NOT_ASSESSED``.

``technical_qa_ok`` en verde significa que los artefactos son consistentes. No
significa que la pieza deba publicarse.

Errores y avisos
----------------

Un **error** invalida la corrida para Gate 5: contrato roto, archivo ausente,
huella que no cuadra, recurso de referencia colándose en el render, elemento
obligatorio ausente. Un **aviso** es información que no bloquea: una línea de
subtítulo larga, una divergencia moderada entre la duración estimada y la real.
Ascender un aviso a error sin razón técnica convertiría una heurística en un
veredicto.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.adapters.media import inspeccionar_audio
from app.contracts.models import (
    CARACTERES_OBJETIVO_LINEA,
    MAX_LINEAS_SUBTITULO,
    TRANSFORMACIONES_OBLIGATORIAS,
    AdaptedScript,
    ClaseFuente,
    ComprobacionQA,
    EstadoQA,
    NivelQA,
    OverlaySpec,
    ProvenanceLedger,
    QAResult,
    SubtitleAsset,
    TransformationSet,
    VoiceAsset,
    WordBoundaryAsset,
    hash_guion,
)
from app.core.errors import ErrorPipeline
from app.core.logging import log_evento
from app.core.stage_runner import ContextoEtapa, Etapa
from app.core.workspace import Workspace
from app.pipeline.transformation import (
    ARTEFACTO_OVERLAY,
    ARTEFACTO_PROCEDENCIA,
    ARTEFACTO_TRANSFORMACION,
)
from app.pipeline.voice import (
    ARTEFACTO_GUION,
    ARTEFACTO_LIMITES,
    ARTEFACTO_SUBTITULOS,
    ARTEFACTO_VOZ,
)

ARTEFACTO_QA = "qa_result"

#: Divergencia entre duración estimada y real a partir de la cual se avisa. Es
#: una heurística sobre la calidad del estimador, no un límite de calidad.
AVISO_DIVERGENCIA = 0.20

#: Tolerancia al volver a medir el audio. Leer el mismo MP3 con la misma
#: herramienta da el mismo valor; el margen cubre diferencias de versión de
#: FFmpeg, no una duración distinta.
TOLERANCIA_MEDICION_S = 0.25

#: Margen con el que el último subtítulo puede acercarse al final del audio.
TOLERANCIA_FINAL_S = 0.25


@dataclass
class Acumulador:
    """Recoge comprobaciones sin decidir el veredicto hasta el final."""

    checks: list[ComprobacionQA] = field(default_factory=list)

    def error(self, nombre: str, ok: bool, detalle: str) -> bool:
        self.checks.append(
            ComprobacionQA(name=nombre, ok=bool(ok), detail=detalle, level=NivelQA.error)
        )
        return bool(ok)

    def aviso(self, nombre: str, ok: bool, detalle: str) -> bool:
        self.checks.append(
            ComprobacionQA(name=nombre, ok=bool(ok), detail=detalle, level=NivelQA.aviso)
        )
        return bool(ok)


def _cargar(acc: Acumulador, ws: Workspace, nombre: str, modelo):
    """Lee un artefacto y registra el resultado como comprobación.

    Un contrato roto se reporta con el mensaje de validación entero: es ahí
    donde aparecen el solapamiento de cues, el índice duplicado o la ruta
    absoluta, y perderlo dejaría un "artefacto inválido" que no dice nada.
    """
    try:
        artefacto = ws.leer_artefacto(nombre, modelo)
    except ErrorPipeline as exc:
        acc.error(f"{nombre}: contrato válido", False, exc.mensaje[:400])
        return None
    acc.error(f"{nombre}: contrato válido", True, f"{modelo.__name__} válido")
    return artefacto


def _archivo(acc: Acumulador, ws: Workspace, etiqueta: str, ruta: str) -> bool:
    """Comprueba que una ruta relativa apunta a un archivo real y no vacío."""
    if ruta.startswith("/") or ruta.startswith("\\"):
        return acc.error(etiqueta, False, f"ruta absoluta: {ruta!r}")
    destino = ws.ruta(ruta)
    if not destino.is_file():
        return acc.error(etiqueta, False, f"no existe {ruta!r}")
    tamano = destino.stat().st_size
    return acc.error(etiqueta, tamano > 0, f"{ruta} ({tamano} bytes)")


# ---------------------------------------------------------------------------
# Comprobaciones
# ---------------------------------------------------------------------------


def _comprobar_run(acc: Acumulador, ws: Workspace) -> None:
    acc.error(
        "run: directorio de la corrida",
        ws.dir.is_dir(),
        str(ws.relativa(ws.dir)) if ws.dir.is_dir() else f"no existe {ws.dir}",
    )
    acc.error(
        "run: el directorio lleva el run_id",
        ws.dir.name == str(ws.run_id),
        f"directorio {ws.dir.name!r}, run_id {ws.run_id}",
    )


def _comprobar_guion(acc: Acumulador, guion: AdaptedScript, run_id) -> None:
    acc.error(
        "adapted_script: run_id de la corrida",
        guion.run_id == run_id,
        f"{guion.run_id}",
    )
    acc.error(
        "adapted_script: texto no vacío",
        bool(guion.full_text.strip()),
        f"{len(guion.full_text)} caracteres, {len(guion.sections)} sección(es)",
    )
    acc.error(
        "adapted_script: duración objetivo válida",
        guion.target_duration_seconds > 0,
        f"{guion.target_duration_seconds}s",
    )
    acc.error(
        "adapted_script: duración estimada válida",
        guion.estimated_duration_seconds > 0,
        f"{guion.estimated_duration_seconds}s",
    )


def _comprobar_voz(
    acc: Acumulador, ws: Workspace, voz: VoiceAsset, huella: str, run_id
) -> None:
    acc.error("voice_asset: run_id de la corrida", voz.run_id == run_id, f"{voz.run_id}")
    _archivo(acc, ws, "voice_asset: el audio existe", voz.audio_path)
    acc.error(
        "voice_asset: duración positiva",
        voz.audio_duration_seconds > 0,
        f"{voz.audio_duration_seconds}s",
    )
    acc.error(
        "voice_asset: huella del guion",
        voz.source_script_sha256 == huella,
        "coincide con el guion actual"
        if voz.source_script_sha256 == huella
        else f"el audio se generó para otro guion ({voz.source_script_sha256[:12]}… "
        f"frente a {huella[:12]}…)",
    )
    acc.error(
        "voice_asset: proveedor y voz registrados",
        bool(voz.provider and voz.voice and voz.engine),
        f"{voz.provider}/{voz.engine} voz {voz.voice}",
    )

    # Volver a medir el archivo: si la duración registrada no es la del MP3,
    # Gate 5 cortaría el vídeo por donde no debe.
    ruta = ws.ruta(voz.audio_path)
    if ruta.is_file():
        try:
            medida = inspeccionar_audio(ruta).duracion_s
        except ErrorPipeline as exc:
            acc.error("voice_asset: el audio se puede medir", False, exc.mensaje[:200])
        else:
            diferencia = abs(medida - voz.audio_duration_seconds)
            acc.error(
                "voice_asset: la duración registrada es la del archivo",
                diferencia <= TOLERANCIA_MEDICION_S,
                f"registrada {voz.audio_duration_seconds:.2f}s, medida ahora "
                f"{medida:.2f}s (±{TOLERANCIA_MEDICION_S}s)",
            )


def _comprobar_limites(
    acc: Acumulador,
    ws: Workspace,
    limites: WordBoundaryAsset,
    voz: VoiceAsset,
    huella: str,
) -> None:
    acc.error(
        "word_boundaries: hay tiempos",
        bool(limites.boundaries),
        f"{len(limites.boundaries)} tiempo(s) por palabra",
    )
    if limites.boundaries:
        indices = [b.index for b in limites.boundaries]
        acc.error(
            "word_boundaries: índices únicos",
            len(set(indices)) == len(indices),
            f"{len(set(indices))} de {len(indices)} distintos",
        )
        acc.error(
            "word_boundaries: ordenados en el tiempo",
            all(
                b.start_seconds <= s.start_seconds
                for b, s in zip(limites.boundaries, limites.boundaries[1:])
            ),
            "los inicios no retroceden",
        )
        acc.error(
            "word_boundaries: sin tiempos negativos",
            all(b.start_seconds >= 0 and b.duration_seconds > 0 for b in limites.boundaries),
            "todos los inicios ≥ 0 y las duraciones > 0",
        )
        fin_ultimo = max(b.end_seconds for b in limites.boundaries)
        acc.error(
            "word_boundaries: dentro del audio",
            fin_ultimo <= limites.audio_duration_seconds + 1e-6,
            f"último tiempo termina en {fin_ultimo:.2f}s, audio "
            f"{limites.audio_duration_seconds:.2f}s",
        )
        # La cola de audio tras el último tiempo es normal: Edge TTS la deja.
        # Se informa porque Gate 5 necesita saber que el vídeo dura más que el
        # último subtítulo.
        cola = limites.audio_duration_seconds - fin_ultimo
        acc.aviso(
            "word_boundaries: cola de audio tras el último tiempo",
            cola <= 0,
            f"{cola:.2f}s de audio después del último WordBoundary",
        )
    acc.error(
        "word_boundaries: duración positiva",
        limites.audio_duration_seconds > 0,
        f"{limites.audio_duration_seconds}s",
    )
    acc.error(
        "word_boundaries: mismo audio que el VoiceAsset",
        limites.audio_path == voz.audio_path,
        f"{limites.audio_path!r} frente a {voz.audio_path!r}",
    )
    acc.error(
        "word_boundaries: huella del guion",
        limites.source_script_sha256 == huella,
        "coincide con el guion actual"
        if limites.source_script_sha256 == huella
        else f"tiempos de otro guion ({limites.source_script_sha256[:12]}…)",
    )


def _comprobar_subtitulos(
    acc: Acumulador,
    ws: Workspace,
    subtitulos: SubtitleAsset,
    voz: VoiceAsset,
    huella: str,
) -> None:
    _archivo(acc, ws, "subtitle_asset: el SRT existe", subtitulos.srt_path)
    _archivo(acc, ws, "subtitle_asset: el ASS existe", subtitulos.ass_path)
    _archivo(
        acc, ws,
        "subtitle_asset: el artefacto de tiempos existe",
        subtitulos.word_boundary_artifact,
    )
    acc.error(
        "subtitle_asset: apunta a los tiempos de la corrida",
        subtitulos.word_boundary_artifact == f"{ARTEFACTO_LIMITES}.json",
        f"{subtitulos.word_boundary_artifact!r}",
    )
    acc.error(
        "subtitle_asset: hay cues",
        subtitulos.cue_count > 0,
        f"{subtitulos.cue_count} cue(s)",
    )
    acc.error(
        "subtitle_asset: cue_count coincide con los cues",
        subtitulos.cue_count == len(subtitulos.cues),
        f"declara {subtitulos.cue_count}, lista {len(subtitulos.cues)}",
    )

    if subtitulos.cues:
        acc.error(
            "subtitle_asset: índices desde 1 y crecientes",
            subtitulos.cues[0].index == 1
            and all(
                s.index > a.index for a, s in zip(subtitulos.cues, subtitulos.cues[1:])
            ),
            f"del {subtitulos.cues[0].index} al {subtitulos.cues[-1].index}",
        )
        acc.error(
            "subtitle_asset: sin solapamientos",
            all(
                s.start_seconds >= a.end_seconds - 1e-6
                for a, s in zip(subtitulos.cues, subtitulos.cues[1:])
            ),
            "ningún cue empieza antes de que acabe el anterior",
        )
        acc.error(
            "subtitle_asset: tiempos válidos",
            all(
                c.start_seconds >= 0 and c.end_seconds > c.start_seconds
                for c in subtitulos.cues
            ),
            "todos los cues tienen inicio ≥ 0 y fin posterior",
        )
        lineas = {c.index: len(c.text.split("\n")) for c in subtitulos.cues}
        excedidos = [i for i, n in lineas.items() if n > MAX_LINEAS_SUBTITULO]
        acc.error(
            f"subtitle_asset: máximo {MAX_LINEAS_SUBTITULO} líneas por cue",
            not excedidos,
            "dentro del límite"
            if not excedidos
            else f"cues con más de {MAX_LINEAS_SUBTITULO} líneas: {excedidos}",
        )
        # Heurística de legibilidad, no una ley: se avisa y no se bloquea.
        largas = [
            c.index
            for c in subtitulos.cues
            if max(len(linea) for linea in c.text.split("\n")) > CARACTERES_OBJETIVO_LINEA
        ]
        acc.aviso(
            f"subtitle_asset: líneas de más de {CARACTERES_OBJETIVO_LINEA} caracteres",
            not largas,
            "ninguna" if not largas else f"cues {largas}",
        )
        fin = subtitulos.cues[-1].end_seconds
        acc.error(
            "subtitle_asset: el último cue cabe en el audio",
            fin <= voz.audio_duration_seconds + TOLERANCIA_FINAL_S,
            f"último cue termina en {fin:.2f}s, audio "
            f"{voz.audio_duration_seconds:.2f}s",
        )

    acc.error(
        "subtitle_asset: duración positiva",
        subtitulos.duration_seconds > 0,
        f"{subtitulos.duration_seconds}s",
    )
    acc.error(
        "subtitle_asset: huella del guion",
        subtitulos.source_script_sha256 == huella,
        "coincide con el guion actual"
        if subtitulos.source_script_sha256 == huella
        else f"subtítulos de otro guion ({subtitulos.source_script_sha256[:12]}…)",
    )


def _comprobar_duraciones(
    acc: Acumulador, guion: AdaptedScript, voz: VoiceAsset, subtitulos: SubtitleAsset
) -> None:
    """Coherencia entre la estimación de G2 y lo que se midió en G3.

    La referencia del final del vídeo es la duración **real** del audio. La
    estimación solo se compara para saber cuánto se desvía el estimador.
    """
    acc.error(
        "duraciones: los subtítulos usan la duración real del audio",
        abs(subtitulos.duration_seconds - voz.audio_duration_seconds) <= 1e-6,
        f"subtítulos {subtitulos.duration_seconds:.2f}s, audio "
        f"{voz.audio_duration_seconds:.2f}s",
    )
    estimada = guion.estimated_duration_seconds
    real = voz.audio_duration_seconds
    delta = real - estimada
    ratio = real / estimada if estimada > 0 else 0.0
    acc.aviso(
        "duraciones: estimada frente a real",
        estimada > 0 and abs(ratio - 1) <= AVISO_DIVERGENCIA,
        f"estimada {estimada:.2f}s, real {real:.2f}s, delta {delta:+.2f}s, "
        f"ratio {ratio:.4f}",
    )


def _comprobar_procedencia(
    acc: Acumulador,
    ws: Workspace,
    ledger: ProvenanceLedger,
    voz: VoiceAsset,
    subtitulos: SubtitleAsset,
    overlay: OverlaySpec,
) -> None:
    acc.error(
        "procedencia: hay recursos declarados",
        bool(ledger.assets),
        f"{len(ledger.assets)} recurso(s), {len(ledger.render_assets)} utilizable(s) "
        f"en el render",
    )

    colados = [
        ruta for ruta in ledger.render_assets if not ledger.permite_render(ruta)
    ]
    acc.error(
        "procedencia: ningún recurso de referencia entre los del render",
        not colados,
        "ninguno" if not colados else f"recursos de referencia en el render: {colados}",
    )

    sin_declarar = [
        ruta
        for ruta in (
            voz.audio_path, subtitulos.srt_path, subtitulos.ass_path,
            overlay.overlay_path,
        )
        if ledger.clase(ruta) is None
    ]
    acc.error(
        "procedencia: todo recurso propio está declarado",
        not sin_declarar,
        "todos declarados" if not sin_declarar else f"sin declarar: {sin_declarar}",
    )

    for ruta in ledger.render_assets:
        _archivo(acc, ws, f"procedencia: existe el recurso {ruta}", ruta)

    referencias = [
        a.asset_path
        for a in ledger.assets
        if a.source_class is ClaseFuente.solo_referencia
    ]
    acc.aviso(
        "procedencia: recursos solo de referencia",
        not referencias,
        "ninguno"
        if not referencias
        else f"{len(referencias)} recurso(s) de referencia, bloqueados para el "
        f"render: {referencias}",
    )

    sin_evidencia = [a.asset_path for a in ledger.assets if not a.evidence_ref.strip()]
    acc.error(
        "procedencia: cada recurso indica su evidencia",
        not sin_evidencia,
        "todos" if not sin_evidencia else f"sin evidencia: {sin_evidencia}",
    )


def _comprobar_transformacion(
    acc: Acumulador,
    ws: Workspace,
    conjunto: TransformationSet,
    ledger: ProvenanceLedger,
) -> None:
    faltantes = sorted(t.value for t in conjunto.faltantes)
    acc.error(
        "transformación: los cinco elementos obligatorios",
        not faltantes,
        f"{len(conjunto.elements)} de {len(TRANSFORMACIONES_OBLIGATORIAS)} "
        + ("completos" if not faltantes else f"— faltan: {faltantes}"),
    )

    sin_recurso = [
        e.kind.value for e in conjunto.elements if not ws.ruta(e.asset_ref).is_file()
    ]
    acc.error(
        "transformación: cada elemento referencia un recurso que existe",
        not sin_recurso,
        "todos verificables"
        if not sin_recurso
        else f"referencias inválidas: {sin_recurso}",
    )

    # Un elemento no puede atribuirse como propio un recurso de referencia.
    reclamados = [
        e.kind.value
        for e in conjunto.elements
        if ledger.clase(e.asset_ref) is ClaseFuente.solo_referencia
    ]
    acc.error(
        "transformación: ningún elemento se apoya en material de referencia",
        not reclamados,
        "ninguno" if not reclamados else f"elementos sobre referencia: {reclamados}",
    )

    pendientes = [
        e.kind.value for e in conjunto.elements if e.validation_status.value != "validated"
    ]
    acc.aviso(
        "transformación: elementos sin validar",
        not pendientes,
        "todos validados" if not pendientes else f"pendientes: {pendientes}",
    )


def _comprobar_overlay(acc: Acumulador, ws: Workspace, overlay: OverlaySpec) -> None:
    _archivo(acc, ws, "overlay: el archivo existe", overlay.overlay_path)
    acc.error(
        "overlay: tiene texto y duración",
        bool(overlay.text.strip()) and overlay.end_seconds > overlay.start_seconds,
        f"{overlay.end_seconds - overlay.start_seconds:.2f}s, "
        f"{overlay.text.count(chr(10)) + 1} línea(s)",
    )
    acc.error(
        "overlay: derivado del guion propio",
        overlay.source_text_reference == f"{ARTEFACTO_GUION}.json",
        f"{overlay.source_text_reference!r}",
    )


# ---------------------------------------------------------------------------
# Evaluación
# ---------------------------------------------------------------------------


def evaluar(ws: Workspace) -> QAResult:
    """Ejecuta todas las comprobaciones y construye el informe.

    No lanza: un artefacto roto es un hallazgo que debe quedar escrito, no una
    excepción que deje la corrida sin informe.
    """
    acc = Acumulador()
    _comprobar_run(acc, ws)

    guion = _cargar(acc, ws, ARTEFACTO_GUION, AdaptedScript)
    voz = _cargar(acc, ws, ARTEFACTO_VOZ, VoiceAsset)
    limites = _cargar(acc, ws, ARTEFACTO_LIMITES, WordBoundaryAsset)
    subtitulos = _cargar(acc, ws, ARTEFACTO_SUBTITULOS, SubtitleAsset)
    overlay = _cargar(acc, ws, ARTEFACTO_OVERLAY, OverlaySpec)
    ledger = _cargar(acc, ws, ARTEFACTO_PROCEDENCIA, ProvenanceLedger)
    conjunto = _cargar(acc, ws, ARTEFACTO_TRANSFORMACION, TransformationSet)

    huella = hash_guion(guion.full_text) if guion else ""

    if guion:
        _comprobar_guion(acc, guion, ws.run_id)
    if voz:
        _comprobar_voz(acc, ws, voz, huella, ws.run_id)
    if limites and voz:
        _comprobar_limites(acc, ws, limites, voz, huella)
    if subtitulos and voz:
        _comprobar_subtitulos(acc, ws, subtitulos, voz, huella)
    if guion and voz and subtitulos:
        _comprobar_duraciones(acc, guion, voz, subtitulos)
    if overlay:
        _comprobar_overlay(acc, ws, overlay)
    if ledger and voz and subtitulos and overlay:
        _comprobar_procedencia(acc, ws, ledger, voz, subtitulos, overlay)
    if conjunto and ledger:
        _comprobar_transformacion(acc, ws, conjunto, ledger)

    fallos = [c for c in acc.checks if not c.ok and c.level is NivelQA.error]
    avisos = [c for c in acc.checks if not c.ok and c.level is NivelQA.aviso]
    estado = (
        EstadoQA.reprobado
        if fallos
        else (EstadoQA.aprobado_con_avisos if avisos else EstadoQA.aprobado)
    )

    return QAResult(
        run_id=ws.run_id,
        status=estado,
        technical_qa_ok=not fallos,
        checks=acc.checks,
        transformation_artifact=f"{ARTEFACTO_TRANSFORMACION}.json",
        provenance_artifact=f"{ARTEFACTO_PROCEDENCIA}.json",
        # Técnicamente consumible por Gate 5. No dice nada sobre si la pieza
        # debe publicarse: ese juicio sigue sin evaluar, por diseño.
        ready_for_render=not fallos,
    )


def ejecutar_qa(ctx: ContextoEtapa) -> QAResult:
    """Etapa de QA técnica."""
    ws: Workspace = ctx.workspace
    informe = evaluar(ws)

    entrada = ctx.manifest.registrar_etapa("technical_qa")
    entrada.metadata = {
        **entrada.metadata,
        "status": informe.status.value,
        "technical_qa_ok": informe.technical_qa_ok,
        "checks": len(informe.checks),
        "errors": [c.name for c in informe.errors],
        "warnings": [c.name for c in informe.warnings],
        "ready_for_render": informe.ready_for_render,
        "editorial_legal_assessment": informe.editorial_legal_assessment.status.value,
    }

    log_evento(
        ws.run_id, "technical_qa", "evaluated",
        status=informe.status.value, checks=len(informe.checks),
        errors=len(informe.errors), warnings=len(informe.warnings),
        ready_for_render=informe.ready_for_render,
        editorial_legal=informe.editorial_legal_assessment.status.value,
    )
    for check in informe.errors:
        log_evento(ws.run_id, "technical_qa", "error", check=check.name, detail=check.detail)
    for check in informe.warnings:
        log_evento(ws.run_id, "technical_qa", "warning", check=check.name, detail=check.detail)

    return informe


def qa_vigente(artefacto: QAResult, ctx: ContextoEtapa) -> None:
    """Un informe anterior a cualquiera de sus dependencias ya no describe nada."""
    from app.core.errors import ArtefactoCorrupto

    for nombre, modelo in (
        (ARTEFACTO_VOZ, VoiceAsset),
        (ARTEFACTO_SUBTITULOS, SubtitleAsset),
        (ARTEFACTO_OVERLAY, OverlaySpec),
        (ARTEFACTO_PROCEDENCIA, ProvenanceLedger),
        (ARTEFACTO_TRANSFORMACION, TransformationSet),
    ):
        dependencia = ctx.workspace.leer_artefacto(nombre, modelo)
        if dependencia.created_at > artefacto.created_at:
            raise ArtefactoCorrupto(f"{nombre} cambió después de esta QA")


ETAPA_QA = Etapa(
    nombre="technical_qa",
    artefacto=ARTEFACTO_QA,
    modelo=QAResult,
    ejecutar=ejecutar_qa,
    vigente=qa_vigente,
)


def construir_pipeline_transformacion() -> list[Etapa]:
    """Overlay → procedencia → transformación → QA técnica. Aquí termina Gate 4."""
    from app.pipeline.transformation import (
        ETAPA_OVERLAY,
        ETAPA_PROCEDENCIA,
        ETAPA_TRANSFORMACION,
    )

    return [ETAPA_OVERLAY, ETAPA_PROCEDENCIA, ETAPA_TRANSFORMACION, ETAPA_QA]
