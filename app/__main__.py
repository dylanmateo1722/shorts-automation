"""CLI del orquestador.

    python -m app run [--pipeline render|linguistic|voice|transformation|e2e]
                      [--run-id UUID] [--force STAGE] [--reference-asset RUTA]
                      [--sources ARCHIVO] [--material-asset ASSET_ID]
    python -m app validate <run-id> [--pipeline ...]
    python -m app publish <run-id> --metadata RUTA --approval RUTA
                          [--confirmar SUBIR]
    python -m app youtube-auth
    python -m app youtube-auth-bootstrap --credentials RUTA
    python -m app youtube-auth-device --credentials RUTA

No existe un comando ``resume`` separado: reanudar es ejecutar ``run`` con el
mismo ``--run-id``, porque las etapas cuyo artefacto sigue siendo válido se
omiten solas. Un alias no aportaría nada.

``publish`` es un comando aparte y no una etapa de ``run`` **a propósito**. La
idempotencia del ``StageRunner`` es «si el artefacto sigue válido se omite, si no
se re-ejecuta», y re-ejecutar una publicación es subir otra vez; como etapa,
``run --force publication`` sería un botón de «súbelo de nuevo». Aquí no hay tal
botón: ``run`` no puede publicar, y ``publish`` sin ``--confirmar SUBIR`` evalúa
la puerta y se detiene sin tocar la red.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from app.config.settings import Settings
from app.contracts.models import EstadoPublicacion
from app.core.errors import ErrorPipeline
from app.core.logging import configurar_logging
from app.core.run_id import nuevo_run_id, parsear_run_id
from app.pipeline.core import construir_pipeline, ejecutar_run, validar_run
from app.pipeline.linguistic import construir_pipeline_linguistico
from app.pipeline.qa import construir_pipeline_transformacion
from app.pipeline.transformation import (
    CLAVE_DECLARACIONES,
    CLAVE_MATERIAL_SELECCIONADO,
    CLAVE_REFERENCIAS,
)
from app.pipeline.voice import construir_pipeline_voz


def _pipeline_voz() -> list:
    """Transcript → … → AdaptedScript → VoiceAsset → SubtitleAsset.

    La voz no se ejecuta sola: necesita un guion adaptado. Encadenar aquí las
    etapas lingüísticas evita tener que lanzar dos corridas y mantiene un solo
    ``run_id`` para todos los artefactos.
    """
    return construir_pipeline_linguistico() + construir_pipeline_voz()


def _pipeline_transformacion() -> list:
    """… → SubtitleAsset → overlay → procedencia → transformación → QA técnica."""
    return _pipeline_voz() + construir_pipeline_transformacion()


def _pipeline_e2e() -> list:
    """Transcript → … → MP4 final → QA del vídeo. El extremo a extremo completo.

    El material visual se genera **antes** de la procedencia: el ledger no puede
    autorizar lo que todavía no existe, y la puerta de procedencia se apoya en
    lo que el ledger declara.
    """
    from app.pipeline.qa_video import ETAPA_QA_VIDEO
    from app.pipeline.render import ETAPA_MATERIAL, construir_pipeline_render

    return (
        _pipeline_voz()
        + [ETAPA_MATERIAL]
        + construir_pipeline_transformacion()
        + [e for e in construir_pipeline_render() if e is not ETAPA_MATERIAL]
        + [ETAPA_QA_VIDEO]
    )


PIPELINES = {
    "render": construir_pipeline,
    "linguistic": construir_pipeline_linguistico,
    "voice": _pipeline_voz,
    "transformation": _pipeline_transformacion,
    "e2e": _pipeline_e2e,
}


def _construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app", description="Orquestador de shorts-automation (Gate 1)"
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    ejecutar = sub.add_parser("run", help="ejecuta o reanuda una corrida")
    ejecutar.add_argument(
        "--pipeline", choices=sorted(PIPELINES), default="render",
        help="secuencia a ejecutar (por defecto: render, el de Gate 1)",
    )
    ejecutar.add_argument(
        "--transcript", default=None, metavar="RUTA",
        help="transcript JSON de partida; obligatorio para linguistic y voice",
    )
    ejecutar.add_argument(
        "--target-duration", type=float, default=None, metavar="SEGUNDOS",
        help="duración objetivo del guion adaptado (por defecto: 45)",
    )
    ejecutar.add_argument(
        "--run-id",
        default=None,
        help="UUID de la corrida; se genera uno si se omite. Reanuda si ya existe",
    )
    ejecutar.add_argument(
        "--reference-asset", action="append", default=None, metavar="RUTA",
        help="recurso consultado SOLO como referencia, relativo al directorio de "
             "la corrida. Se registra en la procedencia y queda bloqueado para "
             "el render. Se puede repetir",
    )
    ejecutar.add_argument(
        "--sources", default=None, metavar="ARCHIVO",
        help="JSON que declara fuentes externas con su base de licencia y su "
             "evidencia. La política decide la clase de cada una; declararlas no "
             "las autoriza. No se descarga nada: las rutas locales deben existir "
             "ya en el directorio de la corrida",
    )
    ejecutar.add_argument(
        "--material-asset", default=None, metavar="ASSET_ID",
        help="identificador de la fuente declarada en --sources que se usa como "
             "material visual de esta pieza. Autorizar y elegir son cosas "
             "distintas: una fuente 'render_allowed' que no se elija aquí no "
             "entra al vídeo, y elegir una que no esté autorizada detiene la "
             "corrida. Sin este argumento se usa el material que genera el "
             "propio pipeline",
    )
    ejecutar.add_argument(
        "--force", default=None, metavar="ETAPA",
        help="re-ejecuta esta etapa aunque su artefacto sea válido",
    )

    validar = sub.add_parser("validate", help="revalida los artefactos de una corrida")
    validar.add_argument("run_id", help="UUID de la corrida")
    validar.add_argument(
        "--pipeline", choices=sorted(PIPELINES), default="render",
        help="secuencia con la que se validan los artefactos",
    )

    publicar = sub.add_parser(
        "publish",
        help="publica en privado el vídeo editorial de una corrida. Sin "
             "--confirmar SUBIR evalúa la puerta y se detiene sin tocar la red",
    )
    publicar.add_argument("run_id", help="UUID de la corrida ya renderizada")
    publicar.add_argument(
        "--metadata", required=True, metavar="RUTA",
        help="JSON con la metadata editorial. Se valida contra MetadataEditorial: "
             "la privacidad tiene que ser 'private' y contains_synthetic_media "
             "tiene que estar declarado. Nada se deriva de otra variable",
    )
    publicar.add_argument(
        "--approval", required=True, metavar="RUTA",
        help="JSON con la aprobación editorial y legal humana. Sin un "
             "APPROVED_BY_HUMAN atado a esta corrida, a este vídeo y a este "
             "material, no se publica",
    )
    publicar.add_argument(
        "--confirmar", default=None, metavar="SUBIR",
        help="escribe exactamente SUBIR para publicar de verdad. Sin este "
             "argumento se evalúa la puerta y se informa el veredicto, y no se "
             "pide token ni se envía un byte",
    )

    sub.add_parser(
        "youtube-auth",
        help="comprueba una autorización de YouTube ya configurada; no sube nada",
    )

    alta = sub.add_parser(
        "youtube-auth-bootstrap",
        help="alta OAuth interactiva y local: obtiene el refresh token. Abre el "
             "navegador y NO sube nada",
    )
    alta.add_argument(
        "--credentials", required=True, metavar="RUTA",
        help="ruta al JSON del cliente OAuth de escritorio descargado de Google "
             "Cloud. El archivo NO se copia ni se versiona: solo se leen de él "
             "client_id y client_secret",
    )
    alta.add_argument(
        "--timeout", type=int, default=None, metavar="SEGUNDOS",
        help="cuánto esperar el consentimiento en el navegador (300 por defecto)",
    )
    alta.add_argument(
        "--forzar-consentimiento", action="store_true",
        help="fuerza la pantalla de consentimiento aunque ya se hubiera "
             "concedido. Es el remedio cuando una reautorización devuelve "
             "access token pero ningún refresh token",
    )

    dispositivo = sub.add_parser(
        "youtube-auth-device",
        help="alta OAuth por flujo de dispositivo: enseña un código para "
             "teclear en otra pantalla. No necesita navegador ni callback, y "
             "NO sube nada",
    )
    dispositivo.add_argument(
        "--credentials", required=True, metavar="RUTA",
        help="ruta al JSON del cliente OAuth de tipo «TVs and Limited Input "
             "devices» descargado de Google Cloud. El archivo NO se copia ni se "
             "versiona: solo se leen de él client_id y client_secret",
    )

    return parser


#: Lo que hay que escribir en ``--confirmar`` para que se suba algo de verdad.
#: Es una palabra concreta y no un ``--yes`` porque teclearla es un acto, y una
#: publicación no debería poder salir de haber repetido un comando sin leerlo.
CONFIRMACION = "SUBIR"


def _publicar(args: argparse.Namespace, settings: Settings) -> int:
    """Gate 7.4-B: publica en privado el vídeo editorial de una corrida.

    Dos fases, y la frontera entre ellas es la red. ``preparar`` lee la corrida,
    valida las dos declaraciones y evalúa la puerta sin pedir token ni abrir
    ninguna conexión; solo si la puerta está satisfecha **y** la confirmación es
    exacta se contacta con YouTube.
    """
    from app.adapters.youtube.auth import cargar_credenciales, obtener_access_token
    from app.adapters.youtube.upload import TransporteUrllib
    from app.pipeline.publicacion_editorial import preparar, publicar_preparada

    run_id = parsear_run_id(args.run_id)
    preparacion = preparar(
        run_id=run_id,
        directorio_corrida=Path(settings.raiz_runs) / str(run_id),
        ruta_metadata=Path(args.metadata),
        ruta_aprobacion=Path(args.approval),
    )

    # El documento no lleva credenciales por construcción: ni token, ni cabeceras,
    # ni la URL de la sesión de subida, que además no es representable aquí.
    veredicto: dict = {
        "run_id": str(run_id),
        "gate_ready": preparacion.gate.listo,
        "gate_reasons": preparacion.gate.motivos,
        "privacy_status": preparacion.metadata.privacy_status.value,
        "contains_synthetic_media": preparacion.metadata.contains_synthetic_media,
        "made_for_kids": preparacion.metadata.made_for_kids,
        "ai_disclosure_status": preparacion.metadata.ai_disclosure.status.value,
        "editorial_approval": preparacion.aprobacion.status.value,
        "editorial_approved_by": preparacion.aprobacion.approved_by,
        "materials": list(preparacion.render_job.materials),
        "video_sha256": preparacion.render_result.sha256,
        "published": False,
    }

    if not preparacion.gate.listo:
        print(json.dumps(veredicto, indent=2, ensure_ascii=False))
        print(f"NOT_READY: {preparacion.gate.resumen}", file=sys.stderr)
        print("No se ha contactado con YouTube.", file=sys.stderr)
        return 3

    if args.confirmar != CONFIRMACION:
        print(json.dumps(veredicto, indent=2, ensure_ascii=False))
        print(
            "La puerta está satisfecha y no se ha publicado nada. Para subir el "
            f"vídeo, repite el comando con --confirmar {CONFIRMACION}.",
            file=sys.stderr,
        )
        return 0

    token = obtener_access_token(
        cargar_credenciales(settings), timeout_s=settings.youtube_timeout_s
    )
    salida = publicar_preparada(
        preparacion,
        token=token,
        transporte=TransporteUrllib(),
        timeout_s=settings.youtube_timeout_s,
    )

    veredicto["published"] = salida.publicado
    veredicto["job_state"] = salida.job.state.value
    veredicto["job_reason"] = salida.job.reason
    if salida.result is not None:
        veredicto["video_id"] = salida.result.video_id
        veredicto["url"] = salida.result.url
        veredicto["result_privacy_status"] = (
            salida.result.privacy_status.value
            if salida.result.privacy_status
            else None
        )
    print(json.dumps(veredicto, indent=2, ensure_ascii=False))

    if not salida.publicado:
        print(
            f"NO se alcanzó COMPLETED ({salida.job.state.value}): "
            f"{salida.job.reason or 'sin motivo'}",
            file=sys.stderr,
        )
        if salida.job.state is EstadoPublicacion.revision_pendiente:
            print(
                "Revisa el canal a mano antes de volver a ejecutar nada: puede "
                "haber un vídeo subido sin confirmar. No existe salida automática "
                "de NEEDS_REVIEW, y volver a publicar podría duplicarlo.",
                file=sys.stderr,
            )
        return 5
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _construir_parser()
    args = parser.parse_args(argv)

    settings = Settings.desde_entorno()
    # La comprobación de YouTube imprime un JSON en stdout, así que sus trazas van
    # a stderr: mezclarlas dejaría la salida sin parsear, que es justo lo que CI
    # necesita hacer con ella.
    configurar_logging(
        getattr(logging, settings.log_level, logging.INFO),
        stream=(
            sys.stderr
            if args.comando
            in (
                "publish",
                "youtube-auth",
                "youtube-auth-bootstrap",
                "youtube-auth-device",
            )
            else None
        ),
    )

    try:
        if args.comando == "youtube-auth":
            # Va antes de resolver el pipeline: comprobar la autorización no
            # tiene nada que ver con las etapas de una corrida, y no debe
            # necesitar ninguna.
            from app.adapters.youtube import ResultadoAuth, comprobar_autenticacion

            comprobacion = comprobar_autenticacion(settings)
            # Se imprime el dict, que por construcción no lleva credenciales.
            print(json.dumps(comprobacion.a_dict(), indent=2, ensure_ascii=False))
            return 0 if comprobacion.resultado is ResultadoAuth.autenticado else 1

        if args.comando == "youtube-auth-bootstrap":
            from app.adapters.youtube import (
                ResultadoAuth,
                TIMEOUT_CALLBACK_S,
                ejecutar_bootstrap,
                instrucciones,
            )

            resultado = ejecutar_bootstrap(
                settings,
                args.credentials,
                timeout_callback_s=args.timeout or TIMEOUT_CALLBACK_S,
                forzar_consentimiento=args.forzar_consentimiento,
            )
            # El resumen va como JSON y NO lleva el refresh token: a_dict solo
            # incluye una pista enmascarada.
            print(json.dumps(resultado.a_dict(), indent=2, ensure_ascii=False))
            # El token, una sola vez, por stderr y separado del documento, para
            # que copiarlo no sea copiar también el JSON del resultado.
            print("\n" + instrucciones(resultado), file=sys.stderr)
            print(
                f"REFRESH TOKEN (cópialo ahora, no se guarda en ningún sitio):\n\n"
                f"    {resultado.refresh_token}\n",
                file=sys.stderr,
            )
            # Mismo criterio que ``youtube-auth``, y no ``autenticado``: ese es
            # cierto también para WRONG_CHANNEL —la credencial sirvió— y un
            # script que mirara el código de salida daría por bueno un canal
            # equivocado. ``AUTHENTICATED`` ya implica las dos cosas: el token
            # funcionó y, si había canal esperado, coincidía.
            return 0 if resultado.comprobacion.resultado is ResultadoAuth.autenticado else 1

        if args.comando == "youtube-auth-device":
            from app.adapters.youtube import (
                ResultadoAuth,
                ejecutar_alta_dispositivo,
                instrucciones_dispositivo,
            )

            resultado = ejecutar_alta_dispositivo(settings, args.credentials)
            # Mismo contrato de salida que el alta de escritorio: el documento
            # por stdout sin el token, y el token aparte por stderr.
            print(json.dumps(resultado.a_dict(), indent=2, ensure_ascii=False))
            print("\n" + instrucciones_dispositivo(resultado), file=sys.stderr)
            print(
                f"REFRESH TOKEN (cópialo ahora, no se guarda en ningún sitio):\n\n"
                f"    {resultado.refresh_token}\n",
                file=sys.stderr,
            )
            # Y el mismo criterio de código de salida, por el mismo motivo:
            # ``autenticado`` también es cierto para WRONG_CHANNEL.
            return 0 if resultado.comprobacion.resultado is ResultadoAuth.autenticado else 1

        if args.comando == "publish":
            return _publicar(args, settings)

        etapas = PIPELINES[args.pipeline]()

        if args.comando == "run":
            run_id = parsear_run_id(args.run_id) if args.run_id else nuevo_run_id()
            parametros = {}
            if args.transcript:
                parametros["transcript_path"] = args.transcript
            if args.target_duration is not None:
                parametros["target_duration_seconds"] = args.target_duration
            if args.reference_asset:
                parametros[CLAVE_REFERENCIAS] = args.reference_asset
            if args.sources:
                parametros[CLAVE_DECLARACIONES] = args.sources
            if args.material_asset:
                parametros[CLAVE_MATERIAL_SELECCIONADO] = args.material_asset
            resultado = ejecutar_run(
                run_id, settings, forzar=args.force,
                parametros=parametros, etapas=etapas,
            )
            print(str(run_id))
            return 0 if resultado.exito else 1

        run_id = parsear_run_id(args.run_id)
        ok, problemas = validar_run(run_id, settings, etapas=etapas)
        if ok:
            print(f"{run_id}: artefactos válidos")
            return 0
        print(f"{run_id}: {len(problemas)} problema(s)", file=sys.stderr)
        for problema in problemas:
            print(f"  - {problema}", file=sys.stderr)
        return 1

    except ErrorPipeline as exc:
        print(f"{type(exc).__name__} [{exc.categoria}]: {exc.mensaje}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
