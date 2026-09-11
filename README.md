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
| Gate 4 | Procedencia, transformación editorial y QA técnica | Aprobado |
| Gate 5 | MVP de extremo a extremo: composición y MP4 final | Aprobado |
| Gate 6 | Fuentes, evidencia y decisiones de licencia | Aprobado |
| Gate 7.0 | Publicación: contratos y máquina de estados | Cerrado |
| **Gate 7.1** | **OAuth con YouTube y verificación del canal** | **Implementado; sin aprobar** |
| Gate 8+ | Discovery, análisis de competencia, publicación real | No iniciado |

Con Gate 5 el pipeline produce un **Short vertical real**: 1080×1920, H.264,
AAC, con narración, subtítulos y rótulo propios, validado sobre el archivo.

Gate 6 añade la capa de procedencia: cada recurso se registra como `SourceAsset`
con su evidencia, recibe una `LicenseDecision` explícita bajo una política
versionada, y **nada llega al motor sin una decisión que lo autorice**.
`render_allowed` significa que la política técnica lo permite — **no** que algo
esté certificado legalmente.

Gate 7.0 formaliza los contratos de publicación y su máquina de estados: qué se
quiere publicar, por dónde va una ejecución y qué devolvió YouTube, con las
transiciones válidas declaradas y las inválidas rechazadas.

Gate 7.1 implementa la autenticación OAuth: el alta interactiva local que obtiene
el refresh token, y la comprobación que lee qué canal quedó autorizado y lo
compara con el esperado. **Autenticar no es publicar:** no hay subida, no hay
`videos.insert` y no hay publisher.

**No hay** Discovery, ni scraping, ni análisis de competencia, ni publicación real,
ni planificación.

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

Reglas que el código impone, no solo documenta:

- **El motor solo se alcanza por `app/adapters/mpt.py`.** Ningún otro módulo
  conoce su ruta, su intérprete, sus flags ni el formato de su salida.
- **El `run_id` es el `--task-id` del motor.** Debe ser un UUID válido, y se
  valida en el primer segundo en vez de fallar a mitad del render.
- **Ningún módulo conoce un proveedor de modelo concreto.** El pipeline solo
  ve las interfaces `ProveedorLLM` y `ProveedorTTS`; la elección del proveedor
  de lenguaje definitivo sigue pendiente de una evaluación comparativa.
- **La duración del audio se mide sobre el archivo.** Nunca se deriva de la
  estimación del guion ni del último tiempo que reporte el TTS.
- **Nada entra al render sin una decisión que lo autorice.** No es una
  convención de nombres: el contrato de procedencia se niega a validar si un
  recurso sin declarar, sin decisión o con una decisión que no sea
  `render_allowed` aparece entre los utilizables, y la puerta —que además
  comprueba la huella del archivo— se aplica **antes** de invocar el motor.
- **La decisión de procedencia es técnica, no jurídica.** `render_allowed` dice
  que la política de este sistema lo permite; el juicio editorial y legal vive
  aparte y su estado por defecto es `NOT_ASSESSED`.
- **Las propiedades del MP4 se leen del archivo.** Que el motor las configurara
  no es evidencia de que estén.

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

### RENDER_ALLOWED no es LEGAL_CERTIFIED

Esto es lo primero que hay que entender de toda la capa, y conviene leerlo antes
que nada de lo que sigue.

Que un recurso esté clasificado `render_allowed` significa **exactamente** esto:

> la procedencia registrada y la evidencia disponible satisfacen la política
> técnica de este sistema para permitir su utilización.

Y **no** significa ninguna de estas cosas:

- que exista garantía legal de nada;
- que no haya reclamaciones de copyright;
- que no vaya a activarse Content ID;
- que la pieza sea monetizable;
- que esto constituya asesoramiento jurídico.

La decisión técnica de procedencia y el juicio editorial/legal están separados a
propósito y viven en sitios distintos: la primera en `LicenseDecision`, el
segundo en `QAResult.editorial_legal_assessment`, cuyo estado por defecto es
`NOT_ASSESSED` y solo una persona puede cambiar. El sistema no los mezcla porque
no son lo mismo.

### La cadena: fuente, evidencia, decisión

```
SourceAsset ──→ Evidence ──→ LicenseDecision ──→ render_assets ──→ MPT
 qué hay y       dónde         qué se puede        lo que esta       el motor
 de dónde        mirarlo       hacer con ello      corrida usa
 viene           para
                 comprobarlo
```

Las tres cosas se persisten juntas en `provenance_ledger.json`, que es el único
ledger del sistema: no hay un segundo registro paralelo. Las decisiones
**referencian** las fuentes por `asset_id` en vez de copiarlas.

`SourceAsset` registra identidad estable (`asset_id`), tipo de medio, origen, URL
cuando la hay, ruta local cuando la hay, huella SHA-256, fecha de obtención, su
evidencia y sus datos de atribución. La ruta es opcional a propósito: una fuente
puede conocerse sin tenerla descargada, y ese es justamente el caso de un Short
ajeno del que solo se sabe la dirección.

`Evidence` no es un gestor documental, es una referencia auditable: tipo
(`license_page`, `permission_document`, `ownership_record`,
`public_domain_record`, `cc_license_record`, `other`), referencia, descripción,
fecha y quién la emitió. **Si el respaldo vive detrás de una credencial, aquí va
el puntero, nunca la credencial.**

`LicenseDecision` registra la fuente, la decisión, la base, qué evidencia la
respalda, el motivo, cuándo se decidió, quién decidió y bajo qué versión de
política.

