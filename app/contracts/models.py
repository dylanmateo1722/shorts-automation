"""Contratos de artefactos entre etapas.

Cada etapa consume y produce artefactos declarados aquí; ninguna importa a
otra. Los artefactos se persisten como JSON dentro del directorio de la
ejecución.

Dos reglas transversales:

* **Rutas relativas.** Todo path se guarda relativo al directorio de la
  ejecución, nunca absoluto, para que un artefacto siga siendo válido en otra
  máquina o en un runner de CI.
* **Procedencia de IA.** Cualquier artefacto generado por un modelo registra
  proveedor, modelo y versión de prompt, sin lo cual la salida no es
  reproducible ni auditable.

Gate 1 declaró los contratos por adelantado y solo producía ``RenderJob`` y
``RenderResult``. Gate 2 implementó los lingüísticos y Gate 3 los de voz y
subtítulos. Los que quedan existen para que las etapas futuras se comuniquen
sin acoplamiento, no porque ya estén implementadas.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1"

# Los artefactos de Gate 3 declaran su propia versión de schema. Se mantiene
# separada de SCHEMA_VERSION para no reescribir la de los artefactos de Gates
# anteriores, que ya están persistidos con "1".
SCHEMA_VERSION_VOZ = "1.0"

# Reglas de legibilidad del subtítulo, fijadas por el contrato. Son una
# heurística inicial —no una garantía de que ningún cue pase de 32 caracteres—
# y cambiarlas es evolucionar el schema, no reconfigurar una corrida.
MAX_LINEAS_SUBTITULO = 2
CARACTERES_OBJETIVO_LINEA = 32


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


# --- tipos transversales ---------------------------------------------------

# Una ruta absoluta en un artefacto lo vuelve inútil en otra máquina. Se
# rechazan las tres formas: POSIX (``/home/...``), UNC (``\\servidor``) y
# unidad de Windows (``C:\...``).
_ABSOLUTA = re.compile(r"^(?:/|\\\\|[A-Za-z]:)")


def _exigir_relativa(valor: str) -> str:
    if not valor.strip():
        raise ValueError("la ruta no puede estar vacía")
    if _ABSOLUTA.match(valor):
        raise ValueError(
            f"la ruta {valor!r} es absoluta; los artefactos guardan rutas "
            f"relativas al directorio de la corrida"
        )
    return valor


def _exigir_texto(valor: str) -> str:
    if not valor.strip():
        raise ValueError("el texto no puede estar vacío")
    return valor


#: Ruta relativa al directorio ``runs/<run_id>/`` de la corrida.
RutaRelativa = Annotated[str, AfterValidator(_exigir_relativa)]

#: Texto con contenido real: ni vacío ni solo espacios.
TextoNoVacio = Annotated[str, AfterValidator(_exigir_texto)]

#: Digest SHA-256 en hexadecimal minúscula.
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def hash_guion(full_text: str) -> str:
    """Huella del guion que se narró, tal como la definen los contratos de voz.

    La normalización es deliberadamente mínima —solo se recortan los espacios
    de los extremos— porque cualquier transformación lingüística adicional
    haría que dos guiones distintos compartieran huella.
    """
    return hashlib.sha256(full_text.strip().encode("utf-8")).hexdigest()


class Artefacto(BaseModel):
    """Base de todo artefacto persistido.

    ``extra="forbid"`` es deliberado: un campo que nadie declaró es casi
    siempre una etapa escribiendo algo que otra no sabrá leer.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    run_id: UUID
    created_at: datetime = Field(default_factory=_ahora)


