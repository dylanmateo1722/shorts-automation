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
from pathlib import Path

from app.adapters.media import sha256_archivo
from app.config.provenance import (
    VERSION_POLITICA,
    ArchivoDeclaraciones,
    declarar_referencia,
    decidir,
)
from app.contracts.models import (
    AdaptedScript,
    BaseLicencia,
    ClaseFuente,
    EstadoValidacion,
    Evidence,
    LicenseDecision,
    OverlaySpec,
    ProvenanceLedger,
    SourceAsset,
    SubtitleAsset,
    TipoEvidencia,
    TipoMedio,
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

#: Clave de parámetro con la ruta del archivo JSON que declara fuentes externas.
#: Es la única entrada de material ajeno: no hay búsqueda ni descarga automática.
CLAVE_DECLARACIONES = "source_declarations"

#: Clave de parámetro con el ``asset_id`` de la fuente declarada que se usa como
#: material visual de **esta** pieza.
#:
#: Son dos decisiones distintas y el sistema las mantiene separadas:
#:
#:   «puedo renderizar este recurso»      lo decide la política de procedencia
#:   «quiero este recurso como material»  lo decide una persona, aquí
#:
#: Autorizar no es seleccionar. Una corrida puede declarar diez fuentes
#: ``render_allowed`` y usar una sola; convertir automáticamente en material
#: visual todo lo autorizado borraría esa diferencia y metería en el vídeo cosas
#: que nadie eligió. Por eso la selección es explícita, se nombra por
#: ``asset_id`` —la identidad estable de la fuente, no su ruta— y queda en la
#: configuración de la corrida, que el manifest persiste.
#:
#: Seleccionar tampoco autoriza: un recurso seleccionado que no esté
#: ``render_allowed`` detiene la corrida con un error, no se cuela ni se ignora.
CLAVE_MATERIAL_SELECCIONADO = "render_material_asset"

#: Prefijo de los identificadores de las fuentes que produce el propio pipeline.
#: El identificador es el papel del recurso, no su ruta: la ruta puede cambiar y
#: la identidad no.
PREFIJO_PROPIO = "pipeline"


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


def material_seleccionado(ctx: ContextoEtapa) -> str | None:
    """``asset_id`` de la fuente elegida como material visual, si hay alguna.

    Devuelve el identificador, no la ruta: resolver la ruta exige el ledger, y
    quien la resuelva debe pasar por la decisión de licencia. Así no hay forma
    de saltarse la procedencia leyendo la configuración directamente.
    """
    bruto = ctx.parametros.get(CLAVE_MATERIAL_SELECCIONADO)
    if bruto is None:
        return None
    asset_id = str(bruto).strip()
    if not asset_id:
        raise EntradaInvalida(
            "se indicó un material visual sin identificador",
            stage="provenance",
        )
    return asset_id


def resolver_material(
    ledger: ProvenanceLedger, asset_id: str, *, etapa: str
) -> str:
    """Ruta del material seleccionado, **solo** si la procedencia lo autoriza.

    Es el único camino de un ``asset_id`` elegido a una ruta utilizable, y pasa
    por las tres preguntas en orden: existe la fuente, hay decisión, y la
    decisión es ``render_allowed``. Un recurso ``reference_only``,
    ``needs_review``, ``blocked`` o sin decidir termina aquí con un error que lo
    nombra: seleccionarlo no lo autoriza.

    La huella no se comprueba en esta función. La comprueba
    ``verificar_procedencia`` sobre ``RenderJob.assets_de_render``, que es la
    puerta única del render; duplicar la comprobación aquí daría dos sitios
    donde puede divergir.
    """
    fuente = ledger.fuente(asset_id)
    if fuente is None:
        raise EntradaInvalida(
            f"se eligió {asset_id!r} como material visual y no hay ninguna "
            f"fuente declarada con ese identificador",
            stage=etapa,
        )
    if not fuente.local_path:
        raise EntradaInvalida(
            f"la fuente {asset_id!r} no tiene archivo local: no puede ser el "
            f"material visual de la pieza",
            stage=etapa,
        )
    decision = ledger.decision(asset_id)
    if decision is None:
        raise ProcedenciaInvalida(
            f"se eligió {asset_id!r} como material visual y no tiene ninguna "
            f"decisión de licencia",
            stage=etapa,
        )
    if decision.decision is not ClaseFuente.render_permitido:
        raise ProcedenciaInvalida(
            f"se eligió {asset_id!r} como material visual y está clasificado "
            f"{decision.decision.value!r}: elegir un recurso no lo autoriza. "
            f"{decision.reason}",
            stage=etapa,
        )
    return fuente.local_path


def _material_visual(ws: Workspace) -> str | None:
    """Ruta del material visual propio, si alguna etapa ya lo generó.

    Se consulta por artefacto persistido, no por convención de nombre ni por
    un estado en memoria: al reanudar una corrida la etapa que lo generó puede
    haberse omitido, y aun así el material sigue siendo suyo.
    """
    from app.contracts.models import RenderResult

    nombre = "render_material"
    if not ws.existe(nombre):
        return None
    try:
        material = ws.leer_artefacto(nombre, RenderResult)
    except ArtefactoCorrupto:
        return None
    return material.output_path


def _propios(ctx: ContextoEtapa) -> list[tuple[str, str, TipoMedio, str, str]]:
    """Recursos que produce el pipeline: papel, ruta, medio, prueba y por qué.

    La evidencia de un recurso propio es el artefacto que lo produjo: registra
    proveedor, modelo y parámetros, así que permite rastrear la generación sin
    pedirle a nadie un documento jurídico. Es lo que distingue ``own`` de
    ``unknown``, que es todo lo que hace falta aquí.
    """
    ws: Workspace = ctx.workspace
    voz = _leer(ctx, ARTEFACTO_VOZ, VoiceAsset)
    subtitulos = _leer(ctx, ARTEFACTO_SUBTITULOS, SubtitleAsset)
    overlay = _leer(ctx, ARTEFACTO_OVERLAY, OverlaySpec)

    def prueba(nombre: str) -> str:
        return ws.relativa(ws.ruta_artefacto(nombre))

    filas = [
        (
            "narration", voz.audio_path, TipoMedio.audio, prueba(ARTEFACTO_VOZ),
            "narración sintetizada a partir del guion propio de esta corrida",
        ),
        (
            "subtitles_srt", subtitulos.srt_path, TipoMedio.subtitulos,
            prueba(ARTEFACTO_SUBTITULOS),
            "subtítulos propios, derivados de la narración de esta corrida",
        ),
        (
            "subtitles_ass", subtitulos.ass_path, TipoMedio.subtitulos,
            prueba(ARTEFACTO_SUBTITULOS),
            "presentación de los subtítulos propios de esta corrida",
        ),
        (
            "overlay", overlay.overlay_path, TipoMedio.subtitulos,
            prueba(ARTEFACTO_OVERLAY),
            "rótulo original generado del gancho del guion propio",
        ),
    ]

    # El material visual, cuando la corrida llega al render. Se lee del artefacto
    # y no de una lista fija porque la etapa que lo genera es posterior a este
    # Gate: si no existe, la corrida termina en la QA de artefactos y no hay
    # nada visual que autorizar.
    if (material := _material_visual(ws)) is not None:
        filas.append(
            (
                "material", material, TipoMedio.video, prueba("render_material"),
                "material visual generado por el motor a partir del guion propio",
            )
        )
    return filas


def _fuente_propia(
    ws: Workspace, papel: str, ruta: str, medio: TipoMedio, prueba: str, descripcion: str
) -> SourceAsset:
    """Registra un recurso del pipeline, con su huella y su trazabilidad."""
    archivo = ws.ruta(ruta)
    if not archivo.is_file():
        raise ProcedenciaInvalida(
            f"se declararía {ruta!r} como utilizable en el render pero el archivo "
            f"no existe",
            stage="provenance",
        )
    asset_id = f"{PREFIJO_PROPIO}:{papel}"
    return SourceAsset(
        run_id=ws.run_id,
        asset_id=asset_id,
        media_kind=medio,
        origin="shorts-automation pipeline",
        local_path=ruta,
        sha256=sha256_archivo(archivo),
        obtained_at=_ahora(),
        evidence=[
            Evidence(
                evidence_id=f"{asset_id}:origen",
                kind=TipoEvidencia.registro_propiedad,
                reference=prueba,
                description=descripcion,
            )
        ],
    )


def _declaraciones(ctx: ContextoEtapa) -> ArchivoDeclaraciones:
    """Fuentes externas declaradas a mano, si la corrida aportó un archivo."""
    ruta = ctx.parametros.get(CLAVE_DECLARACIONES)
    if not ruta:
        return ArchivoDeclaraciones()
    archivo = Path(str(ruta))
    if not archivo.is_file():
        raise EntradaInvalida(
            f"no existe el archivo de fuentes declaradas {str(ruta)!r}",
            stage="provenance",
        )
    try:
        return ArchivoDeclaraciones.leer(archivo)
    except (ValueError, OSError) as exc:
        raise EntradaInvalida(
            f"el archivo de fuentes declaradas {str(ruta)!r} no es válido: {exc}",
            stage="provenance",
        ) from exc


def _fuentes_externas(ctx: ContextoEtapa) -> list[tuple[BaseLicencia, SourceAsset]]:
    """Fuentes externas declaradas, con su huella ya calculada.

    Una declaración que apunta a un archivo local tiene que apuntar a un archivo
    que exista: si no existe, la declaración describe algo que no está, y eso no
    es una fuente sino un error de entrada. Una fuente de la que solo se conoce
    la URL es legítima y simplemente no trae ``local_path``.
    """
    ws: Workspace = ctx.workspace
    externas: list[tuple[BaseLicencia, SourceAsset]] = []
    for declaracion in _declaraciones(ctx).sources:
        try:
            fuente = declaracion.fuente(ws.run_id)
        except ValueError as exc:
            raise EntradaInvalida(
                f"fuente declarada inválida: {exc}", stage="provenance"
            ) from exc
        if fuente.local_path is not None:
            archivo = ws.ruta(fuente.local_path)
            if not archivo.is_file():
                raise EntradaInvalida(
                    f"la fuente declarada {fuente.asset_id!r} apunta a "
                    f"{fuente.local_path!r}, que no existe en la corrida",
                    stage="provenance",
                )
            if fuente.sha256 is None:
                fuente = fuente.model_copy(
                    update={"sha256": sha256_archivo(archivo)}
                )
        externas.append((declaracion.basis, fuente))
    return externas


def registrar_procedencia(ctx: ContextoEtapa) -> ProvenanceLedger:
    """Construye la cadena fuente → evidencia → decisión de toda la corrida.

    Cada recurso se registra como ``SourceAsset`` con su evidencia, y cada uno
    recibe una ``LicenseDecision`` tomada por la política. Lo que el pipeline
    produce se decide con base ``own``; lo declarado como referencia no se evalúa
    y queda ``reference_only``; lo declarado como fuente externa pasa por la
    política con la base que alega, y puede acabar en cualquiera de las cuatro
    clases.
    """
    ws: Workspace = ctx.workspace

    fuentes: list[SourceAsset] = []
    decisiones: list[LicenseDecision] = []
    permitidos: list[str] = []

    for papel, ruta, medio, prueba, descripcion in _propios(ctx):
        fuente = _fuente_propia(ws, papel, ruta, medio, prueba, descripcion)
        decision = decidir(fuente, BaseLicencia.propia, run_id=ws.run_id)
        if decision.decision is not ClaseFuente.render_permitido:
            # No debería ocurrir: un recurso propio con su evidencia cumple la
            # política. Si ocurre, es que la política cambió y el pipeline no.
            raise ProcedenciaInvalida(
                f"la política {VERSION_POLITICA} no autoriza el recurso propio "
                f"{fuente.asset_id!r}: {decision.reason}",
                stage="provenance",
            )
        fuentes.append(fuente)
        decisiones.append(decision)
        permitidos.append(ruta)

    referencias = _referencias(ctx)
    for indice, ruta in enumerate(referencias):
        fuente = SourceAsset(
            run_id=ws.run_id,
            asset_id=f"reference:{indice}",
            media_kind=TipoMedio.otro,
            origin="declarado como referencia en la entrada de la corrida",
            local_path=ruta,
            notes="consultado como referencia temática; no utilizable en el render",
        )
        fuentes.append(fuente)
        decisiones.append(declarar_referencia(fuente, run_id=ws.run_id))

    elegido = material_seleccionado(ctx)
    decidido_por_id: dict[str, LicenseDecision] = {}
    fuente_por_id: dict[str, SourceAsset] = {}

    for basis, fuente in _fuentes_externas(ctx):
        decision = decidir(fuente, basis, run_id=ws.run_id)
        fuentes.append(fuente)
        decisiones.append(decision)
        fuente_por_id[fuente.asset_id] = fuente
        decidido_por_id[fuente.asset_id] = decision

    # La fuente elegida como material visual entra en render_assets **solo** si
    # su decisión la autoriza. Las dos condiciones son necesarias y ninguna
    # basta: autorizado sin elegir no entra —hay que quererlo en esta pieza—, y
    # elegido sin autorizar detiene la corrida en vez de colarse.
    if elegido is not None:
        fuente = fuente_por_id.get(elegido)
        decision = decidido_por_id.get(elegido)
        if fuente is None:
            raise EntradaInvalida(
                f"se eligió {elegido!r} como material visual y no está entre las "
                f"fuentes declaradas de esta corrida",
                stage="provenance",
            )
        if not fuente.local_path:
            raise EntradaInvalida(
                f"la fuente {elegido!r} no tiene archivo local: no puede ser el "
                f"material visual de la pieza",
                stage="provenance",
            )
        if decision is None or decision.decision is not ClaseFuente.render_permitido:
            clase = decision.decision.value if decision else "sin decisión"
            raise ProcedenciaInvalida(
                f"se eligió {elegido!r} como material visual y está clasificado "
                f"{clase!r}: elegir un recurso no lo autoriza",
                stage="provenance",
            )
        permitidos.append(fuente.local_path)

    # El contrato rechaza el ledger si algo que no esté autorizado entrara en
    # render_assets. La restricción es estructural: no hay etapa que pueda
    # olvidarse de comprobarla.
    try:
        ledger = ProvenanceLedger(
            run_id=ws.run_id,
            sources=fuentes,
            decisions=decisiones,
            render_assets=permitidos,
        )
    except ValueError as exc:
        raise ProcedenciaInvalida(
            f"el registro de procedencia es incoherente: {exc}", stage="provenance"
        ) from exc

    por_clase = {
        clase.value: len(ledger.por_clase(clase)) for clase in ClaseFuente
    }
    entrada = ctx.manifest.registrar_etapa("provenance")
    entrada.metadata = {
        **entrada.metadata,
        "sources": len(fuentes),
        "decisions": len(decisiones),
        "render_assets": len(permitidos),
        "policy_version": VERSION_POLITICA,
        **{f"class_{nombre}": total for nombre, total in por_clase.items()},
    }
    log_evento(
        ws.run_id, "provenance", "recorded",
        sources=len(fuentes), decisions=len(decisiones),
        render_assets=len(permitidos), policy_version=VERSION_POLITICA,
        **{f"class_{nombre}": total for nombre, total in por_clase.items()},
    )
    for fuente in ledger.por_clase(ClaseFuente.solo_referencia):
        log_evento(
            ws.run_id, "provenance", "reference_only",
            asset=fuente.asset_id, path=fuente.local_path,
        )
    for clase in (ClaseFuente.revision_pendiente, ClaseFuente.bloqueado):
        for fuente in ledger.por_clase(clase):
            decision = ledger.decision(fuente.asset_id)
            log_evento(
                ws.run_id, "provenance", clase.value,
                asset=fuente.asset_id,
                basis=decision.basis.value if decision else None,
                reason=decision.reason if decision else None,
            )

    return ledger


def _huella_cambiada(fuente: SourceAsset, ws: Workspace) -> str | None:
    """Motivo por el que la huella registrada ya no describe el archivo.

    Devuelve ``None`` cuando no hay nada que objetar, incluido el caso de una
    fuente sin archivo local: conocer solo la URL de algo es legítimo.

    **Política de integridad**: una huella que no coincide *rechaza*, no pasa a
    ``needs_review``. La decisión se tomó sobre un contenido concreto; si el
    archivo cambió, la decisión ya no habla de lo que hay en disco, y degradarla
    a «pendiente de revisión» dejaría en el ledger una decisión que parece
    aplicable y no lo es.
    """
    if fuente.local_path is None or fuente.sha256 is None:
        # Sin huella registrada no hay nada que contradecir. Es el caso de una
        # referencia: se declara la ruta de algo que se consultó, que puede no
        # estar en la corrida, y su contenido nunca respaldó una autorización.
        return None
    archivo = ws.ruta(fuente.local_path)
    if not archivo.is_file():
        return f"{fuente.local_path!r} tenía huella registrada y ya no existe"
    actual = sha256_archivo(archivo)
    if actual != fuente.sha256:
        return (
            f"{fuente.local_path!r} cambió: se registró {fuente.sha256[:12]}… y "
            f"ahora es {actual[:12]}…"
        )
    return None


def validar_procedencia(artefacto: ProvenanceLedger, ws: Workspace) -> None:
    """Un recurso de render que desapareció o cambió invalida el ledger."""
    for ruta in artefacto.render_assets:
        if not ws.ruta(ruta).is_file():
            raise ArtefactoCorrupto(
                f"falta el recurso de render declarado {ruta!r}"
            )
        fuente = artefacto.fuente_de(ruta)
        if fuente is not None and (motivo := _huella_cambiada(fuente, ws)):
            raise ArtefactoCorrupto(f"la procedencia ya no describe el archivo: {motivo}")


def procedencia_vigente(artefacto: ProvenanceLedger, ctx: ContextoEtapa) -> None:
    """El ledger debe describir la corrida de ahora, no la de antes."""
    ws = ctx.workspace

    esperados = {ruta for _, ruta, _, _, _ in _propios(ctx)}
    if esperados - set(artefacto.render_assets):
        raise ArtefactoCorrupto(
            "el ledger no cubre todos los recursos propios de la corrida"
        )

    declaradas = set(_referencias(ctx))
    registradas = set(artefacto.rutas_por_clase(ClaseFuente.solo_referencia))
    if declaradas != registradas:
        raise ArtefactoCorrupto("cambiaron los recursos declarados como referencia")

    # La integridad va antes que la comparación de declaraciones: si cambió el
    # contenido de algo registrado, la decisión sobre ese contenido ya no aplica,
    # y ese es el diagnóstico preciso. Dejarlo para después haría que un archivo
    # externo modificado se reportara como «cambió la declaración», que es cierto
    # pero dice menos.
    for fuente in artefacto.sources:
        if motivo := _huella_cambiada(fuente, ws):
            raise ArtefactoCorrupto(f"la procedencia ya no describe el archivo: {motivo}")

    # Una política nueva no puede dar por buenas las decisiones de la anterior:
    # es exactamente el caso que policy_version existe para detectar.
    anteriores = artefacto.versiones_de_politica() - {VERSION_POLITICA}
    if anteriores:
        raise ArtefactoCorrupto(
            f"el ledger se decidió con la política {sorted(anteriores)} y ahora "
            f"rige {VERSION_POLITICA}"
        )

    # Una fuente externa que se declara, se retira o se modifica cambia lo que el
    # ledger afirma, aunque los recursos propios sigan iguales. Se compara el
    # contenido, no el número: editar la base alegada de una fuente no cambia
    # cuántas hay y sí cambia lo que el ledger autoriza.
    def perfil(basis: BaseLicencia | None, fuente: SourceAsset) -> tuple:
        datos = fuente.model_dump(mode="json", exclude={"created_at"})
        return (basis.value if basis else None, tuple(sorted(datos.items(), key=repr)))

    propias = {f"{PREFIJO_PROPIO}:{papel}" for papel, *_ in _propios(ctx)}
    alegadas = {f.asset_id: perfil(b, f) for b, f in _fuentes_externas(ctx)}
    registradas_ext = {
        f.asset_id: perfil(
            d.basis if (d := artefacto.decision(f.asset_id)) else None, f
        )
        for f in artefacto.sources
        if f.asset_id not in propias and not f.asset_id.startswith("reference:")
    }
    if alegadas != registradas_ext:
        raise ArtefactoCorrupto("cambiaron las fuentes externas declaradas")


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
