// MiDescargador - escritorio (Electron)
// Lanza el backend (servidor.exe empaquetado), espera a que responda en el
// puerto 17890 y abre el panel en su propia ventana. Modo bandeja: la X puede
// ocultar la ventana y la app sigue corriendo en el tray; «Salir» detiene el
// servidor y su árbol de descargas. Autostart: arranca con Windows (--hidden,
// sin ventana) para que las descargas continúen al encender la PC. Incluye
// auto-actualización vía electron-updater (solo en la versión instalada; el
// portable no puede auto-actualizarse).
const { app, BrowserWindow, dialog, shell, Menu, ipcMain, Tray, nativeImage, Notification } = require("electron");
const updateLogic = require("./update_logic");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");
const net = require("net");
const crypto = require("crypto");

// Cache del updater (misma carpeta que electron-updater): %LOCALAPPDATA%\
// midescargador-updater. Ahí vive installer.exe (el Setup ya descargado).
function dirUpdaterCache() {
  const base = process.env.LOCALAPPDATA
    || path.join(os.homedir(), "AppData", "Local");
  return path.join(base, "midescargador-updater");
}

function sha512Base64(ruta) {
  return new Promise((res, rej) => {
    const h = crypto.createHash("sha512");
    const s = fs.createReadStream(ruta);
    s.on("data", (d) => h.update(d));
    s.on("end", () => res(h.digest("base64")));
    s.on("error", rej);
  });
}

const PUERTO = 17890;
const URL_PANEL = "http://127.0.0.1:" + PUERTO;

// La versión real de la app vive en package.json (Electron), pero el backend
// es un proceso aparte que no puede leer el app.asar. Al arrancar, Electron
// escribe un version.json compartido que el backend consulta para el badge.
function escribirVersionBackend() {
  try {
    const dir = path.join(process.env.LOCALAPPDATA || os.tmpdir(), "MiDescargador");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "version.json"),
      JSON.stringify({ version: app.getVersion() }));
  } catch (e) { /* no crítico */ }
}

function log(msg) {
  try {
    console.log("[MiDescargador] " + msg);
  } catch (e) { /* sin consola en empaquetado */ }
}

// ---- preferencias de la app (modo bandeja) ----
// Se persisten en %LOCALAPPDATA%\MiDescargador\preferencias.json para que
// sobrevivan reinicios. minimizarAlCerrar viene APAGADO por defecto (cerrar
// la ventana apaga la app, como siempre); se activa SOLO desde el menú del
// tray: al pulsar la X la ventana se oculta y la app sigue en la bandeja con
// el backend y las descargas corriendo. avisoBandejaVisto = aviso único.
function rutaPreferencias() {
  return path.join(
    process.env.LOCALAPPDATA || app.getPath("appData"),
    "MiDescargador", "preferencias.json");
}

let minimizarAlCerrar = false;
let avisoBandejaVisto = false;

function cargarPreferencias() {
  try {
    const d = JSON.parse(fs.readFileSync(rutaPreferencias(), "utf8"));
    if (typeof d.minimizarAlCerrar === "boolean") minimizarAlCerrar = d.minimizarAlCerrar;
    if (typeof d.avisoBandejaVisto === "boolean") avisoBandejaVisto = d.avisoBandejaVisto;
  } catch (e) { /* primera ejecución: defaults */ }
}

function guardarPreferencias() {
  try {
    fs.mkdirSync(path.dirname(rutaPreferencias()), { recursive: true });
    fs.writeFileSync(rutaPreferencias(), JSON.stringify({
      minimizarAlCerrar: minimizarAlCerrar,
      avisoBandejaVisto: avisoBandejaVisto
    }));
  } catch (e) { log("no se pudo guardar preferencias: " + (e && e.message)); }
}

// ---- iniciar con Windows (autostart) ----
// Se registra en el registro de Windows (HKCU\...\Run) vía setLoginItemSettings:
// al encender la PC la app arranca sola con --hidden (sin ventana, directo al
// tray) para que el backend retome las descargas. El estado lo guarda Windows
// mismo; solo se ofrece en la app instalada (en dev registraría electron.exe,
// que no es la app).
let iniciarConWindows = false;

function leerAutostart() {
  try {
    if (app.isPackaged) {
      iniciarConWindows = app.getLoginItemSettings().openAtLogin === true;
    }
  } catch (e) { log("no se pudo leer autostart: " + (e && e.message)); }
}

