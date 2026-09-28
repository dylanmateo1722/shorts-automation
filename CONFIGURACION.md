# Configuración: qué hace falta y para qué

Este documento dice **exactamente** qué variable falta, para qué acción, dónde
se pone y qué comando la ejercita. Nada más.

Ninguna credencial se versiona. Todas se leen del entorno; `.env` sirve para
desarrollo local y está en `.gitignore`.

---

## Lo que YA funciona sin ninguna credencial

| Pieza | Estado |
|---|---|
| Ingesta desde URL (`app ingest`) | real, solo necesita salida a internet |
| TTS en español (Edge TTS) | real, sin credencial |
| Material visual | real, generado con FFmpeg |
| Subtítulos, composición, render 9:16 | real, FFmpeg |
| QA técnica y de vídeo | real |
| Puerta de publicación | real, se evalúa sin tocar la red |

Con eso, `app short <url>` produce un Short completo **salvo** el paso de
traducción y adaptación, que necesita un modelo.

---

## FALTA: credencial de LLM

**PARA:** que `app short` traduzca y adapte sin intervención. Es lo único que
separa el sistema de ser completamente desatendido.

**DÓNDE CONFIGURARLA:** variables de entorno. El código **solo** lee el entorno;
si prefieres guardarlas en un `.env` (hay plantilla en `.env.example`, y `.env`
está en `.gitignore`), hay que cargarlo a mano: `set -a; . ./.env; set +a`.

### Camino recomendado: OpenRouter Free

Es el más corto porque tiene modelos gratuitos y **no hay que teclear la
`base_url`**: la del proveedor se conoce. Se saca la clave en
`https://openrouter.ai/keys` y son tres variables, ninguna de ellas la URL:

```bash
export LLM_PROVIDER=openrouter
export OPENROUTER_API_KEY=sk-or-v1-...   # NUNCA en el repositorio
export LLM_MODEL=google/gemma-4-31b-it:free
```

**Ojo con `LLM_MODEL`: el catálogo gratuito de OpenRouter cambia, y un
identificador que ya no existe responde 404.** No hay que adivinarlo; se
consulta, y no hace falta credencial para hacerlo:

```bash
curl -s https://openrouter.ai/api/v1/models \
  | python3 -c 'import json,sys; [print(m["id"]) for m in json.load(sys.stdin)["data"] if m["id"].endswith(":free")]'
```

Los gratuitos terminan en `:free`. Si el identificador no existe, el error lo
dice con esas palabras en vez de culpar a la clave.

Dos detalles propios de OpenRouter, ya resueltos en el código:

- **`response_format` va desactivado** con este proveedor. OpenRouter documenta
  que, si el modelo no soporta salidas estructuradas, *la petición falla* en vez
  de ignorarse el parámetro. Y no es un caso raro: consultando ese mismo catálogo,
  **12 de los 17 modelos gratuitos no admiten `response_format`**. Pedirlo por
  defecto convertiría la primera llamada en un error.
  No se pierde nada: los prompts ya piden JSON y el intérprete sabe
  desenvolverlo aunque el modelo lo rodee de prosa. Con un modelo que sí lo
  soporte se activa con `LLM_JSON_MODE=1`; para saber si lo soporta, su entrada
  del catálogo lista `response_format` en `supported_parameters`.
- **Atribución opcional.** `OPENROUTER_SITE_URL` y `OPENROUTER_APP_NAME` mandan
  las cabeceras `HTTP-Referer` y `X-Title`, que solo sirven para aparecer en las
  tablas públicas de OpenRouter. No se envían salvo que se configuren.

### Cualquier otro proveedor compatible

```bash
export LLM_PROVIDER=openai        # o deepseek, groq, moonshot, openai_compatible
export LLM_BASE_URL=https://api.openai.com/v1   # opcional si el nombre se conoce
export LLM_MODEL=gpt-4o-mini      # el identificador que use tu proveedor
export LLM_API_KEY=sk-...         # NUNCA en el repositorio
```

Cualquier proveedor que hable Chat Completions sirve: solo cambian `LLM_BASE_URL`
y `LLM_MODEL`. La lista de nombres reconocidos existe para dar un error claro
ante una errata, no para restringir. `LLM_BASE_URL` solo hace falta si el nombre
del proveedor no está en la tabla conocida, o si se sirve desde otro sitio: lo
que se indique manda siempre sobre lo conocido.

`OPENROUTER_API_KEY` solo se lee con `LLM_PROVIDER=openrouter`, y `LLM_API_KEY`
tiene precedencia si están las dos. Usar la clave de OpenRouter contra OpenAI
daría un 401 desconcertante, así que no se hace.

**PRUEBA QUE SE EJECUTARÁ:**

```bash
# 1. Comprobar la credencial en una sola llamada, sin gastar una corrida.
uv run python -m app llm-check

# 2. Si responde, el Short completo.
uv run python -m app short "https://en.wikipedia.org/wiki/Stanislav_Petrov" \
    --max-caracteres 700 --look frio
```

`llm-check` imprime también `base_url` y `json_mode`, que son justo los dos
valores que no se pueden adivinar mirando el entorno.

Cada rechazo dice qué hacer, porque los remedios son incompatibles entre sí:
**401** la clave no vale (y no se reintenta: seguiría sin valer), **402** no hay
saldo o se agotó el cupo diario del modelo gratuito, **403** permisos o
moderación, **404** el identificador del modelo no existe. Un 408, 429 o 5xx sí
se reintenta, con espera creciente y respetando `Retry-After`.

`llm-check` existe porque descubrir que la clave está mal a mitad de una corrida
es caro: para entonces ya se ingirió la fuente y se va a tirar el trabajo.