### Las cuatro clases

| Clase | Qué significa | ¿Entra al render? |
|---|---|---|
| `render_allowed` | hay base aceptada y la evidencia que la política exige | **sí** |
| `reference_only` | se consultó para investigar, analizar o documentarse | no |
| `needs_review` | falta información para decidir | no |
| `blocked` | se decidió que no | no |

`reference_only` puede informar el análisis, el tema, el `Transcript`, la
`Translation` y el `AdaptedScript`. Lo que no puede ser es fuente de vídeo, de
imagen o de audio, asset de render, entrada de MPT, entrada de FFmpeg, salida
final ni dependencia directa o indirecta de un `RenderJob`.

**La prohibición es estructural, no una convención de nombres.**
`ProvenanceLedger` **se niega a validar** si `render_assets` contiene la ruta de
un recurso sin fuente declarada, sin decisión, o con una decisión que no sea
`render_allowed`. Un archivo llamado `narration_propia_original.mp3` sigue
bloqueado si su procedencia dice que es de referencia; hay un test que lo
comprueba exactamente así.

No hay promoción automática. Pasar de `reference_only` a `render_allowed` exige
una decisión nueva, con base y evidencia:

```
source ─→ provenance evidence ─→ LicenseDecision ─→ RENDER_ALLOWED ─→ RenderJob
```

Y no existe ninguna regla «URL de YouTube → `render_allowed`». Conocer la
dirección de un vídeo ajeno no autoriza a renderizarlo; hay un test que recorre
las siete bases y comprueba que esa fuente no se autoriza con ninguna.

`reference_only` se alcanza **declarándolo**, no evaluándolo: decir «esto lo
consulté» es una afirmación sobre el uso, no sobre la licencia, y por eso no
necesita evidencia de licencia. Las otras tres clases salen de evaluar una base
contra su evidencia. La distinción importa: marcar una referencia como
`needs_review` insinuaría que alguien debería revisarla para desbloquearla.

### Bases de procedencia y qué evidencia exige cada una

El conjunto es cerrado: `own`, `licensed`, `cc_by`, `public_domain`,
`permission`, `unknown`, `fair_use_claim`. Las dos últimas **nunca** habilitan el
render por sí solas, y el contrato rechaza una decisión que lo intente.

| Base | Evidencia que la respalda | Reglas añadidas |
|---|---|---|
| `own` | `ownership_record` | el artefacto que generó el recurso sirve como registro |
| `licensed` | `license_page` o `cc_license_record` | si no consta que la licencia cubra el uso → `needs_review` |
| `cc_by` | `cc_license_record` o `license_page` | exige la licencia concreta; «Creative Commons» a secas → `needs_review` |
| `public_domain` | `public_domain_record` | la afirmación del usuario no es el registro |
| `permission` | `permission_document` | `user_claimed_permission = true` no basta |
| `unknown` | — | siempre `needs_review` |
| `fair_use_claim` | — | siempre `needs_review`; **no hay evaluador de uso legítimo** |

`other` no respalda ninguna base: una evidencia sin clasificar puede quedar
registrada, pero si no se sabe qué es, no se sabe qué demuestra.

Cuando la licencia registrada excluye el uso previsto
(`commercial_use_allowed = false`), la clase es `blocked`. La política **nunca**
deduce `blocked` de la ausencia de datos: lo que la ausencia produce es
`needs_review`, que es lo que de verdad ocurre —falta información—.

### Atribución

El ledger registra `attribution_required`, `attribution_text` y
`attribution_source`. **El texto no se genera automáticamente**: inventar un
crédito legal sería peor que no tenerlo.

Una atribución exigida sin texto es un estado representable a propósito —es el de
quien sabe que hace falta crédito y aún no lo ha escrito—, la política lo
clasifica `needs_review`, y el ledger se niega a validar su `render_allowed`
aunque alguien edite el JSON a mano.

### Versión de política

Cada decisión registra `policy_version`, y la constante vive en **un solo sitio**,
`app/config/provenance.py`:

```python
VERSION_POLITICA = "provenance_policy_v1"
```

Repetirla haría que dos copias divergieran y que un artefacto histórico mintiera
sobre bajo qué reglas se decidió. Si el ledger de una corrida trae decisiones de
otra versión, la etapa lo invalida y vuelve a decidir.

### Integridad por hash

Cada recurso con archivo local lleva su SHA-256, y eso ancla la decisión a un
contenido concreto. El ledger permite detectar archivo cambiado, huella que no
corresponde, ruta cambiada y asset sustituido; dos fuentes que reclamen el mismo
archivo se rechazan, porque su clase sería ambigua.

**Decisión tomada, y es deliberada: una huella que no coincide _rechaza_ el
render; no degrada el recurso a `needs_review`.** La decisión se tomó sobre un
contenido concreto; si el archivo cambió, la decisión ya no habla de lo que hay
en disco, y dejarla como «pendiente de revisión» la haría parecer aplicable
cuando no lo es.

### Declarar recursos y fuentes

Un recurso consultado solo como referencia:

```bash
.venv/bin/python -m app run --pipeline transformation \
  --reference-asset reference/fuente.txt  ...
```

Una fuente externa con su base y su evidencia, en un JSON:

```bash
.venv/bin/python -m app run --pipeline e2e --sources fuentes.json  ...
```