function aplicarAutostart(activar) {
  try {
    if (!app.isPackaged) return;   // en dev no registrar electron.exe
    if (activar) {
      app.setLoginItemSettings({
        openAtLogin: true,
        path: process.execPath,
        args: ["--hidden"],   // arranque silencioso: la ventana queda en el tray
      });
    } else {
      app.setLoginItemSettings({ openAtLogin: false });
    }
    iniciarConWindows = !!activar;
  } catch (e) { log("no se pudo configurar autostart: " + (e && e.message)); }
}

// ---- modo bandeja (tray) ----
// saliendo = quit real (Salir del tray, actualización, cierre de sesión de
// Windows): en ese caso NO se intercepta el cierre de la ventana.
let saliendo = false;
let tray = null;
let arranqueOculto = false;   // lanzado por autostart con --hidden

function ventanaPrincipal() {
  return BrowserWindow.getAllWindows()[0];
}

let ultimoChequeoUpdate = 0;      // marca de tiempo del último checkForUpdates
let ultimoAvisoEnviado = null;    // último estado "disponible"/"lista" reenviado a la UI

// Comprueba actualizaciones de forma controlada: solo si hace más de un rato
// desde la última comprobación (para no golpear GitHub en cada show/focus) y
// reaparece el aviso pendiente si ya se detectó una versión nueva.
function verificarActualizacionAlMostrar() {
  if (!autoUpdater) return;
  const ahora = Date.now();
  // si ya hay una actualización detectada (disponible o lista) y no se le
  // re-avisó a la UI en esta sesión de ventana, reenviar el estado para que
  // el pill/modal vuelva a aparecer aunque la ventana estuviera oculta.
  if (updateInfoActual && ultimoAvisoEnviado !== updateInfoActual.version) {
    ultimoAvisoEnviado = updateInfoActual.version;
    const estado = updateLogic.estadoAlMostrar(updateInfoActual, null);
    if (estado) enviarEstadoActualizacion(estado);
  }
  // comprobación nueva: como mucho una vez cada 30 min (además del arranque
  // y del intervalo de 4 h). Así, al abrir la app desde la bandeja o el
  // autostart oculto se detecta una versión nueva casi al instante.
  if (ahora - ultimoChequeoUpdate > 30 * 60 * 1000) {
    ultimoChequeoUpdate = ahora;
    autoUpdater.checkForUpdates().catch(() => {});
  }
}

function mostrarVentana() {
  const win = ventanaPrincipal();
  if (!win) { crearVentana(); return; }
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
  verificarActualizacionAlMostrar();
}

// clic izquierdo del tray: muestra/oculta la ventana
function alternarVentana() {
  const win = ventanaPrincipal();
  if (!win) { crearVentana(); return; }
  if (win.isVisible() && win.isFocused()) {
    win.hide();
  } else {
    mostrarVentana();
  }
}

// Al hacer doble clic en el tray o volver a la ventana, comprobar
// actualizaciones pendientes (bandeja / autostart oculto).
function comprobarAlRestaurar() {
  verificarActualizacionAlMostrar();
}

// aviso único la primera vez que se oculta a la bandeja, para que nadie
// piense que la app se cerró sola
function avisarBandeja() {
  try {
    const titulo = "MiDescargador";
    const cuerpo = "Sigue en segundo plano: su icono quedó en la barra de tareas (junto al reloj). Las descargas continúan. Para apagarla, usa «Salir» en ese icono.";
    if (tray && typeof tray.displayBalloon === "function") {
      tray.displayBalloon({ title: titulo, content: cuerpo });
    } else {
      new Notification({ title: titulo, body: cuerpo }).show();
    }
  } catch (e) { /* sin notificaciones: no es crítico */ }
}

function crearTray() {
  try {
    tray = new Tray(nativeImage.createFromPath(path.join(__dirname, "icon.ico")));
    tray.setToolTip("MiDescargador");
    actualizarMenuTray();
    tray.on("click", alternarVentana);
    tray.on("double-click", () => { mostrarVentana(); });
  } catch (e) {
    log("no se pudo crear el tray: " + (e && e.message));
  }
}

