"""Gate 7.4-A: material visual real, declarado y seleccionado explícitamente.

Lo que esta capa añade es una distinción que antes no existía:

    «puedo renderizar este recurso»       lo decide la política de procedencia
    «quiero este recurso como material»   lo decide una persona

Autorizar no es seleccionar, y seleccionar no autoriza. Los tests de este
archivo existen para que ninguna de las dos mitades pueda implicar a la otra por
descuido: una fuente autorizada que nadie eligió no entra al vídeo, y una fuente
elegida que no está autorizada detiene la corrida en vez de colarse.

El caso que da sentido a todo el archivo es
``test_un_material_de_referencia_no_llega_al_motor``: no basta con que el objeto
Python falle, hay que demostrar que MoneyPrinterTurbo **ni siquiera se ejecutó**.
"""

from __future__ import annotations

import hashlib
import shutil

import pytest

from app.contracts.models import (
    BaseLicencia,
    ClaseFuente,
    EstadoRender,
    Evidence,
    LicenseDecision,
    ProvenanceLedger,
    RenderJob,
    RenderResult,
    SourceAsset,
    TipoEvidencia,
    TipoMedio,
)
from app.config.provenance import VERSION_POLITICA
from app.core.errors import EntradaInvalida, ProcedenciaInvalida
from app.core.manifest import Manifest
from app.core.stage_runner import StageRunner
from app.pipeline import render, transformation
from app.pipeline.stages import ARTEFACTO_JOB, ARTEFACTO_RESULTADO
from app.pipeline.transformation import (
    CLAVE_MATERIAL_SELECCIONADO,
    material_seleccionado,
    resolver_material,
)

RUTA_LOG_MOTOR = "render/mpt.log"

#: El material real de la pieza. Es un archivo local de la corrida: el pipeline
#: no descarga nada ni descubre nada, alguien lo deja ahí y lo declara.
RUTA_MATERIAL_REAL = "material/aportado.mp4"
ASSET_REAL = "material-editorial"


# ---------------------------------------------------------------------------
# Andamiaje
# ---------------------------------------------------------------------------


@pytest.fixture
def runner(settings_e2e, workspace_e2e) -> StageRunner:
    manifest = Manifest.cargar(workspace_e2e.dir)
    return StageRunner(workspace_e2e, manifest, settings_e2e)


def _colocar_material_real(ws, contenido: bytes | None = None) -> str:
    """Copia el material generado a otra ruta y lo devuelve con su huella.

    Se reutiliza un MP4 que ya existe y que FFmpeg produjo de verdad: la prueba
    va sobre el cableado de la procedencia, no sobre el códec, y un archivo real
    evita que el motor falle por algo que no es lo que se está probando.
    """
    destino = ws.ruta(RUTA_MATERIAL_REAL)
    destino.parent.mkdir(parents=True, exist_ok=True)
    if contenido is None:
        generado = ws.leer_artefacto(render.ARTEFACTO_MATERIAL, RenderResult)
        shutil.copyfile(ws.ruta(generado.output_path), destino)
    else:
        destino.write_bytes(contenido)
    return hashlib.sha256(destino.read_bytes()).hexdigest()