```json
{
  "sources": [
    {
      "basis": "cc_by",
      "asset": {
        "asset_id": "externa:clip-cc",
        "media_kind": "video",
        "origin": "archivo del autor",
        "source_url": "https://example.org/clip",
        "local_path": "external/clip.mp4",
        "license_id": "CC-BY-4.0",
        "commercial_use_allowed": true,
        "attribution_required": true,
        "attribution_text": "Autora Ejemplo (CC BY 4.0)",
        "evidence": [{
          "evidence_id": "ev-cc-1",
          "kind": "cc_license_record",
          "reference": "https://creativecommons.org/licenses/by/4.0/",
          "description": "licencia declarada para el clip"
        }]
      }
    }
  ]
}
```

Declarar una fuente **no la autoriza**: la política decide su clase. Y una fuente
autorizada tampoco entra sola al render —`render_assets` dice qué consume esta
corrida, no qué sería elegible—. No se descarga nada: si la declaración trae
`local_path`, el archivo tiene que existir ya en la corrida.

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

## El render de extremo a extremo

```
material ─→ render_job ─→ MoneyPrinterTurbo ─→ composición FFmpeg ─→ QA del MP4
              │  puerta de           │                  │                │
              │  procedencia    render/final.mp4   final/short.mp4   fotogramas
```

Catorce etapas desde un transcript hasta un MP4 vertical validado, con un solo
`run_id` y un solo directorio.

### Quién hace qué

**MoneyPrinterTurbo** monta el vídeo base: encaja el material visual con la
narración y produce un MP4 vertical sin subtítulos. Sigue siendo un motor
externo fijado por commit, invocado por CLI a través de `app/adapters/mpt.py`,
que es el **único** punto de contacto del proyecto con él.

**FFmpeg** hace la composición final: superpone las capas ASS —subtítulos y
rótulo— en una sola pasada y produce el MP4 que se publica. El motor no acepta
un SRT externo, así que los subtítulos son nuestros y se incrustan después.

Son **etapas separadas** a propósito. Si FFmpeg falla tras un render correcto,
repetir el motor costaría minutos sin arreglar nada: la idempotencia reutiliza
el intermedio y solo rehace la composición. Ocurrió durante el desarrollo de
este Gate y es exactamente lo que se quería.

Todo el texto sobre vídeo pasa por **libass**, nunca por `drawtext`: el FFmpeg
que empaqueta `imageio-ffmpeg` no incluye ese filtro.

### Entradas permitidas

El material visual lo **genera el pipeline**: un degradado vertical del largo de
la narración, determinista y local. **No se descarga contenido de terceros**, y
un Short ajeno nunca es la fuente del render.

Que sea sobrio es intencionado. Gate 5 demuestra que la cadena produce un vídeo
técnicamente válido; el material visual de producción es una decisión editorial
posterior.

### La puerta de procedencia

Antes de invocar el motor, `render_job` resuelve **cada** recurso que entraría al
vídeo —audio, material, subtítulos, rótulo— contra el ledger:

1. si ninguna fuente lo declara → se rechaza;
2. si su fuente no tiene `LicenseDecision` → se rechaza;
3. si la decisión no es `render_allowed` → se rechaza;
4. si el archivo no existe → se rechaza;
5. si su huella no corresponde al archivo → se rechaza;
6. y solo entonces se construye el job y el motor puede ejecutarse.

También se exigen los cinco elementos de transformación: si falta uno, no se
renderiza. Reutilizar un job anterior tampoco se salta la puerta — si un recurso
pierde la autorización, el job deja de ser válido aunque el archivo siga ahí.

Una decisión que se contradice a sí misma no llega ni a la puerta: el contrato no
la valida, así que el ledger **no se puede leer** y la etapa falla antes. Es
defensa en profundidad, y se comprueba con los dos mecanismos por separado.

Hay tests y un paso de CI que lo demuestran donde importa: tras el bloqueo, el
directorio `render/` **ni siquiera existe**. El material visual lo genera FFmpeg
en local, así que la ausencia de `render/mpt.log` prueba que el motor no llegó a
ejecutarse. Se verifican los seis casos: `reference_only`, `needs_review`,
`blocked`, sin declarar, sin decisión y huella distinta.

### RenderJob y RenderResult

`RenderJob` referencia lo que entra al vídeo en vez de copiarlo, y expone
`assets_de_render` para que la puerta tenga un único sitio al que preguntar. La
excepción es `script`, que se guarda entero porque el CLI del motor lo exige
como argumento literal.

`RenderResult` describe **un MP4 inspeccionado**, y `renderer` dice cuál:

| artefacto | `renderer` | qué es |
|---|---|---|
| `render_result.json` | `moneyprinterturbo` | el vídeo del motor, intermedio |
| `final_video.json` | `ffmpeg-libass-burn-in` | el vídeo que se publica |

El final registra `sha256`, resolución, cadencia, códecs, formato de píxel,
frecuencia de muestreo, tamaño y con qué herramienta se leyó todo. Ninguno de
esos valores se hereda del motor: se miden sobre el archivo.

### QA del vídeo final

`render completed = true` no es evidencia de nada. La QA abre el MP4 y
comprueba: existe, la ruta es relativa, el tamaño es mayor que cero, el SHA-256
coincide con el registrado, 1080×1920, H.264, AAC, `yuv420p`, cadencia y
frecuencia de muestreo válidas, duración positiva, el vídeo dura lo que la
narración, los subtítulos caben dentro, las dos capas se compusieron y el final
no es el intermedio del motor.

### Validación visual y su límite

**Ninguna de esas comprobaciones demuestra que el subtítulo se vea.** Un ASS
puede componerse sin un solo error y quedar fuera de cuadro.

