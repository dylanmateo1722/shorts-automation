# shorts-automation

Orquestador para producir YouTube Shorts en español, con
[MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) como motor
de render.

Proyecto **independiente de SmartStockCR**: no comparte código, dependencias,
configuración ni historial con ese repositorio.

## Estado de los Gates

| Gate | Contenido | Estado |
|------|-----------|--------|
| Gate 0 | Auditoría del motor y decisiones D1–D14 | Aprobado |
| Gate 0.5 | Proof of Concept del pipeline de render | Aprobado |
| Gate 1 | Contratos, run/manifest, adaptador y pipeline mínimo | Aprobado |
| Gate 2 | Traducción y adaptación editorial al español | Aprobado |
| Gate 3 | TTS, WordBoundaries y subtítulos (SRT + ASS) | Aprobado |
| **Gate 4** | **Procedencia, transformación editorial y QA técnica** | **En verificación** |
| Gate 5+ | Composición y render final, Discovery, publicación | No iniciado |

Lo que hoy existe es la columna vertebral técnica, la capa lingüística, la de
voz y subtítulos, la de transformación con su QA técnica, y el PoC de Gate 0.5.
**No hay** composición final, ni render del producto, ni Discovery, ni análisis
de competencia, ni integración con YouTube, ni publicación.

## Arquitectura actual

```
app/
├── contracts/    contratos de artefacto, validados con Pydantic
├── core/         run_id, errores, logging, redacción, workspace,
│                 manifest y StageRunner (idempotencia)
├── adapters/     únicos puntos de contacto con lo externo: MoneyPrinterTurbo,
│   ├── llm/      FFmpeg, los proveedores de modelo de lenguaje
│   └── tts/      y los de síntesis de voz
├── subtitles/    alineación, segmentación y serialización a SRT y ASS
├── config/       configuración propia, prompts versionados y config del
│                 motor generada en runtime
└── pipeline/     etapas, validaciones y orquestador
prompts/          prompts versionados, fuera del código de negocio
poc/              Proof of Concept de Gate 0.5, conservado como evidencia
vendor/           MoneyPrinterTurbo como submódulo fijado por commit
```

Cuatro reglas que el código impone, no solo documenta:

- **El motor solo se alcanza por `app/adapters/mpt.py`.** Ningún otro módulo
  conoce su ruta, su intérprete, sus flags ni el formato de su salida.
- **El `run_id` es el `--task-id` del motor.** Debe ser un UUID válido, y se
  valida en el primer segundo en vez de fallar a mitad del render.
- **Ningún módulo conoce un proveedor de modelo concreto.** El pipeline solo
  ve las interfaces `ProveedorLLM` y `ProveedorTTS`; la elección del proveedor
  de lenguaje definitivo sigue pendiente de una evaluación comparativa.
- **La duración del audio se mide sobre el archivo.** Nunca se deriva de la
  estimación del guion ni del último tiempo que reporte el TTS.
- **Un recurso de referencia no puede acabar en el render.** No es una
  convención de nombres: el contrato de procedencia se niega a validar si uno
  aparece entre los utilizables.

## Instalación

