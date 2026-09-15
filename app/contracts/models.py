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

# Los artefactos de transformación y QA llevan la suya. Cada familia se versiona
# por separado a propósito: que evolucione el contrato de subtítulos no debería
# obligar a reescribir el de procedencia.
SCHEMA_VERSION_TRANSFORMACION = "1.0"

# Los de publicación, igual. Empezaron en "1.0" porque los contratos de Gate 1 que
# llevaban estos nombres nunca se produjeron ni se consumieron: no hay artefactos
# persistidos con el esquema anterior que una versión nueva tuviera que distinguir.
#
# "1.1" lo sube Gate 7.2, que añade a los contratos ya existentes la referencia a
# la sesión de subida y el resultado de la reconciliación. Es un cambio aditivo:
# un artefacto "1.0" sigue validando, porque los campos nuevos son opcionales.
SCHEMA_VERSION_PUBLICACION = "1.1"

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
    """Qué se puede hacer con un recurso.

    ``reference_only`` informa el tema y el análisis, pero su material no puede
    aparecer en el render. ``render_allowed`` sí puede, porque existe una base de
    procedencia registrada **y** la evidencia que la política exige.

    ``needs_review`` y ``blocked`` existen para no tener que mentir cuando no se
    sabe: lo primero es «falta información para decidir», lo segundo «se decidió
    que no». Ninguno de los dos entra al render, y esa es la diferencia con
    ``render_allowed``, que es la única clase que lo permite.

    **``render_allowed`` no significa «legalmente certificado».** Significa que
    la procedencia registrada satisface la política técnica del sistema. Ver
    ``LicenseDecision``.

    La distinción se aplica por contrato, no por convención de nombres de
    archivo: ver ``ProvenanceLedger``, que se niega a validar si algo que no sea
    ``render_allowed`` aparece entre los utilizables para el render.
    """

    render_permitido = "render_allowed"
    solo_referencia = "reference_only"
    revision_pendiente = "needs_review"
    bloqueado = "blocked"


class BaseLicencia(str, Enum):
    """Sobre qué se apoya el derecho a usar un recurso.

    Conjunto controlado a propósito. ``cc_by`` y no «creative commons» porque
    la familia no dice nada: hay licencias CC que prohíben el uso comercial y
    otras que lo permiten, así que la licencia concreta tiene que quedar
    registrada. ``unknown`` y ``fair_use_claim`` existen para poder representar
    la ignorancia y la afirmación sin resolverlas: **ninguna de las dos habilita
    el render por sí sola**, y el sistema no implementa ningún evaluador de uso
    legítimo.
    """

    propia = "own"
    licenciada = "licensed"
    cc_by = "cc_by"
    dominio_publico = "public_domain"
    permiso = "permission"
    desconocida = "unknown"
    uso_legitimo_alegado = "fair_use_claim"


class Candidate(Artefacto):
    """Un Short localizado como posible fuente o referencia.

    Conocer su URL no implica poder renderizarlo. Un candidato se registra como
    ``SourceAsset`` y su clase la decide la política, nunca el hecho de haberlo
    encontrado.
    """

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


# ---------------------------------------------------------------------------
# Procedencia: fuente, evidencia y decisión
# ---------------------------------------------------------------------------


#: Bases que jamás habilitan el render por sí solas. ``unknown`` es ignorancia y
#: ``fair_use_claim`` es una afirmación sin resolver; convertir cualquiera de las
#: dos en autorización automática sería inventar una certeza que nadie tiene.
BASES_NUNCA_RENDERIZABLES = frozenset(
    {BaseLicencia.desconocida, BaseLicencia.uso_legitimo_alegado}
)


class TipoMedio(str, Enum):
    video = "video"
    audio = "audio"
    imagen = "image"
    texto = "text"
    subtitulos = "subtitles"
    otro = "other"


class TipoEvidencia(str, Enum):
    """Qué clase de respaldo se está apuntando.

    El sistema no verifica el documento: registra que existe y dónde mirarlo
    para que una persona pueda auditarlo.
    """

    pagina_licencia = "license_page"
    documento_permiso = "permission_document"
    registro_propiedad = "ownership_record"
    registro_dominio_publico = "public_domain_record"
    registro_licencia_cc = "cc_license_record"
    otra = "other"