Por eso la QA extrae cinco fotogramas —uno dentro de la ventana del rótulo— y
los junta en `qa/contact_sheet.png`. La automatización afirma que las capas se
compusieron; que se **vean** lo decide una persona mirando, y eso queda escrito
como un aviso permanente en el informe en lugar de disfrazarse de comprobación.

Sobre la inspección visual de este Gate hay un hallazgo real: durante los
primeros segundos el rótulo y el primer subtítulo **muestran el mismo texto**,
porque ambos salen del gancho del guion. Es redundante en pantalla y ffprobe
jamás lo habría detectado. Queda registrado como asunto editorial pendiente; no
se ha cambiado la semántica del overlay aprobada en Gate 4.

### Sincronía

Se registran tres duraciones y **no se fuerza** que coincidan: nunca se recorta
contenido ni se estira el audio para cuadrarlas.

| valor | de dónde sale |
|---|---|
| narración | medida sobre el MP3 (Gate 3) |
| subtítulos | la duración real del audio |
| vídeo final | medida sobre el MP4 |

La referencia sigue siendo `VoiceAsset.audio_duration_seconds`. Una diferencia
de unas décimas es un **aviso** —el muxing y el cierre del último GOP la
producen—; media pantalla de diferencia es un **error**.

### Sin ffprobe

El FFmpeg que empaqueta `imageio-ffmpeg` **no trae ffprobe**, así que no se
puede exigir. La inspección lo prefiere cuando existe en el sistema y cae a
interpretar `ffmpeg -i` cuando no. `RenderResult.inspected_with` y el informe de
QA dejan escrito cuál se usó en cada corrida en lugar de darlo por supuesto.

### Qué queda pendiente después de Gate 6

**Discovery.** No hay nada: ni búsqueda de Shorts, ni `search.list`, ni ranking
viral, ni scraping, ni crawling, ni análisis de canales o de competencia, ni
descargador de contenido ajeno. Las fuentes externas se declaran a mano en un
JSON, que es lo que permite ejercitar la política sin inventarse un sistema de
descubrimiento. Cuando Discovery exista, producirá `SourceAsset` que entrarán por
esta misma cadena: la clase la seguirá decidiendo la política, nunca el hecho de
haber encontrado algo.

**YouTube.** Gate 7 formaliza los contratos y la máquina de estados (ver abajo),
pero no hay API, ni OAuth, ni tokens, ni subida. Tampoco hay —ni se planea aquí—
clasificador de uso legítimo, clasificador de copyright, clasificador de
monetización, detección de similitud ni nada que pretenda anticipar Content ID.

**Qué sigue necesitando una persona.** La política decide si el pipeline *puede*
utilizar técnicamente un recurso. No decide si *debe*: eso es el juicio editorial
y legal, que sigue en `NOT_ASSESSED` y que ningún Gate va a automatizar. Todo lo
que quede en `needs_review` espera a alguien, por diseño.

En lo visual queda el estilo de producción: material propio de verdad en lugar
del degradado, y resolver la redundancia entre rótulo y primer subtítulo.

## Publicación: contratos y máquina de estados

Esta parte está **formalizada, no implementada**. Existen los contratos y la
máquina de estados; no existe el cliente de YouTube. Los límites exactos están al
final de la sección.

### Tres contratos que no se solapan

```
PublishMetadata ──→ PublishJob ──→ PublishResult
qué se quiere        por dónde va     qué devolvió
publicar             la ejecución     YouTube
(intención)          (operacional)    (hecho observado)
```

La separación no es ornamental. Si la metadata llevara el `video_id`, un reintento
sobreescribiría la intención; si el trabajo llevara el título, un reintento podría
contradecirla. Los contratos **se niegan a validar** si los datos de uno aparecen
en otro, y hay tests que lo comprueban campo por campo.

| | `PublishMetadata` | `PublishJob` | `PublishResult` |
|---|---|---|---|
| Qué es | intención editorial | estado operacional | hecho observado |
| Cuándo se escribe | antes de que exista el vídeo | durante la ejecución | cuando YouTube contesta |
| Lleva `video_id` | **no** | no | sí, cuando existe |
| Lleva título | sí | **no** | **no** |
| Lleva `state`/`status` | no | `state` | `status` |

`privacy_status` aparece en la metadata **y** en el resultado a propósito: son lo
solicitado y lo observado, y compararlos es en qué consiste verificar.

### Divulgación de IA

`ai_disclosure` es un objeto dentro de `PublishMetadata`, con tres estados:

| Estado | Significa | ¿Deja publicar automáticamente? |
|---|---|---|
| `not_required` | no hace falta declarar nada | sí |
| `required` | hay que declararlo, y se declara | sí |
| `requires_current_verification` | hay que volver a comprobar la situación | **no** |

**El sistema no deduce ninguno de los tres.** Que la narración venga de un TTS no
determina por sí solo que haga falta divulgación: eso depende de la pieza y de la
norma aplicable, y es una valoración humana. El contrato exige `reason` y
`decided_by` porque una divulgación sin constancia de por qué y de quién la decidió
sería indistinguible de un valor por defecto — que es justo lo que aquí no debe
existir.

`requires_current_verification` bloquea estructuralmente: entrar en `UPLOADING`
exige presentar la metadata, y la transición se rechaza si la divulgación está en
ese estado. Sin metadata tampoco se pasa, porque entonces no habría forma de
comprobarlo y concederlo por omisión sería el error.

### Los ocho estados

