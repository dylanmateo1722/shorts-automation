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
| **Gate 1** | **Contratos, run/manifest, adaptador y pipeline mínimo** | **En verificación** |
| Gate 2+ | Transcripción, traducción, adaptación, TTS, QA, publicación | No iniciado |

Lo que hoy existe es la columna vertebral técnica más el PoC de Gate 0.5. **No
hay** Discovery, ni análisis de competencia, ni traducción, ni adaptación, ni
integración con YouTube, ni publicación.

## Arquitectura actual

```
app/
├── contracts/    catorce contratos de artefacto, validados con Pydantic
├── core/         run_id, errores, logging, redacción, workspace,
│                 manifest y StageRunner (idempotencia)
├── adapters/     único punto de contacto con MoneyPrinterTurbo y con FFmpeg
├── config/       configuración propia y config del motor generada en runtime
└── pipeline/     etapas y orquestador
poc/              Proof of Concept de Gate 0.5, conservado como evidencia
vendor/           MoneyPrinterTurbo como submódulo fijado por commit
```

Dos reglas que el código impone, no solo documenta:

- **El motor solo se alcanza por `app/adapters/mpt.py`.** Ningún otro módulo
  conoce su ruta, su intérprete, sus flags ni el formato de su salida.
- **El `run_id` es el `--task-id` del motor.** Debe ser un UUID válido, y se
  valida en el primer segundo en vez de fallar a mitad del render.

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

El pipeline de Gate 1 tiene dos etapas: `prepare_input`, que genera una
entrada controlada con FFmpeg, y `render`, que invoca el motor. Sirve para
demostrar la orquestación; todavía no produce contenido real.

No hay comando `resume`: reanudar es ejecutar `run` con el mismo `--run-id`.

## Estructura de una corrida

```
runs/<run_id>/
├── manifest.json        estado, etapas, versiones, artefactos y errores
├── render_job.json       artefacto producido por prepare_input
├── render_result.json    artefacto producido por render
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