function actualizarMenuTray() {
  if (!tray) return;
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Abrir MiDescargador", click: mostrarVentana },
    { type: "separator" },
    {
      label: "Minimizar al cerrar a la bandeja",
      type: "checkbox",
      checked: minimizarAlCerrar,
      click: (item) => {
        minimizarAlCerrar = item.checked;
        guardarPreferencias();
      }
    },
    ...(app.isPackaged ? [{
      label: "Iniciar con Windows",
      type: "checkbox",
      checked: iniciarConWindows,
      click: (item) => aplicarAutostart(item.checked)
    }] : []),
    { type: "separator" },
    { label: "Salir", click: () => app.quit() }
  ]));
}

// Al relanzar tras una actualización, la app hereda el stdout del instalador;
// cuando ese pipe se cierra, cualquier console.* lanza EPIPE y Electron muestra
// el diálogo "A JavaScript error occurred in the main process". Lo evitamos:
// (1) asignando un logger seguro al autoUpdater y (2) ignorando excepciones
// de pipe roto en el proceso principal.
const logSeguro = (msg) => log(msg);
const loggerUpdater = {
  info: logSeguro,
  warn: logSeguro,
  error: logSeguro,
  debug: logSeguro,
};

process.on("uncaughtException", (err) => {
  if (err && (err.code === "EPIPE" || err.code === "ECONNRESET")) {
    // pipe de consola roto tras relanzar desde el instalador: irrelevante
    return;
  }
  try {
    console.error("[MiDescargador] excepción no capturada:", err);
  } catch (e) { /* sin consola */ }
});

// ---- single instance: dos ventanas de la app usarían dos backends ----
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const win = BrowserWindow.getAllWindows()[0];
    if (win) {
      if (win.isMinimized()) win.restore();
      win.show();   // la ventana puede estar oculta en la bandeja
      win.focus();
    }
  });
}

// ¿Es un portable (self-extracting)? electron-updater no puede actualizarlo.
const esPortable = !!process.env.PORTABLE_EXECUTABLE_DIR;

// ---- auto-actualización (solo versión instalada) ----
let autoUpdater = null;
if (app.isPackaged && !esPortable) {
  const { autoUpdater: au } = require("electron-updater");
  autoUpdater = au;
  autoUpdater.autoDownload = false; // preguntamos antes de descargar
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.logger = loggerUpdater; // nunca escriba directo a console
}

let actualizando = false;
let updateInfoActual = null;       // info del update-available
let tokenDescarga = null;          // CancellationToken de la descarga en curso
let ultimoProgreso = { t: 0, bytes: 0 };
let watchdog = null;
let reintentosDescarga = 0;

// builder-util-runtime suele quedar ANIDADO bajo electron-updater en el asar
// (dependencia de electron-updater), así que el require directo desde main.js
// falla con "Cannot find module 'builder-util-runtime'" y la descarga de
// actualizaciones nunca arrancaba (el handler moría antes de downloadUpdate).
// Lo resolvemos desde la carpeta de electron-updater, que siempre lo tiene.
function nuevoTokenCancelacion() {
  try {
    const base = path.dirname(require.resolve("electron-updater"));
    const { CancellationToken } = require(require.resolve("builder-util-runtime", { paths: [base] }));
    return new CancellationToken();
  } catch (e) {
    log("sin CancellationToken (descarga sin cancelación por watchdog): " + (e && e.message));
    return null;
  }
}

// electron-updater NO reintenta ni corta una descarga que se queda sin
// bytes (GitHub lento o conexión cortada): el modal quedaba congelado en
// un porcentaje para siempre. Este watchdog detecta la descarga congelada
// (>75s sin avanzar), la cancela y la reintenta (hasta 3 veces); si agota,
// avisa con un error en vez de quedarse colgado.
// Intervalos configurables (para pruebas aceleradas; en producción son los
// defaults: chequear cada 15 s, congelada si no avanza en 75 s, 3 s de
// pausa entre reintentos).
const WD_CHECK_MS = Number(process.env.MIDESC_WD_CHECK_MS || 15000);
const WD_SIN_AVANCE_MS = Number(process.env.MIDESC_WD_SIN_AVANCE_MS || 75000);
const WD_PAUSA_MS = Number(process.env.MIDESC_WD_PAUSA_MS || 3000);