| Estado | Qué significa |
|---|---|
| `NOT_READY` | el artefacto no cumple las condiciones para publicar |
| `READY` | QA, procedencia, metadata y demás precondiciones están satisfechas |
| `UPLOADING` | subida en curso |
| `UPLOADED` | YouTube devolvió un `video_id` válido |
| `VERIFYING` | se está comprobando que el recurso remoto coincide con lo solicitado |
| `COMPLETED` | verificado: el estado remoto coincide con el solicitado |
| `FAILED` | fallo determinista que **no** debe reintentarse automáticamente |
| `NEEDS_REVIEW` | ambiguo, inconsistente o pendiente de intervención humana |

### `COMPLETED` no significa público

Es la confusión más fácil de cometer y la que saldría más caro. `COMPLETED`
significa que lo subido se verificó y el estado remoto coincide con el solicitado.
Un vídeo `private` verificado está `COMPLETED`. El estado de la publicación y la
visibilidad del vídeo son dos ejes distintos.

### Transiciones

```
                    ┌──────────────────────────────────────────┐
                    ▼                                          │
NOT_READY ──→ READY ──→ UPLOADING ──→ UPLOADED ──→ VERIFYING ──→ COMPLETED
    │           │           │             │            │
    │           │           │             │            ├──→ FAILED
    │           │           ├──→ FAILED   │            │
    ▼           ▼           ▼             ▼            ▼
         NEEDS_REVIEW ◄──────────────────────────────────
              │
              ├──→ VERIFYING   (reconciliar: ir a mirar el remoto)
              └──→ FAILED      (concluir que falló)
```

| Desde | Hacia |
|---|---|
| `NOT_READY` | `READY`, `NEEDS_REVIEW` |
| `READY` | `UPLOADING`, `NOT_READY`, `NEEDS_REVIEW` |
| `UPLOADING` | `UPLOADED`, `FAILED`, `NEEDS_REVIEW` |
| `UPLOADED` | `VERIFYING`, `NEEDS_REVIEW` |
| `VERIFYING` | `COMPLETED`, `FAILED`, `NEEDS_REVIEW` |
| `NEEDS_REVIEW` | `VERIFYING`, `FAILED` |
| `COMPLETED` | — (terminal) |
| `FAILED` | — (terminal) |

Lo que no está en la tabla se rechaza, incluido quedarse en el mismo estado. Hay
un test parametrizado sobre **las 49 transiciones inválidas** de las 64 posibles.

Cuatro ausencias que son decisiones, no olvidos:

- **`UPLOADING` no vuelve a `READY`.** Ver el apartado siguiente.
- **`NEEDS_REVIEW` no vuelve a `READY`.** Sería autorizar otra subida sin haber
  averiguado si la anterior llegó.
- **`READY` no va directo a `FAILED`.** Un fallo determinista existe cuando ya
  hubo trato con YouTube; antes de eso, lo que hay es una precondición sin cumplir
  (`NOT_READY`) o una ambigüedad (`NEEDS_REVIEW`).
- **`FAILED` y `COMPLETED` no salen a ningún sitio.** Retomar un `FAILED` no es
  transitar: es una ejecución nueva, con su propio `attempt` y su propia clave.

### Idempotencia, y el caso de la respuesta perdida

**YouTube no ofrece ninguna clave de idempotencia genérica que podamos asumir.**
`idempotency_key` es nuestra: se deriva del `run_id`, sirve para reconocer que dos
intentos hablan de la misma publicación, y **no hace nada del lado de YouTube**.
Mandarla no evita un duplicado porque no hay a quién mandarla.

La idempotencia real se apoya en tres cosas: el `run_id`, el estado persistido y la
reconciliación posterior.

De ahí el caso que define el diseño:

> La subida empezó. Puede haber terminado. La respuesta se perdió.

Volver a subir a ciegas publicaría el vídeo dos veces, y eso no se deshace. Por eso
desde `UPLOADING` la máquina **no permite volver a `READY`**: el único camino es
`NEEDS_REVIEW` y, desde ahí, `VERIFYING` para ir a comprobar qué hay en el remoto.
`attempt` no se incrementa por pasar a `NEEDS_REVIEW`, porque no se ha hecho un
intento nuevo.

`attempt` cuenta subidas empezadas, no transiciones: vale 0 mientras no se haya
entrado en `UPLOADING`, y el contrato lo exige — un trabajo `UPLOADED` con 0
intentos describiría algo imposible.

### Invariantes del resultado

- **`COMPLETED` exige `video_id`** y `completed_at`: no se puede dar por completada
  una publicación sin constancia de qué se publicó.
- **`FAILED` no exige `video_id`**: un fallo puede ocurrir antes de que YouTube
  devuelva nada. Tampoco lo prohíbe, porque puede fallar después.
- **`NEEDS_REVIEW` no afirma que el upload fallara.** Es el estado de lo que hay
  que ir a comprobar, con o sin `video_id`.
- Un `video_id` va siempre con su `uploaded_at` y su `url`; uno sin los otros deja
  el resultado a medio describir.
- Un `completed_at` con un estado que no sea `COMPLETED` es una contradicción.
- `NOT_READY`, `READY` y `UPLOADING` **no pueden ser el estado de un resultado**:
  describen por dónde va el trabajo, no qué contestó YouTube.

El contrato no valida la forma de `url`: construirla o comprobarla sería afirmar un
esquema de URLs de YouTube que este Gate todavía no utiliza.

### Límites explícitos de G7.0

Formalizado: los tres contratos, los enums, la máquina de estados, las
transiciones, los invariantes y la derivación de la clave de idempotencia.

