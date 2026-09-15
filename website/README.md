# Website de Shorts Foundry

Sitio estático público e informativo del proyecto Shorts Foundry / Shorts
Automation.

Su único propósito es describir con honestidad qué hace el sistema, qué no hace,
cómo funciona la clasificación de procedencia de las fuentes y en qué estado
real está la integración con YouTube, además de alojar la política de privacidad
y los términos de uso.

Este directorio es **independiente del orquestador**: no importa nada de `app/`,
no participa en el pipeline, no se ejecuta en las corridas y no interviene en
ningún Gate. Es contenido, no código de negocio.

## Estructura

```
website/
├── index.html          portada: qué es, qué no es, procedencia, QA,
│                       estado de YouTube, estado del proyecto
├── privacy/
│   └── index.html      política de privacidad
├── terms/
│   └── index.html      términos de uso
├── styles.css          hoja de estilos única, compartida por las tres páginas
└── README.md           este archivo
```

## Rutas

El sitio se sirve desde la raíz del dominio. Cada documento vive en su propio
directorio con un `index.html`, de modo que la ruta limpia funcione sin
reescrituras:

| Ruta | Archivo |
|---|---|
| `/` | `index.html` |
| `/privacy` | `privacy/index.html` |
| `/terms` | `terms/index.html` |

Los enlaces internos son absolutos (`/`, `/privacy/`, `/terms/`) porque el sitio
está pensado para servirse en la raíz del dominio. Si alguna vez se sirviera bajo
un subdirectorio, habría que revisarlos.

## Restricciones de construcción

Son deliberadas y conviene mantenerlas al editar:

- **Sin JavaScript.** Ninguna página incluye `<script>` ni atributos de evento.
- **Sin analítica y sin cookies.** No hay medición, ni identificadores, ni
  almacenamiento en el navegador.
- **Sin recursos externos.** Ni tipografías, ni imágenes, ni hojas de estilo, ni
  scripts de terceros. La única marca gráfica es un SVG en línea y la tipografía
  es la del sistema.
- **Sin dependencias ni paso de compilación.** Son archivos estáticos; no hay
  `package.json`, ni generador de sitios, ni nada que instalar.
- **Sin secretos.** No hay credenciales, tokens, claves de API ni identificadores
  de cliente en ningún archivo.
- **Responsive y accesible.** Mobile-first, enlace para saltar al contenido,
  regiones semánticas, jerarquía de encabezados correcta, foco visible y soporte
  del modo oscuro del sistema mediante `prefers-color-scheme`.

## Vista previa local

No hace falta ninguna herramienta del proyecto. Cualquier servidor de archivos
estáticos sirve, por ejemplo:

```bash
python3 -m http.server --directory website
```

Conviene servir el directorio como raíz —no abrir los archivos con `file://`—
para que los enlaces absolutos resuelvan igual que en producción.

## Despliegue

El sitio está pensado para desplegarse como sitio estático en **Cloudflare
Pages**, sirviendo `website/` como directorio raíz de salida y sin comando de
compilación, ya que no hay ninguno.

Destino previsto:

```
https://shortsfoundry.dev
https://shortsfoundry.dev/privacy
https://shortsfoundry.dev/terms
```

**La configuración de Cloudflare no vive en este repositorio.** Aquí no hay
proyecto de Pages, ni Workers, ni `wrangler.toml`, ni reglas de redirección, ni
cabeceras, ni configuración de dominio. La conexión del repositorio con Pages y
la asignación del dominio son pasos de administración que se realizan fuera del
código y que aún no se han verificado; este README no los documenta para no
describir una configuración que nadie ha comprobado.

## Correo de soporte

El correo de contacto todavía no está definido, así que en su lugar aparece el
marcador literal:

```
SUPPORT_EMAIL_PENDIENTE
```

Está en las tres páginas: en el pie de cada una, y además en el apartado de
contacto de la política de privacidad y en el de los términos.

Para fijar la dirección definitiva, basta con localizarlo y sustituirlo:

```bash
grep -rn SUPPORT_EMAIL_PENDIENTE website/
```

Al sustituirlo conviene convertirlo en un enlace `mailto:` y retirar la clase
`placeholder`, que está pensada precisamente para que el marcador se vea a
simple vista mientras siga pendiente.
