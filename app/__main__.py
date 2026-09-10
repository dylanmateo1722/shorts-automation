"""CLI del orquestador.

    python -m app run [--run-id UUID] [--force STAGE]
    python -m app validate <run-id>

No existe un comando ``resume`` separado: reanudar es ejecutar ``run`` con el
mismo ``--run-id``, porque las etapas cuyo artefacto sigue siendo válido se
omiten solas. Un alias no aportaría nada.
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.config.settings import Settings
from app.core.errors import ErrorPipeline
from app.core.logging import configurar_logging
from app.core.run_id import nuevo_run_id, parsear_run_id
from app.pipeline.core import ejecutar_run, validar_run


def _construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app", description="Orquestador de shorts-automation (Gate 1)"
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    ejecutar = sub.add_parser("run", help="ejecuta o reanuda una corrida")
    ejecutar.add_argument(
        "--run-id",
        default=None,
        help="UUID de la corrida; se genera uno si se omite. Reanuda si ya existe",
    )
    ejecutar.add_argument(
        "--force", default=None, metavar="ETAPA",
        help="re-ejecuta esta etapa aunque su artefacto sea válido",
    )

    validar = sub.add_parser("validate", help="revalida los artefactos de una corrida")
    validar.add_argument("run_id", help="UUID de la corrida")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _construir_parser()
    args = parser.parse_args(argv)

    settings = Settings.desde_entorno()
    configurar_logging(getattr(logging, settings.log_level, logging.INFO))

    try:
        if args.comando == "run":
            run_id = parsear_run_id(args.run_id) if args.run_id else nuevo_run_id()
            resultado = ejecutar_run(run_id, settings, forzar=args.force)
            print(str(run_id))
            return 0 if resultado.exito else 1

        run_id = parsear_run_id(args.run_id)
        ok, problemas = validar_run(run_id, settings)
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
