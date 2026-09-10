"""Capa de transformación editorial: overlay propio, procedencia y elementos.

Tres etapas, cada una con un asunto distinto:

* **overlay** crea el único recurso visual que Gate 4 aporta, a partir del
  gancho del guion propio. Genera el archivo; **no** lo compone sobre vídeo:
  eso es de Gate 5.
* **provenance** declara qué recursos tiene la corrida y cuáles puede tocar el
  render. Es la etapa que convierte ``reference_only`` en una restricción real.
* **transformation** registra los cinco elementos editoriales propios, cada uno
  apuntando a un recurso verificable.

Qué NO hace esta capa
---------------------

No vuelve a sintetizar voz ni a generar subtítulos: Gate 3 ya los produjo y
aquí solo se referencian. No hay un segundo sistema de subtítulos, ni una
segunda copia del guion.

No calcula similitud, ni porcentaje de transformación, ni nada que pretenda
decidir si un contenido evita un sistema de detección de copyright. Registrar
los elementos propios es una cuestión de trazabilidad editorial; la suficiencia
legal es un juicio humano y vive sin evaluar en ``QAResult``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.contracts.models import (
    AdaptedScript,
    AssetProvenance,
    BaseLicencia,
    ClaseFuente,
    EstadoValidacion,
    OverlaySpec,
    ProvenanceLedger,
    SubtitleAsset,
    TipoTransformacion,
    TransformationElement,
    TransformationSet,
    VoiceAsset,
)
from app.core.errors import ArtefactoCorrupto, EntradaInvalida, ProcedenciaInvalida
from app.core.logging import log_evento
from app.core.stage_runner import ContextoEtapa, Etapa
from app.core.workspace import Workspace
from app.pipeline.voice import (
    ARTEFACTO_GUION,
    ARTEFACTO_LIMITES,
    ARTEFACTO_SUBTITULOS,
    ARTEFACTO_VOZ,
)

ARTEFACTO_OVERLAY = "overlay_spec"
ARTEFACTO_PROCEDENCIA = "provenance_ledger"
ARTEFACTO_TRANSFORMACION = "transformation_set"

RUTA_OVERLAY = "overlay/overlay.ass"

#: Cuánto permanece en pantalla el rótulo del gancho. Heurístico: lo suficiente
#: para leerlo sin tapar el arranque de la narración. Gate 5 lo validará sobre
#: vídeo real.
DURACION_OVERLAY_S = 3.0

#: Máximo de caracteres por línea del rótulo. Es más grande que el del
#: subtítulo porque el cuerpo de letra del rótulo también lo es.
CARACTERES_LINEA_OVERLAY = 22

#: Clave de parámetro con los recursos que la corrida consultó como referencia.
#: Se declaran explícitamente: el pipeline no los descubre ni los descarga.
CLAVE_REFERENCIAS = "reference_assets"


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _guion(ctx: ContextoEtapa) -> AdaptedScript:
    guion = ctx.previos.get(ARTEFACTO_GUION)
    if guion is None:
        guion = ctx.workspace.leer_artefacto(ARTEFACTO_GUION, AdaptedScript)
    return guion


def _leer(ctx: ContextoEtapa, nombre: str, modelo):
    artefacto = ctx.previos.get(nombre)
    if artefacto is None:
        artefacto = ctx.workspace.leer_artefacto(nombre, modelo)
    return artefacto


# ---------------------------------------------------------------------------
# Overlay propio
# ---------------------------------------------------------------------------


def _rotulo(hook: str, max_linea: int = CARACTERES_LINEA_OVERLAY) -> str:
    """Reparte el gancho en líneas cortas para el rótulo.

    El texto es el del guion, sin reescribirlo: el overlay es original porque
    el guion lo es, no porque aquí se invente una frase nueva.
    """
    lineas: list[str] = []
    actual = ""
    for palabra in hook.split():
        candidata = f"{actual} {palabra}".strip()
        if actual and len(candidata) > max_linea:
            lineas.append(actual)
            actual = palabra
        else:
            actual = candidata
    if actual:
        lineas.append(actual)
    return "\n".join(lineas)


def generar_overlay(ctx: ContextoEtapa) -> OverlaySpec:
    """Escribe el rótulo propio del pipeline y sus parámetros de composición."""
    ws: Workspace = ctx.workspace
    guion = _guion(ctx)
    voz = _leer(ctx, ARTEFACTO_VOZ, VoiceAsset)

    texto = _rotulo(guion.hook)
    fin = min(DURACION_OVERLAY_S, voz.audio_duration_seconds)
    if fin <= 0:
        raise EntradaInvalida(
            f"la narración dura {voz.audio_duration_seconds}s: no hay dónde "
            f"poner el rótulo",
            stage="overlay",
        )

    destino = ws.ruta(RUTA_OVERLAY)
    destino.parent.mkdir(parents=True, exist_ok=True)
    # Se reutiliza el generador de rótulos validado en Gate 0.5, que existe
    # porque el FFmpeg empaquetado no trae el filtro drawtext pero sí libass.
    from poc.srt import ass_titulo

    destino.write_text(
        ass_titulo(
            texto,
            fin,
            fuente=ctx.settings.subtitle_font,
            tamano=ctx.settings.subtitle_font_size + 20,
        ),
        encoding="utf-8",
    )

    entrada = ctx.manifest.registrar_etapa("overlay")
    entrada.metadata = {
        **entrada.metadata,
        "overlay_format": "ass",
        "overlay_seconds": round(fin, 3),
        "lines": texto.count("\n") + 1,
        "source": ARTEFACTO_GUION,
    }
    log_evento(
        ws.run_id, "overlay", "generated",
        format="ass", seconds=fin, lines=texto.count("\n") + 1,
    )

    return OverlaySpec(
        run_id=ws.run_id,
        overlay_path=ws.relativa(destino),
        overlay_format="ass",
        text=texto,
        start_seconds=0.0,
        end_seconds=fin,
        source_text_reference=ws.relativa(ws.ruta_artefacto(ARTEFACTO_GUION)),
    )


def validar_overlay(artefacto: OverlaySpec, ws: Workspace) -> None:
    ruta = ws.ruta(artefacto.overlay_path)
    if not ruta.is_file() or ruta.stat().st_size == 0:
        raise ArtefactoCorrupto(f"falta el overlay {artefacto.overlay_path!r}")


def overlay_vigente(artefacto: OverlaySpec, ctx: ContextoEtapa) -> None:
    """Un rótulo de otro gancho, o de otra duración de audio, ya no sirve."""
    guion = _guion(ctx)
    if artefacto.text != _rotulo(guion.hook):
        raise ArtefactoCorrupto("el rótulo corresponde a otro gancho del guion")
    voz = ctx.workspace.leer_artefacto(ARTEFACTO_VOZ, VoiceAsset)
    if artefacto.end_seconds > voz.audio_duration_seconds + 1e-6:
        raise ArtefactoCorrupto("el rótulo dura más que la narración actual")


# ---------------------------------------------------------------------------
# Procedencia
# ---------------------------------------------------------------------------


def _referencias(ctx: ContextoEtapa) -> list[str]:
    """Recursos que la corrida consultó solo como referencia.

    Llegan declarados por parámetro. El pipeline no descubre ni descarga nada:
    alguien afirma "esto lo consulté", y a partir de ahí el ledger lo bloquea.
    """
    brutas = ctx.parametros.get(CLAVE_REFERENCIAS) or []
    if isinstance(brutas, str):
        brutas = [brutas]
    rutas: list[str] = []
    for bruta in brutas:
        ruta = str(bruta).strip()
        if not ruta:
            continue
        if ruta.startswith("/") or ruta.startswith("\\"):
            raise EntradaInvalida(
                f"el recurso de referencia {ruta!r} es una ruta absoluta; debe "
                f"ser relativa al directorio de la corrida",
                stage="provenance",
            )
        rutas.append(ruta)
    return rutas


def registrar_procedencia(ctx: ContextoEtapa) -> ProvenanceLedger:
    """Declara cada recurso de la corrida y cuáles puede usar el render."""
    ws: Workspace = ctx.workspace
    voz = _leer(ctx, ARTEFACTO_VOZ, VoiceAsset)
    subtitulos = _leer(ctx, ARTEFACTO_SUBTITULOS, SubtitleAsset)
    overlay = _leer(ctx, ARTEFACTO_OVERLAY, OverlaySpec)

    # Todo lo que el pipeline produce es propio, y esa es su base de
    # procedencia. No se afirma nada sobre material de terceros porque en esta
    # corrida no hay ninguno.
    propios = [
        (voz.audio_path, "narración sintetizada a partir del guion propio"),
        (subtitulos.srt_path, "subtítulos propios, alineados con la narración"),
        (subtitulos.ass_path, "presentación de los subtítulos propios"),
        (overlay.overlay_path, "rótulo original generado del gancho del guion"),
    ]
    assets = [
        AssetProvenance(
            asset_path=ruta,
            source_class=ClaseFuente.render_permitido,
            basis=BaseLicencia.propia,
            evidence_ref="generado por este pipeline",
            description=descripcion,
        )
        for ruta, descripcion in propios
    ]

    referencias = _referencias(ctx)
    for ruta in referencias:
        assets.append(
            AssetProvenance(
                asset_path=ruta,
                source_class=ClaseFuente.solo_referencia,
                basis=BaseLicencia.ninguna,
                description="consultado como referencia temática; no utilizable en el render",
                evidence_ref="declarado como referencia en la entrada de la corrida",
            )
        )

    permitidos = [ruta for ruta, _ in propios]
    for ruta in permitidos:
        if not ws.ruta(ruta).is_file():
            raise ProcedenciaInvalida(
                f"se declararía {ruta!r} como utilizable en el render pero el "
                f"archivo no existe",
                stage="provenance",
            )

    entrada = ctx.manifest.registrar_etapa("provenance")
    entrada.metadata = {
        **entrada.metadata,
        "assets": len(assets),
        "render_allowed": len(permitidos),
        "reference_only": len(referencias),
    }
    log_evento(
        ws.run_id, "provenance", "recorded",
        assets=len(assets), render_allowed=len(permitidos),
        reference_only=len(referencias),
    )
    for ruta in referencias:
        log_evento(ws.run_id, "provenance", "reference_only", asset=ruta)

    # El contrato rechaza el ledger si algo de referencia entrara aquí.
    return ProvenanceLedger(run_id=ws.run_id, assets=assets, render_assets=permitidos)


def validar_procedencia(artefacto: ProvenanceLedger, ws: Workspace) -> None:
    """Un recurso de render que desapareció invalida el ledger."""
    for ruta in artefacto.render_assets:
        if not ws.ruta(ruta).is_file():
            raise ArtefactoCorrupto(
                f"falta el recurso de render declarado {ruta!r}"
            )


def procedencia_vigente(artefacto: ProvenanceLedger, ctx: ContextoEtapa) -> None:
    """El ledger debe cubrir exactamente los recursos de la corrida de ahora."""
    voz = ctx.workspace.leer_artefacto(ARTEFACTO_VOZ, VoiceAsset)
    subtitulos = ctx.workspace.leer_artefacto(ARTEFACTO_SUBTITULOS, SubtitleAsset)
    overlay = ctx.workspace.leer_artefacto(ARTEFACTO_OVERLAY, OverlaySpec)

    esperados = {
        voz.audio_path, subtitulos.srt_path, subtitulos.ass_path,
        overlay.overlay_path,
    }
    if esperados - set(artefacto.render_assets):
        raise ArtefactoCorrupto(
            "el ledger no cubre todos los recursos propios de la corrida"
        )
    declaradas = {r for r in _referencias(ctx)}
    registradas = {
        a.asset_path for a in artefacto.assets
        if a.source_class is ClaseFuente.solo_referencia
    }
    if declaradas != registradas:
        raise ArtefactoCorrupto(
            "cambiaron los recursos declarados como referencia"
        )


# ---------------------------------------------------------------------------
# Elementos de transformación
# ---------------------------------------------------------------------------


def registrar_transformacion(ctx: ContextoEtapa) -> TransformationSet:
    """Registra los cinco elementos editoriales propios de la corrida.

    Cada uno apunta a un recurso que existe y que la procedencia permite usar.
    Un elemento que apuntara a un recurso de referencia es un error, no un
    aviso: significaría atribuirse como propio algo que no lo es.
    """
    ws: Workspace = ctx.workspace
    guion = _leer(ctx, ARTEFACTO_GUION, AdaptedScript)
    voz = _leer(ctx, ARTEFACTO_VOZ, VoiceAsset)
    subtitulos = _leer(ctx, ARTEFACTO_SUBTITULOS, SubtitleAsset)
    overlay = _leer(ctx, ARTEFACTO_OVERLAY, OverlaySpec)
    ledger = _leer(ctx, ARTEFACTO_PROCEDENCIA, ProvenanceLedger)

    ruta_guion = ws.relativa(ws.ruta_artefacto(ARTEFACTO_GUION))
    ruta_limites = ws.relativa(ws.ruta_artefacto(ARTEFACTO_LIMITES))

    # El contexto añadido es el que la adaptación ya registró en sus notas de
    # transformación. No se genera contexto nuevo: inventar un dato factual
    # para poder rellenar un campo sería exactamente lo que Gate 2 prohíbe.
    notas = [n for n in guion.transformation_notes if n.strip()]
    if notas:
        razon_contexto = (
            f"{len(notas)} nota(s) de transformación editorial registradas por la "
            f"adaptación: " + "; ".join(notas[:3])[:240]
        )
    else:
        razon_contexto = (
            "la adaptación no registró contexto adicional; ausencia declarada "
            "en lugar de inventar información factual"
        )

    elementos = [
        TransformationElement(
            kind=TipoTransformacion.narracion_propia,
            rationale=(
                f"narración sintetizada por {voz.provider} con la voz {voz.voice} "
                f"a partir del guion propio ({voz.audio_duration_seconds:.2f}s medidos)"
            ),
            asset_ref=voz.audio_path,
            validation_status=EstadoValidacion.validado,
        ),
        TransformationElement(
            kind=TipoTransformacion.guion_reestructurado,
            rationale=(
                "el guion narrado es el resultado de la adaptación editorial: "
                f"{len(guion.sections)} sección(es) reordenadas y condensadas "
                f"desde la traducción, no una copia de la fuente"
            ),
            asset_ref=ruta_guion,
            validation_status=EstadoValidacion.validado,
        ),
        TransformationElement(
            kind=TipoTransformacion.contexto_agregado,
            rationale=razon_contexto,
            asset_ref=ruta_guion,
            validation_status=(
                EstadoValidacion.validado if notas else EstadoValidacion.pendiente
            ),
        ),
        TransformationElement(
            kind=TipoTransformacion.subtitulos_propios,
            rationale=(
                f"{subtitulos.cue_count} cues propios, alineados con los tiempos "
                f"por palabra de {ruta_limites} y con el texto del guion"
            ),
            asset_ref=subtitulos.srt_path,
            validation_status=EstadoValidacion.validado,
        ),
        TransformationElement(
            kind=TipoTransformacion.overlay_visual,
            rationale=(
                f"rótulo original generado del gancho del guion, visible "
                f"{overlay.end_seconds - overlay.start_seconds:.2f}s"
            ),
            asset_ref=overlay.overlay_path,
            validation_status=EstadoValidacion.validado,
        ),
    ]

    # La procedencia manda: un elemento no puede reclamar como propio algo que
    # el ledger no autoriza para el render.
    for elemento in elementos:
        if elemento.asset_ref.endswith(".json"):
            # Los artefactos del propio pipeline no son assets de render.
            if not ws.ruta(elemento.asset_ref).is_file():
                raise ProcedenciaInvalida(
                    f"el elemento {elemento.kind.value!r} referencia "
                    f"{elemento.asset_ref!r}, que no existe",
                    stage="transformation",
                )
            continue
        if not ledger.permite_render(elemento.asset_ref):
            clase = ledger.clase(elemento.asset_ref)
            raise ProcedenciaInvalida(
                f"el elemento {elemento.kind.value!r} referencia "
                f"{elemento.asset_ref!r}, clasificado "
                f"{clase.value if clase else 'sin declarar'!r}",
                stage="transformation",
            )

    entrada = ctx.manifest.registrar_etapa("transformation")
    entrada.metadata = {
        **entrada.metadata,
        "elements": [e.kind.value for e in elementos],
        "added_context_notes": len(notas),
    }
    log_evento(
        ws.run_id, "transformation", "recorded",
        elements=len(elementos), context_notes=len(notas),
    )

    return TransformationSet(run_id=ws.run_id, elements=elementos)


def validar_transformacion(artefacto: TransformationSet, ws: Workspace) -> None:
    """Un elemento cuyo recurso desapareció deja de ser evidencia."""
    for elemento in artefacto.elements:
        if not ws.ruta(elemento.asset_ref).is_file():
            raise ArtefactoCorrupto(
                f"el elemento {elemento.kind.value!r} apunta a "
                f"{elemento.asset_ref!r}, que no existe"
            )


def transformacion_vigente(artefacto: TransformationSet, ctx: ContextoEtapa) -> None:
    """Si alguna dependencia se regeneró, los elementos registrados son viejos.

    El ledger entra en la lista porque la etapa lo consulta para decidir si un
    elemento puede reclamar su recurso: si cambia la procedencia, la decisión
    hay que volver a tomarla.
    """
    for nombre, modelo in (
        (ARTEFACTO_VOZ, VoiceAsset),
        (ARTEFACTO_SUBTITULOS, SubtitleAsset),
        (ARTEFACTO_OVERLAY, OverlaySpec),
        (ARTEFACTO_PROCEDENCIA, ProvenanceLedger),
    ):
        dependencia = ctx.workspace.leer_artefacto(nombre, modelo)
        if dependencia.created_at > artefacto.created_at:
            raise ArtefactoCorrupto(
                f"{nombre} se regeneró después de registrar la transformación"
            )


# ---------------------------------------------------------------------------
# Etapas
# ---------------------------------------------------------------------------


ETAPA_OVERLAY = Etapa(
    nombre="overlay",
    artefacto=ARTEFACTO_OVERLAY,
    modelo=OverlaySpec,
    ejecutar=generar_overlay,
    validar=validar_overlay,
    vigente=overlay_vigente,
)

ETAPA_PROCEDENCIA = Etapa(
    nombre="provenance",
    artefacto=ARTEFACTO_PROCEDENCIA,
    modelo=ProvenanceLedger,
    ejecutar=registrar_procedencia,
    validar=validar_procedencia,
    vigente=procedencia_vigente,
)

ETAPA_TRANSFORMACION = Etapa(
    nombre="transformation",
    artefacto=ARTEFACTO_TRANSFORMACION,
    modelo=TransformationSet,
    ejecutar=registrar_transformacion,
    validar=validar_transformacion,
    vigente=transformacion_vigente,
)
