"""Capa lingüística: transcripción → traducción → guion adaptado.

Dos etapas separadas a propósito:

* **Traducción** preserva el significado. No mejora, no resume, no adapta.
* **Adaptación** convierte esa traducción en un guion locutable y
  subtitulable, con libertad para reordenar y condensar pero no para inventar.

Mantenerlas separadas es lo que permite demostrar después qué se transformó.

La frontera de Gate 2 termina en ``AdaptedScript``. Aquí no hay síntesis de
voz, ni tiempos por palabra, ni subtítulos.
"""

from __future__ import annotations

from uuid import UUID

from app.adapters.llm import ProveedorLLM
from app.config import prompts
from app.config.settings import Settings
from app.contracts.models import (
    AdaptedScript,
    ProcedenciaIA,
    SeccionGuion,
    SegmentoTexto,
    TipoSeccion,
    Transcript,
    Translation,
)
from app.core.errors import ContenidoInvalido, EntradaInvalida, RespuestaInvalida
from app.core.logging import log_evento
from app.core.stage_runner import ContextoEtapa, Etapa
from app.pipeline import validacion

ARTEFACTO_TRANSCRIPT = "transcript"
ARTEFACTO_TRADUCCION = "translation"
ARTEFACTO_GUION = "adapted_script"

DURACION_OBJETIVO_POR_DEFECTO = 45.0


# ---------------------------------------------------------------------------
# Lectura estricta de la respuesta del modelo
# ---------------------------------------------------------------------------


def _texto_obligatorio(datos: dict, clave: str) -> str:
    valor = datos.get(clave)
    if not isinstance(valor, str) or not valor.strip():
        raise RespuestaInvalida(
            f"la respuesta no trae un campo {clave!r} de texto no vacío"
        )
    return valor.strip()


def _lista_de_textos(datos: dict, clave: str) -> list[str]:
    valor = datos.get(clave) or []
    if not isinstance(valor, list) or any(not isinstance(v, str) for v in valor):
        raise RespuestaInvalida(f"el campo {clave!r} debe ser una lista de textos")
    return [v.strip() for v in valor if v.strip()]


def _secciones(datos: dict) -> list[SeccionGuion]:
    brutas = datos.get("sections")
    if not isinstance(brutas, list) or not brutas:
        raise RespuestaInvalida("la respuesta no trae una lista 'sections' no vacía")

    secciones: list[SeccionGuion] = []
    for indice, bruta in enumerate(brutas):
        if not isinstance(bruta, dict):
            raise RespuestaInvalida(f"la sección nº{indice} no es un objeto")
        tipo = str(bruta.get("kind") or bruta.get("type") or "").strip()
        try:
            kind = TipoSeccion(tipo)
        except ValueError as exc:
            admitidos = [t.value for t in TipoSeccion]
            raise RespuestaInvalida(
                f"tipo de sección desconocido {tipo!r}; admitidos: {admitidos}"
            ) from exc
        texto = bruta.get("text")
        if not isinstance(texto, str) or not texto.strip():
            raise RespuestaInvalida(f"la sección nº{indice} no tiene texto")
        orden = bruta.get("order", indice)
        if not isinstance(orden, int) or orden < 0:
            orden = indice
        secciones.append(SeccionGuion(kind=kind, text=texto.strip(), order=orden))

    secciones.sort(key=lambda s: s.order)
    if not any(s.kind is TipoSeccion.hook for s in secciones):
        raise RespuestaInvalida("el guion no incluye ninguna sección de tipo 'hook'")
    return secciones


# ---------------------------------------------------------------------------
# Traducción
# ---------------------------------------------------------------------------


def traducir(
    transcript: Transcript,
    proveedor: ProveedorLLM,
    settings: Settings,
    *,
    run_id: UUID | None = None,
) -> Translation:
    """Traduce el transcript al idioma destino preservando el significado."""
    version = settings.translation_prompt_version
    plantilla = prompts.cargar(settings.raiz_prompts, "translation", version)
    prompt = prompts.renderizar(
        plantilla,
        source_language=transcript.source_language,
        target_language=settings.target_language,
        source_text=transcript.text,
    )

    datos = proveedor.generar_json(prompt)
    texto = _texto_obligatorio(datos, "text")
    segmentos = [SegmentoTexto(text=t) for t in _lista_de_textos(datos, "segments")]

    informe = validacion.validar_traduccion(
        transcript.text, texto, settings.target_language
    )
    identificador = prompts.identificador("translation", version)
    log_evento(
        run_id or transcript.run_id, "translation", "validated",
        errores=len(informe.errores), avisos=len(informe.avisos),
        prompt_version=identificador,
    )
    if not informe.ok:
        raise ContenidoInvalido(
            "la traducción no cumple las reglas de fidelidad:\n" + informe.resumen(),
            stage="translation",
        )

    return Translation(
        run_id=run_id or transcript.run_id,
        source_language=transcript.source_language,
        target_language=settings.target_language,
        source_transcript_reference=ARTEFACTO_TRANSCRIPT,
        text=texto,
        segments=segmentos,
        provenance=ProcedenciaIA(
            provider=proveedor.nombre,
            model=proveedor.modelo,
            prompt_version=identificador,
        ),
    )