function iniciarWatchdog() {
  pararWatchdog();
  ultimoProgreso = { t: Date.now(), bytes: 0 };
  watchdog = setInterval(() => {
    const ahora = Date.now();
    const sinAvance = ahora - ultimoProgreso.t;
    if (sinAvance > WD_SIN_AVANCE_MS && tokenDescarga) {
      log("descarga congelada " + Math.round(sinAvance / 1000) + "s sin progreso — reintentando");
      reintentosDescarga++;
      if (reintentosDescarga > 3) {
        pararWatchdog();
        const t = tokenDescarga;
        tokenDescarga = null;
        try { t.cancel(); } catch (e) {}
        log("descarga cancelada tras 4 intentos");
        enviarEstadoActualizacion({
          estado: "error",
          error: "La descarga de la actualización se cortó 4 veces (red lenta o inestable). Reintentá más tarde."
        });
        return;
      }
      const t = tokenDescarga;
      tokenDescarga = null;
      try { t.cancel(); } catch (e) {}
      setTimeout(() => {
        if (autoUpdater) {
          log("reintentando descarga (intento " + reintentosDescarga + "/4)");
          tokenDescarga = nuevoTokenCancelacion();
          autoUpdater.downloadUpdate(tokenDescarga || undefined).catch(() => {});
          ultimoProgreso = { t: Date.now(), bytes: 0 };
        }
      }, WD_PAUSA_MS);
    }
  }, WD_CHECK_MS);
}

function pararWatchdog() {
  if (watchdog) {
    clearInterval(watchdog);
    watchdog = null;
  }
}

// Si el instalador de la versión nueva YA está completo en el cache del
// updater (descarga anterior que terminó), no descargarlo de nuevo: se
// ahorra los ~200 MB y el tiempo de GitHub.
// electron-updater solo reutiliza el cache si encuentra pending/update-info.json
// (con el sha512) y el Setup dentro de pending/; por eso lo sembramos acá
// y después dejamos que downloadUpdate() lo detecte y salte la descarga.
async function sembrarInstaladorCacheado() {
  try {
    if (!updateInfoActual || !updateInfoActual.files
        || !updateInfoActual.files.length) return false;
    const fileInfo = updateInfoActual.files[0];
    const esperado = (fileInfo.sha512 || "").replace(/^base64:/, "");
    if (!esperado) return false;
    const inst = path.join(dirUpdaterCache(), "installer.exe");
    if (!fs.existsSync(inst)) return false;
    const h = await sha512Base64(inst);
    if (h !== esperado) return false;
    // nombre del archivo como lo espera electron-updater (basename de la URL
    // del Setup: p. ej. MiDescargador-Setup-2.3.1.exe)
    let nombre = "installer.exe";
    try {
      const u = new URL(fileInfo.url);
      const base = path.basename(u.pathname);
      if (base && base.includes(".exe")) nombre = base;
    } catch (e) { /* url rara: queda installer.exe */ }
    const pending = path.join(dirUpdaterCache(), "pending");
    fs.mkdirSync(pending, { recursive: true });
    const destino = path.join(pending, nombre);
    if (!fs.existsSync(destino) || (await sha512Base64(destino)) !== esperado) {
      fs.copyFileSync(inst, destino);
    }
    fs.writeFileSync(path.join(pending, "update-info.json"), JSON.stringify({
      fileName: nombre,
      sha512: fileInfo.sha512,
      isAdminRightsRequired: fileInfo.isAdminRightsRequired === true
    }));
    log("instalador ya descargado y válido (sha512 OK): " + nombre);
    return true;
  } catch (e) {
    log("no se pudo reutilizar el instalador cacheado: " + (e && e.message));
    return false;
  }
}

function enviarEstadoActualizacion(datos) {
  // Un solo canal: el preload (contextBridge) expone onUpdaterStatus sobre
  // IPC. Antes además se inyectaba window.__onUpdateStatus por
  // executeJavaScript — un canal duplicado que disparaba la UI dos veces.
  try {
    const win = BrowserWindow.getAllWindows()[0];
    if (win && !win.isDestroyed() && win.webContents) {
      win.webContents.send("updater:status", datos);
    }
  } catch (e) { /* ventana cerrándose */ }
}

