// MiDescargador - service worker (fondo)
// Respaldo de descargas usando chrome.downloads cuando el servidor local no responde.
// También sirve de puente: el content script le delega las peticiones a localhost
// para esquivar Chrome Private Network Access (PNA) que bloquea fetch directo
// desde páginas públicas a 127.0.0.1.

const SERVIDOR = "http://127.0.0.1:17890";

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.tipo) return false;

  // --- descarga directa (respaldo del navegador) ---
  if (msg.tipo === "descargar" && msg.url) {
    const url = msg.url;
    if (/^blob:/.test(url)) {
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

  // --- consultar formatos (puente al servidor local) ---
  if (msg.tipo === "formatos" && msg.url) {
    fetchFormatos(msg.url).then(sendResponse).catch(e => {
      sendResponse({ error: e.message || String(e) });
    });
    return true; // respuesta asíncrona
  }

  // --- descargar con formato específico (puente al servidor local) ---
  if (msg.tipo === "descargar-formato" && msg.url) {
    fetchDescargar(msg.url, msg.formato, msg.segmentos, msg.carpeta)
      .then(sendResponse).catch(e => {
        sendResponse({ error: e.message || String(e) });
      });
    return true;
  }

  return false;
});

async function fetchFormatos(url) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 35000);
  try {
    const r = await fetch(SERVIDOR + "/api/formatos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    const d = await r.json();
    if (d.error) return { error: d.error };
    return { formatos: d.formatos || [] };
  } catch (err) {
    clearTimeout(timer);
    if (err.name === "AbortError") {
      return { error: "El servidor tardó demasiado en responder. Verifica que MiDescargador esté abierto." };
    }
    return { error: "No se pudo conectar al servidor local. Asegúrate de que MiDescargador esté ejecutándose." };
  }
}

async function fetchDescargar(url, formato, segmentos, carpeta) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 10000);
  try {
    const r = await fetch(SERVIDOR + "/api/descargar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, formato, segmentos: segmentos || 8, carpeta }),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) return { error: d.error || "El servidor rechazó la descarga" };
    return { ok: true, id: d.id };
  } catch (err) {
    clearTimeout(timer);
    return { error: "No se pudo conectar al servidor local." };
  }
}
