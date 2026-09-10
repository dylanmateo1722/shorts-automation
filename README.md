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
| **Gate 2** | **Traducción y adaptación editorial al español** | **En verificación** |
| Gate 3+ | TTS, subtítulos, transformación, QA, Discovery, publicación | No iniciado |

Lo que hoy existe es la columna vertebral técnica, la capa lingüística y el
PoC de Gate 0.5. **No hay** síntesis de voz productiva, ni generación de
subtítulos, ni Discovery, ni análisis de competencia, ni integración con
YouTube, ni publicación.

## Arquitectura actual

```
app/
├── contracts/    catorce contratos de artefacto, validados con Pydantic
├── core/         run_id, errores, logging, redacción, workspace,
│                 manifest y StageRunner (idempotencia)
├── adapters/     únicos puntos de contacto con lo externo: MoneyPrinterTurbo,
│   └── llm/      FFmpeg y los proveedores de modelo de lenguaje
├── config/       configuración propia, prompts versionados y config del
│                 motor generada en runtime
└── pipeline/     etapas, validaciones lingüísticas y orquestador
prompts/          prompts versionados, fuera del código de negocio
poc/              Proof of Concept de Gate 0.5, conservado como evidencia
vendor/           MoneyPrinterTurbo como submódulo fijado por commit
```

Dos reglas que el código impone, no solo documenta:

- **El motor solo se alcanza por `app/adapters/mpt.py`.** Ningún otro módulo
  conoce su ruta, su intérprete, sus flags ni el formato de su salida.
- **El `run_id` es el `--task-id` del motor.** Debe ser un UUID válido, y se
  valida en el primer segundo en vez de fallar a mitad del render.
- **Ningún módulo conoce un proveedor de modelo concreto.** El pipeline solo
  ve la interfaz `ProveedorLLM`, y la elección del proveedor definitivo sigue
  pendiente de una evaluación comparativa.

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

**Ni el pipeline de Gate 1 ni el PoC de Gate 0.5 necesitan credenciales.** Las
variables de `.env.example` corresponden a etapas que aún no existen.

Ajustes opcionales del orquestador, por entorno:

| Variable | Por defecto | Para qué |
|---|---|---|
| `SHORTS_RUNS_DIR` | `runs/` | dónde viven las corridas |
| `SHORTS_MPT_DIR` | `vendor/moneyprinterturbo` | dónde está el motor |
| `SHORTS_MPT_TIMEOUT_S` | `1800` | tiempo máximo de un render |
| `SHORTS_LOG_LEVEL` | `INFO` | nivel de log |

## Ejecutar los tests

```bash
.venv/bin/python -m pytest
```

La suite no necesita red ni el motor real: usa un motor falso que respeta el
contrato del CLI (mismos flags, mismo JSON, mismos códigos de salida).

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

Ahí termina Gate 2. La síntesis de voz, los tiempos por palabra y los
subtítulos son trabajo de Gates posteriores.

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
una sola voz y una sola muestra. La duración real se medirá cuando exista
audio.

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

Hay dos secuencias:

- `--pipeline render` (por defecto) — el pipeline de Gate 1: `prepare_input`
  genera una entrada controlada con FFmpeg y `render` invoca el motor. Sirve
  para demostrar la orquestación; no produce contenido real.
- `--pipeline linguistic` — el de Gate 2: `ingest_transcript`, `translation`,
  `adaptation`.

### Una corrida lingüística con el fixture incluido

Sin credenciales, con el proveedor falso:

```bash
export LLM_PROVIDER=fake
export LLM_MODEL=fake-1
export LLM_FAKE_RESPONSES=tests/fixtures/respuestas/estudio_atencion.json

.venv/bin/python -m app run \
  --pipeline linguistic \
  --transcript tests/fixtures/transcripts/estudio_atencion.json \
  --target-duration 30
```

Con un proveedor real, basta cambiar `LLM_PROVIDER`, `LLM_MODEL`,
`LLM_BASE_URL` y `LLM_API_KEY`. La credencial se lee **solo** del entorno.

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

## Alcance explícito

Este código no descarga contenido de terceros, no publica en ninguna
plataforma y no implementa ningún mecanismo destinado a eludir Content ID ni
sistemas de detección de copyright.

El QA técnico está separado del juicio editorial y legal, que **no se
automatiza**: que los elementos de transformación estén registrados no implica
que una pieza sea jurídicamente transformativa ni monetizable.

Las validaciones lingüísticas comprueban propiedades objetivas del texto. No
puntúan la calidad editorial, que sigue siendo un juicio humano.

## Limitaciones conocidas

- La llamada a un proveedor real **no está verificada**: no hay credenciales
  en el entorno de desarrollo. La interfaz, el mapeo de errores y el parseo sí
  están probados, con respuestas y errores reales de `urllib`.
- `DEFAULT_WPM` viene de una sola medición con una sola voz.
- La detección de idioma y de nombres propios es heurística: sirve para
  detectar el caso evidente, no para afirmar con certeza.
- No hay política de reintentos por etapa: solo el contrato de errores que la
  distingue transitorio, permanente e infraestructura.
