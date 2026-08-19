# ⬇ MiDescargador

Un gestor de descargas **gratis, sin anuncios, sin límites y sin registrarse**,
hecho a medida: descarga **segmentada en N conexiones paralelas** (como IDM,
pero nuestro), con reanudación, pausa, y soporte de **videos y MediaFire**
vía yt-dlp. Incluye una **extensión de Chrome** que pone un botón de descarga
sobre cada video de la página.

## Cómo empezar (2 minutos)

1. **Doble clic en `Iniciar.bat`** — abre el panel en el navegador
   (http://127.0.0.1:17890) y enciende el servidor local.
2. Pega un enlace en el panel y pulsa **Descargar**. Eso es todo.

La primera vez, `Iniciar.bat` ya prepara todo lo necesario (Python, yt-dlp,
ffmpeg) automáticamente. No se instala nada en el sistema: todo vive en esta
carpeta.

## La extensión de Chrome (descarga estilo IDM en los videos)

1. Abre `chrome://extensions` en Chrome.
2. Activa **Modo de desarrollador** (interruptor arriba a la derecha).
3. Clic en **Cargar descomprimida** → elige la carpeta `extension` de este
   proyecto.
4. Listo. Al **pasar el ratón sobre un video** aparece una pequeña pestaña
   **⬇ Descargar** (como IDM). Al pulsarla se consultan al servidor local las
   **resoluciones disponibles** (240p, 360p, 720p, 1080p… y solo audio) y
   puedes elegir la que quieras. En YouTube, TikTok, Instagram, Twitch… se
   usa la URL de la página, que es la que yt-dlp sabe resolver con todas las
   calidades.

> El servidor debe estar encendido (Iniciar.bat) para que el botón use las
> descargas segmentadas. Si está apagado, la extensión intenta descargar con
> el navegador como respaldo.

## Qué hace el motor por dentro

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
  (404, formato inexistente, sesión vencida…) **no** se reintentan: reintentar
  solo repetiría el mismo fallo, y el botón Reintentar del panel sigue
  disponible siempre.
- Si el servidor **no** soporta rangos (o responde 200 ignorando el Range),
  cae automáticamente a una sola conexión con reanudación.
- URLs de **YouTube, TikTok, MediaFire, Instagram, Twitter/X, Twitch, Vimeo,
  SoundCloud**… se enrutan a **yt-dlp** (instalado en el venv) y se fusionan
  con **ffmpeg** (video + audio en un solo archivo).
- **Torrents (magnet, .torrent y los enlaces TORRENT de zona-leros)**: el enlace
  de zetrrent.com se resuelve solo (cookies + token CSRF -> URL directa del
  `.torrent`) y el contenido se baja por BitTorrent con **aria2c** (en `bin/`),
  con progreso en el panel igual que las demás descargas. Al terminar, los
  comprimidos se extraen solos con la contraseña automática.

## Versión de escritorio (Electron)

Además del panel en el navegador hay una **app de escritorio** que lanza el
servidor y abre el panel en su propia ventana, sin necesitar Chrome:

```
electron/dist/MiDescargador-Setup-2.0.1.exe     ← instalador (con auto-update)
electron/dist/MiDescargador-2.0.1-portable.exe  ← portable (sin auto-update)
```

### Auto-actualización

La **versión instalada** (Setup) se actualiza sola: al arrancar y cada 4 horas
consulta los releases de GitHub, avisa cuando hay versión nueva, la descarga
en segundo plano y al reiniciar instala y vuelve a abrir la app. Tus datos
(config, logs, descargas) viven fuera de la carpeta de la app y nunca se tocan.

El **portable** no puede auto-actualizarse (se auto-extrae a una carpeta
temporal y no puede reemplazarse a sí mismo); descarga la versión nueva
manualmente cuando haya.

Para publicar una versión nueva (requiere `gh` autenticado), hay dos vías:

**Automática (recomendada)** — un solo comando hace todo: bump de
`electron/package.json` + `extension/manifest.json`, reconstruye el backend
(PyInstaller), el instalador (electron-builder), verifica los artefactos,
commitea, pushea y publica el release:

```
node build_mei/release.js [--patch | --minor | --major | X.Y.Z] [--notes "..."]
```

- `--patch`/`--minor`/`--major` incrementan desde la versión actual (por
defecto `--patch`); también se puede pasar una versión exacta `X.Y.Z`.
- `--dry-run` imprime el plan sin ejecutar nada.
- El bump solo toca las dos líneas de versión (el resto del árbol queda
  intacto); los commits de código van aparte, como siempre.

**Manual** (el flujo histórico):

```
cd electron
npm run dist                                   # genera Setup + portable + latest.yml
gh release create vX.Y.Z \
  dist/MiDescargador-Setup-X.Y.Z.exe \
  dist/MiDescargador-Setup-X.Y.Z.exe.blockmap \
  dist/MiDescargador-X.Y.Z-portable.exe \
  dist/latest.yml \
  --repo luiggiberaldi/Midescargador --title "MiDescargador X.Y.Z"
```

### Verificación automática de versiones (workflow de GitHub Actions)

El repositorio tiene un workflow (`.github/workflows/check-versiones.yml`) que
**verifica que las versiones estén sincronizadas antes de cada release**. Se
ejecuta en cada PR, en cada push a `main` y, sobre todo, **cuando se empuja el
tag `vX.Y.Z`**: como `gh release create vX.Y.Z` crea y empuja ese tag, la
verificación se dispara automáticamente en el momento de publicar.

Comprueba tres cosas y falla si alguna no cuadra:

1. **`electron/package.json` ↔ `extension/manifest.json`**: ejecuta
   `build_mei/sync-version.js` y verifica que no queden diferencias (un bump
   hecho solo en un lado se detecta).
2. **`latest.yml` ↔ `package.json`** (si el archivo existe; está en
   `.gitignore` y solo aparece tras un build local): el `version:` de
   `latest.yml` debe coincidir con la versión de la app.
3. **Tag `vX.Y.Z` ↔ código**: al empujar un tag `v*`, el número del tag debe
   coincidir con `electron/package.json` y `extension/manifest.json`.

Si el workflow falla en el paso 3 (tag ≠ código), la causa casi siempre es
haber publicado el release sin hacer antes el bump; corrígelo con
`electron/package.json` + `node build_mei/sync-version.js`, vuelve a
committear/pushear y repite el `gh release create` con el tag correcto.

Las apps instaladas detectan el release nuevo automáticamente (sin firmar,
Windows puede pedir confirmación: *Más información → Ejecutar de todas formas*).

- La app (Electron) lanza el backend compilado (`servidor.exe`, hecho con
  PyInstaller) y muestra el panel en `http://127.0.0.1:17890`. Al cerrar la
  ventana, detiene el servidor. Todo lo demás funciona igual: yt-dlp/ffmpeg,
  torrents (aria2c), Mega, hosters y la extracción de ZonaLeros (que usa tu
  Chrome).
- Windows puede pedir confirmación la primera vez (el .exe no está firmado):
  pulsa *Más información → Ejecutar de todas formas*.
- Para reconstruirlo desde el código:

  ```
  # 1) backend: Python -> servidor.exe (PyInstaller, con yt-dlp, ffmpeg, aria2c y el panel)
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

  # 2) app de escritorio: Electron + electron-builder
  cd electron && npm install && npm run dist
  # -> electron/dist/MiDescargador-2.0.0-portable.exe
  ```

## Estructura del proyecto

```
MiDescargador/
├── Iniciar.bat          ← doble clic para arrancar (panel en el navegador)
├── motor.py             ← motor de descargas segmentadas (solo stdlib)
├── torrents.py          ← torrents: resolver zetrrent + motor aria2c
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
│   └── dist/            ← MiDescargador-2.0.0-portable.exe
└── venv/                ← Python + yt-dlp (todo local, nada global)
```

## API REST (por si quieres automatizar)

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

## ¿Qué tiene que IDM no tiene? (las mejoras)

| | MiDescargador | IDM |
|---|---|---|
| **Precio** | Gratis, para siempre | ~$25 (prueba de 30 días) |
| **Anuncios / adware** | Cero | El instalador histórico arrastra basura |
| **Límite de prueba / "serial"** | No existe el concepto | El motivo de tu carpeta pirata 😉 |
| **Código** | Abierto, en esta carpeta | Cerrado |
| **Videos (YouTube, TikTok…)** | yt-dlp (miles de sitios, actualizado) | Solo los que IDM soporta |
| **Torrents (magnet / .torrent)** | aria2c integrado, con los enlaces TORRENT de ZonaLeros resueltos automáticamente | No |
| **Fusión video+audio** | ffmpeg automático | Parcial |
| **Reanudación** | Por segmento (`.part`) | Sí, pero propietaria |
| **Reintentos** | 6 intentos con backoff por segmento | Opaco |
| **Telemetría** | Ninguna, todo local | Envía datos de uso |
| **Multiplataforma** | Windows/macOS/Linux | Windows |
| **Integrable** | API REST abierta | No |

## Limitaciones honestas

- **DRM** (Netflix, Prime Video, Disney+…) no se puede descargar con ninguna
  herramienta legítima — ni con IDM ni con esta. Eso es protección legal.
- Algunos sitios limitan descargas por IP o exigen sesión; para esos,
  yt-dlp lee las cookies de tu navegador si le pasas `--cookies-from-browser`.
- No todos los servidores dejan usar rangos: los que no, se bajan con una
  sola conexión (igual de confiable, solo menos veloz).
- El proyecto es tuyo y para uso personal. Respeta los términos de cada sitio
  y el contenido que descargues.

## Trucos rápidos

- **Pegar varias URLs a la vez**: sepáralas con espacios o salto de línea en
  el campo de URL (el panel las encola si las pegas una por una; la cola se
  puede ampliar cuando lo pidas).
- **Cambiar conexiones**: el campo "Conexiones" va de 1 a 32. 8 es buena
  medida; sube a 16 en conexiones rápidas, baja a 2 si el servidor se queja.
- **Prueba del motor**: `python motor.py --selftest` verifica el motor
  segmentado descargando un archivo local en 4 conexiones y comparando el SHA.
- **Detener todo**: cierra la ventana negra (o Ctrl+C) y `taskkill /F /IM
  python.exe` si algo se quedara colgado.
