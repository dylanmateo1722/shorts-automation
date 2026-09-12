"""CLI del orquestador.

    python -m app run [--pipeline render|linguistic|voice|transformation|e2e]
                      [--run-id UUID] [--force STAGE] [--reference-asset RUTA]
                      [--sources ARCHIVO]
    python -m app validate <run-id> [--pipeline ...]
    python -m app youtube-auth
    python -m app youtube-auth-bootstrap --credentials RUTA

No existe un comando ``resume`` separado: reanudar es ejecutar ``run`` con el
mismo ``--run-id``, porque las etapas cuyo artefacto sigue siendo válido se
omiten solas. Un alias no aportaría nada.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app.config.settings import Settings
from app.core.errors import ErrorPipeline
from app.core.logging import configurar_logging
from app.core.run_id import nuevo_run_id, parsear_run_id
from app.pipeline.core import construir_pipeline, ejecutar_run, validar_run
from app.pipeline.linguistic import construir_pipeline_linguistico
from app.pipeline.qa import construir_pipeline_transformacion
from app.pipeline.transformation import CLAVE_DECLARACIONES, CLAVE_REFERENCIAS
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
        "--force", default=None, metavar="ETAPA",
        help="re-ejecuta esta etapa aunque su artefacto sea válido",
    )

    validar = sub.add_parser("validate", help="revalida los artefactos de una corrida")
    validar.add_argument("run_id", help="UUID de la corrida")
    validar.add_argument(
        "--pipeline", choices=sorted(PIPELINES), default="render",
        help="secuencia con la que se validan los artefactos",
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

    return parser


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
            if args.comando in ("youtube-auth", "youtube-auth-bootstrap")
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
