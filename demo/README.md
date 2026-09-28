# Demo de extremo a extremo: de un texto real en inglés a un Short en español

Esta carpeta contiene todo lo necesario para reproducir una pieza completa:
contenido real en inglés, narración real en español y un MP4 vertical listo para
revisión humana.

## Qué se produce

```
texto real en inglés (CC BY-SA)
        ↓ ingesta
    transcript
        ↓ traducción          EN → ES, conserva todas las cifras
    translation
        ↓ adaptación          gancho, contexto, desarrollo, remate
    adapted_script            89 palabras ≈ 45 s
        ↓ voz                 Edge TTS, es-CR-JuanNeural
    narración real            45,65 s de MP3
        ↓ subtítulos          alineados con los WordBoundary reales
    16 cues
        ↓ procedencia         quién puede entrar al vídeo y quién no
        ↓ transformación      elementos editoriales propios
        ↓ QA técnica
        ↓ render              material + narración → 1080×1920
        ↓ composición         quema subtítulos y rótulo
    final/short.mp4           1080×1920, 45,67 s, h264 + aac
        ↓ QA de vídeo         + fotogramas y hoja de contactos
    revisión humana
```

## Requisitos

* El entorno del proyecto (`uv sync`).
* El motor de render: `scripts/setup_engine.sh`. No hace falta MoneyPrinterTurbo.
* Salida a internet para Edge TTS. **No** hace falta ninguna credencial.

## Reproducirla

```bash
# 1. El motor de render propio (una sola vez)
scripts/setup_engine.sh

# 2. El material visual (no se versiona: se regenera)
uv run python scripts/generar_material.py demo/material/fondo_frio.mp4 \
    --duracion 48 --look frio

# 3. Configuración de la corrida
export LLM_PROVIDER=fake
export LLM_MODEL=autoria-humana-v1
export LLM_FAKE_RESPONSES=demo/llm_responses.json
export TTS_PROVIDER=edge
export SHORTS_MPT_DIR="$PWD/engine"

RID=$(uv run python -c "import uuid;print(uuid.uuid4())")

# 4. Lingüística y voz. Crea el directorio de la corrida.
uv run python -m app run --pipeline voice \
    --transcript demo/transcript.json --target-duration 45 --run-id "$RID"

# 5. El material y la fuente consultada entran en la corrida
mkdir -p "runs/$RID/material" "runs/$RID/source"
cp demo/material/fondo_frio.mp4 "runs/$RID/material/aportado.mp4"
cp demo/fuente_en.txt           "runs/$RID/source/fuente_en.txt"

# 6. Extremo a extremo, con el material declarado y elegido a mano
uv run python -m app run --pipeline e2e \
    --transcript demo/transcript.json --target-duration 45 --run-id "$RID" \
    --sources demo/sources.json \
    --material-asset fondo-editorial-frio \
    --reference-asset source/fuente_en.txt

# 7. El resultado
ls -lh "runs/$RID/final/short.mp4"
open  "runs/$RID/qa/contact_sheet.png"
```

El paso 4 existe porque la declaración de procedencia apunta a rutas **dentro**
de la corrida, y el directorio lo crea el pipeline. Reanudar con el mismo
`--run-id` no repite nada: las etapas cuyo artefacto sigue siendo válido se
omiten solas.

## Qué demuestra la procedencia

La corrida acaba con siete recursos registrados y **no** todos pueden entrar al
vídeo:

| recurso | base | clase | ¿entra al vídeo? |
|---|---|---|---|
| `voice/narration.mp3` | own | render_allowed | sí |
| `subtitles/subtitles.ass` | own | render_allowed | sí |
| `overlay/overlay.ass` | own | render_allowed | sí |
| `material/aportado.mp4` | own | render_allowed | **sí, elegido** |
| `material/base.mp4` | own | render_allowed | no: autorizado pero **no elegido** |
| `source/fuente_en.txt` | unknown | **reference_only** | **no: bloqueado para el render** |

Las dos últimas filas son el punto. El degradado que genera el pipeline está
autorizado y aun así no entra, porque autorizar no es elegir. Y el texto en
inglés del que sale todo se consultó, se registró y **queda bloqueado**: no
puede acabar dentro del vídeo.

## Los ficheros

| fichero | qué es |
|---|---|
| `fuente_en.txt` | el extracto real en inglés, con su URL, su licencia y su atribución |
| `transcript.json` | ese texto como artefacto de entrada del pipeline |
| `llm_responses.json` | la traducción y la adaptación al español |
| `sources.json` | la declaración del material visual, con su base y su evidencia |

## Sobre `llm_responses.json`

El pipeline llama al modelo dos veces: para traducir y para adaptar. En esta
demo esas dos respuestas **están escritas de antemano** y se inyectan con
`LLM_PROVIDER=fake`, porque el entorno donde se produjo no tiene `LLM_API_KEY`.

El contenido es una traducción y una adaptación reales, y pasa las mismas
validaciones que pasaría una respuesta del modelo: conserva todas las cifras del
original, no inventa ninguna, está en español y es subtitulable. Lo que no hubo
es la llamada de red.

Para ejecutarla contra un modelo de verdad, basta cambiar la configuración; no
hay que tocar código:

```bash
export LLM_PROVIDER=openai        # o deepseek, groq, openrouter, moonshot…
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=...
export LLM_API_KEY=...            # solo desde el entorno, nunca en el repo
unset LLM_FAKE_RESPONSES
```

## Publicación

No forma parte de esta demo y **no se ejecuta**. La pieza queda en
`final/short.mp4` para revisión humana, y ahí acaba lo que hace esta carpeta.

El subcomando `publish` **no existe en esta rama**: llega con Gate 7.4-B, que
en el momento de escribir esto está en revisión en el PR #6. Cuando se
incorpore, publicar exigirá metadata editorial declarada y una aprobación
editorial humana atada a esta corrida, a este vídeo y a este material, y sin
`--confirmar SUBIR` evaluará la puerta y se detendrá sin tocar la red.

Mientras tanto, para publicar hay que hacerlo a mano desde YouTube Studio.
