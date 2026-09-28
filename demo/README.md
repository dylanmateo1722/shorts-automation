# Demo: de una URL en inglés a un Short en español

## Un solo comando

```bash
uv run python -m app short "https://en.wikipedia.org/wiki/Stanislav_Petrov" \
    --max-caracteres 700 --look frio
```

Eso es todo. El comando ingiere la fuente, traduce, adapta, narra, genera el
material visual, lo declara en la procedencia, compone, renderiza en 9:16 y pasa
la QA. Imprime dónde quedó el Short y no publica nada.

Antes de la primera vez, una sola preparación:

```bash
scripts/setup_engine.sh     # deja el motor de render listo
```

Y la configuración del modelo. Si no tienes credencial todavía, ver
[`CONFIGURACION.md`](../CONFIGURACION.md); para probar con respuestas
preparadas:

```bash
export LLM_PROVIDER=fake
export LLM_MODEL=autoria-humana-v1
export LLM_FAKE_RESPONSES=demo/respuestas/petrov.json
export TTS_PROVIDER=edge
export SHORTS_MPT_DIR="$PWD/engine"
```

## Qué recorre

```
URL real
  ↓ ingesta          texto real + atribución; de Wikipedia por su API
  ↓ traducción       EN → ES, conserva todas las cifras
  ↓ adaptación       gancho, contexto, desarrollo, remate
  ↓ voz              Edge TTS, es-CR-JuanNeural
  ↓ subtítulos       alineados con los WordBoundary reales
  ↓ material         FFmpeg: gradiente animado, barrido, grano, viñeta
  ↓ procedencia      qué puede entrar al vídeo y qué no
  ↓ composición      subtítulos y rótulo quemados
  ↓ QA               + fotogramas y hoja de contactos
final/short.mp4      1080×1920, h264 + aac
```

## Qué queda en la corrida

```
runs/<run_id>/
  source/transcript.json   lo que se ingirió
  source/fuente.txt        URL, licencia y atribución, legible
  source/sources.json      la declaración del material, escrita por el sistema
  material/aportado.mp4    el material visual
  voice/narration.mp3      la narración real
  subtitles/               .srt y .ass completos
  final/short.mp4          el Short
  qa/contact_sheet.png     para la revisión humana
```

## Lo que demuestra la procedencia

La corrida termina con siete recursos registrados, y **no todos pueden entrar
al vídeo**:

| recurso | base | clase | ¿entra? |
|---|---|---|---|
| `material/aportado.mp4` | own | render_allowed | **sí, elegido** |
| `material/base.mp4` (el que genera el pipeline) | own | render_allowed | no: autorizado pero **no elegido** |
| `source/fuente.txt` | unknown | **reference_only** | **no: bloqueado para el render** |
| narración, subtítulos, rótulo | own | render_allowed | sí |

Las dos filas en negrita son el punto: autorizar no es elegir, y el texto del
que sale todo se consulta pero no se renderiza.

## Repetir con otro contenido

El mismo comando, otra URL. No se edita código ni JSON:

```bash
export LLM_FAKE_RESPONSES=demo/respuestas/arkhipov.json
uv run python -m app short "https://en.wikipedia.org/wiki/Vasily_Arkhipov" \
    --max-caracteres 700 --look neutro
```

Con una credencial de LLM configurada, el `export` sobra y el comando funciona
con cualquier URL sin preparar nada.

## Los tres «looks»

`--look frio | calido | neutro`. No son decorativos: cambian la temperatura de
color y con ella el tono. Todos son oscuros y de contraste bajo a propósito,
porque encima se quema el subtítulo.

## Publicación

No forma parte de esta demo y no se ejecuta. El Short queda para revisión
humana. Para publicar hace falta credencial de YouTube y la puerta editorial de
`app publish`; está en [`CONFIGURACION.md`](../CONFIGURACION.md).
