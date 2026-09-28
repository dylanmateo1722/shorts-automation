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

**DÓNDE CONFIGURARLA:** variables de entorno, o `.env` en la raíz.

```bash
export LLM_PROVIDER=openai        # o deepseek, groq, openrouter, moonshot, openai_compatible
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=gpt-4o-mini      # el identificador que use tu proveedor
export LLM_API_KEY=sk-...         # NUNCA en el repositorio
```

Cualquier proveedor que hable Chat Completions sirve: solo cambian `LLM_BASE_URL`
y `LLM_MODEL`. La lista de nombres reconocidos existe para dar un error claro
ante una errata, no para restringir.

**PRUEBA QUE SE EJECUTARÁ:**

```bash
uv run python -m app short "https://en.wikipedia.org/wiki/Stanislav_Petrov" \
    --max-caracteres 700 --look frio
```

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
| `LLM_API_KEY` (+ provider, model, base_url) | traducir y adaptar sin intervención | la automatización completa |
| `YOUTUBE_*` | subir el vídeo | solo la publicación |
| nada | todo lo demás | — |