Si la respuesta del modelo pierde una cifra del original, inventa una que no
estaba, no está en español o no es subtitulable, el pipeline la **rechaza** y
la corrida falla en esa etapa. No se publica un guion que no pasó sus
validaciones.

### Mientras no haya credencial

El sistema no se queda parado: se le pueden dar las dos respuestas preparadas.

```bash
export LLM_PROVIDER=fake
export LLM_MODEL=autoria-humana-v1
export LLM_FAKE_RESPONSES=demo/respuestas/petrov.json
```

El archivo lleva dos objetos: la traducción y la adaptación, en ese orden. Pasan
exactamente las mismas validaciones que una respuesta del modelo. Hay dos
ejemplos en `demo/respuestas/`.

Esto es un andamio de desarrollo, no el producto: cada pieza nueva necesita sus
propias respuestas, y por eso la credencial es la prioridad.

### Qué está ya verificado del cliente real

El cliente de Chat Completions —el mismo código que hablará con OpenAI o
DeepSeek— está probado contra un servidor HTTP real: el POST, la cabecera de
autorización, la envoltura de la respuesta, el JSON entre vallas, el
truncamiento y el reintento ante 429 y 5xx.

Contra la API **real** de OpenRouter está verificado todo lo que no necesita una
clave válida: que `https://openrouter.ai/api/v1` se resuelve sin indicar
`LLM_BASE_URL`, que la petición sale sin `response_format`, que la cabecera de
autorización llega íntegra al servidor, y que un 401 se detecta, nombra
`OPENROUTER_API_KEY` y no se reintenta. Lo único sin verificar es la llamada con
una clave que exista: hace falta la credencial.

Para reproducirlo sin credencial:

```bash
scripts/servidor_llm_local.py --respuestas demo/respuestas/petrov.json &
export LLM_PROVIDER=openai_compatible
export LLM_BASE_URL=http://127.0.0.1:8099/v1
export LLM_MODEL=servidor-local
export LLM_API_KEY=no-es-una-credencial-real
uv run python -m app short "https://en.wikipedia.org/wiki/Stanislav_Petrov" --max-caracteres 700
```

Eso recorre el cliente real de punta a punta. Cambiar a un proveedor de verdad
es cambiar `LLM_BASE_URL`, `LLM_MODEL` y `LLM_API_KEY`; nada más.

### Si una fuente larga agota el presupuesto de tokens

`LLM_MAX_TOKENS` (4000 por defecto). Si el modelo lo agota, el JSON llega
cortado y el error lo dice con esas palabras, en vez de culpar al formato.

---

## FALTA: credenciales de YouTube

**PARA:** publicar realmente. Todo lo anterior —metadata, aprobación editorial,
puerta de publicación, subida reanudable, reconciliación— está implementado y
se ejercita sin red; lo único que no se puede hacer sin credencial es la subida.

**DÓNDE CONFIGURARLA:**

```bash
export YOUTUBE_CLIENT_ID=...
export YOUTUBE_CLIENT_SECRET=...
export YOUTUBE_REFRESH_TOKEN=...
export YOUTUBE_CHANNEL_ID=...      # opcional; si está, se comprueba el canal
```

El refresh token se obtiene una sola vez, en local, con uno de estos dos
comandos. Ninguno sube nada:

```bash
uv run python -m app youtube-auth-bootstrap --credentials cliente_oauth.json
uv run python -m app youtube-auth-device    --credentials cliente_oauth.json
```

El JSON del cliente OAuth se descarga de Google Cloud y **no se copia al
repositorio**: solo se leen de él `client_id` y `client_secret`.

**PRUEBA QUE SE EJECUTARÁ:**

```bash
# 1. Comprobar la autorización. No sube nada.
uv run python -m app youtube-auth

# 2. Evaluar la puerta de publicación. Tampoco toca la red.
uv run python -m app publish "$RUN_ID" \
    --metadata  publicacion/metadata.json \
    --approval  publicacion/aprobacion.json

# 3. Publicar de verdad, en privado.
uv run python -m app publish "$RUN_ID" \
    --metadata  publicacion/metadata.json \
    --approval  publicacion/aprobacion.json \
    --confirmar SUBIR
```

### Dos cosas que conviene saber antes de publicar

* **El refresh token caduca en 7 días** mientras la pantalla de consentimiento
  esté en *External* + *Testing*. Está documentado por Google y no es un fallo
  del proyecto.
* **Todo vídeo subido queda forzado a privado** si el proyecto de API no está
  verificado y se creó después del 28‑07‑2020, hasta superar la auditoría de
  cumplimiento. El estado de esa auditoría para este proyecto **no está
  determinado**.
* La cuota de `videos.insert` es de **100 llamadas diarias**.

---

## OPCIONAL: metraje de archivo

**PARA:** sustituir el material generado por metraje real.

No hace falta ninguna credencial para producir: `app short` genera el material
con FFmpeg. Cuando haya metraje con licencia en regla, entra por el mismo
camino sin tocar código: se declara en un JSON de fuentes con su base y su
evidencia, y se elige con `--material-asset`.

```bash
uv run python -m app run --pipeline e2e --run-id "$RUN_ID" \
    --transcript ... --sources mis_fuentes.json --material-asset mi-metraje
```

La procedencia decide si puede entrar al vídeo. Una fuente cuya licencia no
consta se clasifica como desconocida y queda **bloqueada para el render**.

---

## Resumen

| Falta | Para | Bloquea |
|---|---|---|
| `OPENROUTER_API_KEY` (+ `LLM_PROVIDER=openrouter`, `LLM_MODEL`) | traducir y adaptar sin intervención | la automatización completa |
| `YOUTUBE_*` | subir el vídeo | solo la publicación |
| nada | todo lo demás | — |
