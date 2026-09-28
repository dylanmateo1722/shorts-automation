"""Un solo comando: de una URL a un Short listo para revisar.

Por qué existe
--------------

Todas las piezas ya estaban, pero encadenarlas eran seis pasos manuales: ingerir,
correr la voz, generar el material, copiarlo dentro de la corrida, escribir a
mano la declaración de procedencia y volver a correr el pipeline con los flags
correctos. Seis pasos son cinco oportunidades de equivocarse, y convertían
«hacer otro Short» en un procedimiento que había que recordar.

Aquí se hacen los seis, en orden, y sin que nadie edite un JSON.

Por qué el pipeline se ejecuta en dos tramos
--------------------------------------------

No es un capricho ni un apaño. La declaración de procedencia apunta a rutas
**dentro** del directorio de la corrida, y ese directorio lo crea el pipeline al
arrancar. Así que primero se corre hasta la voz —que es lo que lo crea y además
fija la duración real de la narración—, se deja el material dentro, y solo
entonces se corre el resto.

Hacerlo al revés obligaría a inventar la duración del material antes de saber
cuánto dura el audio, y a colocar archivos en un directorio que aún no existe.

Qué decide y qué no
-------------------

Decide lo que es mecánico: el tamaño del material (la duración de la narración
más un margen), su huella, cómo se declara y cómo se elige.

No decide lo que es humano: no inventa una licencia para la fuente, no aprueba
nada editorialmente y no publica. El Short queda en ``final/short.mp4`` para que
lo mire una persona.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.adapters.fuente import DocumentoFuente, ingerir
from app.adapters.material import LOOKS, generar
from app.config.settings import Settings
from app.contracts.models import RenderResult, VoiceAsset
from app.core.errors import EntradaInvalida, ErrorPipeline
from app.core.logging import log_evento
from app.core.manifest import EstadoEtapa
from app.core.run_id import nuevo_run_id
from app.core.workspace import Workspace
from app.pipeline.core import ejecutar_run
from app.pipeline.linguistic import construir_pipeline_linguistico
from app.pipeline.qa import construir_pipeline_transformacion
from app.pipeline.transformation import (
    CLAVE_DECLARACIONES,
    CLAVE_MATERIAL_SELECCIONADO,
    CLAVE_REFERENCIAS,
)
from app.pipeline.voice import ARTEFACTO_VOZ, construir_pipeline_voz

#: Dónde queda cada cosa dentro de la corrida. Son rutas relativas porque todo
#: artefacto las guarda así: una corrida hecha aquí tiene que seguir siendo
#: interpretable en otra máquina.
RUTA_MATERIAL = "material/aportado.mp4"
RUTA_FUENTE = "source/fuente.txt"
RUTA_DECLARACIONES = "source/sources.json"
RUTA_SHORT = "final/short.mp4"

#: Identificador de la fuente visual dentro del ledger.
ASSET_MATERIAL = "material-generado"

#: Cuánto material se genera de más respecto a la narración. El motor recorta lo
#: que sobra, pero no puede inventar lo que falta, y el audio real siempre sale
#: algo más largo que la estimación del guion.
MARGEN_MATERIAL_S = 3.0


@dataclass(frozen=True)
class ResultadoProduccion:
    """Lo que salió, y por dónde pasó."""

    run_id: UUID
    directorio: Path
    documento: DocumentoFuente
    short: Path
    duracion_s: float
    ancho: int
    alto: int
    sha256: str

    @property
    def licencia_consta(self) -> bool:
        return self.documento.licencia_consta


def _pipeline_hasta_voz() -> list:
    return construir_pipeline_linguistico() + construir_pipeline_voz()


def _pipeline_completo() -> list:
    from app.pipeline.qa_video import ETAPA_QA_VIDEO
    from app.pipeline.render import ETAPA_MATERIAL, construir_pipeline_render

    return (
        _pipeline_hasta_voz()
        + [ETAPA_MATERIAL]
        + construir_pipeline_transformacion()
        + [e for e in construir_pipeline_render() if e is not ETAPA_MATERIAL]
        + [ETAPA_QA_VIDEO]
    )


def _declarar_material(ruta: Path, huella: str, look: str) -> dict:
    """La declaración de procedencia del material, escrita por el sistema.

    La base es ``own`` y se puede defender: el archivo lo produce este mismo
    repositorio a partir de filtros de FFmpeg, sin incorporar ninguna obra
    ajena, y cualquiera puede regenerarlo con el mismo comando y comparar la
    huella. No es una licencia alegada: es una que se verifica reproduciéndola.
    """
    return {
        "note": (
            "Declaración generada por 'app short'. El material visual es propio "
            "y procedimental; la fuente del texto se declara aparte y solo como "
            "referencia."
        ),
        "sources": [
            {
                "basis": "own",
                "asset": {
                    "asset_id": ASSET_MATERIAL,
                    "media_kind": "video",
                    "origin": (
                        f"generado por app.adapters.material con FFmpeg "
                        f"(look {look}): gradiente animado, líneas de barrido, "
                        f"grano y viñeta. Sin fuente externa."
                    ),
                    "local_path": RUTA_MATERIAL,
                    "sha256": huella,
                    "commercial_use_allowed": True,
                    "evidence": [
                        {
                            "evidence_id": "ev-material-generado",
                            "kind": "ownership_record",
                            "reference": f"app.adapters.material.generar(look={look!r})",
                            "description": (
                                "El archivo lo produce este repositorio con "
                                "filtros de FFmpeg y es reproducible: "
                                "regenerarlo con el mismo comando permite "
                                "comprobar la huella."
                            ),
                            "issued_by": "shorts-automation",
                        }
                    ],
                },
            }
        ],
    }


def producir_short(
    url: str,
    *,
    settings: Settings | None = None,
    run_id: UUID | None = None,
    duracion_objetivo_s: float = 45.0,
    look: str = "frio",
    max_caracteres: int | None = None,
    idioma_fuente: str = "en",
) -> ResultadoProduccion:
    """De una URL a ``final/short.mp4``. Sin pasos manuales por el medio.

    Requiere que el proveedor de LLM esté configurado; si no lo está, la etapa
    de traducción falla con un error que dice qué falta. Eso es deliberado: un
    Short sin traducir no es un Short a medias, es otra cosa.

    Raises:
        EntradaInvalida: el ``look`` no existe, o la URL no es utilizable.
        ErrorPipeline: lo que falle dentro del pipeline, con su etapa.
    """
    if look not in LOOKS:
        raise EntradaInvalida(
            f"look desconocido {look!r}; admitidos: {', '.join(sorted(LOOKS))}"
        )

    settings = settings or Settings.desde_entorno()
    run_id = run_id or nuevo_run_id()
    workspace = Workspace(settings.raiz_runs, run_id)

    # --- 1. La fuente ------------------------------------------------------
    documento = ingerir(
        url,
        idioma=idioma_fuente,
        **({} if max_caracteres is None else {"max_caracteres": max_caracteres}),
    )
    log_evento(
        run_id, "produccion", "ingerido",
        provider=documento.provider,
        characters=len(documento.text),
        license_id=documento.license_id or "no-declarada",
    )

    # El transcript se deja en el directorio de la corrida, no en un temporal:
    # así la corrida contiene su propia entrada y se puede reproducir sola.
    workspace.crear()
    ruta_transcript = workspace.ruta("source/transcript.json")
    ruta_transcript.parent.mkdir(parents=True, exist_ok=True)
    ruta_transcript.write_text(
        json.dumps(documento.a_transcript(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    workspace.ruta(RUTA_FUENTE).write_text(
        documento.a_atribucion(), encoding="utf-8"
    )

    parametros_base = {
        "transcript_path": str(ruta_transcript),
        "target_duration_seconds": duracion_objetivo_s,
    }

    # --- 2. Hasta la voz: crea la corrida y fija la duración real ----------
    resultado = ejecutar_run(
        run_id, settings, parametros=dict(parametros_base),
        etapas=_pipeline_hasta_voz(),
    )
    if not resultado.exito:
        raise _fallo(resultado, "no se pudo llegar a la narración")

    voz = workspace.leer_artefacto(ARTEFACTO_VOZ, VoiceAsset)
    duracion_audio = voz.audio_duration_seconds

    # --- 3. El material, del largo que hace falta -------------------------
    destino_material = workspace.ruta(RUTA_MATERIAL)
    generar(
        destino_material,
        duracion_s=duracion_audio + MARGEN_MATERIAL_S,
        look=look,
    )
    huella = hashlib.sha256(destino_material.read_bytes()).hexdigest()
    log_evento(
        run_id, "produccion", "material",
        look=look, seconds=round(duracion_audio + MARGEN_MATERIAL_S, 2),
        bytes=destino_material.stat().st_size,
    )

    # --- 4. Su declaración, escrita por el sistema ------------------------
    ruta_declaraciones = workspace.ruta(RUTA_DECLARACIONES)
    ruta_declaraciones.write_text(
        json.dumps(_declarar_material(destino_material, huella, look),
                   indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # --- 5. El resto del pipeline -----------------------------------------
    parametros = {
        **parametros_base,
        CLAVE_DECLARACIONES: str(ruta_declaraciones),
        CLAVE_MATERIAL_SELECCIONADO: ASSET_MATERIAL,
        # El texto de la fuente se consulta, no se renderiza: entra como
        # referencia y la procedencia lo bloquea para el render.
        CLAVE_REFERENCIAS: [RUTA_FUENTE],
    }
    resultado = ejecutar_run(
        run_id, settings, parametros=parametros, etapas=_pipeline_completo()
    )
    if not resultado.exito:
        raise _fallo(resultado, "no se pudo llegar al Short")

    final = workspace.leer_artefacto("final_video", RenderResult)
    short = workspace.ruta(final.output_path or RUTA_SHORT)
    log_evento(
        run_id, "produccion", "listo",
        output=final.output_path, seconds=final.duration_s,
        width=final.width, height=final.height,
    )

    return ResultadoProduccion(
        run_id=run_id,
        directorio=workspace.dir,
        documento=documento,
        short=short,
        duracion_s=final.duration_s or 0.0,
        ancho=final.width or 0,
        alto=final.height or 0,
        sha256=final.sha256 or "",
    )


def _fallo(resultado, mensaje: str) -> ErrorPipeline:
    """Convierte un run fallido en un error que dice en qué etapa se paró.

    ``ejecutar_run`` no devuelve la excepción original —la registra y devuelve
    ``exito=False``—, así que la etapa se recupera del manifest, que es donde
    queda escrita. Sin esto el error diría «no se pudo» sin decir dónde, que es
    justo lo que no sirve para arreglarlo.
    """
    fallidas = [
        entrada
        for entrada in resultado.manifest.stages
        if entrada.status is EstadoEtapa.fallida
    ]
    if not fallidas:
        return ErrorPipeline(f"{mensaje}; revisa el log de la corrida")
    etapa = fallidas[0]
    causa = f": {etapa.error}" if etapa.error else ""
    return ErrorPipeline(f"{mensaje}, en la etapa {etapa.name!r}{causa}")
