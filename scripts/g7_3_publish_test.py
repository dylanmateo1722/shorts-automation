#!/usr/bin/env python3
"""Punto de entrada de la prueba de integración real de Gate 7.3.

Una sola subida, deliberadamente privada, contra YouTube de verdad.

Qué es este script y qué no es
------------------------------

Es **solo** un orquestador de la prueba: lee la corrida que el pipeline ya
produjo, arma la intención editorial, pasa por la puerta de publicación y llama
al publisher de Gate 7.2. No reimplementa nada.

La subida resumable, la reconciliación, la máquina de estados, la verificación
posterior y la protección contra la doble subida **siguen viviendo donde ya
estaban**, en ``app/adapters/youtube/``. Si este script tuviera una sola línea
de protocolo, habría dos implementaciones de lo mismo y una de las dos se
quedaría atrás.

Vive en ``scripts/`` y no como subcomando del CLI a propósito: el CLI sigue sin
poder publicar, así que la capacidad de subir no está a una bandera de distancia
de una corrida normal.

Salida
------

Tres archivos JSON saneados —``publish_job.json``, ``publish_result.json`` y
``g7_3_report.json``— y un resumen legible en stdout. Ninguno contiene
credenciales: el ``session_url`` está excluido de la serialización por contrato
y el access token no entra en ningún artefacto.

Código de salida: ``0`` solo si la publicación llegó a ``COMPLETED``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from uuid import UUID

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app.adapters.youtube.auth import (  # noqa: E402
    ResultadoAuth,
    TokenAcceso,
    cargar_credenciales,
    comprobar_autenticacion,
    obtener_access_token,
)
from app.adapters.youtube.publisher import publicar  # noqa: E402
from app.adapters.youtube.upload import (  # noqa: E402
    MULTIPLO_FRAGMENTO,
    URL_SUBIDA,
    RespuestaHTTP,
    TransporteUrllib,
)
from app.config.settings import Settings  # noqa: E402
from app.contracts.models import (  # noqa: E402
    DivulgacionIA,
    EstadoDivulgacionIA,
    EstadoPublicacion,
    Privacidad,
    ProvenanceLedger,
    PublishMetadata,
    QAResult,
    RenderJob,
    RenderResult,
)
from app.core.errors import ErrorPipeline  # noqa: E402
from app.pipeline.publicacion import evaluar_precondiciones  # noqa: E402

# ---------------------------------------------------------------------------
# Constantes de la prueba
# ---------------------------------------------------------------------------

#: El alcance que esta prueba exige tener **concedido**. No se pide uno nuevo ni
#: se cambia ninguno: se comprueba el que el refresh token ya trae.
SCOPE_REQUERIDO = "https://www.googleapis.com/auth/youtube"

#: Fragmento deliberadamente pequeño —el mínimo que el protocolo admite— para
#: que un vídeo de ~30 s produzca varios PUT y la prueba ejercite de verdad la
#: subida por fragmentos. En producción el valor por defecto es de 8 MiB.
TAMANO_FRAGMENTO = MULTIPLO_FRAGMENTO  # 256 KiB

#: Con este fragmento, un archivo mayor que esto garantiza al menos dos PUT.
MINIMO_PARA_DOS_FRAGMENTOS = TAMANO_FRAGMENTO + 1

#: La divulgación de IA de **este fixture concreto**, decidida por una persona.
#:
#: No es un valor por defecto del proyecto y no debe convertirse en uno: cada
#: pieza futura exige su propia decisión humana. Aquí aplica porque el material
#: es un degradado generado por el propio pipeline, sin nada que pueda
#: confundirse con la realidad.
MOTIVO_DIVULGACION = (
    "Fixture de prueba de integración de Gate 7.3. Utiliza visuales generados "
    "no realistas —un degradado producido por el propio pipeline— y narración "
    "sintética. No representa una persona real, no representa un "
    "acontecimiento real, no representa un lugar real y no representa una "
    "escena fotorrealista sintética. La decisión aplica solo a este fixture y "
    "no es un valor por defecto para ningún Short posterior: cada pieza exige "
    "su propia decisión humana."
)

TITULO = "Shorts Automation — G7.3 Integration Test"


# ---------------------------------------------------------------------------
# Transporte instrumentado
# ---------------------------------------------------------------------------


@dataclass
class TransporteContado:
    """Envuelve el transporte real y cuenta lo que ocurre, sin guardar URLs.

    Hace falta para poder **demostrar** que hubo una sola creación de sesión y
    al menos dos fragmentos. Lo que registra son métodos, códigos y tamaños;
    nunca el ``session_url``, que es secreto operativo, ni ninguna cabecera.

    No altera el comportamiento: delega en ``TransporteUrllib`` y devuelve su
    respuesta tal cual.
    """

    interno: TransporteUrllib = field(default_factory=TransporteUrllib)
    sesiones_creadas: int = 0
    fragmentos_enviados: int = 0
    consultas_de_progreso: int = 0
    lecturas: int = 0
    bytes_enviados: int = 0
    traza: list[dict] = field(default_factory=list)

    def peticion(
        self,
        metodo: str,
        url: str,
        *,
        headers: Mapping[str, str],
        cuerpo: bytes | None = None,
        timeout_s: int = 30,
    ) -> RespuestaHTTP:
        es_creacion = metodo == "POST" and url.startswith(URL_SUBIDA)
        tamano = len(cuerpo) if cuerpo else 0

        if es_creacion:
            self.sesiones_creadas += 1
            clase = "crear_sesion"
        elif metodo == "PUT" and tamano > 0:
            self.fragmentos_enviados += 1
            self.bytes_enviados += tamano
            clase = "fragmento"
        elif metodo == "PUT":
            self.consultas_de_progreso += 1
            clase = "consulta_progreso"
        else:
            self.lecturas += 1
            clase = "lectura"

        respuesta = self.interno.peticion(
            metodo, url, headers=headers, cuerpo=cuerpo, timeout_s=timeout_s
        )
        # Se anota la clase de operación y el código. El URL **no** se anota:
        # el de la sesión es un secreto y no hay forma segura de recortarlo.
        self.traza.append(
            {"operacion": clase, "metodo": metodo, "status": respuesta.status,
             "bytes": tamano}
        )
        return respuesta

    def resumen(self) -> dict:
        return {
            "session_post_count": self.sesiones_creadas,
            "chunk_put_count": self.fragmentos_enviados,
            "progress_query_count": self.consultas_de_progreso,
            "read_count": self.lecturas,
            "bytes_uploaded": self.bytes_enviados,
            "trace": self.traza,
        }


# ---------------------------------------------------------------------------
# Errores de la prueba
# ---------------------------------------------------------------------------


class PruebaAbortada(RuntimeError):
    """Una precondición de la prueba no se cumple. No se contacta con YouTube."""


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight(settings: Settings) -> dict:
    """Comprueba la autorización real y el alcance **concedido**.

    No pide un alcance nuevo, no genera un refresh token y no imprime ninguno.
    Lo que hace es leer qué concedió Google al canjear el refresh token, que es
    el único dato determinista sobre el alcance: la configuración local dice qué
    se pidió, no qué se tiene.

    La comparación es por igualdad exacta sobre la lista de alcances, no por
    subcadena. ``…/auth/youtube.upload`` **contiene** ``…/auth/youtube`` como
    texto y no es el mismo alcance; comprobarlo con ``in`` daría por bueno un
    token que no sirve para esta prueba.
    """
    comprobacion = comprobar_autenticacion(settings)
    concedidos = comprobacion.scope_concedido.split()

    informe = {
        **comprobacion.a_dict(),
        "required_scope": SCOPE_REQUERIDO,
        "granted_scopes": concedidos,
        "scope_ok": SCOPE_REQUERIDO in concedidos,
    }

    if comprobacion.resultado is not ResultadoAuth.autenticado:
        raise PruebaAbortada(
            f"el AUTH CHECK no autenticó: {comprobacion.resultado.value} "
            f"({comprobacion.detalle})"
        )
    if not comprobacion.canal_correcto:
        raise PruebaAbortada(
            "la autorización no corresponde al canal esperado; no se sube nada"
        )
    if not informe["scope_ok"]:
        raise PruebaAbortada(
            f"el alcance concedido no incluye {SCOPE_REQUERIDO!r}. Concedidos: "
            f"{concedidos or '(ninguno declarado)'}. No se intenta ninguna "
            f"subida: habría que reautorizar, y eso es una decisión aparte."
        )
    return informe


# ---------------------------------------------------------------------------
# Artefactos de la corrida
# ---------------------------------------------------------------------------


def _leer(directorio: Path, nombre: str, modelo):
    ruta = directorio / nombre
    if not ruta.is_file():
        raise PruebaAbortada(f"la corrida no tiene {nombre}: {ruta}")
    try:
        return modelo.model_validate_json(ruta.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - se reenvía con contexto
        raise PruebaAbortada(f"{nombre} no valida contra su contrato: {exc}") from exc


def construir_metadata(run_id: UUID) -> PublishMetadata:
    """La intención editorial de esta prueba, explícita en todos sus campos.

    Nada aquí se deja al valor por defecto de la biblioteca: la privacidad, la
    declaración para menores y la divulgación de IA se escriben a mano porque
    son decisiones, no configuración.
    """
    return PublishMetadata(
        run_id=run_id,
        title=TITULO,
        description=(
            "Prueba privada de integración técnica del proyecto Shorts "
            "Automation (Gate 7.3).\n\n"
            "No es contenido editorial y no está pensado para su visionado. "
            "El vídeo se genera íntegramente con el pipeline del proyecto: "
            "visual de degradado, narración sintética, subtítulos y rótulo "
            "propios. No contiene material de terceros.\n\n"
            f"run_id: {run_id}\n"
            "Este vídeo es privado y puede eliminarse sin consecuencias."
        ),
        tags=[],
        category_id=None,
        privacy_status=Privacidad.privado,
        language="es",
        made_for_kids=False,
        ai_disclosure=DivulgacionIA(
            status=EstadoDivulgacionIA.no_requerida,
            reason=MOTIVO_DIVULGACION,
            decided_by="human",
        ),
    )


def exigir_privacidad_privada(metadata: PublishMetadata) -> None:
    """Última barrera antes del publisher. Esta prueba solo sube en privado."""
    if metadata.privacy_status is not Privacidad.privado:
        raise PruebaAbortada(
            f"esta prueba solo publica en privado y la metadata pide "
            f"{metadata.privacy_status.value!r}"
        )


def exigir_tamano_para_varios_fragmentos(total_bytes: int) -> None:
    """Sin esto, la prueba del resumable no probaría el resumable.

    Un archivo que cabe en un fragmento se sube con un solo ``PUT``, y entonces
    el criterio «al menos dos fragmentos» no puede cumplirse por mucho que el
    tamaño de fragmento esté configurado.
    """
    if total_bytes < MINIMO_PARA_DOS_FRAGMENTOS:
        raise PruebaAbortada(
            f"el vídeo mide {total_bytes} bytes y con fragmentos de "
            f"{TAMANO_FRAGMENTO} haría falta más de {TAMANO_FRAGMENTO} para "
            f"producir al menos dos PUT de datos"
        )


# ---------------------------------------------------------------------------
# Programa
# ---------------------------------------------------------------------------


def ejecutar(run_id: UUID, directorio: Path, salida: Path) -> int:
    settings = Settings.desde_entorno()
    salida.mkdir(parents=True, exist_ok=True)

    informe: dict = {
        "gate": "7.3",
        "run_id": str(run_id),
        "required_scope": SCOPE_REQUERIDO,
        "chunk_size_bytes": TAMANO_FRAGMENTO,
    }

    def volcar(estado: str, **extra) -> None:
        informe["status"] = estado
        informe.update(extra)
        (salida / "g7_3_report.json").write_text(
            json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # --- 1. Preflight OAuth -------------------------------------------------
    print("[1/5] Preflight OAuth…")
    try:
        informe["preflight"] = preflight(settings)
    except PruebaAbortada as exc:
        volcar("ABORTED_PREFLIGHT", error=str(exc))
        print(f"ABORTADA en el preflight: {exc}", file=sys.stderr)
        return 2
    print(f"      autenticado, canal correcto, alcance {SCOPE_REQUERIDO} concedido")

    # --- 2. Artefactos de la corrida ---------------------------------------
    print("[2/5] Leyendo los artefactos de la corrida…")
    try:
        render_result = _leer(directorio, "final_video.json", RenderResult)
        # El job dice qué entró al vídeo. Es lo que la puerta necesita para
        # preguntarle al ledger por las **entradas** del render: el MP4 final es
        # el producto y no lleva decisión de licencia propia.
        render_job = _leer(directorio, "render_job.json", RenderJob)
        qa_result = _leer(directorio, "final_video_qa.json", QAResult)
        ledger = _leer(directorio, "provenance_ledger.json", ProvenanceLedger)
    except PruebaAbortada as exc:
        volcar("ABORTED_ARTIFACTS", error=str(exc))
        print(f"ABORTADA leyendo artefactos: {exc}", file=sys.stderr)
        return 2

    metadata = construir_metadata(run_id)
    informe["metadata"] = {
        "title": metadata.title,
        "privacy_status": metadata.privacy_status.value,
        "made_for_kids": metadata.made_for_kids,
        "language": metadata.language,
        "category_id": metadata.category_id,
        "ai_disclosure_status": metadata.ai_disclosure.status.value,
        "ai_disclosure_decided_by": metadata.ai_disclosure.decided_by,
    }

    # --- 3. Puerta de publicación ------------------------------------------
    print("[3/5] Evaluando la puerta de publicación…")
    gate = evaluar_precondiciones(
        render_result=render_result,
        render_job=render_job,
        qa_result=qa_result,
        ledger=ledger,
        metadata=metadata,
        directorio_corrida=directorio,
    )
    informe["gate"] = {
        "ready": gate.listo,
        "reasons": gate.motivos,
        "video_relative_path": render_result.output_path,
        "video_sha256": gate.video_sha256,
        "total_bytes": gate.total_bytes,
        "render_inputs": render_job.assets_de_render,
    }
    if not gate.listo:
        volcar("NOT_READY", error=gate.resumen)
        print(f"NOT_READY: {gate.resumen}", file=sys.stderr)
        print("No se ha contactado con YouTube.", file=sys.stderr)
        return 3

    try:
        exigir_privacidad_privada(metadata)
        exigir_tamano_para_varios_fragmentos(gate.total_bytes)
    except PruebaAbortada as exc:
        volcar("ABORTED_PRECONDITION", error=str(exc))
        print(f"ABORTADA: {exc}", file=sys.stderr)
        return 3
    print(
        f"      listo: {gate.total_bytes} bytes, "
        f"{-(-gate.total_bytes // TAMANO_FRAGMENTO)} fragmentos previstos"
    )

    # --- 4. Publicar --------------------------------------------------------
    print("[4/5] Publicando (una sola subida, privada)…")
    transporte = TransporteContado()
    try:
        token: TokenAcceso = obtener_access_token(
            cargar_credenciales(settings), timeout_s=settings.youtube_timeout_s
        )
        salida_publicacion = publicar(
            run_id=run_id,
            metadata=metadata,
            gate=gate,
            token=token,
            transporte=transporte,
            timeout_s=settings.youtube_timeout_s,
            tamano_fragmento=TAMANO_FRAGMENTO,
        )
    except ErrorPipeline as exc:
        volcar("ERROR", error=str(exc), transport=transporte.resumen())
        print(f"La publicación falló: {exc}", file=sys.stderr)
        return 4

    trabajo = salida_publicacion.job
    resultado = salida_publicacion.result

    (salida / "publish_job.json").write_text(
        trabajo.model_dump_json(indent=2), encoding="utf-8"
    )
    if resultado is not None:
        (salida / "publish_result.json").write_text(
            resultado.model_dump_json(indent=2), encoding="utf-8"
        )

    # --- 5. Informe ---------------------------------------------------------
    print("[5/5] Informe")
    informe["transport"] = transporte.resumen()
    informe["job_state"] = trabajo.state.value
    informe["job_attempt"] = trabajo.attempt
    informe["job_reason"] = trabajo.reason
    if resultado is not None:
        informe["result"] = {
            "status": resultado.status.value,
            "video_id": resultado.video_id,
            "url": resultado.url,
            "privacy_status": (
                resultado.privacy_status.value if resultado.privacy_status else None
            ),
            "upload_attempts": resultado.upload_attempts,
            "video_sha256": resultado.video_sha256,
            "reconciliation_outcome": (
                resultado.reconciliation.outcome.value
                if resultado.reconciliation
                else None
            ),
        }

    completado = trabajo.state is EstadoPublicacion.completado
    volcar("COMPLETED" if completado else trabajo.state.value)

    print(f"      estado:     {trabajo.state.value}")
    print(f"      sesiones:   {transporte.sesiones_creadas}")
    print(f"      fragmentos: {transporte.fragmentos_enviados}")
    if resultado is not None and resultado.video_id:
        print(f"      video_id:   {resultado.video_id}")
        print(f"      privacidad: {informe['result']['privacy_status']}")
    if not completado:
        print(
            f"NO se alcanzó COMPLETED ({trabajo.state.value}): "
            f"{trabajo.reason or 'sin motivo'}",
            file=sys.stderr,
        )
        if trabajo.state is EstadoPublicacion.revision_pendiente:
            print(
                "Revisa el canal a mano antes de volver a ejecutar nada: puede "
                "haber un vídeo subido sin confirmar.",
                file=sys.stderr,
            )
        return 5
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prueba de integración real de Gate 7.3: una subida privada a "
            "YouTube usando el publisher de Gate 7.2."
        )
    )
    parser.add_argument("run_id", help="UUID de la corrida ya producida")
    parser.add_argument(
        "--runs-dir", default="runs", help="raíz de las corridas (por defecto: runs)"
    )
    parser.add_argument(
        "--output", default="g7_3_artifacts",
        help="dónde escribir los artefactos saneados",
    )
    args = parser.parse_args(argv)

    try:
        run_id = UUID(args.run_id)
    except ValueError:
        print(f"run_id no es un UUID válido: {args.run_id!r}", file=sys.stderr)
        return 2

    directorio = Path(args.runs_dir) / str(run_id)
    if not directorio.is_dir():
        print(f"no existe la corrida {directorio}", file=sys.stderr)
        return 2

    return ejecutar(run_id, directorio, Path(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