class Evidence(BaseModel):
    """Un respaldo trazable de la procedencia de un recurso.

    No es un gestor documental: es una referencia auditable. ``reference`` puede
    ser una URL, un identificador de factura o una ruta; lo que **no** debe ser
    es un secreto. Si el respaldo vive detrás de una credencial, aquí va el
    puntero, nunca la credencial.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: TextoNoVacio
    kind: TipoEvidencia
    reference: TextoNoVacio
    description: TextoNoVacio
    obtained_at: datetime | None = None
    issued_by: str | None = None


class SourceAsset(Artefacto):
    """Un recurso del que el pipeline sabe algo, con su procedencia.

    Cubre por igual lo que produce el propio pipeline y lo que viene de fuera.
    ``asset_id`` es la identidad estable: el resto —ruta, hash— puede cambiar,
    y cuando cambia hay que volver a decidir.

    ``local_path`` es opcional porque una fuente puede conocerse sin tenerla
    descargada: un Short ajeno del que solo se sabe la URL es una fuente
    perfectamente registrable, y precisamente por eso su clase la decide la
    política y no el hecho de conocerlo.

    ``sha256`` ancla la decisión a un contenido concreto. Si el archivo cambia,
    la decisión dejó de hablar de lo que hay en disco.
    """

    asset_id: TextoNoVacio
    media_kind: TipoMedio
    origin: TextoNoVacio
    source_url: str | None = None
    local_path: RutaRelativa | None = None
    sha256: Sha256Hex | None = None
    obtained_at: datetime | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    attribution_required: bool = False
    attribution_text: str | None = None
    attribution_source: str | None = None
    license_id: str | None = None
    commercial_use_allowed: bool | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _comprobar_fuente(self) -> "SourceAsset":
        vistos: set[str] = set()
        for evidencia in self.evidence:
            if evidencia.evidence_id in vistos:
                raise ValueError(
                    f"evidencia duplicada: {evidencia.evidence_id!r}"
                )
            vistos.add(evidencia.evidence_id)
        if self.sha256 is not None and self.local_path is None:
            raise ValueError(
                "hay huella sin archivo al que corresponda: sha256 exige local_path"
            )
        # Una atribución exigida sin texto **sí** es representable: es el estado
        # real de quien sabe que la licencia pide crédito y aún no lo ha escrito.
        # Prohibirlo aquí obligaría a mentir —declarar que no hace falta— o a
        # inventar el crédito. La política lo clasifica ``needs_review`` y el
        # ledger se niega a autorizar su render, que es donde importa.
        return self

    def evidencia(self, evidence_id: str) -> Evidence | None:
        for evidencia in self.evidence:
            if evidencia.evidence_id == evidence_id:
                return evidencia
        return None


class LicenseDecision(Artefacto):
    """Decisión explícita sobre qué se puede hacer con un ``SourceAsset``.

    **``render_allowed`` no es una certificación legal.** Significa que la
    procedencia registrada y la evidencia disponible satisfacen la política
    técnica identificada por ``policy_version``. No afirma ausencia de
    reclamaciones de copyright, ni compatibilidad con Content ID, ni derecho a
    monetizar, ni constituye asesoramiento jurídico. Ese juicio es humano y vive
    aparte, en ``QAResult.editorial_legal_assessment``.

    El contrato rechaza las combinaciones que se contradicen a sí mismas: no se
    puede autorizar un render sobre una base desconocida, sobre un uso legítimo
    alegado, sobre un permiso sin documento, ni sobre una atribución exigida que
    nadie escribió.
    """

    asset_id: TextoNoVacio
    decision: ClaseFuente
    basis: BaseLicencia
    evidence_ids: list[str] = Field(default_factory=list)
    reason: TextoNoVacio
    policy_version: TextoNoVacio
    decided_at: datetime = Field(default_factory=_ahora)
    decided_by: TextoNoVacio = "provenance_policy"

    @model_validator(mode="after")
    def _comprobar_coherencia(self) -> "LicenseDecision":
        if self.decision is not ClaseFuente.render_permitido:
            return self

        if self.basis in BASES_NUNCA_RENDERIZABLES:
            raise ValueError(
                f"no se puede autorizar el render de {self.asset_id!r} con base "
                f"{self.basis.value!r}: exige una decisión humana, no automática"
            )
        if not self.evidence_ids:
            raise ValueError(
                f"no se puede autorizar el render de {self.asset_id!r} sin ninguna "
                f"evidencia; 'allowed = true' no es una base de procedencia"
            )
        return self


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


class ArtefactoTransformacion(Artefacto):
    """Base de los artefactos de procedencia, transformación y QA."""

    schema_version: str = SCHEMA_VERSION_TRANSFORMACION


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


class ProvenanceLedger(ArtefactoTransformacion):
    """El registro auditable de la corrida: fuentes, evidencia y decisiones.

    La cadena es ``SourceAsset → Evidence → LicenseDecision``. Las fuentes dicen
    qué hay y de dónde viene; la evidencia, dónde mirar para comprobarlo; la
    decisión, qué se puede hacer con ello y bajo qué versión de política.

    ``render_assets`` no es informativo. El contrato **se niega a validar** si
    contiene la ruta de un recurso sin decisión, con una decisión que no sea
    ``render_allowed``, o que ninguna fuente declara. Es la forma estructural de
    la restricción: no depende de que ninguna etapa se acuerde de comprobarla, ni
    de cómo se llame el archivo.

    Que un recurso esté en ``render_assets`` **no certifica nada legalmente**.
    Certifica que la política técnica identificada en su decisión lo permitió.
    """

    sources: list[SourceAsset] = Field(default_factory=list)
    decisions: list[LicenseDecision] = Field(default_factory=list)
    render_assets: list[RutaRelativa] = Field(default_factory=list)

    @model_validator(mode="after")
    def _comprobar_procedencia(self) -> "ProvenanceLedger":
        por_id: dict[str, SourceAsset] = {}
        por_ruta: dict[str, str] = {}
        for fuente in self.sources:
            if fuente.asset_id in por_id:
                raise ValueError(f"fuente declarada dos veces: {fuente.asset_id!r}")
            por_id[fuente.asset_id] = fuente
            # Dos fuentes sobre el mismo archivo harían ambigua su clase, y una
            # clase ambigua en la puerta del render es una puerta abierta.
            if fuente.local_path is not None:
                if fuente.local_path in por_ruta:
                    raise ValueError(
                        f"{fuente.local_path!r} lo reclaman dos fuentes "
                        f"({por_ruta[fuente.local_path]!r} y {fuente.asset_id!r}): "
                        f"su clasificación sería ambigua"
                    )
                por_ruta[fuente.local_path] = fuente.asset_id

        decidido: dict[str, LicenseDecision] = {}
        for decision in self.decisions:
            if decision.asset_id in decidido:
                raise ValueError(
                    f"hay dos decisiones para {decision.asset_id!r}; una decisión "
                    f"de procedencia no puede contradecirse consigo misma"
                )
            fuente = por_id.get(decision.asset_id)
            if fuente is None:
                raise ValueError(
                    f"la decisión sobre {decision.asset_id!r} no corresponde a "
                    f"ninguna fuente registrada"
                )
            # La evidencia que invoca la decisión tiene que existir en la fuente.
            for evidence_id in decision.evidence_ids:
                if fuente.evidencia(evidence_id) is None:
                    raise ValueError(
                        f"la decisión sobre {decision.asset_id!r} invoca la "
                        f"evidencia {evidence_id!r}, que la fuente no registra"
                    )
            if (
                decision.decision is ClaseFuente.render_permitido
                and fuente.attribution_required
                and not (fuente.attribution_text or "").strip()
            ):
                raise ValueError(
                    f"{decision.asset_id!r} exige atribución y no hay texto con el "
                    f"que atribuir; no puede autorizarse el render"
                )
            decidido[decision.asset_id] = decision

        for ruta in self.render_assets:
            fuente = self._por_ruta(ruta)
            if fuente is None:
                raise ValueError(
                    f"el recurso {ruta!r} se usaría en el render sin procedencia "
                    f"declarada"
                )
            decision = decidido.get(fuente.asset_id)
            if decision is None:
                raise ValueError(
                    f"el recurso {ruta!r} se usaría en el render sin ninguna "
                    f"decisión de licencia"
                )
            if decision.decision is not ClaseFuente.render_permitido:
                raise ValueError(
                    f"el recurso {ruta!r} está clasificado "
                    f"{decision.decision.value!r} y no puede usarse en el render"
                )
        return self

    # --- consultas ---------------------------------------------------------

    def _por_ruta(self, ruta: str) -> SourceAsset | None:
        for fuente in self.sources:
            if fuente.local_path == ruta:
                return fuente
        return None

    def fuente(self, asset_id: str) -> SourceAsset | None:
        for fuente in self.sources:
            if fuente.asset_id == asset_id:
                return fuente
        return None

    def decision(self, asset_id: str) -> LicenseDecision | None:
        for decision in self.decisions:
            if decision.asset_id == asset_id:
                return decision
        return None

    def fuente_de(self, ruta: str) -> SourceAsset | None:
        """La fuente cuyo archivo es esa ruta, o None si ninguna lo es."""
        return self._por_ruta(ruta)

    def clase(self, ruta: str) -> ClaseFuente | None:
        """Clase decidida para un recurso, o None si no hay fuente o decisión.

        Sin decisión no hay clase. Una fuente registrada pero sin decidir **no**
        es utilizable: es exactamente el caso que ``needs_review`` describe, y
        tratarla como permitida sería inventar la decisión que falta.
        """
        fuente = self._por_ruta(ruta)
        if fuente is None:
            return None
        decision = self.decision(fuente.asset_id)
        return decision.decision if decision else None

    def permite_render(self, ruta: str) -> bool:
        """True solo si el recurso tiene decisión y esa decisión lo permite."""
        return self.clase(ruta) is ClaseFuente.render_permitido

    def sin_decidir(self) -> list[str]:
        """Fuentes registradas para las que nadie tomó una decisión."""
        decididas = {d.asset_id for d in self.decisions}
        return [f.asset_id for f in self.sources if f.asset_id not in decididas]

    def por_clase(self, clase: ClaseFuente) -> list[SourceAsset]:
        return [
            fuente
            for decision in self.decisions
            if decision.decision is clase and (fuente := self.fuente(decision.asset_id))
        ]

    def rutas_por_clase(self, clase: ClaseFuente) -> list[str]:
        """Archivos de los recursos clasificados así. Omite los que no tienen."""
        return [
            fuente.local_path
            for fuente in self.por_clase(clase)
            if fuente.local_path is not None
        ]

    def versiones_de_politica(self) -> set[str]:
        """Bajo qué políticas se decidió lo que hay aquí.

        Más de una no es un error del pasado: un ledger puede arrastrar
        decisiones tomadas antes de que la política cambiara. Lo que sí importa
        es poder detectarlo, y de eso se encarga la etapa.
        """
        return {d.policy_version for d in self.decisions}


class TipoTransformacion(str, Enum):
    narracion_propia = "own_narration"
    guion_reestructurado = "restructured_script"
    contexto_agregado = "added_context"
    subtitulos_propios = "own_subtitles"
    overlay_visual = "visual_overlay"


#: Los cinco elementos que toda corrida válida debe registrar.
TRANSFORMACIONES_OBLIGATORIAS = frozenset(TipoTransformacion)


class EstadoValidacion(str, Enum):
    validado = "validated"
    pendiente = "pending"
    invalido = "invalid"


class TransformationElement(BaseModel):
    """Un elemento editorial propio incorporado a la pieza.

    Registrarlo NO implica que el resultado sea jurídicamente transformativo,
    suficientemente original ni monetizable: ese juicio es humano y vive en
    ``QAResult.editorial_legal_assessment``.

    ``asset_ref`` es obligatorio a propósito. Un elemento sin recurso al que
    apuntar sería indistinguible de declarar ``transformation = true``, que no
    es evidencia de nada.
    """

    model_config = ConfigDict(extra="forbid")

    kind: TipoTransformacion
    rationale: TextoNoVacio
    asset_ref: RutaRelativa
    validation_status: EstadoValidacion = EstadoValidacion.pendiente


class TransformationSet(ArtefactoTransformacion):
    """Los elementos editoriales propios de una corrida.

    No exige los cinco aquí: que falte uno es un hallazgo que la QA técnica
    debe poder **reportar**, y un contrato que lo impidiera haría imposible
    persistir el estado incompleto para inspeccionarlo.
    """

    elements: list[TransformationElement] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sin_duplicados(self) -> "TransformationSet":
        vistos: set[TipoTransformacion] = set()
        for elemento in self.elements:
            if elemento.kind in vistos:
                raise ValueError(
                    f"elemento de transformación duplicado: {elemento.kind.value!r}"
                )
            vistos.add(elemento.kind)
        return self

    @property
    def tipos(self) -> set[TipoTransformacion]:
        return {e.kind for e in self.elements}

    @property
    def faltantes(self) -> set[TipoTransformacion]:
        return set(TRANSFORMACIONES_OBLIGATORIAS) - self.tipos


class OverlaySpec(ArtefactoTransformacion):
    """El overlay propio del pipeline y sus parámetros de composición.

    Gate 4 genera el archivo; **no** lo compone sobre el vídeo. La composición
    es de Gate 5, que leerá estos parámetros en lugar de volver a decidirlos.

    El texto sale del guion propio de la corrida, nunca de un recurso de
    referencia: eso lo hace original del pipeline.
    """

    overlay_path: RutaRelativa
    overlay_format: str = Field(min_length=1)
    text: TextoNoVacio
    start_seconds: float = Field(ge=0)
    end_seconds: float
    source_text_reference: RutaRelativa

    @model_validator(mode="after")
    def _comprobar_tiempos(self) -> "OverlaySpec":
        if self.end_seconds <= self.start_seconds:
            raise ValueError(
                f"el overlay termina en {self.end_seconds}s, que no es posterior "
                f"a su inicio en {self.start_seconds}s"
            )
        return self


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
    """Entrada del motor de render y de la composición posterior.

    ``task_id`` es el mismo valor que ``run_id``: MoneyPrinterTurbo lo exige
    como ``--task-id`` y mantenerlos iguales hace trazables sus artefactos sin
    llevar un mapeo aparte.

    Los campos de subtítulos y overlay son **referencias**, no copias: el job
    dice qué archivos entran a la pieza, y quien quiera su contenido lo lee del
    artefacto correspondiente. ``script`` es la excepción y se guarda entero
    porque el CLI del motor lo exige como argumento literal.

    ``assets_de_render`` es la lista completa de lo que acaba dentro del vídeo.
    Existe para que la puerta de procedencia tenga un sitio único al que
    preguntar, en vez de recorrer campos sueltos y olvidarse de uno.
    """

    task_id: UUID
    script: str = Field(min_length=1)
    audio_path: RutaRelativa
    materials: list[RutaRelativa] = Field(min_length=1)
    aspect: RelacionAspecto = RelacionAspecto.vertical
    fit_mode: ModoAjuste = ModoAjuste.cubrir
    # Los subtítulos son nuestros: el motor renderiza sin ellos y el burn-in
    # es un paso posterior.
    subtitles_enabled: bool = False
    bgm_type: Literal["none", "random"] = "none"
    # --- composición (Gate 5) ---
    subtitle_path: RutaRelativa | None = None
    overlay_path: RutaRelativa | None = None
    output_path: RutaRelativa | None = None
    composition: str | None = None
    # Commit del motor con el que se construyó el job. Si el pin cambia, el
    # resultado anterior deja de describir lo que produciría ahora.
    engine_commit: str | None = None

    @property
    def assets_de_render(self) -> list[str]:
        """Todo lo que entra al vídeo, en un solo sitio."""
        rutas = [self.audio_path, *self.materials]
        rutas += [r for r in (self.subtitle_path, self.overlay_path) if r]
        return rutas


class EstadoRender(str, Enum):
    exito = "success"
    fallo = "failed"


class RenderResult(Artefacto):
    """Un MP4 producido por el pipeline, ya inspeccionado y tipado.

    El mismo contrato describe dos cosas distintas según quién lo produzca, y
    ``renderer`` dice cuál: el vídeo que devuelve el motor (``moneyprinterturbo``)
    y el vídeo final compuesto (``ffmpeg-composition``). Son dos artefactos
    separados a propósito —``render_result`` y ``final_video``— porque si la
    composición falla después de un render correcto, repetir el motor costaría
    minutos sin motivo: la idempotencia debe poder reutilizar el intermedio.

    Todos los valores medidos salen de inspeccionar el archivo, no de lo que el
    motor diga haber configurado. ``inspected_with`` registra con qué
    herramienta se leyeron.
    """

    status: EstadoRender
    exit_code: int
    output_path: RutaRelativa | None = None
    combined_path: RutaRelativa | None = None
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
    # --- medición del archivo (Gate 5) ---
    sha256: Sha256Hex | None = None
    audio_sample_rate_hz: int | None = None
    pixel_format: str | None = None
    renderer: str | None = None
    inspected_with: str | None = None


# ---------------------------------------------------------------------------
# QA y publicación
# ---------------------------------------------------------------------------


class NivelQA(str, Enum):
    """Qué implica que una comprobación falle.

    Un ``warning`` que falla es información, no un veredicto: ascenderlo a error
    sin una razón técnica convertiría una heurística de legibilidad en un
    bloqueo.
    """

    error = "error"
    aviso = "warning"


class ComprobacionQA(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    ok: bool
    detail: str
    level: NivelQA = NivelQA.error


class EstadoQA(str, Enum):
    aprobado = "pass"
    aprobado_con_avisos = "pass_with_warnings"
    reprobado = "fail"


class EstadoEvaluacion(str, Enum):
    """Estado del juicio editorial y legal.

    Solo ``not_assessed`` lo pone el pipeline. Los otros dos existen para que
    una persona pueda registrar su decisión; **ninguna** comprobación
    automática los establece.
    """

    no_evaluado = "NOT_ASSESSED"
    aprobado_por_humano = "APPROVED_BY_HUMAN"
    rechazado_por_humano = "REJECTED_BY_HUMAN"


NOTA_EDITORIAL = (
    "La presencia de elementos de transformación NO implica que el resultado sea "
    "jurídicamente transformativo, suficientemente original ni monetizable. Este "
    "juicio es editorial y legal, requiere revisión humana y no se automatiza."
)


class EvaluacionEditorialLegal(BaseModel):
    """El juicio que **no** se automatiza. Se declara, no se calcula.

    ``automated_similarity_threshold_applied`` es estructuralmente falso: el
    contrato rechaza el valor contrario. No existe puntuación de similitud ni
    umbral de diferencia en este proyecto, y el campo está aquí para que eso
    quede afirmado en cada artefacto en lugar de solo en la documentación.
    """

    model_config = ConfigDict(extra="forbid")

    status: EstadoEvaluacion = EstadoEvaluacion.no_evaluado
    automated_similarity_threshold_applied: Literal[False] = False
    note: str = NOTA_EDITORIAL
    assessed_by: str | None = None


class QAResult(ArtefactoTransformacion):
    """Informe de QA con las dos capas explícitamente separadas.

    ``technical_qa_ok`` y ``editorial_legal_assessment`` son independientes: la
    QA técnica puede estar en verde mientras el juicio editorial sigue sin
    evaluar, y eso es el estado normal de una corrida. Mezclarlos haría creer
    que un artefacto válido es un artefacto publicable.

    ``errors`` y ``warnings`` se derivan de ``checks`` en lugar de persistirse
    aparte: dos listas que pueden discrepar de su origen acaban discrepando.
    """

    status: EstadoQA
    technical_qa_ok: bool
    checks: list[ComprobacionQA] = Field(default_factory=list)
    # Los elementos viven en TransformationSet; aquí solo se referencian, para
    # no tener dos copias de la misma verdad.
    transformation_artifact: RutaRelativa
    provenance_artifact: RutaRelativa
    editorial_legal_assessment: EvaluacionEditorialLegal = Field(
        default_factory=EvaluacionEditorialLegal
    )
    # Si Gate 5 puede consumir esta corrida. Es una afirmación técnica: no dice
    # nada sobre si la pieza debe publicarse.
    ready_for_render: bool = False

    @model_validator(mode="after")
    def _comprobar_coherencia(self) -> "QAResult":
        fallos = [c for c in self.checks if not c.ok and c.level is NivelQA.error]
        avisos = [c for c in self.checks if not c.ok and c.level is NivelQA.aviso]

        esperado = (
            EstadoQA.reprobado
            if fallos
            else (EstadoQA.aprobado_con_avisos if avisos else EstadoQA.aprobado)
        )
        if self.status is not esperado:
            raise ValueError(
                f"el estado declarado es {self.status.value!r} y las "
                f"comprobaciones dan {esperado.value!r} "
                f"({len(fallos)} error(es), {len(avisos)} aviso(s))"
            )
        if self.technical_qa_ok != (not fallos):
            raise ValueError(
                f"technical_qa_ok es {self.technical_qa_ok} con {len(fallos)} "
                f"comprobación(es) de nivel error sin pasar"
            )
        if self.ready_for_render and fallos:
            raise ValueError(
                "no se puede declarar la corrida lista para el render con "
                f"{len(fallos)} error(es) de QA"
            )
        return self

    @property
    def errors(self) -> list[ComprobacionQA]:
        return [c for c in self.checks if not c.ok and c.level is NivelQA.error]

    @property
    def warnings(self) -> list[ComprobacionQA]:
        return [c for c in self.checks if not c.ok and c.level is NivelQA.aviso]

    def resumen(self) -> str:
        etiqueta = {
            (True, NivelQA.error): "OK   ", (False, NivelQA.error): "FALLA",
            (True, NivelQA.aviso): "OK   ", (False, NivelQA.aviso): "AVISO",
        }
        return "\n".join(
            f"  [{etiqueta[(c.ok, c.level)]}] {c.name}: {c.detail}" for c in self.checks
        )


# ---------------------------------------------------------------------------
# Publicación: intención, estado operacional y resultado
#
# Tres contratos con tres responsabilidades que no se solapan:
#
#   PublishMetadata  qué se quiere publicar          (intención editorial)
#   PublishJob       por dónde va una ejecución      (estado operacional)
#   PublishResult    qué devolvió YouTube            (hecho observado)
#
# La separación no es ornamental. Mezclarlas haría que un reintento sobreescribiera
# la intención, o que la intención pareciera un hecho. Los contratos se niegan a
# validar si los datos de uno aparecen en otro.
# ---------------------------------------------------------------------------


class Privacidad(str, Enum):
    """Visibilidad solicitada para el vídeo en YouTube.

    El conjunto es cerrado. ``private`` es el único que usa el primer flujo real;
    los otros dos existen en el contrato porque la intención es representable,
    no porque ya haya política para ellos.
    """

    privado = "private"
    no_listado = "unlisted"
    publico = "public"


class EstadoDivulgacionIA(str, Enum):
    """Qué hace falta declarar sobre el uso de IA en la pieza.

    Ninguno de los tres lo deduce el sistema. Que una narración venga de un TTS
    **no** determina por sí solo que haga falta divulgación: eso depende de la
    pieza y de la norma aplicable, y esa valoración es humana. El contrato
    registra la decisión y quién la tomó; no la calcula.

    ``requires_current_verification`` es el estado que bloquea: significa que la
    situación debe comprobarse de nuevo antes de publicar, y por tanto la
    publicación automática no puede seguir adelante.
    """

    requerida = "required"
    no_requerida = "not_required"
    requiere_verificacion_actual = "requires_current_verification"


class DivulgacionIA(BaseModel):
    """La decisión de divulgación, registrada con su motivo y su autor.

    ``reason`` y ``decided_by`` son obligatorios a propósito: una divulgación sin
    constancia de por qué y de quién la decidió sería indistinguible de un valor
    por defecto, y el valor por defecto es justo lo que aquí no debe existir.
    """

    model_config = ConfigDict(extra="forbid")

    status: EstadoDivulgacionIA
    reason: TextoNoVacio
    decided_by: TextoNoVacio
    decided_at: datetime = Field(default_factory=_ahora)

    @property
    def permite_publicacion_automatica(self) -> bool:
        """False cuando la situación debe verificarse antes de publicar."""
        return self.status is not EstadoDivulgacionIA.requiere_verificacion_actual


class EstadoPublicacion(str, Enum):
    """Los ocho estados por los que puede pasar una publicación.

    Los valores van en mayúsculas, como los de ``EstadoEvaluacion``, porque así
    los nombra la arquitectura de este Gate. No coinciden en forma con los de
    ``ClaseFuente`` y eso es deliberado, no un descuido: son vocabularios de dos
    dominios distintos.

    ``COMPLETED`` merece una aclaración porque es la confusión más fácil de
    cometer: significa que lo subido se verificó y el estado remoto coincide con
    el solicitado. **No** significa que el vídeo sea público. Un vídeo
    ``private`` verificado está ``COMPLETED``.
    """

    no_listo = "NOT_READY"
    listo = "READY"
    subiendo = "UPLOADING"
    subido = "UPLOADED"
    verificando = "VERIFYING"
    completado = "COMPLETED"
    fallido = "FAILED"
    revision_pendiente = "NEEDS_REVIEW"


#: Transiciones permitidas. Lo que no está aquí se rechaza: la tabla es la
#: definición, no una sugerencia.
#:
#: Dos ausencias que son decisiones, no olvidos:
#:
#: * **``UPLOADING`` no vuelve a ``READY``.** Si el upload se quedó sin respuesta
#:   no se sabe si terminó, y volver a ``READY`` permitiría subirlo otra vez a
#:   ciegas. Ese camino lleva a ``NEEDS_REVIEW``, que es donde se reconcilia.
#: * **``FAILED`` y ``COMPLETED`` no salen a ningún sitio.** Son terminales.
#:   Retomar un ``FAILED`` no es transitar: es una ejecución nueva, con su propio
#:   ``attempt`` y su propia clave.
TRANSICIONES_PUBLICACION: dict[EstadoPublicacion, frozenset[EstadoPublicacion]] = {
    EstadoPublicacion.no_listo: frozenset(
        {EstadoPublicacion.listo, EstadoPublicacion.revision_pendiente}
    ),
    EstadoPublicacion.listo: frozenset(
        {
            EstadoPublicacion.subiendo,
            # Una precondición que deja de cumplirse devuelve el trabajo a
            # NOT_READY; seguir en READY afirmaría algo que ya no es cierto.
            EstadoPublicacion.no_listo,
            EstadoPublicacion.revision_pendiente,
        }
    ),
    EstadoPublicacion.subiendo: frozenset(
        {
            EstadoPublicacion.subido,
            EstadoPublicacion.fallido,
            EstadoPublicacion.revision_pendiente,
        }
    ),
    EstadoPublicacion.subido: frozenset(
        {EstadoPublicacion.verificando, EstadoPublicacion.revision_pendiente}
    ),
    EstadoPublicacion.verificando: frozenset(
        {
            EstadoPublicacion.completado,
            EstadoPublicacion.fallido,
            EstadoPublicacion.revision_pendiente,
        }
    ),
    # Salir de NEEDS_REVIEW pasa por volver a mirar el recurso remoto, o por
    # concluir que falló. No se vuelve a READY: sería autorizar otra subida sin
    # haber averiguado si la anterior llegó.
    EstadoPublicacion.revision_pendiente: frozenset(
        {EstadoPublicacion.verificando, EstadoPublicacion.fallido}
    ),
    EstadoPublicacion.completado: frozenset(),
    EstadoPublicacion.fallido: frozenset(),
}

#: Estados desde los que no se sale.
ESTADOS_TERMINALES = frozenset(
    {EstadoPublicacion.completado, EstadoPublicacion.fallido}
)

#: Estados en los que ya hubo trato con YouTube, y que por tanto puede llevar un
#: ``PublishResult``. ``NOT_READY``, ``READY`` y ``UPLOADING`` describen por dónde
#: va el trabajo, no qué contestó YouTube, así que no son resultados.
ESTADOS_DE_RESULTADO = frozenset(
    {
        EstadoPublicacion.subido,
        EstadoPublicacion.verificando,
        EstadoPublicacion.completado,
        EstadoPublicacion.fallido,
        EstadoPublicacion.revision_pendiente,
    }
)

#: Estados que implican que al menos un intento de subida empezó.
ESTADOS_CON_INTENTO = frozenset(
    {
        EstadoPublicacion.subiendo,
        EstadoPublicacion.subido,
        EstadoPublicacion.verificando,
        EstadoPublicacion.completado,
        EstadoPublicacion.fallido,
    }
)


def transicion_valida(
    origen: EstadoPublicacion, destino: EstadoPublicacion
) -> bool:
    """Si la tabla permite pasar de un estado al otro."""
    return destino in TRANSICIONES_PUBLICACION[origen]


def exigir_transicion(
    origen: EstadoPublicacion,
    destino: EstadoPublicacion,
    *,
    metadata: "PublishMetadata | None" = None,
) -> None:
    """Rechaza una transición que la tabla no contempla.

    Para entrar en ``UPLOADING`` hay que presentar la metadata, y no por
    formalismo: es donde se comprueba que la divulgación de IA no exige volver a
    verificar la situación. Sin metadata no hay forma de comprobarlo, así que la
    transición se rechaza en vez de concederse por omisión.
    """
    if not transicion_valida(origen, destino):
        permitidos = sorted(e.value for e in TRANSICIONES_PUBLICACION[origen])
        raise ValueError(
            f"transición no permitida {origen.value!r} -> {destino.value!r}; "
            f"desde {origen.value!r} solo se puede pasar a {permitidos or 'ningún estado'}"
        )

    if destino is not EstadoPublicacion.subiendo:
        return

    if metadata is None:
        raise ValueError(
            "no se puede pasar a 'UPLOADING' sin la metadata: hay que comprobar "
            "la divulgación de IA antes de subir"
        )
    if not metadata.ai_disclosure.permite_publicacion_automatica:
        raise ValueError(
            "la divulgación de IA está en 'requires_current_verification': la "
            "publicación automática queda bloqueada hasta que una persona "
            "verifique la situación"
        )


def clave_idempotencia(run_id: UUID) -> str:
    """Clave determinista de esta ejecución de publicación, derivada del run.

    **YouTube no ofrece ninguna clave de idempotencia que podamos usar.** Esta es
    nuestra, sirve para reconocer que dos intentos hablan de la misma publicación
    y no hace absolutamente nada del lado de YouTube: mandarla no evita un
    duplicado. La idempotencia real se apoya en el estado persistido y en la
    reconciliación posterior, no en este valor.
    """
    return hashlib.sha256(f"publish:{run_id}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Gate 7.2: sesión de subida y reconciliación
#
# Tres piezas que no se solapan con las tres anteriores:
#
#   UploadSession          la sesión resumable viva      (secreto operativo)
#   ReconciliationRequest  qué hay que averiguar         (pregunta)
#   ReconciliationResult   qué se averiguó               (respuesta)
# ---------------------------------------------------------------------------


def publication_fingerprint(
    run_id: UUID, video_sha256: str, metadata_sha256: str
) -> str:
    """Identidad interna y determinista de una publicación.

    **YouTube no conoce este valor y no sirve para nada del lado del proveedor.**
    No es una clave de idempotencia remota: mandarla no evitaría un duplicado,
    porque no hay dónde mandarla. Sirve para que dos intentos nuestros puedan
    reconocerse como la misma publicación —mismo run, mismo vídeo, misma
    metadata— y para que un cambio en cualquiera de los tres produzca una
    identidad distinta.

    Se distingue de ``clave_idempotencia``, que solo deriva del ``run_id``: el
    fingerprint ata además el contenido, así que cambiar el MP4 o el título lo
    cambia. Los dos coexisten a propósito.

    Los componentes van separados por ``:`` para que no puedan solaparse: sin
    separador, dos ternas distintas podrían concatenarse en la misma cadena.
    """
    crudo = f"{run_id}:{video_sha256}:{metadata_sha256}"
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()


class UploadSession(BaseModel):
    """La sesión resumable abierta contra el proveedor.

    ``session_url`` es un **secreto operativo**: quien lo tenga puede subir
    bytes a esa sesión y completar la publicación. Por eso se declara con
    ``exclude=True`` y ``repr=False``, y la consecuencia es deliberada:

    * no aparece en ``model_dump()`` ni en ``model_dump_json()``, así que no
      puede llegar a ``runs/`` aunque alguien embeba la sesión en un artefacto
      —``Workspace.escribir_artefacto`` serializa con ``model_dump_json``—;
    * no aparece en el ``repr`` ni en el ``str``, así que no se cuela en una
      traza ni en un mensaje de error.

    **Una sesión no puede reconstruirse desde un artefacto.** ``session_url`` es
    obligatorio y está excluido del volcado, de modo que el JSON resultante no
    valida como ``UploadSession``. Eso no es un inconveniente: es el mecanismo
    que hace que un proceso reiniciado *no pueda* creer que tiene una sesión
    viva. Sin URL no hay consulta posible, y el desenlace honesto es ``UNKNOWN``.
    """

    model_config = ConfigDict(extra="forbid")

    provider: TextoNoVacio = "youtube"
    #: Secreto operativo. Nunca se serializa ni se representa. Ver el docstring.
    session_url: str = Field(repr=False, exclude=True)
    run_id: UUID
    #: Número de sesión dentro del intento. Empieza en 1.
    attempt: int = Field(ge=1)
    video_sha256: Sha256Hex
    metadata_sha256: Sha256Hex
    total_bytes: int = Field(gt=0)
    bytes_confirmed: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=_ahora)
    updated_at: datetime = Field(default_factory=_ahora)
    #: Cuándo deja de servir la sesión, si el proveedor lo informa.
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _comprobar_sesion(self) -> "UploadSession":
        if not self.session_url.strip():
            raise ValueError("una sesión sin URL no es una sesión")
        if self.bytes_confirmed > self.total_bytes:
            raise ValueError(
                f"bytes_confirmed ({self.bytes_confirmed}) supera total_bytes "
                f"({self.total_bytes}): el proveedor no puede haber recibido más "
                f"de lo que hay"
            )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at es anterior a created_at")
        return self

    @property
    def referencia(self) -> str:
        """Identificador no secreto de esta sesión, apto para persistir.

        Es el SHA-256 del URL. Permite afirmar «este trabajo tuvo *esta* sesión»
        sin revelar cuál, y no es reversible.
        """
        return hashlib.sha256(self.session_url.encode("utf-8")).hexdigest()

    @property
    def completa(self) -> bool:
        return self.bytes_confirmed >= self.total_bytes

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"UploadSession({self.provider}, {self.bytes_confirmed}/"
            f"{self.total_bytes} bytes, ref={self.referencia[:12]}…)"
        )


class MotivoReconciliacion(str, Enum):
    """Por qué hubo que ir a preguntar. Conjunto cerrado."""

    respuesta_perdida = "RESPONSE_LOST"
    tiempo_agotado = "UPLOAD_TIMEOUT"
    conexion_reiniciada = "CONNECTION_RESET"
    resultado_desconocido = "UNKNOWN_PROVIDER_RESULT"
    proceso_interrumpido = "PROCESS_INTERRUPTED"


class DesenlaceReconciliacion(str, Enum):
    """Qué se averiguó. Conjunto cerrado.

    ``UNKNOWN`` no es un fallo del sistema: es la respuesta correcta cuando no
    hay evidencia. Tratarlo como «no se subió» es exactamente el error que este
    Gate existe para impedir.
    """

    confirmado_subido = "CONFIRMED_UPLOADED"
    confirmado_no_subido = "CONFIRMED_NOT_UPLOADED"
    subida_en_curso = "UPLOAD_IN_PROGRESS"
    desconocido = "UNKNOWN"


class ConfianzaReconciliacion(str, Enum):
    """De dónde sale la conclusión.

    No hay ningún nivel que signifique «lo deducimos»: o el proveedor lo dijo, o
    no se sabe.
    """

    #: El proveedor devolvió el recurso o un desenlace inequívoco.
    provider_confirmado = "PROVIDER_CONFIRMED"
    #: El proveedor informó progreso, pero no desenlace.
    provider_parcial = "PROVIDER_PARTIAL"
    #: No se pudo obtener evidencia de ningún tipo.
    sin_evidencia = "NONE"


class ReconciliationRequest(Artefacto):
    """Qué hay que averiguar sobre una subida cuyo resultado no consta.

    ``upload_session`` es opcional **por diseño**, no por comodidad. Cuando el
    proceso se ha reiniciado, el ``session_url`` ya no existe en memoria y no hay
    forma documentada de recuperarlo: la petición se construye sin sesión y el
    único desenlace honesto es ``UNKNOWN``. El validador lo impone para que ese
    caso no pueda representarse de otra manera.
    """

    schema_version: str = SCHEMA_VERSION_PUBLICACION

    attempt: int = Field(ge=1)
    reason: MotivoReconciliacion
    publication_fingerprint: Sha256Hex
    #: La sesión viva, si todavía se tiene. ``None`` tras un reinicio.
    upload_session: UploadSession | None = None
    last_known_bytes: int = Field(default=0, ge=0)
    requested_at: datetime = Field(default_factory=_ahora)

    @model_validator(mode="after")
    def _comprobar_peticion(self) -> "ReconciliationRequest":
        if self.reason is MotivoReconciliacion.proceso_interrumpido:
            if self.upload_session is not None:
                raise ValueError(
                    "'PROCESS_INTERRUPTED' significa que el proceso se reinició y "
                    "el session_url se perdió con él; una petición así no puede "
                    "traer una sesión viva"
                )
        if self.upload_session is not None:
            if self.upload_session.run_id != self.run_id:
                raise ValueError(
                    "la sesión pertenece a otra corrida que la que se reconcilia"
                )
            if self.upload_session.bytes_confirmed > self.last_known_bytes:
                raise ValueError(
                    "last_known_bytes es menor que los bytes que la propia sesión "
                    "da por confirmados"
                )
        return self

    @property
    def sesion_consultable(self) -> bool:
        """Si existe un URL con el que preguntar al proveedor."""
        return self.upload_session is not None


class ReconciliationResult(Artefacto):
    """Qué se averiguó, y con qué respaldo.

    Los validadores impiden las combinaciones que mentirían: no hay
    ``CONFIRMED_UPLOADED`` sin identificador ni sin confirmación del proveedor, y
    no hay ``UNKNOWN`` que traiga un ``video_id`` o que presuma de evidencia.
    """

    schema_version: str = SCHEMA_VERSION_PUBLICACION

    attempt: int = Field(ge=1)
    outcome: DesenlaceReconciliacion
    video_id: str | None = None
    #: Bytes que el proveedor dio por recibidos. Es **el** dato con el que se
    #: reanuda una sesión, y por eso es un entero del contrato y no un número
    #: que alguien tenga que sacar de un texto: un offset equivocado reenvía
    #: contenido o deja un hueco, y las dos cosas rompen la subida.
    #:
    #: Obligatorio en ``UPLOAD_IN_PROGRESS``, que es el único desenlace desde el
    #: que se continúa. En los demás no significa nada y se rechaza.
    bytes_confirmed: int | None = Field(default=None, ge=0)
    #: Qué dijo el proveedor, en texto corto y saneado. Es **descriptivo**: sirve
    #: para que una persona lea qué pasó, y ninguna decisión depende de él.
    observed_state: str = ""
    confidence: ConfianzaReconciliacion
    checked_at: datetime = Field(default_factory=_ahora)
    #: Constancia de en qué se apoya la conclusión. Texto saneado, sin secretos.
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _comprobar_resultado(self) -> "ReconciliationResult":
        tiene_id = bool((self.video_id or "").strip())

        if self.outcome is DesenlaceReconciliacion.confirmado_subido:
            if not tiene_id:
                raise ValueError(
                    "'CONFIRMED_UPLOADED' sin video_id no confirma nada: si el "
                    "proveedor aceptó el vídeo, devolvió su identificador"
                )
            if self.confidence is not ConfianzaReconciliacion.provider_confirmado:
                raise ValueError(
                    "'CONFIRMED_UPLOADED' exige confirmación del proveedor; "
                    "cualquier otra confianza sería una deducción nuestra"
                )
        elif tiene_id:
            raise ValueError(
                f"hay video_id con desenlace {self.outcome.value!r}: solo "
                f"'CONFIRMED_UPLOADED' afirma que existe un vídeo"
            )

        if self.outcome is DesenlaceReconciliacion.desconocido:
            if self.confidence is not ConfianzaReconciliacion.sin_evidencia:
                raise ValueError(
                    "'UNKNOWN' con evidencia es una contradicción: si hubiera "
                    "evidencia, el desenlace no sería desconocido"
                )

        if self.outcome is DesenlaceReconciliacion.subida_en_curso:
            if self.confidence is ConfianzaReconciliacion.sin_evidencia:
                raise ValueError(
                    "'UPLOAD_IN_PROGRESS' sin evidencia no es observable: saber "
                    "que una subida va por la mitad exige que el proveedor lo "
                    "haya dicho"
                )
            if self.bytes_confirmed is None:
                raise ValueError(
                    "'UPLOAD_IN_PROGRESS' exige bytes_confirmed: es el offset "
                    "desde el que se reanuda, y sin él no habría por dónde "
                    "seguir"
                )
        elif self.bytes_confirmed is not None:
            raise ValueError(
                f"hay bytes_confirmed con desenlace {self.outcome.value!r}: solo "
                f"'UPLOAD_IN_PROGRESS' describe una subida que continúa"
            )
        return self

    @property
    def permite_nueva_sesion(self) -> bool:
        """Si es lícito abrir otra sesión de subida.

        **Solo** ``CONFIRMED_NOT_UPLOADED``. Esta propiedad es la regla absoluta
        de Gate 7.2 escrita en el contrato: ``UNKNOWN`` devuelve ``False``, así
        que ningún camino puede convertir la incertidumbre en una subida nueva.
        """
        return self.outcome is DesenlaceReconciliacion.confirmado_no_subido

    @property
    def exige_revision(self) -> bool:
        """Si el desenlace obliga a que lo mire una persona."""
        return self.outcome is DesenlaceReconciliacion.desconocido


class PublishMetadata(Artefacto):
    """Qué se quiere publicar. Intención, no resultado.

    Aquí no hay ``video_id``, ni URL, ni estado de subida, ni fechas de subida o
    de finalización: todo eso son hechos que YouTube devuelve y viven en
    ``PublishResult``. La metadata se puede escribir antes de que exista el vídeo
    y sigue siendo válida después; confundirla con el resultado haría que un
    reintento pisara la intención.

    ``made_for_kids`` no tiene valor por defecto **a propósito**. Es una
    declaración con consecuencias y nadie puede hacerla en nombre de otro: poner
    ``False`` por omisión sería afirmar algo que nadie decidió.
    """

    schema_version: str = SCHEMA_VERSION_PUBLICACION

    title: str = Field(min_length=1, max_length=100)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    category_id: str | None = None
    privacy_status: Privacidad = Privacidad.privado
    language: str = "es"
    made_for_kids: bool
    ai_disclosure: DivulgacionIA

    @property
    def permite_publicacion_automatica(self) -> bool:
        """Si la divulgación registrada deja publicar sin intervención humana."""
        return self.ai_disclosure.permite_publicacion_automatica


class PublishJob(Artefacto):
    """Por dónde va una ejecución de publicación. Estado operacional.

    No lleva metadata editorial: el título y la descripción viven en
    ``PublishMetadata``, y duplicarlos aquí haría que un reintento pudiera
    contradecir la intención original.

    ``attempt`` cuenta las subidas empezadas, no las transiciones. Vale 0
    mientras no se haya entrado en ``UPLOADING``, y el contrato lo exige: un
    trabajo ``UPLOADED`` con 0 intentos describiría algo imposible.
    """

    schema_version: str = SCHEMA_VERSION_PUBLICACION

    state: EstadoPublicacion = EstadoPublicacion.no_listo
    attempt: int = Field(default=0, ge=0)
    #: Nuestra, no de YouTube. Ver ``clave_idempotencia``.
    idempotency_key: TextoNoVacio
    started_at: datetime = Field(default_factory=_ahora)
    updated_at: datetime = Field(default_factory=_ahora)
    #: Por qué el trabajo está donde está. Obligatorio en los estados que no se
    #: explican solos: un ``NEEDS_REVIEW`` sin motivo no le dice a nadie qué
    #: reconciliar, y un ``FAILED`` sin motivo no distingue un fallo de un
    #: abandono.
    reason: str | None = None
    #: Gate 7.2. Huella no secreta de la sesión de subida (``UploadSession.
    #: referencia``). Aquí **no** va el ``session_url``: es un secreto operativo
    #: y este artefacto se persiste en ``runs/``. Guardar la huella permite
    #: afirmar que el trabajo tuvo una sesión concreta sin revelar cuál.
    upload_session_ref: Sha256Hex | None = None
    #: Gate 7.2. Lo último que se averiguó sobre una subida incierta.
    reconciliation: ReconciliationResult | None = None

    @model_validator(mode="after")
    def _comprobar_trabajo(self) -> "PublishJob":
        if self.updated_at < self.started_at:
            raise ValueError(
                "updated_at es anterior a started_at: el trabajo no puede haberse "
                "actualizado antes de empezar"
            )
        if self.state in ESTADOS_CON_INTENTO and self.attempt < 1:
            raise ValueError(
                f"el estado {self.state.value!r} implica al menos un intento de "
                f"subida y attempt es {self.attempt}"
            )
        if (
            self.state in (EstadoPublicacion.no_listo, EstadoPublicacion.listo)
            and self.attempt != 0
        ):
            raise ValueError(
                f"el estado {self.state.value!r} es previo a cualquier subida y "
                f"attempt es {self.attempt}"
            )
        if self.state in (
            EstadoPublicacion.revision_pendiente,
            EstadoPublicacion.fallido,
        ) and not (self.reason or "").strip():
            raise ValueError(
                f"el estado {self.state.value!r} exige un motivo: sin él no hay "
                f"nada que reconciliar ni que explicar"
            )
        return self

    def transicionar(
        self,
        destino: EstadoPublicacion,
        *,
        metadata: PublishMetadata | None = None,
        reason: str | None = None,
        momento: datetime | None = None,
    ) -> "PublishJob":
        """Devuelve el trabajo avanzado al estado siguiente.

        No muta: cada transición produce un artefacto nuevo, de modo que el
        anterior sigue siendo evidencia de por dónde se pasó. Una transición que
        la tabla no contempla levanta ``ValueError`` y no devuelve nada.

        Entrar en ``UPLOADING`` incrementa ``attempt``, porque es el único punto
        donde empieza una subida de verdad.

        El trabajo nuevo se construye validándolo, no copiándolo: ``model_copy``
        se salta los validadores, y por esa puerta entraría un ``NEEDS_REVIEW``
        sin motivo o un ``attempt`` incoherente.
        """
        exigir_transicion(self.state, destino, metadata=metadata)
        datos = self.model_dump()
        datos.update(
            state=destino,
            attempt=self.attempt + (1 if destino is EstadoPublicacion.subiendo else 0),
            updated_at=momento or _ahora(),
            reason=reason if reason is not None else self.reason,
        )
        return PublishJob.model_validate(datos)

    @property
    def terminal(self) -> bool:
        return self.state in ESTADOS_TERMINALES


class PublishResult(Artefacto):
    """Qué devolvió YouTube, una vez comprobado. Hecho observado.

    No sustituye a ``PublishMetadata``: la metadata dice qué se pidió y esto dice
    qué hay. ``privacy_status`` aparece en los dos porque son cosas distintas —lo
    solicitado y lo observado— y compararlos es precisamente en qué consiste
    verificar.

    ``COMPLETED`` exige ``video_id``: no se puede dar por completada una
    publicación sin el identificador de lo publicado. ``FAILED`` **no** lo exige,
    porque un fallo puede ocurrir antes de que YouTube devuelva nada. Y
    ``NEEDS_REVIEW`` no afirma ni que funcionara ni que fallara: es el estado de
    lo que hay que ir a comprobar, con o sin ``video_id``.

    El contrato no valida la forma de ``url``: construirla o comprobarla sería
    afirmar un esquema de URLs de YouTube que este Gate no utiliza todavía.
    """

    schema_version: str = SCHEMA_VERSION_PUBLICACION

    provider: TextoNoVacio = "youtube"
    video_id: str | None = None
    url: str | None = None
    status: EstadoPublicacion
    privacy_status: Privacidad | None = None
    upload_attempts: int = Field(ge=1)
    uploaded_at: datetime | None = None
    completed_at: datetime | None = None
    metadata_sha256: Sha256Hex | None = None
    video_sha256: Sha256Hex | None = None
    #: Gate 7.2. Qué se averiguó cuando el resultado de la subida no constaba.
    #: Un ``NEEDS_REVIEW`` que venga de una incertidumbre lo trae; uno que venga
    #: de una precondición incumplida, no.
    reconciliation: ReconciliationResult | None = None

    @model_validator(mode="after")
    def _comprobar_resultado(self) -> "PublishResult":
        if self.status not in ESTADOS_DE_RESULTADO:
            permitidos = sorted(e.value for e in ESTADOS_DE_RESULTADO)
            raise ValueError(
                f"{self.status.value!r} describe por dónde va el trabajo, no lo que "
                f"contestó YouTube; un resultado solo puede estar en {permitidos}"
            )

        tiene_id = bool((self.video_id or "").strip())

        if self.status is EstadoPublicacion.completado:
            if not tiene_id:
                raise ValueError(
                    "no se puede dar por completada una publicación sin video_id: "
                    "no habría constancia de qué se publicó"
                )
            if self.completed_at is None:
                raise ValueError("un resultado 'COMPLETED' registra cuándo se completó")
        elif self.completed_at is not None:
            raise ValueError(
                f"hay completed_at con estado {self.status.value!r}: solo "
                f"'COMPLETED' se ha completado"
            )

        # Un identificador y su fecha de subida van juntos: uno sin el otro deja
        # el resultado a medio describir.
        if tiene_id and self.uploaded_at is None:
            raise ValueError(
                "hay video_id sin uploaded_at: falta cuándo lo aceptó YouTube"
            )
        if self.uploaded_at is not None and not tiene_id:
            raise ValueError(
                "hay uploaded_at sin video_id: no consta qué se subió"
            )
        if tiene_id and not (self.url or "").strip():
            raise ValueError("hay video_id sin url: falta dónde quedó el vídeo")

        # UPLOADED y VERIFYING describen un vídeo que YouTube ya aceptó.
        if (
            self.status
            in (EstadoPublicacion.subido, EstadoPublicacion.verificando)
            and not tiene_id
        ):
            raise ValueError(
                f"el estado {self.status.value!r} significa que YouTube devolvió un "
                f"identificador, y no hay video_id"
            )
        return self
