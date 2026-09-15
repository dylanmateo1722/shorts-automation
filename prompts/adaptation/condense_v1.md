# adaptation_condense_v1

El guion siguiente es demasiado largo. Tiene unas {current_words} palabras y
debe quedar en torno a **{target_words}** para durar
{target_duration_seconds} segundos.

Condénsalo. **No lo cortes**: reescríbelo más breve.

## Reglas

- Elimina redundancias y rodeos antes que información.
- Mantén el gancho y el remate.
- No inventes nada nuevo al reescribir.
- No alteres ninguna cifra ni ningún nombre que decidas conservar.
- Mantén las frases cortas y locutables.

## Guion actual

```
{source_text}
```

## Salida

Mismo formato JSON que la adaptación, sin texto alrededor:

{{"hook": "...", "sections": [{{"kind": "hook", "text": "...", "order": 0}}], "notes": ["..."]}}
