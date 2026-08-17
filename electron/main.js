// MiDescargador - escritorio (Electron)
// Lanza el backend (servidor.exe empaquetado), espera a que responda en el
// puerto 17890 y abre el panel en su propia ventana. Al cerrar la app,
// detiene el servidor. Incluye auto-actualización vía electron-updater
// (solo en la versión instalada; el portable no puede auto-actualizarse).
const { app, BrowserWindow, dialog, shell, Menu, ipcMain } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");
const net = require("net");

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

function enviarEstadoActualizacion(datos) {
  try {
    const win = BrowserWindow.getAllWindows()[0];
    if (win && !win.isDestroyed() && win.webContents) {
      win.webContents.send("updater:status", datos);
      win.webContents.executeJavaScript(`
        if (typeof window.__onUpdateStatus === 'function') {
          window.__onUpdateStatus(${JSON.stringify(datos)});
        }
      `).catch(() => {});
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

  autoUpdater.on("error", (err) => {
    log("error de actualización: " + (err && err.message ? err.message : err));
    enviarEstadoActualizacion({ estado: "error", error: err && err.message ? err.message : String(err) });
  });

  autoUpdater.on("update-available", (info) => {
    log("actualización disponible: " + info.version);
    enviarEstadoActualizacion({
      estado: "disponible",
      version: info.version,
      releaseNotes: info.releaseNotes || null
    });

    const win = BrowserWindow.getAllWindows()[0];
    // Fallback nativo elegante con botones estándar (noLink: true) si el usuario no interactúa en la app
    dialog.showMessageBox(win, {
      type: "info",
      title: "Actualización disponible · MiDescargador",
      message: "Nueva versión disponible: MiDescargador " + info.version,
      detail: "Se han incorporado mejoras de rendimiento y estabilidad en las descargas.\n¿Deseas descargar la actualización ahora en segundo plano?",
      buttons: ["Descargar e instalar", "Más tarde"],
      defaultId: 0,
      cancelId: 1,
      noLink: true,
    }).then(({ response }) => {
      if (response === 0) {
        actualizando = true;
        autoUpdater.downloadUpdate();
      }
    });
  });

  autoUpdater.on("download-progress", (p) => {
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
    log("actualización descargada: " + info.version);
    enviarEstadoActualizacion({
      estado: "lista",
      version: info.version
    });

    const win = BrowserWindow.getAllWindows()[0];
    // Diálogo nativo profesional (noLink: true para evitar los enlaces azules con flecha)
    dialog.showMessageBox(win, {
      type: "info",
      title: "Actualización lista · MiDescargador",
      message: "MiDescargador " + info.version + " está listo para instalarse",
      detail: "La actualización se descargó con éxito.\nReinicia la aplicación para aplicar todos los cambios de inmediato (se reiniciará automáticamente).",
      buttons: ["Reiniciar e instalar ahora", "Instalar al cerrar"],
      defaultId: 0,
      cancelId: 1,
      noLink: true,
    }).then(({ response }) => {
      if (response === 0) {
        autoUpdater.quitAndInstall(false, true);
      }
    });
  });

  // Controladores IPC para responder a acciones desde la UI web
  ipcMain.on("updater:descargar", () => {
    if (autoUpdater) {
      actualizando = true;
      autoUpdater.downloadUpdate();
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
  servidorProc = spawn(exe, [], {
    cwd: path.dirname(exe),
    windowsHide: true,
    stdio: "ignore",
  });
  servidorProc.on("exit", (codigo) => {
    log("backend terminó (código " + codigo + ")");
    if (codigo !== null && codigo !== 0) {
      // arrancó y murió: avisamos en la ventana si existe
      const win = BrowserWindow.getAllWindows()[0];
      if (win) {
        win.loadURL("data:text/html;charset=utf-8," +
          encodeURIComponent(
            "<body style='background:#0b0f19;color:#e2e8f0;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh'>" +
            "<div style='text-align:center'><h2>El servidor local falló al arrancar (código " + codigo + ")</h2>" +
            "<p>Revisa el log en %LOCALAPPDATA%\\MiDescargador\\servidor.log</p></div></body>"));
      }
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
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
      sandbox: false,
    },
  });
  win.loadURL(URL_PANEL);
  // que la ventana no navegue fuera del panel local
  win.webContents.on("will-navigate", (e, url) => {
    if (!url.startsWith("http://127.0.0.1:" + PUERTO)) e.preventDefault();
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  return win;
}

app.whenReady().then(async () => {
  escribirVersionBackend();
  await asegurarServidor();
  crearVentana();
  configurarAutoUpdate();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) crearVentana();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  if (servidorProc && servidorProc.exitCode === null) {
    try { servidorProc.kill(); } catch (e) {}
    log("backend detenido");
  }
});
