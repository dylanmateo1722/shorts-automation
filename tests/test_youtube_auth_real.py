"""Comprobación contra el OAuth **real** de Google. Aislada y opcional.

Esta suite sí habla con Google, y por eso está separada del resto y detrás de
una marca. Las demás pruebas simulan ``urlopen``: son válidas, pero no
demuestran que una autorización real funcione. Confundir las dos cosas sería
declarar PASS sobre algo que no se ejecutó.

    RUN_REAL_YOUTUBE_AUTH=1 pytest tests/test_youtube_auth_real.py -v -s

Sin esa variable se omite entera, con el motivo escrito en el informe de pytest.
**No se ejecuta en CI**: CI no tiene credenciales y no debe tenerlas para esto.

Qué hace y qué no
-----------------

Hace dos peticiones: canjear el refresh token y leer la identidad del canal. **No
sube nada**, no llama a ``videos.insert`` y no modifica nada en YouTube; el
módulo que usa no tiene escrito ningún otro endpoint.

Las credenciales salen del entorno y **no se imprimen**. Lo único que esta
prueba muestra es el desenlace y el identificador del canal, que es público.

Por qué existe
--------------

Para resolver la única incertidumbre que no se puede resolver offline: si el
alcance ``youtube.upload`` que fija D17 basta para leer la identidad del canal.
La documentación de Google no lo afirma ni lo niega. Si aquí sale
``INSUFFICIENT_SCOPE``, la respuesta es que no basta, y entonces hay una
necesidad técnica demostrada para ampliar el alcance.
"""

from __future__ import annotations

import os

import pytest

from app.adapters.youtube import ResultadoAuth, comprobar_autenticacion
from app.config.settings import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_YOUTUBE_AUTH") != "1",
    reason="prueba contra el OAuth real de Google; se activa con "
    "RUN_REAL_YOUTUBE_AUTH=1 y exige credenciales propias en el entorno",
)


def test_la_autorizacion_real_identifica_el_canal_esperado(capsys):
    """Comprueba la autorización de verdad y deja dicho el desenlace.

    No se afirma que pase: se ejecuta y se reporta. Si el alcance no alcanza, el
    resultado lo dice con ese nombre y eso **es** el hallazgo, no un fallo del
    código.
    """
    settings = Settings.desde_entorno()
    faltan = [
        nombre
        for nombre, valor in (
            ("YOUTUBE_CLIENT_ID", settings.youtube_client_id),
            ("YOUTUBE_CLIENT_SECRET", settings.youtube_client_secret),
            ("YOUTUBE_REFRESH_TOKEN", settings.youtube_refresh_token),
            ("EXPECTED_YOUTUBE_CHANNEL_ID", settings.expected_youtube_channel_id),
        )
        if not valor.strip()
    ]
    if faltan:
        pytest.skip(f"faltan variables en el entorno: {', '.join(faltan)}")

    comprobacion = comprobar_autenticacion(settings)

    with capsys.disabled():
        print(f"\n  desenlace ....... {comprobacion.resultado.value}")
        print(f"  autenticado ..... {comprobacion.autenticado}")
        print(f"  canal correcto .. {comprobacion.canal_correcto}")
        print(f"  canal ........... {comprobacion.channel_id}")
        print(f"  alcance pedido .. {comprobacion.scope_solicitado}")
        print(f"  alcance dado .... {comprobacion.scope_concedido or '(no informado)'}")
        print(f"  detalle ......... {comprobacion.detalle}")
        if comprobacion.resultado is ResultadoAuth.alcance_insuficiente:
            print(
                "\n  HALLAZGO: el alcance mínimo de D17 no cubre la lectura de la\n"
                "  identidad del canal. Hay necesidad técnica demostrada para\n"
                "  ampliarlo; decidirlo no es cosa de este test."
            )

    # Lo único que se afirma: la credencial sirvió y el canal es el esperado. Si
    # no, el mensaje dice exactamente qué pasó en vez de un assert opaco.
    assert comprobacion.canal_correcto, (
        f"la comprobación terminó en {comprobacion.resultado.value}: "
        f"{comprobacion.detalle}"
    )


def test_la_prueba_real_no_filtra_credenciales(capsys):
    """Lo que esta suite imprime tampoco puede llevar la credencial real."""
    settings = Settings.desde_entorno()
    if not settings.youtube_refresh_token.strip():
        pytest.skip("sin YOUTUBE_REFRESH_TOKEN no hay nada que comprobar")

    comprobacion = comprobar_autenticacion(settings)
    import json

    superficies = (
        json.dumps(comprobacion.a_dict()),
        repr(comprobacion),
        comprobacion.detalle,
    )
    for texto in superficies:
        assert settings.youtube_refresh_token not in texto
        assert settings.youtube_client_secret not in texto
