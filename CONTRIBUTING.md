# Contribuyendo a MiDescargador

¡Gracias por querer ayudar! Este proyecto es una beta abierta: cada reporte y
cada mejora cuenta. Estas son las pautas para que la colaboración sea fluida.

---

## 🐛 Reportar un bug

Usá la plantilla **"Reporte de bug"** (aparece al pulsar *New issue*). Lo más
importante:

1. **La versión** que usás (Configuración → Actualizaciones). Un bug de la
   2.3.12 puede ya estar arreglado en la 2.3.13.
2. **Los pasos exactos** para reproducirlo (qué pegaste, cuántas conexiones,
   qué botón pulsaste).
3. **El texto del error o el estado del panel** (ej. `Error 404`,
   `Sesión vencida`).
4. **Logs** si podés: la app guarda `errores.log` en su carpeta de datos
   (Información → Abrir carpeta de datos). Pegar las últimas líneas acelera
   muchísimo el diagnóstico.

Antes de crear el issue, buscá si ya existe uno similar y sumate ahí con un
comentario en lugar de duplicar.

## 💡 Proponer una funcionalidad

Usá la plantilla **"Solicitud de funcionalidad"**. Contá **qué querés hacer**
y **qué problema te resuelve** — no hace falta proponer cómo implementarlo.
Las ideas concretas y de un solo tema se discuten mejor que las listas
largas de cambios.

## ❓ Dudas de uso

Las preguntas de instalación, uso o de la extensión de Chrome van en
**Discussions**, no en Issues. Los Issues son para bugs y mejoras concretas.

## 🧑‍💻 Colaborar con código

### Antes de empezar

- Mirá los Issues abiertos: si hay uno etiquetado con `good first issue`, es
  un buen punto de entrada.
- Si querés tocar algo que no está en un issue, abrí uno primero para
  coordinar y evitar trabajo duplicado.

### Cómo se hace un PR

1. **Fork** el repositorio y creá una rama descriptiva:
   `fix/cola-pausa`, `feat/descarga-programada`, etc.
2. Hacé **cambios chicos y enfocados**: un PR por problema.
3. Verificá que la versión siga sincronizada si tocaste
   `electron/package.json` o `extension/manifest.json`:
   ```bash
   node build_mei/sync-version.js
   ```
4. Comprobá que el guard de CI (`check-versiones.yml`) pase sobre tu rama.
   Si el CI falla por versiones, el PR no se puede mergear.
5. Describí en el PR **qué problema resuelve** y **cómo lo probaste**.

### Reglas del proyecto

- **`main` está protegida**: los PRs requieren revisión y el check de CI en
  verde. No se pushea directo a `main`.
- El motor de descargas (`motor.py`) es **solo stdlib** de Python — sin
  dependencias nuevas ahí, a propósito.
- No agregues telemetría ni nada que envíe datos fuera de la PC del usuario:
  es un principio del proyecto.
- El código y los mensajes de commit en español o inglés, lo que te sea más
  cómodo; los comentarios del código, en español (como está hoy).

### Pruebas (cinco suites)

`npm test` corre, en orden, **cinco suites** (si una falla, falla todo):

- **lint** — `scripts/lint.js`: sintaxis de todo el JS propio
  (`node --check`: electron/, extension/, scripts/, test_frontend/) y de los
  .py de la raíz (`py_compile`). Sin dependencias.
- **backend** — `python -m unittest discover -s tests` (unittest, stdlib):
  filtro de selección (`tests/test_seleccion_filtro.py`), subida a Drive
  (`test_drive.py`), extractor KaranPC (`test_karanpc.py`), API HTTP de
  extremo a extremo (`test_api.py`), scheduler de reintentos
  (`test_reintentos.py`), hosters sin red (`test_hosters.py`), empaquetado de
  Electron (`test_electron_package.py`).
- **frontend** — `test_frontend/seleccion.test.js` (jsdom) sobre la lógica
  real de `static/seleccion.js`: que el panel liste solo los servidores
  detectados, que desmarcar una temporada desmarque sus episodios (y
  viceversa) y que resolver la selección solo envíe a `/api/enlaces` los
  episodios y servidores elegidos.
