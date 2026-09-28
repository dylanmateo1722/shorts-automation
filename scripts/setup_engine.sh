#!/usr/bin/env bash
# Prepara el motor de render propio (engine/) para que el adaptador lo encuentre.
#
# ``MPTAdapter`` busca dos cosas en el directorio del motor:
#
#   <dir>/cli.py            el CLI              — ya está en el repositorio
#   <dir>/.venv/bin/python  el intérprete       — lo crea este script
#
# El motor solo necesita la biblioteca estándar y el FFmpeg que empaqueta
# ``imageio-ffmpeg``, que ya es dependencia del proyecto. Así que en vez de
# construir un entorno aparte, se enlaza el del proyecto.
#
# Se enlaza el directorio ``.venv`` **completo**, no solo ``bin/python``. Un
# enlace solo al intérprete no basta: Python deduce ``sys.prefix`` de la ruta por
# la que se le invoca, así que con ``engine/.venv/bin/python`` buscaría los
# paquetes en ``engine/.venv/lib``, que no existiría, y no vería ninguna
# dependencia del proyecto. Enlazando el directorio, ``pyvenv.cfg`` y
# ``site-packages`` se resuelven a los del proyecto.
#
# ``Settings.python_mpt`` NO resuelve enlaces simbólicos a propósito, así que el
# enlace se respeta y no se termina ejecutando el Python del sistema.
#
# Uso:
#   scripts/setup_engine.sh
#   SHORTS_MPT_DIR="$PWD/engine" python -m app run --pipeline e2e ...

set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MOTOR="$RAIZ/engine"
VENV_PROYECTO="$RAIZ/.venv"

if [[ ! -f "$MOTOR/cli.py" ]]; then
  echo "error: no existe $MOTOR/cli.py" >&2
  exit 1
fi

if [[ ! -x "$VENV_PROYECTO/bin/python" ]]; then
  echo "error: no existe el intérprete del proyecto en $VENV_PROYECTO/bin/python" >&2
  echo "       crea el entorno primero (uv sync)" >&2
  exit 1
fi

# -n para que, si ya es un enlace, se reemplace en vez de escribirse dentro.
rm -rf "$MOTOR/.venv"
ln -sfn "$VENV_PROYECTO" "$MOTOR/.venv"

echo "motor preparado:"
echo "  cli:         $MOTOR/cli.py"
echo "  entorno:     $MOTOR/.venv -> $VENV_PROYECTO"
echo "  intérprete:  $("$MOTOR/.venv/bin/python" -c 'import sys;print(sys.executable)')"
echo "  ffmpeg:      $("$MOTOR/.venv/bin/python" -c 'import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())' 2>/dev/null || echo 'NO VISIBLE')"
echo
echo "para usarlo:  export SHORTS_MPT_DIR=\"$MOTOR\""

# Comprobación real: que el CLI arranca y que rechaza un task-id inválido con 2,
# que es lo que el adaptador distingue de un fallo de render.
if "$MOTOR/.venv/bin/python" "$MOTOR/cli.py" --task-id no-es-uuid >/dev/null 2>&1; then
  echo "advertencia: el CLI no rechazó un task-id inválido" >&2
else
  codigo=$?
  if [[ "$codigo" == "2" ]]; then
    echo "comprobado:   el CLI rechaza argumentos inválidos con código 2"
  else
    echo "advertencia: el CLI salió con $codigo ante un task-id inválido (se esperaba 2)" >&2
  fi
fi