Requisitos: Python 3.11, [uv](https://docs.astral.sh/uv/) y git.
FFmpeg no hace falta instalarlo: se resuelve del sistema o del paquete
`imageio-ffmpeg`.

```bash
git clone --recurse-submodules https://github.com/dylanmateo1722/shorts-automation
cd shorts-automation

uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
```

## Inicializar el motor

```bash
./scripts/setup_mpt.sh
```

Inicializa el submódulo en el commit fijado y crea su entorno virtual, aislado
del nuestro. El motor **no** se instala como paquete: su wheel no incluye
`cli.py` ni `resource/fonts`, ambos imprescindibles.

## Configuración

```bash
cp .env.example .env
```

Ningún secreto se versiona. El `config.toml` del motor se genera en tiempo de
ejecución y tampoco se versiona.

**Solo la capa lingüística necesita credencial**, y solo si se usa un proveedor
real: Edge TTS no pide ninguna, el motor de render tampoco y el PoC de Gate 0.5
tampoco. Las demás variables de `.env.example` corresponden a etapas que aún no
existen.

Ajustes opcionales del orquestador, por entorno:

| Variable | Por defecto | Para qué |
|---|---|---|
| `SHORTS_RUNS_DIR` | `runs/` | dónde viven las corridas |
| `SHORTS_MPT_DIR` | `vendor/moneyprinterturbo` | dónde está el motor |
| `SHORTS_MPT_TIMEOUT_S` | `1800` | tiempo máximo de un render |
| `SHORTS_LOG_LEVEL` | `INFO` | nivel de log |
| `TTS_PROVIDER` | `edge` | proveedor de voz: `edge` o `fake` |
| `TTS_VOICE` | `es-CR-JuanNeural` | voz de la narración |
| `TTS_RATE` | *(neutro)* | ritmo, como lo acepta el proveedor: `+10%` |
| `TTS_PITCH` | *(neutro)* | tono: `-5Hz` |
| `TTS_TIMEOUT_S` | `120` | tiempo máximo de una síntesis |
| `SUBTITLE_MARGIN_V` | `420` | margen inferior del subtítulo quemado |
| `SUBTITLE_MARGIN_H` | `60` | margen lateral |
| `SUBTITLE_FONT` | `DejaVu Sans` | tipografía del ASS |
| `SUBTITLE_FONT_SIZE` | `64` | cuerpo de la tipografía |

## Ejecutar los tests

```bash
.venv/bin/python -m pytest
```

La suite no necesita red, ni credenciales, ni el motor real: usa un motor falso
que respeta el contrato del CLI (mismos flags, mismo JSON, mismos códigos de
salida), un proveedor de lenguaje falso y un proveedor de voz falso que genera
audio de verdad con FFmpeg.

La prueba contra el Edge TTS **real** está aparte y se activa a propósito:

```bash
RUN_REAL_TTS=1 .venv/bin/python -m pytest tests/test_tts_real.py -v -s
```

Sin esa variable se omite, con el motivo escrito en el informe. Es la única
suite que sale a internet.

## La capa lingüística

```
Transcript (EN) → Translation (ES) → AdaptedScript (ES)
```

Dos etapas separadas a propósito:

- **Traducción**: preserva el significado. No mejora, no resume, no adapta.
  Es el registro de qué decía el original.
- **Adaptación**: convierte esa traducción en un guion locutable y
  subtitulable. Puede reordenar, condensar y eliminar redundancias; **no**
  puede inventar hechos, cifras, nombres ni contexto.

Ahí termina Gate 2, y ahí empieza la capa de voz.

### Qué se valida

Solo propiedades objetivas del texto:

| Comprobación | Traducción | Adaptación |
|---|---|---|
| Idioma de salida | error si no coincide | error si no coincide |
| Cifras que desaparecen | **error** | aviso: condensar es legítimo |
| Cifras que aparecen sin estar en la fuente | — | **error**: es invención |
| Nombres propios ausentes | aviso | aviso |
| Frase imposible de subtitular (>140 car.) | — | **error** |
| Frases por encima de 64 caracteres | — | aviso |
| `¿` o `¡` sin su cierre | — | **error** |
| Duración por encima del objetivo | — | condensa y reintenta |
| Duración por debajo del objetivo | — | aviso; no se alarga |

No existe ninguna puntuación de similitud ni umbral de diferencia. La
transformación editorial es una cuestión de calidad, no un mecanismo de
evasión, y este proyecto no implementa lo segundo.

### Duración

`estimated_duration_seconds = palabras / DEFAULT_WPM * 60`.

El valor por defecto, 119 palabras por minuto, se midió en Gate 0.5: 60
palabras en 30,29 s con la voz `es-CR-JuanNeural`. Es **una estimación**, con
una sola voz y una sola muestra. La duración real la mide Gate 3 sobre el audio
generado; ver [Duración: estimada y real](#duración-estimada-y-real).

Si el guion sale largo se pide una condensación al modelo, con un máximo de
intentos. **Nunca se recorta el texto por caracteres**: cortar una frase a la
mitad produce un guion inservible, no uno más corto. Si tras los intentos
sigue fuera de rango, el artefacto se declara inválido.

### Prompts versionados

```
prompts/
├── translation/v1.md
└── adaptation/
    ├── v1.md
    └── condense_v1.md
```

Cada artefacto registra `provider`, `model` y `prompt_version`, y el manifest
los repite por etapa. Sin eso una salida de modelo no es reproducible.

### Proveedores

El pipeline solo conoce la interfaz `ProveedorLLM`. Hay dos implementaciones:

- **`fake`** — respuestas preparadas, sin red y sin credenciales. Es lo que
  usan los tests y CI.
- **Compatible con OpenAI** — un único cliente de Chat Completions que sirve
  para OpenAI, Moonshot, DeepSeek, Groq, OpenRouter y cualquier otro
  proveedor con la misma API; solo cambian `LLM_BASE_URL` y `LLM_MODEL`.

## Voz y subtítulos

```
AdaptedScript → VoiceAsset ─┬─→ WordBoundaryAsset ─→ SubtitleAsset ─┬─→ SRT
                            └─→ voice/narration.mp3                 └─→ ASS
```

Dos etapas, no tres. `voice` sintetiza y persiste los tiempos en la misma
etapa porque Edge TTS entrega audio y `WordBoundary` en la misma respuesta:
separarlas obligaría a sintetizar dos veces. Produce dos artefactos, porque son
dos contratos distintos aunque nazcan de una sola llamada.

### El proveedor de TTS

El pipeline solo conoce la interfaz `ProveedorTTS`. Hay dos implementaciones:

- **`edge`** — Edge TTS, el proveedor inicial. Es el único validado (Gate 0.5)
  y **no requiere credencial**. Voz por defecto `es-CR-JuanNeural`, la única
  sobre la que hay medición; no se afirma que sea mejor que otra.
- **`fake`** — determinista y sin red. No simula el audio: lo genera con FFmpeg,
  así que la duración se mide sobre un archivo real. Reproduce a propósito las
  tres conductas de Edge TTS que complican el subtitulado (texto sin
  puntuación, números agrupados con la palabra siguiente y cola de audio tras
  el último evento), porque sin ellas un test en verde no probaría nada.

El **fallback de la decisión D3 no está implementado**. La interfaz lo admite
—sería otra clase y nada más—, pero pedir `TTS_PROVIDER=elevenlabs` falla con
un mensaje que lo dice, en vez de conmutar en silencio a un proveedor que no
existe.

### De los tiempos al subtítulo

Edge TTS emite un evento por palabra con tiempos reales, pero su texto **llega
sin puntuación** y a veces **agrupa varios tokens** del guion. Comprobado
contra la salida real de `es-CR-JuanNeural`:

```
text='Sabías'      (el guion decía "¿Sabías")
text='73 %'        (un evento, dos tokens del guion)
text='3 segundos'  (idem)
```

Por eso el subtítulo **no se construye con el texto de los eventos**. La
estrategia es la inversa:

- **el texto canónico es `AdaptedScript.full_text`**, que sí trae los acentos,
  las comas y —lo que importa en español— los signos de apertura `¿` y `¡`;
- **los eventos solo aportan el tiempo**, anclándose al guion con dos punteros
  que saltan la puntuación que el evento no trae.

Un evento que no case limpiamente se descarta y **se cuenta**; si se descarta
más de la cuarta parte, la etapa falla en vez de entregar un subtitulado
desincronizado. No se adivina en silencio.

### Reglas de segmentación

Máximo 2 líneas y unos 32 caracteres por línea. Los 32 son una **heurística de
legibilidad**, no una ley: pasarse genera un aviso, no invalida el subtítulo.

Al partir una frase larga la prioridad es: puntuación → frontera natural →
caracteres. Nunca se corta dejando colgando una preposición, un artículo o una
conjunción («lo puso a | prueba»), ni se separa un nombre propio («Instituto |
Vega») ni un número de lo que cuantifica («240 | voluntarios»). Las preguntas y
exclamaciones se tratan como unidades: si no caben, se parten, pero los signos
quedan donde el español los pide y ninguno se pierde.

Ningún cue baja de 0,7 s: los que salen más cortos se fusionan con su vecino.

### SRT y ASS

El **SRT es el formato canónico**: se archiva, se audita y se puede subir a
YouTube como pista. El **ASS se deriva de los mismos cues** para el burn-in y
nunca es una segunda fuente de verdad; el reparto en líneas viaja dentro de
`SubtitleCue.text`, así que las dos salidas no pueden discrepar.

El ASS usa **centésimas de segundo** (`0:00:03.61`), no milisegundos. Derivar
su marca de la del SRT producía `0:00:03.610`, que libass no lee como 3,61 s y
desincronizaba el subtitulado entero: se comprobó sobre el video renderizado en
Gate 0.5. Hay un test de regresión específico para esa conversión.

La posición del subtítulo es **configuración explícita**, no una suposición: no
se da por hecho que el borde inferior del video sea zona segura, porque en
Shorts lo tapa la interfaz de YouTube. Los valores por defecto
(`SUBTITLE_MARGIN_V=420`) son **heurísticos** y quedan pendientes de inspección
visual real; el estilo definitivo se decide en Gate 5.

### Duración: estimada y real

Se registran tres valores distintos, y **no se asume que coincidan**:

| Valor | De dónde sale |
|---|---|
| `AdaptedScript.estimated_duration_seconds` | palabras ÷ WPM |
| `VoiceAsset.audio_duration_seconds` | **medido sobre el MP3 con FFmpeg** |
| `SubtitleAsset.duration_seconds` | el audio medido |

La referencia para el final del video es el **audio medido**. Ni la estimación,
ni el final del último `WordBoundary`: Gate 0.5 comprobó que Edge TTS sigue
sonando después del último evento, y una medición de Gate 3 con la misma voz lo
repite —audio 30,29 s, último evento 29,33 s, **cola 0,96 s**—. Usar el último
evento como final recortaría casi un segundo de narración.

El manifest anota `estimated`, `actual`, `delta` y `ratio` por corrida. Esos
datos **solo se registran**: el WPM no se recalibra automáticamente, porque una
muestra no es una medición.

### Idempotencia e invalidación

Un audio válido no se vuelve a sintetizar, y unos subtítulos válidos no se
vuelven a generar. Se rehacen cuando cambia algo que afecta al resultado:

| Cambia | Se detecta con |
|---|---|
| el guion | `source_script_sha256` del artefacto |
| el proveedor o la voz | campos `provider` y `voice` del `VoiceAsset` |
| el ritmo o el tono | huella de configuración en el manifest |
| el audio o el SRT desaparecen | comprobación del archivo |
| los tiempos se regeneran | los subtítulos quedan anteriores a ellos |

Ritmo y tono no caben en el contrato del artefacto —mantenerlo pequeño es
deliberado—, así que su huella vive en la metadata de la etapa.

## Procedencia, transformación y QA técnica

```
SubtitleAsset ─→ overlay ─→ procedencia ─→ transformación ─→ QA técnica
                    │            │               │                │
              OverlaySpec  ProvenanceLedger  TransformationSet  QAResult
```

### REFERENCE_ONLY y RENDER_ALLOWED

Todo recurso de una corrida se declara en una de dos clases, y la diferencia es
la frontera que más importa en este proyecto:

| | `reference_only` | `render_allowed` |
|---|---|---|
| Para qué sirve | referencia temática, investigación, análisis, inspiración editorial | material que puede aparecer en la pieza |
| Puede existir en la corrida | sí | sí |
| Puede informar el guion | sí | sí |
| Puede usarse en el render | **no** | sí |
| Base de procedencia | ninguna declarada | propia, stock, Creative Commons, dominio público o permiso documentado |

**La restricción es estructural, no una convención.** `ProvenanceLedger` lleva
dos listas: los recursos declarados y los que el render puede usar, y el
contrato **se niega a validar** si la segunda contiene uno clasificado
`reference_only` o uno que no esté declarado. Un archivo llamado
`narration_propia_original.mp3` sigue bloqueado si su procedencia dice que es
de referencia; hay un test que lo comprueba exactamente así. No hay promoción
automática de una clase a la otra: hay que cambiar la clase declarada.

Declarar un recurso de referencia en una corrida:

```bash
.venv/bin/python -m app run --pipeline transformation \
  --reference-asset reference/fuente.txt  ...
```

Queda registrado con su clase, aparece como aviso en la QA y **no** entra en
`render_assets`.

Gate 4 **no resuelve la cuestión legal de las licencias**. Registra de dónde
viene cada recurso y a qué evidencia apuntar, y bloquea técnicamente lo que no
está autorizado. Que `basis = stock` esté escrito no significa que alguien haya
verificado esa licencia.

### Qué significa transformación editorial

Cada corrida válida registra cinco elementos propios, cada uno apuntando a un
recurso que existe:

| Elemento | A qué apunta |
|---|---|
| `own_narration` | el MP3 que sintetizó Gate 3 |
| `restructured_script` | el `adapted_script.json` de Gate 2 |
| `added_context` | las notas de transformación de la adaptación |
| `own_subtitles` | el SRT propio de Gate 3 |
| `visual_overlay` | el rótulo ASS que genera Gate 4 |

Nada se duplica: la narración no se vuelve a sintetizar, no hay un segundo
sistema de subtítulos y no se crea otra copia del guion. Gate 4 **referencia**
lo que los Gates anteriores produjeron.

`asset_ref` es obligatorio a propósito. Un elemento sin recurso al que apuntar
sería indistinguible de declarar `transformation = true`, que no es evidencia de
nada.

El `added_context` sale de las notas que la adaptación ya registró. Si no hay
ninguna, se declara la **ausencia justificada** en lugar de generar un dato
factual para rellenar el campo: siguen valiendo las reglas de fidelidad de Gate
2 —no inventar hechos, nombres, cifras ni citas, y no aumentar artificialmente
la certeza—.

El overlay es el único recurso visual que aporta Gate 4: un rótulo con el
gancho del guion propio, en ASS porque el FFmpeg empaquetado no trae el filtro
`drawtext` pero sí libass. Gate 4 **genera el archivo y sus parámetros de
composición**; componerlo sobre vídeo es de Gate 5.

### Qué comprueba la QA técnica

Propiedades medibles y deterministas de lo que hay en disco. Unos 60 checks por
corrida, cada uno con nombre y nivel propios:

- **run** — el directorio existe y lleva el `run_id`.
- **AdaptedScript** — contrato válido, texto no vacío, duraciones objetivo y
  estimada positivas.
- **VoiceAsset** — contrato válido, ruta relativa, el MP3 existe, duración > 0,
  huella del guion coincidente, proveedor y voz registrados, y **la duración
  registrada se vuelve a medir contra el archivo**.
- **WordBoundaryAsset** — índices únicos, tiempos ordenados y no negativos,
  dentro del audio, mismo audio que el `VoiceAsset`, huella coincidente.
- **SubtitleAsset** — SRT y ASS existen, cues válidos, sin solapamientos,
  índices desde 1 y crecientes, máximo 2 líneas, apunta al artefacto de tiempos
  correcto, huella coincidente.
- **duraciones** — los subtítulos usan la duración **real** del audio, y la
  estimación de Gate 2 se compara solo para medir la desviación.
- **overlay**, **procedencia** y **transformación** — archivo presente, cada
  recurso declarado, ningún elemento apoyado en material de referencia, los
  cinco elementos registrados.

**Qué NO comprueba.** Nada sobre valor editorial, originalidad suficiente,
cumplimiento de derechos de autor ni monetizabilidad. No existe puntuación de
similitud, ni porcentaje de transformación, ni umbral alguno destinado a decidir
si un contenido pasa un sistema de detección de copyright, y esos campos no
caben en los contratos.

### QA técnica y juicio editorial son cosas distintas

```
technical_qa_ok: true          ← los artefactos son consistentes
editorial_legal_assessment:
    status: NOT_ASSESSED       ← nadie ha juzgado la pieza
```

Ese es el estado **normal** de una corrida correcta, y los dos campos no se
mezclan nunca. `technical_qa_ok` en verde significa que Gate 5 puede consumir
los artefactos; no significa que la pieza deba publicarse. El único estado que
pone el pipeline es `NOT_ASSESSED`: los otros dos existen para que una persona
registre su decisión, y `automated_similarity_threshold_applied` es
estructuralmente `false` —el contrato rechaza el valor contrario—.

### Errores y avisos

Un **error** invalida la corrida para Gate 5: contrato roto, UUID inválido, ruta
absoluta, archivo ausente, huella que no cuadra, subtítulos solapados, recurso
de referencia en el render, elemento obligatorio ausente, referencia inválida.

Un **aviso** no bloquea: una línea de subtítulo por encima de los 32 caracteres,
una divergencia moderada entre la duración estimada y la real, la cola de audio
tras el último `WordBoundary`, un recurso de referencia registrado. Ascender un
aviso a error sin razón técnica convertiría una heurística en un veredicto.

`status` resume las dos capas: `pass`, `pass_with_warnings` o `fail`. El
contrato comprueba que cuadre con las comprobaciones, así que un informe no
puede declararse aprobado mientras tenga errores.

### Qué queda pendiente para Gate 5

La composición final: quemar los subtítulos y el overlay sobre el material
visual, invocar el motor de render, y el QA del MP4 resultante —resolución,
fps, volumen, duración—, que es lo que ya prueba el PoC de Gate 0.5 pero todavía
no está integrado en el pipeline. La duración de referencia para ese vídeo sigue
siendo `VoiceAsset.audio_duration_seconds`.

## Ejecutar una corrida

```bash
# nueva corrida, con run_id generado
.venv/bin/python -m app run

# reanudar: las etapas cuyo artefacto siga siendo válido se omiten
.venv/bin/python -m app run --run-id <UUID>

# repetir una etapa concreta aunque su artefacto valide
.venv/bin/python -m app run --run-id <UUID> --force render

# revalidar los artefactos de una corrida
.venv/bin/python -m app validate <UUID>
```

Hay cuatro secuencias:

- `--pipeline render` (por defecto) — el pipeline de Gate 1: `prepare_input`
  genera una entrada controlada con FFmpeg y `render` invoca el motor. Sirve
  para demostrar la orquestación; no produce contenido real.
- `--pipeline linguistic` — el de Gate 2: `ingest_transcript`, `translation`,
  `adaptation`.
- `--pipeline voice` — el de Gate 3: encadena las etapas lingüísticas y añade
  `voice` y `subtitles`. La voz no se ejecuta sola, porque necesita un guion
  adaptado, y encadenarlas mantiene un solo `run_id` para todos los artefactos.
- `--pipeline transformation` — el de Gate 4: lo anterior más `overlay`,
  `provenance`, `transformation` y `technical_qa`. Nueve etapas en total.

### Una corrida completa con el fixture incluido

Sin credenciales, con el proveedor de lenguaje falso y el TTS real:

```bash
export LLM_PROVIDER=fake
export LLM_MODEL=fake-1
export LLM_FAKE_RESPONSES=tests/fixtures/respuestas/estudio_atencion.json
export TTS_PROVIDER=edge          # o "fake" para no salir a la red

.venv/bin/python -m app run \
  --pipeline voice \
  --transcript tests/fixtures/transcripts/estudio_atencion.json \
  --target-duration 30
```

Con un proveedor de lenguaje real, basta cambiar `LLM_PROVIDER`, `LLM_MODEL`,
`LLM_BASE_URL` y `LLM_API_KEY`. La credencial se lee **solo** del entorno.
Edge TTS no necesita ninguna.

No hay comando `resume`: reanudar es ejecutar `run` con el mismo `--run-id`.
Una etapa cuyo artefacto siga siendo válido se omite, **sin volver a llamar al
modelo**: es la diferencia entre reanudar gratis y pagar dos veces.

## Estructura de una corrida

```
runs/<run_id>/
├── manifest.json         estado, etapas, versiones, artefactos y errores
│
│   pipeline linguistic
├── transcript.json       transcripción de partida, adoptada por la corrida
├── translation.json      traducción fiel
├── adapted_script.json   guion adaptado: el artefacto principal de Gate 2
│
│   pipeline voice
├── voice_asset.json      narración: proveedor, voz y duración medida
├── word_boundaries.json  tiempos por palabra tal como los dio el proveedor
├── subtitle_asset.json   cues con su texto y sus tiempos, en segundos
├── voice/
│   └── narration.mp3     el audio
├── subtitles/
│   ├── subtitles.srt     formato canónico
│   └── subtitles.ass     presentación para el burn-in
│
│   pipeline transformation
├── overlay_spec.json     rótulo propio y sus parámetros de composición
├── overlay/
│   └── overlay.ass       el rótulo
├── provenance_ledger.json  de dónde viene cada recurso y cuál puede renderizarse
├── transformation_set.json los cinco elementos editoriales propios
├── qa_result.json        informe de QA técnica, con el juicio editorial aparte
│
│   pipeline render
├── render_job.json
├── render_result.json
├── input/                audio y material de la entrada controlada
└── render/
    ├── final.mp4         salida del motor, importada al directorio
    ├── combined.mp4
    └── mpt.log           salida completa del motor, ya redactada
```

Los artefactos aparecen cuando una etapa los produce; no se crean vacíos.
Todas las rutas que guarda un artefacto son **relativas a este directorio**,
para que la corrida siga siendo interpretable en otra máquina o en un runner.

## Proof of Concept de Gate 0.5

Se conserva en `poc/` como evidencia de que el camino
`TTS → SRT propio → motor → burn-in → QA` funciona:

```bash
.venv/bin/python -m poc.run_poc --out-dir runs/poc
```

Requiere red, porque sintetiza voz real con Edge TTS. Su render pasa por el
mismo adaptador que el resto del proyecto.

Gate 3 **trasladó** su segmentador y sus serializadores a `app/subtitles/` en
lugar de reimplementarlos: son código validado contra la salida real de Edge
TTS y contra video renderizado. `poc/segmenter.py` y `poc/srt.py` los
reexportan, así que el PoC sigue ejecutándose exactamente como se aprobó.

## Alcance explícito

Este código no descarga contenido de terceros, no publica en ninguna
plataforma y no implementa ningún mecanismo destinado a eludir Content ID ni
sistemas de detección de copyright. Tampoco hay puntuación de similitud,
porcentaje de transformación ni umbral de diferencia: esos campos no existen en
los contratos y su ausencia está cubierta por tests.

El QA técnico está separado del juicio editorial y legal, que **no se
automatiza**: que los elementos de transformación estén registrados no implica
que una pieza sea jurídicamente transformativa ni monetizable.

Las validaciones lingüísticas comprueban propiedades objetivas del texto. No
puntúan la calidad editorial, que sigue siendo un juicio humano.

## Limitaciones conocidas

- La llamada a un proveedor **de lenguaje** real **no está verificada**: no hay
  credenciales en el entorno de desarrollo. La interfaz, el mapeo de errores y
  el parseo sí están probados, con respuestas y errores reales de `urllib`.
  Edge TTS, en cambio, **sí** está probado de extremo a extremo contra el
  servicio real.
- `DEFAULT_WPM` viene de una sola medición con una sola voz. Gate 3 registra
  estimada, real y delta por corrida, pero **no recalibra**: hacen falta más
  muestras y esa es una decisión posterior.
- **No hay proveedor de TTS alternativo.** El fallback de la decisión D3 está
  previsto por la interfaz y no implementado.
- La detección de idioma y de nombres propios es heurística: sirve para
  detectar el caso evidente, no para afirmar con certeza. Lo mismo vale para la
  detección de unidades inseparables al partir un subtítulo, que se apoya en
  mayúsculas y dígitos, no en reconocimiento de entidades.
- **La posición del subtítulo no está validada visualmente.** Los márgenes por
  defecto son heurísticos y la franja que tapa la interfaz de YouTube sigue sin
  medirse sobre la app real; eso es trabajo de Gate 5.
- La calidad de la segmentación se comprueba con reglas objetivas, no con
  juicio de legibilidad. Un cue puede cumplirlas todas y aun así leerse peor
  que uno cortado a mano.
- El `config` del manifest se escribe cuando se crea la corrida y no se
  reescribe al reanudarla: si se cambia la voz a mitad, la voz efectiva está en
  la metadata de la etapa `voice`, que es el registro autoritativo.
- **La procedencia se registra, no se verifica.** Que un recurso declare
  `basis = stock` o `creative_commons` no significa que nadie haya comprobado
  esa licencia: el campo apunta a una evidencia que una persona debe auditar.
  Lo que sí es técnico y sí se aplica es el bloqueo de `reference_only`.
- Los recursos de referencia **se declaran**, no se descubren. El pipeline no
  busca ni descarga nada: alguien afirma "esto lo consulté" y a partir de ahí
  queda bloqueado para el render.
- La QA técnica recorre los artefactos de una corrida, no el vídeo final. El QA
  del MP4 —resolución, fps, volumen, duración— existe en el PoC de Gate 0.5 y
  todavía no está integrado en el pipeline: es trabajo de Gate 5.
- No hay política de reintentos por etapa: solo el contrato de errores que la
  distingue transitorio, permanente e infraestructura.