def _con_fuente_real(
    ledger: ProvenanceLedger,
    huella: str,
    *,
    clase: ClaseFuente = ClaseFuente.render_permitido,
    basis: BaseLicencia = BaseLicencia.propia,
    autorizar: bool = True,
) -> ProvenanceLedger:
    """Ledger con una fuente externa más, la del material aportado.

    ``autorizar`` controla si la ruta entra además en ``render_assets``. Separa
    las dos cosas a propósito: una fuente puede estar decidida
    ``render_allowed`` y no figurar como utilizable en esta corrida.
    """
    evidencia = Evidence(
        evidence_id="ev-material-real",
        kind=TipoEvidencia.registro_propiedad,
        reference="captura propia aportada a la corrida",
        description="material visual editorial de esta pieza",
        issued_by="editorial",
    )
    fuente = SourceAsset(
        run_id=ledger.run_id,
        asset_id=ASSET_REAL,
        media_kind=TipoMedio.video,
        origin="aportado a mano por la persona que edita",
        local_path=RUTA_MATERIAL_REAL,
        sha256=huella,
        evidence=[evidencia],
    )
    decision = LicenseDecision(
        run_id=ledger.run_id,
        asset_id=ASSET_REAL,
        decision=clase,
        basis=basis,
        evidence_ids=(
            ["ev-material-real"] if clase is ClaseFuente.render_permitido else []
        ),
        reason="material editorial aportado y decidido a mano",
        decided_by="tests",
        policy_version=VERSION_POLITICA,
    )
    datos = ledger.model_dump()
    datos["sources"] = [*datos["sources"], fuente.model_dump()]
    datos["decisions"] = [*datos["decisions"], decision.model_dump()]
    if autorizar and clase is ClaseFuente.render_permitido:
        datos["render_assets"] = [*datos["render_assets"], RUTA_MATERIAL_REAL]
    return ProvenanceLedger.model_validate(datos)


def _preparar(runner, **kwargs):
    """Deja la corrida con el material real colocado y declarado en el ledger."""
    ws = runner.workspace
    huella = _colocar_material_real(ws, kwargs.pop("contenido", None))
    ledger = ws.leer_artefacto(transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger)
    nuevo = _con_fuente_real(ledger, huella, **kwargs)
    ws.escribir_artefacto(transformation.ARTEFACTO_PROCEDENCIA, nuevo)
    runner.producidos.pop(transformation.ARTEFACTO_PROCEDENCIA, None)
    return huella


def _job_con_seleccion(runner, asset_id: str | None) -> RenderJob:
    if asset_id is not None:
        runner.parametros[CLAVE_MATERIAL_SELECCIONADO] = asset_id
    return runner.ejecutar(render.ETAPA_JOB, forzar=True).artefacto


# ---------------------------------------------------------------------------
# 1. La fuente seleccionada llega a RenderJob.materials
# ---------------------------------------------------------------------------


def test_una_fuente_render_allowed_seleccionada_es_el_material(runner):
    _preparar(runner)
    job = _job_con_seleccion(runner, ASSET_REAL)

    assert job.materials == [RUTA_MATERIAL_REAL]
    assert RUTA_MATERIAL_REAL in job.assets_de_render


def test_el_material_generado_deja_de_ser_el_del_video(runner):
    """Sustituye al degradado; no se añade a él."""
    ws = runner.workspace
    generado = ws.leer_artefacto(render.ARTEFACTO_MATERIAL, RenderResult).output_path
    _preparar(runner)
    job = _job_con_seleccion(runner, ASSET_REAL)

    assert generado not in job.materials


# ---------------------------------------------------------------------------
# 2-6 y 11. Autorizar y seleccionar son cosas distintas
# ---------------------------------------------------------------------------


def test_una_fuente_render_allowed_no_seleccionada_no_entra(runner):
    """El criterio arquitectónico: autorizado no implica usado."""
    ws = runner.workspace
    generado = ws.leer_artefacto(render.ARTEFACTO_MATERIAL, RenderResult).output_path
    _preparar(runner)

    job = _job_con_seleccion(runner, None)

    assert job.materials == [generado], "se coló una fuente que nadie eligió"
    assert RUTA_MATERIAL_REAL not in job.assets_de_render


@pytest.mark.parametrize(
    "clase",
    [
        ClaseFuente.solo_referencia,
        ClaseFuente.bloqueado,
        ClaseFuente.revision_pendiente,
    ],
)
def test_una_clase_que_no_autoriza_no_puede_seleccionarse(runner, clase):
    """Elegir un recurso no lo autoriza: la corrida se detiene."""
    _preparar(runner, clase=clase, autorizar=False)

    with pytest.raises(ProcedenciaInvalida) as capturado:
        _job_con_seleccion(runner, ASSET_REAL)

    assert clase.value in str(capturado.value)
    assert ASSET_REAL in str(capturado.value)