**No implementado, y no simulado:** subida, subida reanudable, publicación real,
programación y Discovery. Ningún contrato de publicación tiene un campo donde
pudiera guardarse un token — hay un test que recorre los cuatro modelos para
comprobarlo.

El primer flujo real usará `privacy_status = private`. El contrato admite
`unlisted` y `public` porque la intención es representable, no porque ya exista
política para ellos.

## Autenticación OAuth con YouTube (G7.1)

Implementado: cargar credenciales, canjear el refresh token por un access token,
leer qué canal quedó autorizado y compararlo con el esperado. **Nada más.** No
hay subida, no hay `videos.insert` y no hay publisher.

### Arquitectura

```
app/adapters/youtube/
├── auth.py              comprueba una autorización que YA existe
└── oauth_bootstrap.py   la OBTIENE, una vez, a mano, con navegador
```

Dos momentos distintos de la vida del proyecto: `auth` corre en cada
comprobación y en CI; `oauth_bootstrap` corre una vez cada muchos meses y **nunca**
en CI. El alta reutiliza el AUTH CHECK de `auth` en vez de duplicar la
clasificación de desenlaces.

Cuando exista el publisher será otro módulo **hermano**, no una ampliación de
ninguno de estos: la autenticación tiene que poder comprobarse
sin que exista la capacidad de publicar, y hay un test que comprueba que `auth`
no importa nada que huela a publisher ni a los contratos de G7.0.

No se añadió ninguna dependencia. Se usa `urllib` de la biblioteca estándar,
igual que `app/adapters/llm/openai_compatible.py`, por dos razones: no introducir
una segunda manera de hacer HTTP en el proyecto, y porque con
`google-api-python-client` el objeto de servicio deja `videos.insert` a una línea
de distancia. Sin esa librería, lo prohibido está **ausente**, no solo sin
escribir. Hay un test que enumera las URLs que el módulo contiene y comprueba que
son exactamente cuatro, ninguna de subida.

### Alcance

```
https://www.googleapis.com/auth/youtube.upload
```

Uno solo, el mínimo que fija D17, configurable con `YOUTUBE_SCOPE`.

**Hay una incertidumbre documentada aquí.** `videos.insert` publica la lista de
alcances que acepta; `channels.list` **no publica ninguna** —su apartado de
autorización solo cubre el caso especial de `auditDetails`—. Así que la
documentación oficial no afirma que `youtube.upload` baste para leer la identidad
del canal con `mine=true`, ni afirma lo contrario. No se puede comprobar sin una
autorización real. Ver «Qué queda por resolver».

### Flujo de autorización

El consentimiento inicial es **manual y local**, una sola vez:

```
inicialización local  →  navegador  →  consentimiento de Google
                                              ↓
                                      authorization code
                                              ↓
                                        refresh token
                                              ↓
                            almacenamiento seguro fuera del repositorio
```

Endpoints, según la documentación de Google para aplicaciones instaladas:

| Paso | Endpoint |
|---|---|
| Consentimiento | `https://accounts.google.com/o/oauth2/v2/auth` |
| Canje del token | `https://oauth2.googleapis.com/token` |
| Identidad del canal | `https://www.googleapis.com/youtube/v3/channels?part=id&mine=true` |

Para aplicaciones de escritorio, el redirect recomendado es la IP de loopback
(`http://127.0.0.1:puerto`); el esquema de URI propio está desaconsejado.

**Esta fase no automatiza el consentimiento inicial**: implementa usar una
autorización ya obtenida, que es lo que hace falta para que CI funcione. El canje
usa `grant_type=refresh_token` con `client_id`, `client_secret` y `refresh_token`.
Se pide `part=id` y nada más: comparar identidades no necesita el título del canal,
y pedir menos datos es la misma disciplina que pedir el alcance mínimo.

### Variables de entorno

| Variable | Qué es | ¿Secreto? |
|---|---|---|
| `YOUTUBE_CLIENT_ID` | identificador de la aplicación OAuth | no |
| `YOUTUBE_CLIENT_SECRET` | secreto de la aplicación | **sí** |
| `YOUTUBE_REFRESH_TOKEN` | la autorización persistente del canal | **sí** |
| `EXPECTED_YOUTUBE_CHANNEL_ID` | canal en el que se permite publicar | no |
| `YOUTUBE_SCOPE` | alcance; vacío usa el mínimo | no |
| `YOUTUBE_TIMEOUT_S` | tiempo máximo por petición (30) | no |

Las tres credenciales son **propiedades** de `Settings`, no campos del dataclass,
por el mismo motivo que `LLM_API_KEY`: un campo entra en el `repr` y de ahí en
cualquier traza. Y sus nombres contienen `SECRET` y `TOKEN`, que es exactamente lo
que `app/core/redaction.py` reconoce, así que si una llegara a un log se
enmascara. Las dos cosas, no una sola.

### Verificación del canal

```
canal autenticado == canal esperado  →  AUTHENTICATED
canal autenticado != canal esperado  →  WRONG_CHANNEL   (no se continúa)
```

**`authenticated` y `correct_channel` no son lo mismo**, y el resultado los
distingue: en `WRONG_CHANNEL` la credencial funcionó —`authenticated` es
verdadero— y aun así no se puede seguir. Solo `correct_channel` autoriza a
avanzar hacia un futuro publisher.

Sin `EXPECTED_YOUTUBE_CHANNEL_ID` la comprobación **falla** en vez de dar por
bueno cualquier canal, y ni siquiera llama a Google: sin saber qué se espera no
hay nada que comparar.