- **updater** — `electron/update_logic.test.js`: lógica determinista del
  aviso de actualización (que `update-available` muestre el aviso), sin red.
- **e2e** — `scripts/e2e_smoke.py`: smoke de extremo a extremo 100% local —
  levanta un servidor de archivos con soporte Range y la API real, descarga
  vía `/api/descargar`, espera `completa` y verifica el sha256 byte a byte.

#### Cómo correrlas

**Con `npm test`** (desde la raíz, las dos suites en orden y un solo
resultado; falla con código != 0 si alguna no pasa):

```bash
npm test
```

Para correr solo una:

```bash
npm run test:backend    # o: npm test -- --backend
npm run test:frontend   # o: npm test -- --frontend
npm test -- --solo backend|frontend
```

**En Windows sin npm/Node:** `ejecutar_pruebas.bat` corre backend y frontend
directamente (backend con `python -m unittest discover -s tests` y frontend
con `node test_frontend/seleccion.test.js`). Si jsdom aún no está instalado
en `test_frontend`, el `.bat` lo instala automáticamente la primera vez.

**En CI (GitHub Actions):** `.github/workflows/pruebas.yml` corre `npm test`
en un solo job (setup-node + setup-python; instala jsdom en `test_frontend`),
disparado en cada **push a `main`**, en cada **PR** y de forma manual
(`workflow_dispatch`). Las cinco suites corren juntas (lint incluida), de
modo que un error de sintaxis no puede llegar a empaquetarse. Si alguna suite
queda roja, el CI falla y el PR no puede mergearse.

**Ejecutar solo una suite:**

```bash
npm test -- --solo lint
npm test -- --solo backend
npm test -- --solo frontend
npm test -- --solo updater
npm test -- --solo e2e
```

**Pre-commit:** el repo activa `npm test` antes de cada commit local (hook en
`.githooks/pre-commit`, activado con `git config core.hooksPath .githooks`;
en un clon nuevo lo configura el script `prepare` de `package.json` tras
`npm install`). Si las pruebas fallan, el commit se cancela. Para omitirlas
puntualmente: `git commit --no-verify`.

### Estructura rápida

```
motor.py             ← motor de descargas segmentadas (solo stdlib)
torrents.py          ← torrents: resolver zetrrent + aria2c
hosters.py           ← extractores de hosters (MediaFire, GoFile, Drive…)
zonaleros_copia.py   ← único módulo ZonaLeros (series/episodios/juegos, CDP)
pivigames.py         ← extractor PiviGames
karanpc.py           ← extractor KaranPC (posts de programas)
catalogo.py          ← catálogo navegable del sitio
cuenta.py            ← sesiones de navegador (cookies para yt-dlp)
drive.py             ← subida a Google Drive (OAuth + resumable)
servidor.py          ← servidor local + API REST
static/index.html    ← interfaz web (panel)
static/seleccion.js  ← lógica del panel selección (temporadas + servidores)
tests/               ← pruebas backend (unittest) + API + empaquetado
test_frontend/       ← pruebas del panel (jsdom, importa seleccion.js)
scripts/             ← run_tests.js (5 suites), lint.js, e2e_smoke.py
extension/           ← extensión de Chrome (MV3)
electron/            ← app de escritorio (Electron + electron-builder)
build_mei/           ← scripts de release (solo local, gitignored)
```

---

## 📦 Publicar un release

Está automatizado en `build_mei/release.js` (no commiteado, vive en local).
Quien mantiene el repo:

```bash
node build_mei/release.js --patch --notes "Resumen de cambios"
```

Esto bumpea `electron/package.json` + `extension/manifest.json`, reconstruye
el backend y el instalador, verifica artefactos, commitea, pushea y publica
el release en `construacero-sistemas/Midescargador`.

## 🙏 Gracias

Cada issue bien reportado y cada PR revisado hace que la beta mejore para
todos. Si el proyecto te sirve, la donación voluntaria también es bienvenida
(Información → Apoyar el proyecto).