@pytest.mark.parametrize(
    "base",
    [BaseLicencia.desconocida, BaseLicencia.uso_legitimo_alegado],
)
def test_las_bases_que_no_se_sostienen_solas_no_llegan_al_material(runner, base):
    """``unknown`` y ``fair_use_claim`` siempre acaban en revisión."""
    from app.config.provenance import decidir

    ws = runner.workspace
    huella = _colocar_material_real(ws)
    fuente = SourceAsset(
        run_id=ws.run_id,
        asset_id=ASSET_REAL,
        media_kind=TipoMedio.video,
        origin="material de origen incierto",
        local_path=RUTA_MATERIAL_REAL,
        sha256=huella,
    )
    decision = decidir(fuente, base, run_id=ws.run_id)
    assert decision.decision is ClaseFuente.revision_pendiente, (
        f"la política v1 cambió para {base.value}"
    )

    ledger = ws.leer_artefacto(transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger)
    datos = ledger.model_dump()
    datos["sources"] = [*datos["sources"], fuente.model_dump()]
    datos["decisions"] = [*datos["decisions"], decision.model_dump()]
    ws.escribir_artefacto(
        transformation.ARTEFACTO_PROCEDENCIA,
        ProvenanceLedger.model_validate(datos),
    )
    runner.producidos.pop(transformation.ARTEFACTO_PROCEDENCIA, None)

    with pytest.raises(ProcedenciaInvalida):
        _job_con_seleccion(runner, ASSET_REAL)


def test_seleccionar_un_asset_que_no_existe_se_rechaza(runner):
    _preparar(runner)
    with pytest.raises(EntradaInvalida, match="no hay ninguna fuente declarada"):
        _job_con_seleccion(runner, "no-existe")


def test_seleccionar_una_fuente_sin_archivo_local_se_rechaza(runner):
    """Un Short ajeno del que solo se sabe la URL no puede ser el material."""
    ws = runner.workspace
    ledger = ws.leer_artefacto(transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger)
    fuente = SourceAsset(
        run_id=ws.run_id,
        asset_id="solo-url",
        media_kind=TipoMedio.video,
        origin="un vídeo del que solo se conoce la dirección",
        source_url="https://example.invalid/algo",
    )
    decision = LicenseDecision(
        run_id=ws.run_id,
        asset_id="solo-url",
        decision=ClaseFuente.solo_referencia,
        basis=BaseLicencia.propia,
        reason="consultado como referencia",
        decided_by="tests",
        policy_version=VERSION_POLITICA,
    )
    datos = ledger.model_dump()
    datos["sources"] = [*datos["sources"], fuente.model_dump()]
    datos["decisions"] = [*datos["decisions"], decision.model_dump()]
    ws.escribir_artefacto(
        transformation.ARTEFACTO_PROCEDENCIA,
        ProvenanceLedger.model_validate(datos),
    )
    runner.producidos.pop(transformation.ARTEFACTO_PROCEDENCIA, None)

    with pytest.raises(EntradaInvalida, match="no tiene archivo local"):
        _job_con_seleccion(runner, "solo-url")


# ---------------------------------------------------------------------------
# 7. Integridad: el archivo no puede cambiar después de la decisión
# ---------------------------------------------------------------------------


def test_si_el_material_cambia_tras_decidirse_el_render_se_rechaza(runner):
    """La decisión se tomó sobre un contenido concreto."""
    _preparar(runner)
    # Se sustituye el archivo después de registrar su huella.
    runner.workspace.ruta(RUTA_MATERIAL_REAL).write_bytes(b"otro-contenido" * 64)

    with pytest.raises(ProcedenciaInvalida, match="cambió desde que se autorizó"):
        _job_con_seleccion(runner, ASSET_REAL)


def test_la_huella_se_comprueba_contra_el_archivo_no_contra_el_nombre(runner):
    """Renombrar no basta: lo que se compara es el contenido."""
    huella = _preparar(runner)
    ws = runner.workspace
    real = hashlib.sha256(ws.ruta(RUTA_MATERIAL_REAL).read_bytes()).hexdigest()
    assert real == huella

    job = _job_con_seleccion(runner, ASSET_REAL)
    fuente = ws.leer_artefacto(
        transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger
    ).fuente_de(job.materials[0])
    assert fuente is not None
    assert fuente.sha256 == real


