"""Configuración de MoneyPrinterTurbo generada en tiempo de ejecución.

MPT lee un ``config.toml`` de su propio directorio. Ese archivo **no se
versiona**: se escribe antes de cada ejecución con lo mínimo que nuestro
camino necesita.

Se genera en vez de mantener una copia del ejemplo de MPT porque ese ejemplo
tiene más de seiscientas líneas de opciones que no usamos, y una copia parcial
se desactualiza en silencio.
"""

from __future__ import annotations

from pathlib import Path

# Solo las claves que nuestro camino usa. Ningún secreto: el render con
# material local y audio externo no requiere ninguna credencial.
PLANTILLA = """# Generado por shorts-automation en tiempo de ejecución.
# No editar a mano y no versionar.
log_level = "{log_level}"
listen_host = "127.0.0.1"
listen_port = 8080

[app]
video_source = "local"
subtitle_provider = ""
tls_verify = true

[ui]
"""


def escribir_config(raiz_mpt: Path, *, log_level: str = "INFO") -> Path:
    """Escribe el ``config.toml`` que MPT usará en esta ejecución."""
    destino = Path(raiz_mpt) / "config.toml"
    destino.write_text(PLANTILLA.format(log_level=log_level), encoding="utf-8")
    return destino