class ProcedenciaIA(BaseModel):
    """Origen de un artefacto generado por un modelo."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    prompt_version: str | None = None


# ---------------------------------------------------------------------------
# Descubrimiento y licencia (productores en Gates posteriores)
# ---------------------------------------------------------------------------


class ClaseFuente(str, Enum):
    """Qué se puede hacer con una fuente.

    ``reference_only`` informa el tema y el análisis, pero su material no
    puede aparecer en el render. La distinción se aplica por contrato.
    """

    autorizada = "authorized"
    solo_referencia = "reference_only"


class BaseLicencia(str, Enum):
    propia = "own"
    creative_commons = "creative_commons"
    stock = "stock"
    permiso_escrito = "written_permission"
    ninguna = "none"


class Candidate(Artefacto):
    """Un Short localizado como posible fuente o referencia."""

    video_id: str
    title: str
    channel_id: str
    published_at: datetime
    duration_s: float
    view_count: int
    like_count: int | None = None
    comment_count: int | None = None
    score: float | None = None
    # Toda métrica derivada es una heurística nuestra, no un dato de YouTube.
    score_basis: str | None = None


class LicenseDecision(Artefacto):
    """Decisión bloqueante sobre el uso de una fuente."""

    source_class: ClaseFuente
    basis: BaseLicencia
    evidence_ref: str
    decided_at: datetime = Field(default_factory=_ahora)
    decided_by: str


# ---------------------------------------------------------------------------
# Material y texto
# ---------------------------------------------------------------------------


class SourceAsset(Artefacto):
    """Material de origen ya adquirido y preparado para transcribir."""

    audio_path: str
    video_path: str | None = None
    duration_s: float
    sample_rate: int
    language_hint: str | None = None
    license_ref: str | None = None


class SegmentoTexto(BaseModel):
    """Un fragmento de texto, con tiempo si la fuente lo aporta.

    Los tiempos son opcionales a propósito: una transcripción puede llegar sin
    marcas temporales y sigue siendo válida. Rellenarlas con valores estimados
    sería inventar datos que después se leerían como medidos.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    start_s: float | None = None
    end_s: float | None = None

    @property
    def tiene_tiempos(self) -> bool:
        return self.start_s is not None and self.end_s is not None


class PalabraTiempo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_s: float
    end_s: float
    word: str


class Transcript(Artefacto):
    """Transcripción de la fuente.

    ``text`` es la representación completa y siempre está presente; los
    segmentos conservan la estructura temporal solo cuando la fuente la tenía.
    """

    source_language: str = Field(min_length=2)
    text: str = Field(min_length=1)
    segments: list[SegmentoTexto] = Field(default_factory=list)
    words: list[PalabraTiempo] = Field(default_factory=list)
    provenance: ProcedenciaIA | None = None

    @property
    def tiene_tiempos(self) -> bool:
        """True si algún segmento aporta marcas temporales reales."""
        return any(s.tiene_tiempos for s in self.segments)


class Translation(Artefacto):
    """Traducción fiel del transcript. Separada de la adaptación a propósito.

    Es el registro de qué decía el original: su objetivo es **preservar el
    significado**, no mejorarlo. Toda la creatividad pertenece a
    ``AdaptedScript``. Sin esta separación es imposible demostrar después qué
    se transformó.
    """

    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    source_transcript_reference: str
    text: str = Field(min_length=1)
    segments: list[SegmentoTexto] = Field(default_factory=list)
    provenance: ProcedenciaIA

    @property
    def provider(self) -> str:
        return self.provenance.provider

    @property
    def model(self) -> str:
        return self.provenance.model

    @property
    def prompt_version(self) -> str | None:
        return self.provenance.prompt_version


class TipoSeccion(str, Enum):
    """Función narrativa de una sección.

    No todas aparecen en todo Short: un Short puede no tener llamada a la
    acción, y forzarla produciría relleno.
    """

    hook = "hook"
    contexto = "context"
    desarrollo = "development"
    remate = "payoff"
    llamada_a_accion = "cta"


class SeccionGuion(BaseModel):
    """Una sección del guion adaptado.

    Se usa ``kind`` y no ``type`` por coherencia con ``TransformationElement``,
    que ya nombra así el mismo concepto.
    """

    model_config = ConfigDict(extra="forbid")

    kind: TipoSeccion
    text: str = Field(min_length=1)
    order: int = Field(ge=0)


