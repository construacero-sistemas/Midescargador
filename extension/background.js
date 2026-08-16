// MiDescargador - service worker (fondo)
// Respaldos de descarga usando chrome.downloads cuando el servidor local no responde.

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.tipo === "descargar" && msg.url) {
    const url = msg.url;
    if (/^blob:/.test(url)) {
      // blob: solo puede descargarlo el navegador desde la página
      sendResponse({ ok: false, error: "blob no soportado aquí" });
      return false;
    }
    try {
      chrome.downloads.download(
        { url, conflictAction: "uniquify", saveAs: false },
        (id) => {
          if (chrome.runtime.lastError) {
            sendResponse({ ok: false, error: chrome.runtime.lastError.message });
          } else {
            sendResponse({ ok: true, id });
          }
        }
      );
      return true; // respuesta asíncrona
    } catch (e) {
      sendResponse({ ok: false, error: String(e) });
      return false;
    }
  }
  return false;
});
