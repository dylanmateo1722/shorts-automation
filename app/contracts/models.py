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

Gate 1 define los catorce contratos pero solo produce ``RenderJob`` y
``RenderResult``. Los demás existen para que las etapas futuras se comuniquen
sin acoplamiento, no porque ya estén implementadas.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


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


class VoiceAsset(Artefacto):
    """Narración sintetizada con sus tiempos por palabra."""

    audio_path: str
    duration_s: float
    provider: str
    voice_name: str
    word_boundaries: list[PalabraTiempo] = Field(default_factory=list)
    fallback_used: bool = False


class SubtitleAsset(Artefacto):
    """Subtítulos propios: SRT canónico y ASS derivado para el burn-in."""

    srt_path: str
    ass_path: str | None = None
    cue_count: int
    max_chars_per_line: int
    # Reglas que no pudieron cumplirse; se reportan, no se ocultan.
    violations: list[str] = Field(default_factory=list)


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
