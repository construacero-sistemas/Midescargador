// MiDescargador - escritorio (Electron)
// Lanza el backend (servidor.exe empaquetado), espera a que responda en el
// puerto 17890 y abre el panel en su propia ventana. Al cerrar la app,
// detiene el servidor.
const { app, BrowserWindow, dialog, shell } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const net = require("net");

const PUERTO = 17890;
const URL_PANEL = "http://127.0.0.1:" + PUERTO;

function log(msg) {
  try {
    console.log("[MiDescargador] " + msg);
  } catch (e) { /* sin consola en empaquetado */ }
}

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
      sandbox: true,
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
  await asegurarServidor();
  crearVentana();
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
