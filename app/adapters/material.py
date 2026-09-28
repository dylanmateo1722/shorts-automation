"""Genera material visual vertical con FFmpeg, sin banco de vídeo ni credenciales.

Por qué existe
--------------

El material visual es el eslabón que más fácilmente bloquea una entrega: los
bancos de vídeo piden credencial y cuota, y el material de terceros arrastra una
licencia que hay que declarar y defender. Este script produce material **propio**
—generado procedimentalmente, sin fuente externa— para que una pieza pueda
producirse de punta a punta sin depender de nada de eso.

No sustituye al material real. Cuando haya metraje con licencia en regla, entra
por el mismo camino que este: declarándolo en el archivo de ``--sources`` y
eligiéndolo con ``--material-asset``. Lo que esto evita es que *no tener*
metraje impida producir.

Qué genera
----------

Un fondo 1080×1920 en movimiento, pensado para que el subtítulo quemado encima
se lea: oscuro, de contraste bajo y sin detalle que compita con el texto.

    gradiente animado ── líneas de barrido ── grano ── viñeta

Los tres «looks» no son decorativos: cambian la temperatura de color, y con ella
el tono de la pieza.

El CLI que lo envuelve es ``scripts/generar_material.py``; ``app short`` lo
llama directamente, sin pasar por el script.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.adapters.media import binario_ffmpeg
from app.core.errors import ComposicionFallida

ANCHO = 1080
ALTO = 1920
FPS = 30

#: Paletas. Cada una son dos colores entre los que el gradiente se mueve, más el
#: color de la viñeta. Se mantienen oscuras a propósito: el subtítulo va en
#: blanco con borde, y sobre un fondo claro deja de leerse.
#:
#: Los dos colores necesitan separación real de luminancia: dos tonos casi
#: iguales producen una pantalla plana, y dos tonos casi negros producen una
#: pantalla negra. El extremo claro se mantiene por debajo del gris medio para
#: que el subtítulo blanco con borde siga destacando.
#: El extremo oscuro tampoco puede ser casi negro. El gradiente deriva, y cuando
#: su extremo claro sale del encuadre lo que queda en pantalla es el oscuro: si
#: ese es #050505, el Short se ve como una pantalla apagada durante segundos.
#: Medido sobre fotogramas reales del render, no elegido a ojo.
LOOKS = {
    # Azul de monitor de madrugada. Archivo, tecnología, guerra fría.
    "frio": ("#20567f", "#0a1622"),
    # Ámbar de lámpara. Historia, memoria, relato personal.
    "calido": ("#7a4a12", "#231607"),
    # Gris de hormigón. Neutro, cuando el tema no debe teñirse de nada.
    "neutro": ("#4a5058", "#1a1e23"),
}


def construir_filtro(look: str) -> str:
    """La cadena de filtros que produce el fondo.

    El orden importa y no es intercambiable:

    * El **gradiente** se mueve muy despacio. Un fondo quieto se lee como una
      imagen fija y delata que no hay metraje; uno que se mueve rápido compite
      con el texto.
    * Las **líneas de barrido** dan textura de monitor. Van a opacidad baja: a
      opacidad alta emborronan el subtítulo que se quemará encima.
    * El **grano** evita el bandeado que produce un degradado plano al
      comprimirse en H.264, que es el defecto más visible de un fondo generado.
    * La **viñeta** cierra los bordes y empuja la mirada al centro, donde va el
      rótulo.
    """
    inicio, fin = LOOKS[look]
    return (
        # speed muy bajo: un ciclo completo dura más que el Short, así que el
        # fondo deriva sin llegar a repetirse dentro de la pieza.
        f"gradients=s={ANCHO}x{ALTO}:c0={inicio}:c1={fin}:x0=0:y0=0:"
        f"x1={ANCHO}:y1={ALTO}:speed=0.006:type=linear,"
        f"format=yuv420p,"
        # Líneas cada 5 px y muy tenues: a más opacidad emborronan el subtítulo
        # que se quemará encima.
        f"drawgrid=w=0:h=5:t=1:c=black@0.07,"
        # Grano bajo. Sube por encima de 4 y el bitrate se dispara sin que se
        # vea mejor: el ruido es lo más caro de comprimir en H.264.
        f"noise=alls=3:allf=t,"
        # Viñeta suave. Por debajo de PI/5 (el valor por omisión) cierra los
        # bordes sin apagar el centro.
        f"vignette=PI/6,"
        f"fps={FPS}"
    )


def generar(destino: Path, *, duracion_s: float, look: str) -> Path:
    destino.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        binario_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"{construir_filtro(look)}",
        "-t", f"{duracion_s:.3f}",
        # CRF alto a propósito: esto es material intermedio que el motor vuelve
        # a codificar. Gastar bitrate aquí solo engorda un archivo que se tira,
        # y el grano —lo más caro de comprimir— es justamente lo que lo dispara.
        "-c:v", "libx264", "-preset", "medium", "-crf", "26",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(destino),
    ]
    proceso = subprocess.run(argv, capture_output=True, text=True, timeout=900)
    if proceso.returncode != 0:
        raise ComposicionFallida(
            f"FFmpeg falló ({proceso.returncode}): {(proceso.stderr or '').strip()[:600]}",
            stage="material",
        )
    if not destino.is_file() or destino.stat().st_size == 0:
        raise ComposicionFallida(f"FFmpeg terminó bien pero no escribió {destino}", stage="material"
        )
    return destino