function configurarAutoUpdate() {
  if (!autoUpdater) return;

  autoUpdater.on("checking-for-update", () => {
    log("buscando actualizaciones...");
    enviarEstadoActualizacion({ estado: "comprobando" });
  });

  autoUpdater.on("update-not-available", (info) => {
    log("sin actualizaciones");
    enviarEstadoActualizacion({ estado: "al-dia", version: info && info.version ? info.version : app.getVersion() });
  });

  autoUpdater.on("update-available", (info) => {
    updateInfoActual = info;
    log("actualización disponible: " + info.version);
    enviarEstadoActualizacion(updateLogic.estadoUpdateDisponible(info));
  });

  autoUpdater.on("download-progress", (p) => {
    ultimoProgreso = { t: Date.now(), bytes: p ? p.transferred : 0 };
    const progreso = Math.floor(p && p.percent ? p.percent : 0);
    if (progreso % 5 === 0) {
      log("descargando actualización: " + progreso + "%");
    }
    enviarEstadoActualizacion({
      estado: "descargando",
      progreso: progreso,
      velocidad: p ? p.bytesPerSecond : 0,
      transferido: p ? p.transferred : 0,
      total: p ? p.total : 0
    });
  });

  autoUpdater.on("update-downloaded", (info) => {
    pararWatchdog();
    tokenDescarga = null;
    log("actualización descargada: " + info.version);
    enviarEstadoActualizacion({
      estado: "lista",
      version: info.version
    });
  });

  autoUpdater.on("error", (err) => {
    // error por cancelación del watchdog: no es un fallo real, se reintenta
    pararWatchdog();
    tokenDescarga = null;
    log("error de actualización: " + (err && err.message ? err.message : err));
    enviarEstadoActualizacion({ estado: "error", error: err && err.message ? err.message : String(err) });
  });

  // Controladores IPC para responder a acciones desde la UI web
  ipcMain.on("updater:descargar", async () => {
    if (!autoUpdater) return;
    // si la versión nueva ya está descargada (Setup completo en cache),
    // sembrar el estado que electron-updater reconoce para que salte la
    // descarga de ~200 MB (y deje listo quitAndInstall)
    const reutilizado = await sembrarInstaladorCacheado();
    actualizando = true;
    reintentosDescarga = 0;
    tokenDescarga = nuevoTokenCancelacion();
    iniciarWatchdog();
    autoUpdater.downloadUpdate(tokenDescarga || undefined).catch(() => {});
    if (reutilizado) {
      pararWatchdog();
    }
  });

  ipcMain.on("updater:instalar", () => {
    if (autoUpdater) {
      autoUpdater.quitAndInstall(false, true);
    }
  });

  ipcMain.on("updater:comprobar", () => {
    if (autoUpdater) {
      autoUpdater.checkForUpdates().catch(() => {});
    }
  });

  // comprobar al arrancar (tras unos segundos) y cada 4 horas
  setTimeout(() => autoUpdater.checkForUpdates().catch(() => {}), 10000);
  setInterval(() => autoUpdater.checkForUpdates().catch(() => {}), 4 * 60 * 60 * 1000);

  // menú para forzar la comprobación (Alt muestra la barra)
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: "MiDescargador",
      submenu: [
        { label: "Buscar actualizaciones…", click: () => autoUpdater.checkForUpdates().catch(() => {}) },
        { type: "separator" },
        { role: "quit", label: "Salir" },
      ],
    },
    { role: "editMenu" },
  ]));
}

function rutaBackend() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "backend", "servidor", "servidor.exe");
  }
  return path.join(__dirname, "..", "backend", "servidor", "servidor.exe");
}

let servidorProc = null;
let relanzandoBackend = false;

function puertoAbierto() {
  return new Promise((resolve) => {
    const s = net.connect({ port: PUERTO, host: "127.0.0.1" });
    const fin = (ok) => { try { s.destroy(); } catch (e) {} resolve(ok); };
    s.on("connect", () => fin(true));
    s.on("error", () => fin(false));
    s.setTimeout(1500);
    s.on("timeout", () => fin(false));
  });
}

