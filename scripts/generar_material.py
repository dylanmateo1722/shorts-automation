#!/usr/bin/env python
"""CLI sobre ``app.adapters.material``: genera material visual vertical.

La lógica vive en el módulo y no aquí, porque ``app short`` la llama
directamente. Un script que reimplementara lo mismo acabaría divergiendo del
que usa el pipeline, y el fondo de la demo dejaría de ser el fondo del producto.

    scripts/generar_material.py salida.mp4 --duracion 46 --look frio
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.adapters.material import LOOKS, generar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("destino", help="ruta del MP4 a escribir")
    parser.add_argument(
        "--duracion", type=float, required=True, metavar="SEGUNDOS",
        help="duración del material. Conviene que supere la de la narración: "
             "el motor recorta lo que sobra, pero no puede inventar lo que falta",
    )
    parser.add_argument(
        "--look", choices=sorted(LOOKS), default="frio",
        help="paleta. Cambia el tono de la pieza, no solo su color",
    )
    args = parser.parse_args(argv)

    salida = generar(Path(args.destino), duracion_s=args.duracion, look=args.look)
    print(
        f"{salida}  ({salida.stat().st_size} bytes, {args.duracion:.2f}s, {args.look})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
