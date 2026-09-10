#!/usr/bin/env bash
# Prepara el motor MoneyPrinterTurbo fijado por SHA en vendor/moneyprinterturbo.
#
# MPT no es instalable como paquete: su wheel declara packages = ["app"], por lo
# que no incluiría cli.py ni resource/fonts, ambos imprescindibles. Por eso se
# consume como checkout con su propio entorno virtual, aislado del nuestro.
#
# No usa `set -x`: este script corre en CI y sus logs son públicos.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MPT="$RAIZ/vendor/moneyprinterturbo"

echo "==> Inicializando submódulo de MoneyPrinterTurbo"
git -C "$RAIZ" submodule update --init --depth 1 vendor/moneyprinterturbo

echo "==> Commit fijado: $(git -C "$MPT" rev-parse HEAD)"

echo "==> Instalando dependencias de MPT en su propio entorno"
cd "$MPT"
uv sync --frozen --python 3.11

echo "==> Motor listo en $MPT"