// Si el backend ya está corriendo (Iniciar.bat), nos conectamos a él;
// si no, lo lanzamos.
async function asegurarServidor() {
  if (await puertoAbierto()) {
    log("backend ya estaba corriendo en :" + PUERTO);
    return true;
  }
  const exe = rutaBackend();
  if (!fs.existsSync(exe)) {
    dialog.showErrorBox("MiDescargador",
      "No se encontró el backend:\n" + exe + "\n\nReinstala la aplicación.");
    return false;
  }
  log("lanzando backend: " + exe);
  // El stderr del backend (tracebacks de Python) se guarda en un log para
  // diagnosticar crashes: con stdio:"ignore" un fallo del servidor quedaba
  // sin rastro y el panel simplemente dejaba de responder.
  const stderrLog = path.join(
    process.env.LOCALAPPDATA || app.getPath("appData"),
    "MiDescargador", "backend-stderr.log");
  servidorProc = spawn(exe, [], {
    cwd: path.dirname(exe),
    windowsHide: true,
    stdio: ["ignore", "ignore", "pipe"],
  });
  servidorProc.stderr.on("data", (d) => {
    try {
      fs.appendFileSync(stderrLog, "[" + new Date().toLocaleString() + "] " + d);
    } catch (e) {}
  });
  servidorProc.on("exit", (codigo) => {
    log("backend terminó (código " + codigo + ")");
    if (relanzandoBackend) return;
    if (codigo !== null && codigo !== 0) {
      // El backend murió con la app abierta: lo relanzamos solo (el panel
      // muestra "servidor no responde" mientras tanto y se recupera solo).
      // Antes esto dejaba el panel congelado hasta reiniciar la app.
      relanzandoBackend = true;
      const win = BrowserWindow.getAllWindows()[0];
      setTimeout(async () => {
        const ok = await asegurarServidor();
        relanzandoBackend = false;
        log("backend relanzado: " + ok);
        if (ok && win && !win.isDestroyed()) {
          try { win.reload(); } catch (e) {}
        }
      }, 1500);
    }
  });
  // espera hasta ~30 s a que responda
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 500));
    if (await puertoAbierto()) {
      log("backend respondiendo");
      return true;
    }
    if (servidorProc.exitCode !== null) {
      log("backend murió al arrancar");
      return false;
    }
  }
  log("el backend no respondió a tiempo");
  return false;
}

function crearVentana() {
  const win = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 940,
    minHeight: 600,
    title: "MiDescargador",
    autoHideMenuBar: true,
    backgroundColor: "#0b0f19",
    // icono propio en la ventana (dev; el instalado lo toma del .exe)
    icon: path.join(__dirname, "icon.png"),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
      sandbox: false,
    },
  });
  win.loadURL(URL_PANEL);
  if (arranqueOculto) win.hide();   // autostart: arranca sin ventana (tray)
  // que la ventana no navegue fuera del panel local
  win.webContents.on("will-navigate", (e, url) => {
    if (!url.startsWith("http://127.0.0.1:" + PUERTO)) e.preventDefault();
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  // cerrar (X) con el modo bandeja activo: la ventana se oculta y la app
  // sigue corriendo en el tray. Solo «Salir» (o una actualización) la cierra
  // de verdad; saliendo lo marca before-quit.
  win.on("close", (e) => {
    if (!saliendo && minimizarAlCerrar) {
      e.preventDefault();
      win.hide();
      if (!avisoBandejaVisto) {
        avisoBandejaVisto = true;
        guardarPreferencias();
        avisarBandeja();
      }
    }
  });
  return win;
}

app.whenReady().then(async () => {
  // AppUserModelID para que las notificaciones de Windows salgan con el
  // nombre/icono de MiDescargador (no el de Electron).
  try { app.setAppUserModelId("com.midescargador.desktop"); } catch (e) {}
  arranqueOculto = process.argv.includes("--hidden");   // autostart silencioso
  cargarPreferencias();   // antes de crearVentana: el close handler la lee
  leerAutostart();        // estado del registro de Windows para el menú del tray
  escribirVersionBackend();
  await asegurarServidor();
  crearVentana();
  crearTray();
  configurarAutoUpdate();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) crearVentana();
    else mostrarVentana();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

// Cierre de sesión de Windows / apagado: dejar pasar el cierre (no ocultar a
// la bandeja ni bloquear el apagado) y terminar limpio.
app.on("session-end", () => {
  saliendo = true;
  app.quit();
});

app.on("before-quit", () => {
  saliendo = true;   // cierre real: dejar pasar el close de la ventana
  if (servidorProc && servidorProc.exitCode === null) {
    // Mata el backend y su árbol de hijos (aria2c, yt-dlp): si solo se
    // mataba el backend, sus descargas seguían corriendo huérfanas.
    try { servidorProc.kill(); } catch (e) {}
    try {
      require("child_process").execFileSync(
        "taskkill", ["/PID", String(servidorProc.pid), "/T", "/F"],
        { stdio: "ignore" });
    } catch (e) {}
    log("backend detenido (árbol completo)");
  }
});