No se valida el formato del identificador. La documentación garantiza que cada
canal tiene uno único, pero no publica una gramática normativa; rechazar lo que no
empiece por `UC` sería inventar una regla, y además inútil, porque lo que protege
de publicar en el canal equivocado es la **comparación**, no el aspecto de la
cadena.

### Los siete desenlaces

| Resultado | Qué pasó | ¿Reintentar? |
|---|---|---|
| `AUTHENTICATED` | credencial válida y canal correcto | no hace falta |
| `WRONG_CHANNEL` | credencial válida, **otro** canal | no |
| `CREDENTIALS_MISSING` | falta alguna variable | no |
| `AUTHORIZATION_INVALID` | revocada, caducada o `invalid_grant` | **no** |
| `INSUFFICIENT_SCOPE` | 403 `insufficientPermissions` | no |
| `TRANSIENT_ERROR` | 429, 5xx, red caída, timeout, cuota | **sí** |
| `API_ERROR` | la API rechazó la petición de forma permanente | no |

`reintentable` se declara en positivo —solo `TRANSIENT_ERROR`— para que un
desenlace nuevo no nazca reintentable por descuido. Una autorización revocada no
se desrevoca reintentando, y hacerlo en bucle solo gastaría cuota y escondería
que hace falta repetir el consentimiento.

La cuota agotada se clasifica como transitoria porque se repone sola, sin que
intervenga nadie. **La política de reintentos no se implementa aquí**: G7.1 solo
deja la semántica lista para que el futuro publisher la reutilice.

### Seguridad

El refresh token no caduca solo, vale hasta que alguien lo revoque y basta por sí
mismo para acceder al canal. Nunca entra en el repositorio, en `runs/`, en un
artefacto, en un log, en un mensaje de error ni en un commit.

Cuatro barreras, no una:

1. las credenciales son propiedades, no campos, así que no hay `repr` que las lleve;
2. `CredencialesOAuth` y `TokenAcceso` declaran `repr=False` en los campos sensibles;
3. **el cuerpo de una respuesta de error no se propaga** —puede repetir lo que se
   envió—: se extrae solo la etiqueta corta del campo `error` y se descarta el
   resto, `error_description` incluida;
4. el redactor del proyecto enmascara cualquier valor cuyo nombre de variable
   contenga `TOKEN` o `SECRET`, por si algo se colara pese a lo anterior.

Hay un test que recorre logs, stdout, stderr, el resultado serializado y el `repr`
de cada objeto que toca la credencial, buscando el token de prueba.

### Dos comandos que no significan lo mismo

| Comando | Qué hace | Cuándo se usa |
|---|---|---|
| `youtube-auth-bootstrap` | **obtiene** la autorización | una vez, a mano, con navegador |
| `youtube-auth` | **comprueba** una autorización ya configurada | en cada verificación y en CI |

El primero abre un navegador y produce un refresh token. El segundo no abre nada
y no produce nada: solo dice si lo que hay configurado sirve.

### Alta OAuth local (una sola vez)

Antes de empezar hace falta un cliente OAuth de tipo **Desktop** creado en Google
Cloud, y su JSON descargado. **Ese archivo no se versiona, no se copia al
repositorio y no se sube a GitHub Actions.** Se queda donde lo dejó el navegador
y solo se lee de ahí:

```bash
python -m app youtube-auth-bootstrap --credentials ~/Descargas/client_secret_....json
```

Lo que ocurre, en orden:

1. se leen del JSON **solo** `client_id` y `client_secret`;
2. se genera PKCE (`code_verifier` de 64 caracteres y su `code_challenge` S256) y
   un `state` aleatorio;
3. se levanta un servidor **únicamente en loopback**, en un puerto que elige el
   sistema;
4. se abre el navegador en la pantalla de consentimiento de Google, pidiendo el
   alcance de `YOUTUBE_SCOPE` y acceso offline;
5. al volver, se comprueba que el `state` coincide —si no, la respuesta se
   descarta— y el servidor **se cierra de inmediato**;
6. se canjea el código con `grant_type=authorization_code` y el `code_verifier`;
7. se ejecuta el mismo AUTH CHECK de `youtube-auth` con el token recién obtenido.

Opciones:

| Opción | Para qué |
|---|---|
| `--timeout SEGUNDOS` | cuánto esperar el consentimiento (300 por defecto) |
| `--forzar-consentimiento` | añade `prompt=consent`; es el remedio cuando una reautorización devuelve access token pero **ningún** refresh token |

**El refresh token no se guarda en ningún archivo.** No se escribe en el
repositorio, ni en `runs/`, ni en un `.env` que el comando cree por su cuenta:
escribirlo sería decidir por ti dónde vive tu credencial más sensible, y crear un
archivo que alguien acabaría subiendo sin querer. Se muestra **una vez** por
stderr para que lo copies a tu gestor de secretos; el JSON del resultado que va a
stdout lleva solo una pista enmascarada de sus extremos.

Después del alta, configura estas cuatro variables —el propio comando te las
imprime con el canal ya rellenado—:

```
YOUTUBE_CLIENT_ID=...
YOUTUBE_CLIENT_SECRET=...
YOUTUBE_REFRESH_TOKEN=...
EXPECTED_YOUTUBE_CHANNEL_ID=UC...
```

En local van en tu `.env`, que está en `.gitignore`. En CI van como secretos del
repositorio.

### Comprobar la autorización

```bash
python -m app youtube-auth
```