# ---------------------------------------------------------------------------
# Adaptación
# ---------------------------------------------------------------------------


def _construir_guion(datos: dict) -> tuple[str, list[SeccionGuion], list[str], str]:
    hook = _texto_obligatorio(datos, "hook")
    secciones = _secciones(datos)
    notas = _lista_de_textos(datos, "notes")
    texto_completo = " ".join(s.text for s in secciones)
    return hook, secciones, notas, texto_completo


def adaptar(
    traduccion: Translation,
    proveedor: ProveedorLLM,
    settings: Settings,
    *,
    duracion_objetivo_s: float = DURACION_OBJETIVO_POR_DEFECTO,
    run_id: UUID | None = None,
) -> AdaptedScript:
    """Convierte la traducción en un guion locutable y subtitulable.

    Si el guion sale largo se pide una condensación al modelo, con un número
    acotado de intentos. **Nunca** se recorta el texto por caracteres: cortar
    una frase a la mitad produce un guion inservible, no uno más corto.

    Raises:
        ContenidoInvalido: si tras los intentos sigue fuera de rango, o si
            viola las reglas de fidelidad o de subtitulabilidad.
    """
    identificador_run = run_id or traduccion.run_id
    version = settings.adaptation_prompt_version
    wpm = settings.default_wpm
    palabras_objetivo = max(1, round(duracion_objetivo_s / 60 * wpm))

    plantilla = prompts.cargar(settings.raiz_prompts, "adaptation", version)
    prompt = prompts.renderizar(
        plantilla,
        target_language=settings.target_language,
        target_duration_seconds=round(duracion_objetivo_s),
        wpm=wpm,
        target_words=palabras_objetivo,
        source_text=traduccion.text,
    )

    datos = proveedor.generar_json(prompt)
    hook, secciones, notas, texto_completo = _construir_guion(datos)
    estimada = validacion.estimar_duracion_s(texto_completo, wpm)

    intentos = 0
    informe_duracion, veredicto = validacion.validar_duracion(
        estimada, duracion_objetivo_s, settings.duration_tolerance
    )
    while veredicto == "larga" and intentos < settings.max_condensation_attempts:
        intentos += 1
        log_evento(
            identificador_run, "adaptation", "condense",
            attempt=intentos, estimated_s=estimada, target_s=duracion_objetivo_s,
        )
        plantilla_condensa = prompts.cargar(
            settings.raiz_prompts, "adaptation", f"condense_{version}"
        )
        prompt_condensa = prompts.renderizar(
            plantilla_condensa,
            current_words=validacion.contar_palabras(texto_completo),
            target_words=palabras_objetivo,
            target_duration_seconds=round(duracion_objetivo_s),
            source_text=texto_completo,
        )
        datos = proveedor.generar_json(prompt_condensa)
        hook, secciones, notas, texto_completo = _construir_guion(datos)
        estimada = validacion.estimar_duracion_s(texto_completo, wpm)
        informe_duracion, veredicto = validacion.validar_duracion(
            estimada, duracion_objetivo_s, settings.duration_tolerance
        )

    if veredicto == "larga":
        raise ContenidoInvalido(
            f"el guion sigue durando {estimada:.1f}s frente a un objetivo de "
            f"{duracion_objetivo_s:.0f}s tras {intentos} condensación(es); "
            f"no se trunca el texto porque produciría un guion inservible",
            stage="adaptation",
        )

    informe = validacion.validar_adaptacion(
        traduccion.text, texto_completo, settings.target_language
    ).combinar(informe_duracion)

    identificador = prompts.identificador("adaptation", version)
    log_evento(
        identificador_run, "adaptation", "validated",
        errores=len(informe.errores), avisos=len(informe.avisos),
        estimated_s=estimada, condensations=intentos, prompt_version=identificador,
    )
    if not informe.ok:
        raise ContenidoInvalido(
            "el guion adaptado no cumple las reglas:\n" + informe.resumen(),
            stage="adaptation",
        )

    return AdaptedScript(
        run_id=identificador_run,
        language=settings.target_language,
        hook=hook,
        sections=secciones,
        full_text=texto_completo,
        target_duration_seconds=duracion_objetivo_s,
        estimated_duration_seconds=estimada,
        transformation_notes=notas,
        source_translation_reference=ARTEFACTO_TRADUCCION,
        provenance=ProcedenciaIA(
            provider=proveedor.nombre,
            model=proveedor.modelo,
            prompt_version=identificador,
        ),
    )


