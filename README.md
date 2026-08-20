# ⬇ MiDescargador

Un gestor de descargas **gratis, sin anuncios, sin límites y sin registrarse**,
hecho a medida: descarga **segmentada en N conexiones paralelas** (como IDM,
pero nuestro), con reanudación, pausa, y soporte de **videos, torrents y
hosters** vía yt-dlp/aria2c. Incluye una **extensión de Chrome** que pone un
botón de descarga sobre cada video de la página.

Todo pasa **en tu propia computadora**: sin telemetría, sin datos enviados a
ningún sitio, sin publicidad.

---

## 🧑‍💻 Para usuarios

### Descargar e instalar (Windows)

1. Ve a la pestaña **[Releases](https://github.com/luigiberaldi-code/Midescargador/releases)** de este repositorio y baja el archivo
   `MiDescargador-Setup-X.Y.Z.exe` (el instalador).
2. **Doble clic** en el instalador y sigue los pasos (elige la carpeta donde
   quieras instalarlo).
3. La app se abre sola con su panel. Pega un enlace arriba y pulsa
   **Descargar**. Eso es todo.

> Windows puede mostrar un aviso azul la primera vez (el programa no está
> firmado): pulsa **Más información → Ejecutar de todas formas**. Es normal.
> También hay una versión **portable** (`...-portable.exe`) que no necesita
> instalación.

### La extensión de Chrome (botón de descarga sobre los videos)

1. Abre la app y ve a **Información → Extensión de Chrome**.
2. Pulsa **«Abrir carpeta de la extensión»** (se abre en el explorador).
3. En Chrome, abre `chrome://extensions`, activa **Modo de desarrollador**
   (arriba a la derecha) y pulsa **«Cargar descomprimida»**, eligiendo esa
   carpeta.
4. Listo. Al **pasar el ratón sobre un video** (YouTube, TikTok, Instagram,
   Twitch…) aparece una pequeña pestaña **⬇ Descargar**. Al pulsarla se
   consultan las **resoluciones disponibles** (240p, 360p, 720p, 1080p… y solo
   audio) y puedes elegir la que quieras.

> La extensión está instalada para siempre en tu Chrome. Si Chrome la
> desactiva sola, vuelve a pulsar «Cargar descomprimida» con la misma carpeta.

### Qué puedes descargar

- **Archivos directos**: cualquier enlace HTTP/HTTPS a un archivo.
- **Videos y audio**: YouTube, TikTok, Instagram, Facebook, Twitter/X, Twitch, Vimeo,
  SoundCloud… (se fusionan video + audio en un solo archivo mediante yt-dlp y ffmpeg).
- **Servidores y hosters**: MediaFire, Mega, Rootz, 1Fichier, MegaUp, GoFile y más.
- **Torrents**: enlaces `magnet:` y archivos `.torrent` (descarga BitTorrent mediante aria2c).
- **Extracción de enlaces**: pega la URL de páginas con múltiples mirrors o servidores y pulsa
  «Extraer enlaces de descarga» para obtenerlos todos automáticamente.

### Auto-actualización

La **versión instalada** se actualiza sola: al arrancar y cada 4 horas
consulta los releases, avisa cuando hay versión nueva, la descarga en segundo
plano y al reiniciar instala y vuelve a abrir la app. Tus datos (config, logs,
descargas) viven fuera de la carpeta de la app y nunca se tocan.

El **portable** no puede auto-actualizarse; baja la versión nueva a mano.

### Apoyar el proyecto (donaciones)

MiDescargador es gratis, sin anuncios y sin telemetría. Si te es útil y querés
que siga mejorando, podés invitarme un café con una donación voluntaria por
PayPal (botón de donación en la app, Información → Apoyar el proyecto):

[![Donar con PayPal](https://img.shields.io/badge/Donar-PayPal-00457C?logo=paypal&logoColor=white)](https://www.paypal.com/donate/?hosted_button_id=MA97UAYFK7JSC)

### Reportar un problema

Abre un **Issue** en este repositorio usando la plantilla de reporte
(aparece sola al pulsar *New issue*): la **versión** que usas (se ve en
Configuración → Actualizaciones), **qué hiciste**, **qué esperabas** y
**qué pasó** (con el texto del error, si aparece).

Para saber cómo reportar bien o cómo colaborar con código, leé
[CONTRIBUTING.md](CONTRIBUTING.md).

---

## 🛠️ Para desarrolladores

### Arrancar desde el código (sin compilar)

1. **Doble clic en `Iniciar.bat`** — abre el panel en el navegador
   (http://127.0.0.1:17890) y enciende el servidor local.
2. Pega un enlace en el panel y pulsa **Descargar**. Eso es todo.

La primera vez, `Iniciar.bat` prepara todo lo necesario (Python, yt-dlp,
ffmpeg) automáticamente. No se instala nada en el sistema: todo vive en esta
carpeta.

### Cómo funciona el motor por dentro

- **HEAD** para descubrir tamaño y si el servidor soporta rangos.
- Si soporta (`Accept-Ranges: bytes`): parte el archivo en N pedazos y baja
  cada uno **en paralelo** con cabeceras `Range: bytes=...-...`.
- **Reanudación**: si interrumpes, los pedazos `.part` se conservan y la
  próxima vez continúa desde donde iba.
- **Pausa / Reanudar / Cancelar** por descarga desde el panel.
- **Reintentos con backoff**: si un pedazo falla, espera y vuelve a intentarlo
  (hasta 6 veces por segmento).
- **Scheduler de reintentos automáticos (estilo IDM)**: si la descarga entera
  falla por un error transitorio (red caída, servidor ocupado, timeout), el
  servidor la vuelve a lanzar sola con **backoff exponencial** (30 s → 60 s →
  120 s → 240 s → 480 s, tope 10 min, máximo 5 reintentos). El contador se
  persiste en `cola.json`, así que el ciclo sobrevive al reinicio del servidor.
  El panel muestra un aviso azul con la cuenta regresiva
  ("Reintento automático en Xs (intento N/5)"). Los errores **permanentes**
  (404, formato inexistente, sesión vencida…) **no** se reintentan.
- Si el servidor **no** soporta rangos (o responde 200 ignorando el Range),
  cae automáticamente a una sola conexión con reanudación.
- URLs de **YouTube, TikTok, MediaFire, Instagram, Twitter/X, Twitch, Vimeo,
  SoundCloud**… se enrutan a **yt-dlp** y se fusionan con **ffmpeg** (video +
  audio en un solo archivo).
- **Torrents (magnet y .torrent)**: descarga BitTorrent integrada con
  **aria2c** y soporte de extracción automática de archivos comprimidos al
  finalizar.

### Versión de escritorio (Electron)

La app de escritorio lanza el servidor compilado (`servidor.exe`, hecho con
PyInstaller) y abre el panel en su propia ventana, sin necesitar Chrome.

Para **publicar una versión nueva** (requiere `gh` autenticado), hay dos vías:

**Automática (recomendada)** — un solo comando hace todo: bump de
`electron/package.json` + `extension/manifest.json`, reconstruye el backend
(PyInstaller), el instalador (electron-builder), verifica los artefactos,
commitea, pushea y publica el release:

```bash
node build_mei/release.js [--patch | --minor | --major | X.Y.Z] [--notes "..."]
```

- `--patch`/`--minor`/`--major` incrementan desde la versión actual (por
  defecto `--patch`); también se puede pasar una versión exacta `X.Y.Z`.
- `--dry-run` imprime el plan sin ejecutar nada.
- El bump solo toca las dos líneas de versión; los commits de código van
  aparte, como siempre.

**Manual** (el flujo histórico):

```bash
cd electron
npm run dist                                   # genera Setup + portable + latest.yml
gh release create vX.Y.Z \
  dist/MiDescargador-Setup-X.Y.Z.exe \
  dist/MiDescargador-Setup-X.Y.Z.exe.blockmap \
  dist/MiDescargador-X.Y.Z-portable.exe \
  dist/latest.yml \
  --repo luigiberaldi-code/Midescargador --title "MiDescargador X.Y.Z"
```

Para reconstruir el backend desde el código:

```bash
venv/Scripts/pyinstaller.exe --noconfirm --clean --name servidor --onedir --console \
  --distpath backend --workpath build_mei --specpath build_mei \
  --add-binary "venv/Scripts/yt-dlp.exe;venv/Scripts/yt-dlp.exe" \
  --add-binary "bin/ffmpeg.exe;bin/ffmpeg.exe" \
  --add-binary "bin/ffprobe.exe;bin/ffprobe.exe" \
  --add-binary "bin/aria2c.exe;bin/aria2c.exe" \
  --add-data "static;static" servidor.py
# (si los binarios quedan anidados en _internal/bin/<nombre>/<nombre>,
#  subirlos un nivel: mover el .exe a la carpeta de su nombre)
# Los binarios de bin/ vienen de los builds essentials de gyan.dev
# (https://www.gyan.dev/ffmpeg/builds/): ffmpeg.exe y ffprobe.exe del mismo
# paquete (ffprobe es necesario para extraer audio a mp3).
```

### Verificación automática de versiones (workflow de GitHub Actions)

El repositorio tiene un workflow (`.github/workflows/check-versiones.yml`) que
**verifica que las versiones estén sincronizadas antes de cada release**. Se
ejecuta en cada PR, en cada push a `main` y, sobre todo, **cuando se empuja el
tag `vX.Y.Z`**: como `gh release create vX.Y.Z` crea y empuja ese tag, la
verificación se dispara automáticamente en el momento de publicar.

Comprueba tres cosas y falla si alguna no cuadra:

1. **`electron/package.json` ↔ `extension/manifest.json`**: la versión del
   manifest debe coincidir con la de package.json en el mismo commit.
2. **`latest.yml` ↔ `package.json`** (si el archivo existe; está en
   `.gitignore` y solo aparece tras un build local).
3. **Tag `vX.Y.Z` ↔ código**: al empujar un tag `v*`, el número del tag debe
   coincidir con `electron/package.json` y `extension/manifest.json`.

Si el workflow falla en el paso 3 (tag ≠ código), la causa casi siempre es
haber publicado el release sin hacer antes el bump; corrígelo con
`electron/package.json` + `node build_mei/sync-version.js`, vuelve a
committear/pushear y repite el `gh release create` con el tag correcto.

### Estructura del proyecto

```
MiDescargador/
├── Iniciar.bat          ← doble clic para arrancar (panel en el navegador)
├── motor.py             ← motor de descargas segmentadas (solo stdlib)
├── torrents.py          ← torrents: motor BitTorrent vía aria2c
├── servidor.py          ← servidor local + API REST
├── static/index.html    ← interfaz web (panel)
├── extension/           ← extensión de Chrome (MV3)
│   ├── manifest.json
│   ├── content.js       ← detecta videos, pone el botón
│   ├── background.js    ← respaldo con chrome.downloads
│   └── popup.html
├── bin/ffmpeg.exe       ← ffmpeg estático (fusiona video+audio)
├── bin/ffprobe.exe      ← ffprobe estático (extrae audio a mp3)
├── bin/aria2c.exe       ← motor BitTorrent (magnet y .torrent)
├── backend/servidor/    ← servidor.exe compilado (PyInstaller, usado por Electron)
├── electron/            ← app de escritorio (Electron + electron-builder)
│   ├── main.js          ← lanza el backend y abre la ventana del panel
│   ├── package.json     ← configuración de electron-builder
│   └── dist/            ← instalador + portable
└── venv/                ← Python + yt-dlp (todo local, nada global)
```

### API REST (por si quieres automatizar)

```
POST /api/descargar   {"url", "segmentos"?, "carpeta"?}   → {"id"}
GET  /api/estado                                           → [ {id, nombre, estado, descargado, total, velocidad, eta, ...} ]
POST /api/pausar      {"id"}
POST /api/reanudar    {"id"}
POST /api/cancelar    {"id"}
POST /api/borrar      {"id"}
POST /api/abrir       {"id"}       (abre la carpeta en el explorador)
POST /api/carpeta                  (abre la carpeta de descargas en el
                                   explorador de archivos del sistema)
GET  /api/media/<id>                (sirve el archivo con soporte de Range:
                                     el reproductor integrado del panel)
```

### ¿Qué tiene que IDM no tiene? (las mejoras)

| | MiDescargador | IDM |
|---|---|---|
| **Precio** | Gratis, para siempre | ~$25 (prueba de 30 días) |
| **Anuncios / adware** | Cero | El instalador histórico arrastra basura |
| **Límite de prueba / "serial"** | No existe (100% libre) | Exige serial o licencias periódicas |
| **Código** | Abierto (MIT) | Cerrado |
| **Videos (YouTube, TikTok…)** | yt-dlp (miles de sitios, actualizado) | Solo los que IDM soporta |
| **Torrents (magnet / .torrent)** | aria2c integrado para descargas BitTorrent y enlaces magnet | No |
| **Fusión video+audio** | ffmpeg automático | Parcial |
| **Reanudación** | Por segmento (`.part`) | Sí, pero propietaria |
| **Reintentos** | 6 intentos con backoff por segmento | Opaco |
| **Telemetría** | Ninguna, todo local | Envía datos de uso |
| **Multiplataforma** | Windows/macOS/Linux (el motor es stdlib) | Windows |
| **Integrable** | API REST abierta | No |

### Limitaciones honestas

- **DRM** (Netflix, Prime Video, Disney+…) no se puede descargar con ninguna
  herramienta legítima — ni con IDM ni con esta. Eso es protección legal.
- Algunos sitios limitan descargas por IP o exigen sesión; para esos, yt-dlp
  lee las cookies de tu navegador si le pasas `--cookies-from-browser`.
- No todos los servidores dejan usar rangos: los que no, se bajan con una
  sola conexión (igual de confiable, solo menos veloz).
- El proyecto es para uso personal. Respeta los términos de cada sitio y el
  contenido que descargues.

### Trucos rápidos

- **Pegar varias URLs a la vez**: sepáralas con espacios o salto de línea en
  el campo de URL (el panel las encola una por una).
- **Cambiar conexiones**: el campo "Conexiones" va de 1 a 32. 8 es buena
  medida; sube a 16 en conexiones rápidas, baja a 2 si el servidor se queja.
- **Prueba del motor**: `python motor.py --selftest` verifica el motor
  segmentado descargando un archivo local en 4 conexiones y comparando el SHA.
- **Detener todo**: cierra la ventana negra (o Ctrl+C) y `taskkill /F /IM
  python.exe` si algo se quedara colgado.

## Licencia

[MIT](LICENSE) — libre de usar, modificar y compartir.