Solo **verifica** lo que ya está configurado: no autoriza nada, no abre navegador
y no obtiene ningún token nuevo. Imprime el resultado como JSON en stdout —las
trazas van a stderr, para que la salida se pueda parsear— y termina con código 0
solo si el resultado es `AUTHENTICATED`. El JSON no lleva credenciales por
construcción.

A diferencia del alta, este comando **exige** `EXPECTED_YOUTUBE_CHANNEL_ID`: en el
alta el canal es lo que se está descubriendo, aquí es contra lo que se compara.

### Todavía no existe publicación

Ni el alta ni la comprobación suben nada. No hay `videos.insert`, no hay
publisher, no hay programación. Lo único que el proyecto sabe hacer con YouTube
hoy es autenticarse y decir de qué canal es la autorización.

### Cómo se probará en GitHub Actions

CI **no tiene ni debe tener** credenciales para los tests normales. Lo que el
workflow comprueba hoy es que la falta de credenciales produce un desenlace
explícito (`CREDENTIALS_MISSING`), con la salida y el log libres de cualquier cosa
con forma de credencial.

Cuando llegue el momento de comprobarlo de verdad, el refresh token irá como
secreto del repositorio y se expondrá solo al paso que lo necesite:

```yaml
env:
  YOUTUBE_CLIENT_ID: ${{ secrets.YOUTUBE_CLIENT_ID }}
  YOUTUBE_CLIENT_SECRET: ${{ secrets.YOUTUBE_CLIENT_SECRET }}
  YOUTUBE_REFRESH_TOKEN: ${{ secrets.YOUTUBE_REFRESH_TOKEN }}
  EXPECTED_YOUTUBE_CHANNEL_ID: ${{ vars.EXPECTED_YOUTUBE_CHANNEL_ID }}
run: .venv/bin/python -m app youtube-auth
```

GitHub enmascara los secretos en los logs, pero eso es la última barrera y no la
primera: el diseño ya evita que lleguen ahí. **No se usa `set -x`** en ningún paso.

La prueba contra el OAuth real está aislada y se activa a propósito:

```bash
RUN_REAL_YOUTUBE_AUTH=1 pytest tests/test_youtube_auth_real.py -v -s
```

Sin esa variable se omite entera, con el motivo escrito en el informe. No se
ejecuta en CI, no contiene credenciales y no sube nada.

### Qué queda fuera de G7.1

`videos.insert`, subida, subida reanudable, publicación, modificación de metadatos
de vídeos, programación, Discovery, analytics, multi-canal, publicación pública y
la política de reintentos.

El consentimiento OAuth inicial **sí** está implementado, como alta interactiva
local: ver «Alta OAuth local». Lo que no se automatiza es ejecutarlo sin una
persona delante, y eso es deliberado — el consentimiento lo da un humano.

### Qué queda por resolver

**Si `youtube.upload` basta para leer la identidad del canal.** La documentación
de Google no lo dice, y no se puede averiguar sin una autorización real. El
sistema está preparado para las dos respuestas: usa el alcance mínimo que manda
D17, lo deja configurable, y si resulta que no basta el resultado sale como
`INSUFFICIENT_SCOPE` nombrando el remedio, en vez de como un fallo opaco. Si eso
ocurre, habrá necesidad técnica demostrada para ampliar el alcance —probablemente
a `youtube.readonly`—, y esa es una decisión de arquitectura, no del código.

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
- `--pipeline e2e` — el de Gate 5, el completo: catorce etapas desde el
  transcript hasta el MP4 final validado. Es el único que invoca el motor.

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
├── provenance_ledger.json  fuentes, evidencia y decisiones de licencia
├── transformation_set.json los cinco elementos editoriales propios
├── qa_result.json        informe de QA técnica, con el juicio editorial aparte
│
│   pipeline e2e
├── render_material.json  material visual propio, generado por el pipeline
├── material/base.mp4
├── render_job.json       qué entra al vídeo, ya autorizado por la procedencia
├── render_result.json    el vídeo del motor, intermedio
├── final_video.json      el MP4 que se publica, con su SHA-256
├── final_video_qa.json   QA del archivo final
├── final/
│   └── short.mp4         **el Short**
├── qa/
│   ├── frames/           fotogramas para la inspección visual
│   └── contact_sheet.png
│
│   pipeline render (Gate 1)
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

La decisión de procedencia tampoco lo implica. `render_allowed` significa que la
procedencia registrada satisface la política técnica del sistema, y **no** afirma
legalidad, ausencia de reclamaciones de copyright, compatibilidad con Content ID
ni derecho a monetizar. No hay clasificador de uso legítimo: `fair_use_claim` es
una afirmación de alguien y siempre acaba en `needs_review`.

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
- **La validación visual es parcial por diseño.** La automatización comprueba
  que las capas se compusieron; que el subtítulo y el rótulo se **vean** exige
  mirar los fotogramas. Ese aviso está siempre presente en el informe.
- **El rótulo y el primer subtítulo repiten el mismo texto** durante los
  primeros segundos, porque los dos salen del gancho. Detectado mirando el
  vídeo; pendiente de decisión editorial.
- **No hay ffprobe garantizado**: el binario empaquetado no lo incluye. Se usa
  cuando está en el sistema y se cae a `ffmpeg -i` cuando no; el informe dice
  cuál se usó.
- El material visual es un degradado generado por el pipeline. Sirve para
  demostrar la cadena, no como estilo de producción.
- No hay política de reintentos: un fallo del motor o de FFmpeg detiene la
  corrida con su categoría y su código de salida a la vista.
- No hay política de reintentos por etapa: solo el contrato de errores que la
  distingue transitorio, permanente e infraestructura.
