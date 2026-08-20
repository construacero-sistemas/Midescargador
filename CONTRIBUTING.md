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

### Estructura rápida

```
motor.py             ← motor de descargas segmentadas (solo stdlib)
torrents.py          ← torrents: resolver zetrrent + aria2c
servidor.py          ← servidor local + API REST
static/index.html    ← interfaz web (panel)
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
el release en `luigiberaldi-code/Midescargador`.

## 🙏 Gracias

Cada issue bien reportado y cada PR revisado hace que la beta mejore para
todos. Si el proyecto te sirve, la donación voluntaria también es bienvenida
(Información → Apoyar el proyecto).