# ---------------------------------------------------------------------------
# Etapas
# ---------------------------------------------------------------------------


CLAVE_PROVEEDOR = "proveedor_llm"


def _proveedor(ctx: ContextoEtapa) -> ProveedorLLM:
    """Obtiene el proveedor de la corrida, construyéndolo una sola vez.

    Se admite uno inyectado por parámetros —así los tests pueden contar las
    llamadas—; si no, se construye desde la configuración y se guarda en los
    recursos de la corrida. Construir uno por etapa reiniciaría su estado y
    abriría una conexión nueva cada vez.
    """
    from app.adapters.llm import construir_proveedor

    if (inyectado := ctx.parametros.get(CLAVE_PROVEEDOR)) is not None:
        return inyectado
    if (cacheado := ctx.recursos.get(CLAVE_PROVEEDOR)) is not None:
        return cacheado
    proveedor = construir_proveedor(ctx.settings)
    ctx.recursos[CLAVE_PROVEEDOR] = proveedor
    return proveedor


def _ingerir_transcript(ctx: ContextoEtapa) -> Transcript:
    """Carga el transcript de partida y lo adopta como artefacto de la corrida."""
    import json
    from pathlib import Path

    ruta = ctx.parametros.get("transcript_path")
    if not ruta:
        raise EntradaInvalida(
            "falta el transcript de partida; indícalo con --transcript",
            stage="ingest_transcript",
        )
    origen = Path(ruta)
    if not origen.is_file():
        raise EntradaInvalida(f"no existe el transcript {ruta!r}", stage="ingest_transcript")

    try:
        datos = json.loads(origen.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise EntradaInvalida(
            f"el transcript no es JSON válido: {exc}", stage="ingest_transcript"
        ) from exc

    # El run_id lo pone la corrida: un fixture no tiene por qué conocerlo.
    datos["run_id"] = str(ctx.workspace.run_id)
    try:
        return Transcript.model_validate(datos)
    except Exception as exc:
        raise EntradaInvalida(
            f"el transcript no cumple su contrato: {exc}", stage="ingest_transcript"
        ) from exc


def _traducir(ctx: ContextoEtapa) -> Translation:
    transcript = ctx.previos.get(ARTEFACTO_TRANSCRIPT) or ctx.workspace.leer_artefacto(
        ARTEFACTO_TRANSCRIPT, Transcript
    )
    return traducir(transcript, _proveedor(ctx), ctx.settings, run_id=ctx.workspace.run_id)


def _adaptar(ctx: ContextoEtapa) -> AdaptedScript:
    traduccion = ctx.previos.get(ARTEFACTO_TRADUCCION) or ctx.workspace.leer_artefacto(
        ARTEFACTO_TRADUCCION, Translation
    )
    objetivo = float(
        ctx.parametros.get("target_duration_seconds") or DURACION_OBJETIVO_POR_DEFECTO
    )
    return adaptar(
        traduccion, _proveedor(ctx), ctx.settings,
        duracion_objetivo_s=objetivo, run_id=ctx.workspace.run_id,
    )


ETAPA_INGESTA = Etapa(
    nombre="ingest_transcript",
    artefacto=ARTEFACTO_TRANSCRIPT,
    modelo=Transcript,
    ejecutar=_ingerir_transcript,
)

ETAPA_TRADUCCION = Etapa(
    nombre="translation",
    artefacto=ARTEFACTO_TRADUCCION,
    modelo=Translation,
    ejecutar=_traducir,
)

ETAPA_ADAPTACION = Etapa(
    nombre="adaptation",
    artefacto=ARTEFACTO_GUION,
    modelo=AdaptedScript,
    ejecutar=_adaptar,
)


def construir_pipeline_linguistico() -> list[Etapa]:
    """Transcript → Translation → AdaptedScript. Aquí termina Gate 2."""
    return [ETAPA_INGESTA, ETAPA_TRADUCCION, ETAPA_ADAPTACION]
