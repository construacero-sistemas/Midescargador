// MiDescargador - Electron Preload Script
// Expone de forma segura la API del actualizador y eventos del sistema al panel web.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  isElectron: true,
  onUpdaterStatus: (callback) => {
    ipcRenderer.on("updater:status", (_, data) => {
      if (typeof callback === "function") callback(data);
    });
  },
  descargarActualizacion: () => {
    ipcRenderer.send("updater:descargar");
  },
  instalarActualizacion: () => {
    ipcRenderer.send("updater:instalar");
  },
  comprobarActualizaciones: () => {
    ipcRenderer.send("updater:comprobar");
  }
});
