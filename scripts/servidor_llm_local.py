#!/usr/bin/env python
"""Servidor local que habla Chat Completions, para probar el cliente real.

Para qué sirve, y para qué NO
-----------------------------

Sirve para ejercitar el **cliente** real de extremo a extremo: el POST, la
cabecera de autorización, la envoltura de Chat Completions, el reintento, el
parseo. Apuntando ``LLM_BASE_URL`` aquí, ``app short`` recorre exactamente el
mismo código que recorrerá contra OpenAI o DeepSeek.

**No sirve para evaluar la calidad del guion.** Al otro lado no hay un modelo:
hay un archivo de respuestas preparadas que este servidor devuelve según el
prompt que reciba. Lo que demuestra es que la integración funciona, no que el
modelo escriba bien.

Cuando haya credencial, esto sobra: se apunta ``LLM_BASE_URL`` al proveedor de
verdad y no cambia nada más.

Uso
---

    scripts/servidor_llm_local.py --respuestas demo/respuestas/petrov.json &
    export LLM_PROVIDER=openai_compatible
    export LLM_BASE_URL=http://127.0.0.1:8099/v1
    export LLM_MODEL=servidor-local
    export LLM_API_KEY=no-es-una-credencial-real
    app short "https://en.wikipedia.org/wiki/Stanislav_Petrov"
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

#: Cómo se reconoce cada llamada. El pipeline llama primero para traducir y
#: después para adaptar, pero no se cuenta el orden: se mira el prompt, porque
#: contar llamadas se rompe en cuanto haya una condensación por el medio.
MARCA_ADAPTACION = "hook"


def _elegir(respuestas: list[dict], prompt: str) -> dict:
    """La respuesta que toca, según lo que pide el prompt.

    El prompt de adaptación pide un ``hook``; el de traducción no. Es la
    diferencia más estable entre los dos, y no depende del orden de llamada.
    """
    pide_adaptacion = MARCA_ADAPTACION in prompt.lower()
    for respuesta in respuestas:
        tiene_hook = "hook" in respuesta
        if tiene_hook == pide_adaptacion:
            return respuesta
    return respuestas[-1]


def construir(respuestas: list[dict]) -> type[BaseHTTPRequestHandler]:
    class Manejador(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - lo fija BaseHTTPRequestHandler
            largo = int(self.headers.get("Content-Length", 0))
            peticion = json.loads(self.rfile.read(largo).decode("utf-8"))
            prompt = peticion["messages"][0]["content"]

            contenido = json.dumps(_elegir(respuestas, prompt), ensure_ascii=False)
            cuerpo = json.dumps(
                {
                    "id": "chatcmpl-local",
                    "object": "chat.completion",
                    "model": peticion.get("model", "servidor-local"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": contenido},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def log_message(self, formato, *args):
            print(f"[servidor-llm] {formato % args}", file=sys.stderr)

    return Manejador


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--respuestas", required=True, metavar="RUTA",
        help="JSON con la lista de respuestas preparadas",
    )
    parser.add_argument("--puerto", type=int, default=8099)
    args = parser.parse_args(argv)

    respuestas = json.loads(Path(args.respuestas).read_text(encoding="utf-8"))
    if not isinstance(respuestas, list) or not respuestas:
        raise SystemExit("el archivo de respuestas debe ser una lista no vacía")

    servidor = HTTPServer(("127.0.0.1", args.puerto), construir(respuestas))
    print(
        f"[servidor-llm] escuchando en http://127.0.0.1:{args.puerto}/v1 "
        f"con {len(respuestas)} respuesta(s)",
        file=sys.stderr,
    )
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