# ---------------------------------------------------------------------------
# 12. Enforcement real: el motor no se ejecuta
# ---------------------------------------------------------------------------


def test_un_material_de_referencia_no_llega_al_motor(runner):
    """La prueba que importa: MoneyPrinterTurbo **ni siquiera arranca**.

    Que el job falle no basta. Si el motor llegara a ejecutarse, el vídeo ya
    estaría hecho con material que nadie autorizó, y eso no se deshace.
    """
    ws = runner.workspace
    _preparar(runner, clase=ClaseFuente.solo_referencia, autorizar=False)

    assert not ws.ruta(RUTA_LOG_MOTOR).exists(), "el motor ya había corrido"

    with pytest.raises(ProcedenciaInvalida):
        _job_con_seleccion(runner, ASSET_REAL)

    # El motor deja siempre su log al ejecutarse, en éxito o en fallo.
    assert not ws.ruta(RUTA_LOG_MOTOR).exists(), "el motor se ejecutó pese al bloqueo"
    assert not ws.existe(ARTEFACTO_JOB)
    assert not ws.existe(ARTEFACTO_RESULTADO)


def test_el_material_real_llega_de_verdad_al_motor(runner, settings_e2e):
    """El otro lado del enforcement: lo que el motor recibe es el archivo elegido.

    No se afirma sobre el objeto ``RenderJob`` sino sobre lo que el motor
    registró haber recibido en ``--video-materials``. Entre el job y el motor
    está el adaptador, y esta es la única forma de comprobar que no se pierde
    nada por el camino.
    """
    from app.pipeline.stages import ETAPA_RENDER
    from tests.conftest import ultima_invocacion

    ws = runner.workspace
    _preparar(runner)
    job = _job_con_seleccion(runner, ASSET_REAL)
    assert job.materials == [RUTA_MATERIAL_REAL]

    runner.ejecutar(ETAPA_RENDER, forzar=True)

    assert ws.ruta(RUTA_LOG_MOTOR).exists(), "el motor no llegó a ejecutarse"
    recibido = ultima_invocacion(settings_e2e.raiz_mpt)
    materiales = [m for m in recibido["video_materials"] if m]
    assert len(materiales) == 1, materiales
    assert materiales[0].endswith(RUTA_MATERIAL_REAL), materiales[0]

    generado = ws.leer_artefacto(render.ARTEFACTO_MATERIAL, RenderResult).output_path
    assert not materiales[0].endswith(generado), "el motor recibió el degradado"

    resultado = ws.leer_artefacto(ARTEFACTO_RESULTADO, RenderResult)
    assert resultado.status is EstadoRender.exito


# ---------------------------------------------------------------------------
# 8-10. Lo que ya funcionaba sigue funcionando
# ---------------------------------------------------------------------------


def test_sin_seleccion_el_material_generado_sigue_funcionando(runner):
    """G7.4-A añade una capacidad; no sustituye la existente."""
    ws = runner.workspace
    generado = ws.leer_artefacto(render.ARTEFACTO_MATERIAL, RenderResult).output_path

    job = _job_con_seleccion(runner, None)

    assert job.materials == [generado]
    assert generado in job.assets_de_render


def test_render_job_admite_varios_materiales(runner):
    """El contrato ya lo permitía y se comprueba que se mantiene."""
    ws = runner.workspace
    generado = ws.leer_artefacto(render.ARTEFACTO_MATERIAL, RenderResult).output_path
    _preparar(runner)

    job = RenderJob(
        run_id=ws.run_id,
        task_id=ws.run_id,
        script="guion",
        audio_path=ws.leer_artefacto(render.ARTEFACTO_MATERIAL, RenderResult).output_path,
        materials=[generado, RUTA_MATERIAL_REAL],
    )
    assert job.materials == [generado, RUTA_MATERIAL_REAL]
    assert generado in job.assets_de_render
    assert RUTA_MATERIAL_REAL in job.assets_de_render