class AdaptedScript(Artefacto):
    """Guion adaptado en español: el artefacto principal de la etapa lingüística.

    Deriva de la traducción pero no es igual a ella. Puede reordenar,
    condensar, simplificar y eliminar redundancias; **no** puede inventar
    hechos, cifras, nombres ni contexto que la fuente no traía.

    ``estimated_duration_seconds`` es una estimación determinista por palabras
    por minuto. No equivale a la duración real de una narración sintetizada:
    esa se medirá cuando exista audio.
    """

    language: str = Field(min_length=2)
    hook: str = Field(min_length=1)
    sections: list[SeccionGuion] = Field(min_length=1)
    full_text: str = Field(min_length=1)
    target_duration_seconds: float = Field(gt=0)
    estimated_duration_seconds: float = Field(ge=0)
    transformation_notes: list[str] = Field(default_factory=list)
    source_translation_reference: str | None = None
    provenance: ProcedenciaIA

    @property
    def provider(self) -> str:
        return self.provenance.provider

    @property
    def model(self) -> str:
        return self.provenance.model

    @property
    def prompt_version(self) -> str | None:
        return self.provenance.prompt_version


# ---------------------------------------------------------------------------
# Voz, subtítulos y transformación
# ---------------------------------------------------------------------------


class ArtefactoVoz(Artefacto):
    """Base de los artefactos de voz y subtítulos.

    Solo cambia la versión de schema: estos artefactos nacen en Gate 3 con la
    suya propia, mientras los de Gates anteriores conservan la que ya tienen
    persistida.
    """

    schema_version: str = SCHEMA_VERSION_VOZ


class VoiceAsset(ArtefactoVoz):
    """Narración sintetizada por el proveedor de TTS.

    ``audio_duration_seconds`` es la duración **medida sobre el archivo**, no
    la estimación del guion ni el final del último ``WordBoundary``: Gate 0.5
    comprobó que Edge TTS deja cola de audio tras el último evento. Es el valor
    de referencia para el final del video.

    ``source_script_sha256`` identifica el ``AdaptedScript.full_text`` que se
    narró. El texto completo **no** se copia aquí: con la huella basta para
    detectar que el audio ya no corresponde al guion actual.
    """

    language: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    # El proveedor puede no tener "modelo" en el sentido de un LLM. Edge TTS
    # expone un motor de síntesis, así que el campo se llama como lo que es.
    engine: str = Field(min_length=1)
    voice: str = Field(min_length=1)
    audio_path: RutaRelativa
    audio_format: str = Field(min_length=1)
    audio_duration_seconds: float = Field(gt=0)
    sample_rate_hz: int = Field(gt=0)
    channels: int = Field(ge=1)
    source_script_sha256: Sha256Hex


class WordBoundary(BaseModel):
    """Una unidad temporal tal como la entrega el proveedor de TTS.

    ``end_seconds`` es derivado a propósito: persistirlo junto a ``start`` y
    ``duration`` permitiría que los tres dejaran de cuadrar entre sí.

    El texto viene del proveedor y **no** sirve como texto del subtítulo: Edge
    TTS lo emite sin puntuación y a veces agrupa varios tokens del guion en un
    solo evento.
    """

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    text: TextoNoVacio

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds


