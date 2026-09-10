# shorts-automation

Automatización de producción de YouTube Shorts en español, con
MoneyPrinterTurbo como motor de render.

Proyecto **independiente de SmartStockCR**: no comparte código, dependencias,
configuración ni historial con ese repositorio.

## Estado

| Gate | Contenido | Estado |
|------|-----------|--------|
| Gate 0 | Auditoría de MoneyPrinterTurbo y decisiones D1–D14 | Aprobado |
| **Gate 0.5** | **Proof of Concept — este código** | **En verificación** |
| Gate 1+ | Adaptador, núcleo lingüístico, transformación, MVP | No iniciado |

Este repositorio contiene únicamente el Proof of Concept de Gate 0.5. No hay
Discovery, ni integración con YouTube, ni publicación, ni dashboards.

## Qué valida el PoC

    guion ES → Edge TTS (+WordBoundary) → SRT propio → MoneyPrinterTurbo
             → burn-in con FFmpeg → QA técnico → MP4

Dos hipótesis:

- **A** — Edge TTS entrega tiempos por palabra utilizables para construir
  nuestros propios subtítulos en español, con la puntuación intacta.
- **B** — el pipeline de render puede ejecutarse en un runner de GitHub
  Actions.

## Ejecutar en local

```bash
git clone --recurse-submodules https://github.com/dylanmateo1722/shorts-automation
cd shorts-automation

uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
./scripts/setup_mpt.sh

.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m poc.run_poc --out-dir runs/poc
```

**El PoC no necesita ninguna credencial.** Edge TTS no usa API key y el
material visual se genera con FFmpeg.

Salida en `runs/poc/` y evidencia de la corrida en `evidence/`.

## Arquitectura del PoC

| Módulo | Responsabilidad |
|--------|-----------------|
| `poc/tts_es.py` | Narración con Edge TTS, capturando eventos `WordBoundary` |
| `poc/segmenter.py` | Alineación de esos eventos contra el guion y segmentación en español |
| `poc/srt.py` | SRT canónico, ASS para el burn-in, validación de reglas |
| `poc/mpt_render.py` | Único punto de contacto con el CLI de MoneyPrinterTurbo |
| `poc/burn_in.py` | Quemado de los subtítulos con FFmpeg |
| `poc/qa.py` | QA técnico, separado del juicio editorial y legal |
| `poc/evidence.py` | Registro de tiempos, tamaños, versiones y commit del motor |

## Decisiones que este código materializa

- **D1** — MoneyPrinterTurbo entra como submódulo fijado por SHA en
  `vendor/moneyprinterturbo` y se invoca por CLI. No se importa como paquete
  (su wheel no incluye `cli.py` ni `resource/fonts`) ni se forkea.
- **D2** — la configuración de MPT se genera en tiempo de ejecución y no se
  versiona. Ningún secreto en git.
- **D3** — el TTS es propio y usa la librería `edge-tts` directamente, porque
  necesitamos los `WordBoundary` que MPT no expone.
- **D4** — los subtítulos son nuestros de punta a punta: MPT renderiza con
  `--no-subtitle-enabled` y el burn-in es un paso FFmpeg posterior.
- **D6** — el QA técnico está separado del juicio editorial y legal, que **no
  se automatiza**. Que los elementos de transformación estén registrados no
  implica que la pieza sea jurídicamente transformativa ni monetizable.
- **D8** — la información de contenido sintético se **registra** por corrida;
  la política de divulgación no se decide automáticamente.

## Alcance explícito

Este código no descarga contenido de terceros, no publica en ninguna
plataforma y no implementa ningún mecanismo destinado a eludir Content ID ni
sistemas de detección de copyright.