def test_la_seleccion_es_determinista(runner):
    """Dos construcciones del job con la misma entrada dan el mismo material."""
    _preparar(runner)
    primero = _job_con_seleccion(runner, ASSET_REAL)
    segundo = _job_con_seleccion(runner, ASSET_REAL)

    assert primero.materials == segundo.materials == [RUTA_MATERIAL_REAL]
    assert primero.assets_de_render == segundo.assets_de_render


# ---------------------------------------------------------------------------
# Resolución: la función que traduce asset_id a ruta
# ---------------------------------------------------------------------------


def test_material_seleccionado_lee_la_configuracion_de_la_corrida():
    from app.core.stage_runner import ContextoEtapa

    ctx = ContextoEtapa(
        workspace=None, manifest=None, settings=None, previos={},
        parametros={CLAVE_MATERIAL_SELECCIONADO: "  mi-asset  "},
    )
    assert material_seleccionado(ctx) == "mi-asset"


def test_sin_configuracion_no_hay_seleccion():
    from app.core.stage_runner import ContextoEtapa

    ctx = ContextoEtapa(
        workspace=None, manifest=None, settings=None, previos={}, parametros={}
    )
    assert material_seleccionado(ctx) is None


def test_una_seleccion_vacia_es_un_error():
    from app.core.stage_runner import ContextoEtapa

    ctx = ContextoEtapa(
        workspace=None, manifest=None, settings=None, previos={},
        parametros={CLAVE_MATERIAL_SELECCIONADO: "   "},
    )
    with pytest.raises(EntradaInvalida, match="sin identificador"):
        material_seleccionado(ctx)


def test_resolver_material_exige_decision_render_allowed(runner):
    _preparar(runner, clase=ClaseFuente.bloqueado, autorizar=False)
    ledger = runner.workspace.leer_artefacto(
        transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger
    )
    with pytest.raises(ProcedenciaInvalida, match="elegir un recurso no lo autoriza"):
        resolver_material(ledger, ASSET_REAL, etapa="prueba")


# ---------------------------------------------------------------------------
# E2E: material real local recorriendo la cadena entera
# ---------------------------------------------------------------------------


def test_e2e_material_real_hasta_el_video_final(runner, settings_e2e):
    """La cadena completa con material aportado, sin red y sin YouTube.

        material local → provenance render_allowed → selección explícita
        → RenderJob.materials → MPT → RenderResult → composición → MP4 final

    El fixture es un MP4 pequeño y determinista producido por el propio
    pipeline: no se usa material de terceros en CI.
    """
    from app.pipeline.stages import ETAPA_RENDER

    ws = runner.workspace
    huella = _preparar(runner)

    # 1. El material está declarado, autorizado y con su huella registrada.
    ledger = ws.leer_artefacto(transformation.ARTEFACTO_PROCEDENCIA, ProvenanceLedger)
    assert ledger.permite_render(RUTA_MATERIAL_REAL)
    assert ledger.fuente(ASSET_REAL).sha256 == huella

    # 2. La selección explícita lo convierte en el material de la pieza.
    job = _job_con_seleccion(runner, ASSET_REAL)
    assert job.materials == [RUTA_MATERIAL_REAL]

    # 3. El motor lo recibe.
    runner.ejecutar(ETAPA_RENDER, forzar=True)
    intermedio = ws.leer_artefacto(ARTEFACTO_RESULTADO, RenderResult)
    assert intermedio.status is EstadoRender.exito

    # 4. La composición produce el MP4 final, medido sobre el archivo.
    final = runner.ejecutar(render.ETAPA_COMPOSICION, forzar=True).artefacto
    assert final.status is EstadoRender.exito
    assert final.output_path
    archivo = ws.ruta(final.output_path)
    assert archivo.is_file() and archivo.stat().st_size > 0
    assert final.sha256 == hashlib.sha256(archivo.read_bytes()).hexdigest()

    # 5. La trazabilidad se sostiene de punta a punta.
    assert final.combined_path == intermedio.output_path
    assert RUTA_MATERIAL_REAL in job.assets_de_render
    assert ledger.fuente_de(job.materials[0]).asset_id == ASSET_REAL