class WordBoundaryAsset(ArtefactoVoz):
    """Los tiempos por palabra de una narración, con su audio de referencia.

    No se exige que los ``boundaries`` cubran todo el audio: que el último
    termine antes de ``audio_duration_seconds`` es normal y está explícitamente
    permitido, porque Edge TTS deja una cola de audio tras el último evento.
    """

    provider: str = Field(min_length=1)
    engine: str = Field(min_length=1)
    voice: str = Field(min_length=1)
    audio_path: RutaRelativa
    audio_duration_seconds: float = Field(gt=0)
    boundaries: list[WordBoundary] = Field(default_factory=list)
    source_script_sha256: Sha256Hex

    @model_validator(mode="after")
    def _comprobar_boundaries(self) -> "WordBoundaryAsset":
        if not self.boundaries:
            return self
        if self.boundaries[0].index != 0:
            raise ValueError(
                f"el primer boundary tiene index {self.boundaries[0].index}; "
                f"la numeración empieza en 0"
            )
        for anterior, siguiente in zip(self.boundaries, self.boundaries[1:]):
            if siguiente.index == anterior.index:
                raise ValueError(f"index duplicado: {siguiente.index}")
            if siguiente.index < anterior.index:
                raise ValueError(
                    f"boundaries desordenados: index {siguiente.index} después "
                    f"de {anterior.index}"
                )
            if siguiente.start_seconds < anterior.start_seconds:
                raise ValueError(
                    f"el boundary {siguiente.index} empieza en "
                    f"{siguiente.start_seconds}s, antes que el {anterior.index} "
                    f"en {anterior.start_seconds}s"
                )
        return self


class SubtitleCue(BaseModel):
    """Un subtítulo con su tramo de tiempo.

    Los tiempos se guardan en segundos. La marca ``00:00:02,340`` pertenece al
    serializador de SRT, no al artefacto.

    ``text`` puede contener saltos de línea: son los del subtítulo, y tanto el
    SRT como el ASS los necesitan. Guardarlos aquí evita que el ASS tenga que
    recalcular el reparto de líneas y convertirse en una segunda fuente.
    """

    model_config = ConfigDict(extra="forbid")

    # Empieza en 1 porque es el índice del bloque SRT.
    index: int = Field(ge=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float
    text: TextoNoVacio

    @model_validator(mode="after")
    def _comprobar_tiempos(self) -> "SubtitleCue":
        if self.end_seconds <= self.start_seconds:
            raise ValueError(
                f"el cue {self.index} termina en {self.end_seconds}s, que no es "
                f"posterior a su inicio en {self.start_seconds}s"
            )
        return self


class SubtitleAsset(ArtefactoVoz):
    """Subtítulos propios: el SRT es el canónico y el ASS su presentación.

    ``max_lines`` y ``target_chars_per_line`` quedan fijados por el contrato en
    2 y 32. Son una heurística de legibilidad, no una ley: cambiarlos exige
    evolucionar el schema, no reconfigurar una corrida, para que un artefacto
    no pueda declarar una regla distinta de la que se aplicó.
    """

    language: str = Field(min_length=1)
    source_script_sha256: Sha256Hex
    word_boundary_artifact: RutaRelativa
    srt_path: RutaRelativa
    ass_path: RutaRelativa
    cue_count: int = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    max_lines: int
    target_chars_per_line: int
    cues: list[SubtitleCue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _comprobar_cues(self) -> "SubtitleAsset":
        if self.max_lines != MAX_LINEAS_SUBTITULO:
            raise ValueError(
                f"max_lines debe ser {MAX_LINEAS_SUBTITULO}; recibido {self.max_lines}"
            )
        if self.target_chars_per_line != CARACTERES_OBJETIVO_LINEA:
            raise ValueError(
                f"target_chars_per_line debe ser {CARACTERES_OBJETIVO_LINEA}; "
                f"recibido {self.target_chars_per_line}"
            )
        if self.cue_count != len(self.cues):
            raise ValueError(
                f"cue_count declara {self.cue_count} cues y la lista trae "
                f"{len(self.cues)}"
            )
        if self.cues and self.cues[0].index != 1:
            raise ValueError(
                f"el primer cue tiene index {self.cues[0].index}; la numeración "
                f"SRT empieza en 1"
            )
        for anterior, siguiente in zip(self.cues, self.cues[1:]):
            if siguiente.index <= anterior.index:
                raise ValueError(
                    f"índices de cue no crecientes: {siguiente.index} después "
                    f"de {anterior.index}"
                )
            if siguiente.start_seconds < anterior.end_seconds:
                raise ValueError(
                    f"el cue {siguiente.index} empieza en "
                    f"{siguiente.start_seconds}s y el {anterior.index} aún no "
                    f"ha terminado en {anterior.end_seconds}s"
                )
        return self


class TipoTransformacion(str, Enum):
    narracion_propia = "own_narration"
    guion_reestructurado = "restructured_script"
    contexto_agregado = "added_context"
    subtitulos_propios = "own_subtitles"
    overlay_visual = "visual_overlay"


class TransformationElement(BaseModel):
    """Un elemento editorial propio incorporado a la pieza.

    Registrarlo NO implica que el resultado sea jurídicamente transformativo
    ni monetizable: ese juicio es humano y vive en ``QAResult``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: TipoTransformacion
    rationale: str
    duration_s: float | None = None
    asset_ref: str | None = None


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


class RelacionAspecto(str, Enum):
    vertical = "9:16"
    horizontal = "16:9"
    cuadrado = "1:1"


class ModoAjuste(str, Enum):
    cubrir = "cover"
    contener = "contain"


class RenderJob(Artefacto):
    """Entrada del motor de render.

    ``task_id`` es el mismo valor que ``run_id``: MoneyPrinterTurbo lo exige
    como ``--task-id`` y mantenerlos iguales hace trazables sus artefactos sin
    llevar un mapeo aparte.
    """

    task_id: UUID
    script: str = Field(min_length=1)
    audio_path: str
    materials: list[str] = Field(min_length=1)
    aspect: RelacionAspecto = RelacionAspecto.vertical
    fit_mode: ModoAjuste = ModoAjuste.cubrir
    # Los subtítulos son nuestros: el motor renderiza sin ellos y el burn-in
    # es un paso posterior.
    subtitles_enabled: bool = False
    bgm_type: Literal["none", "random"] = "none"


class EstadoRender(str, Enum):
    exito = "success"
    fallo = "failed"


class RenderResult(Artefacto):
    """Salida del motor, ya validada y tipada."""

    status: EstadoRender
    exit_code: int
    output_path: str | None = None
    combined_path: str | None = None
    duration_s: float | None = None
    file_size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    engine_commit: str | None = None
    failed_stage: str | None = None
    # Mensaje corto y saneado. Los logs completos quedan en disco, no aquí.
    error: str | None = None


# ---------------------------------------------------------------------------
# QA y publicación
# ---------------------------------------------------------------------------


class ComprobacionQA(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool
    detail: str


class QAResult(Artefacto):
    """Informe de QA con las dos capas explícitamente separadas."""

    technical_qa_ok: bool
    checks: list[ComprobacionQA]
    transformation_elements: list[TransformationElement] = Field(default_factory=list)
    # El juicio editorial y legal no se automatiza. Se declara, no se calcula.
    editorial_legal_assessment: dict = Field(
        default_factory=lambda: {
            "status": "NOT_ASSESSED",
            "automated_similarity_threshold_applied": False,
        }
    )


class PublishMetadata(Artefacto):
    """Metadatos de publicación. Ciclo de vida distinto al del guion."""

    title: str = Field(max_length=100)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    category_id: str | None = None
    language: str = "es"
    privacy_status: Literal["private", "unlisted", "public"] = "private"
    publish_at: datetime | None = None
    # La decisión de divulgación no se automatiza; solo se registra.
    synthetic_disclosure: dict = Field(
        default_factory=lambda: {"decision": "UNDECIDED", "basis": None}
    )
    provenance: ProcedenciaIA | None = None


class PublishResult(Artefacto):
    """Resultado de la subida."""

    video_id: str
    url: str
    privacy_status: str
    published_at: datetime
    caption_track_id: str | None = None
