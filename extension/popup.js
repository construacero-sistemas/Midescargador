// MiDescargador - lógica del popup.
// Script EXTERNO a propósito: la CSP de MV3 (script-src 'self') bloquea los
// scripts inline de las páginas de la extensión (el badge de versión quedaba
// vacío y el estado clavado en "Conectando…"). token.js se carga antes.
const $ = (id) => document.getElementById(id);

// Leer versión del manifest para mantenerla siempre sincronizada
const manifest = chrome.runtime.getManifest();
const ver = manifest.version;
$("version-badge").textContent = ver;

const SERVIDOR = "http://127.0.0.1:17890";

function setStatus(estado, texto) {
  $("status-box").className = "status-box " + estado;
  $("status-text").textContent = texto;
}

function setSesion(d) {
  $("sesion-box").hidden = false;
  const t = $("sesion-text");
  if (d && d.activa) {
    $("sesion-box").className = "sesion activa";
    t.textContent = "Sesión de YouTube activa"
      + (d.edad_min ? " · hace " + d.edad_min + " min" : "");
  } else if (d && d.rotada) {
    $("sesion-box").className = "sesion vencida";
    t.textContent = "Sesión de YouTube vencida — reexportala";
  } else {
    $("sesion-box").className = "sesion";
    t.textContent = "Sin sesión de YouTube (exportala desde el perfil logueado)";
  }
}

// Estado en vivo: servidor + actividad + sesión, todo en paralelo
async function cargarEstado() {
  setStatus("busy", "Conectando…");
  $("stats").hidden = true;
  $("sesion-box").hidden = true;
  try {
    const [v, lote, sesion] = await Promise.all([
      mdmFetch(SERVIDOR + "/api/version", { method: "GET" })
        .then(r => r.ok ? r.json() : null).catch(() => null),
      mdmFetch(SERVIDOR + "/api/lote", { method: "GET" })
        .then(r => r.ok ? r.json() : null).catch(() => null),
      mdmFetch(SERVIDOR + "/api/sesion", { method: "GET" })
        .then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    if (!v) throw new Error("sin servidor");
    setStatus("ok", "Servidor listo · v" + (v.version || "?"));
    if (lote) {
      $("stats").hidden = false;
      $("chip-descargando").classList.toggle("activo", !!lote.descargando);
      $("stat-descargando").textContent = lote.descargando ? "Descargando…" : "En reposo";
      $("stat-cola").textContent = "En cola: " + (lote.pendientes || 0);
    }
    if (sesion && sesion.youtube) setSesion(sesion.youtube);
  } catch (e) {
    setStatus("err", "Servidor apagado");
  }
}
cargarEstado();

// Exportar la sesión de YouTube del perfil actual (chrome.cookies) al
// servidor: Chrome descifra las cookies por nosotros, así que funciona
// incluso con las nuevas v20/App-Bound de Chrome 2025+.
const btnExportar = $("btn-exportar");
const txtExportar = $("btn-exportar-text");
const TEXTO_EXPORTAR = "Exportar sesión de este perfil";
btnExportar.addEventListener("click", async () => {
  if (btnExportar.disabled) return;
  btnExportar.disabled = true;
  txtExportar.textContent = "Leyendo cookies…";
  try {
    const todas = await chrome.cookies.getAll({});
    const relevantes = todas.filter(c =>
      ["google.com", "youtube.com", "googleapis.com"]
        .some(d => (c.domain || "").indexOf(d) !== -1));
    if (!relevantes.length) {
      txtExportar.textContent = "✗ No hay cookies de YouTube/Google en este perfil";
      setTimeout(() => { txtExportar.textContent = TEXTO_EXPORTAR; }, 3500);
      return;
    }
    txtExportar.textContent = "Enviando al servidor…";
    const r = await mdmFetch(SERVIDOR + "/api/sesion/exportar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        plataforma: "youtube",
        cookies: relevantes.map(c => ({
          name: c.name, value: c.value, domain: c.domain, path: c.path,
          secure: c.secure, httpOnly: c.httpOnly,
          expires: c.expirationDate ? Math.floor(c.expirationDate) : 0,
        })),
      }),
    });
    const d = await r.json().catch(() => ({}));
    if (d.activa) {
      txtExportar.textContent = "✓ Sesión exportada y validada por YouTube";
      setSesion({ activa: true, edad_min: 0 });
    } else if (d.rotada) {
      txtExportar.textContent = "✗ Cookies rechazadas por YouTube (vencidas)";
      setSesion({ rotada: true });
    } else if (d.error) {
      txtExportar.textContent = "✗ " + d.error;
    } else {
      txtExportar.textContent = "✗ La sesión quedó incompleta";
    }
  } catch (e) {
    txtExportar.textContent = "✗ Error: " + (e.message || String(e));
  }
  setTimeout(() => { txtExportar.textContent = TEXTO_EXPORTAR; }, 3500);
  setTimeout(() => { btnExportar.disabled = false; }, 400);
});

// Acceso rápido a la carpeta de descargas: el servidor la abre en el
// explorador de archivos del sistema (POST /api/carpeta).
const btnCarpeta = $("btn-carpeta");
const txtCarpeta = $("btn-carpeta-text");
const TEXTO_CARPETA = "Carpeta de descargas";
btnCarpeta.addEventListener("click", async () => {
  if (btnCarpeta.disabled) return;
  btnCarpeta.disabled = true;
  txtCarpeta.textContent = "Abriendo carpeta…";
  try {
    const r = await mdmFetch(SERVIDOR + "/api/carpeta", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    if (d.ok) txtCarpeta.textContent = "✓ Carpeta abierta en el explorador";
    else txtCarpeta.textContent = "✗ " + (d.error || "El servidor no pudo abrirla");
  } catch (e) {
    txtCarpeta.textContent = "✗ Servidor apagado";
  }
  setTimeout(() => { txtCarpeta.textContent = TEXTO_CARPETA; }, 3000);
  setTimeout(() => { btnCarpeta.disabled = false; }, 400);
});

// Modo de captura de enlaces (compartido con content.js vía storage):
//   auto      → el takeover envía todo a MiDescargador sin preguntar
//   preguntar → al clicar un enlace de descarga se elige MiDescargador/navegador
// El cambio aplica EN VIVO en las pestañas abiertas (storage.onChanged).
const selModo = $("modo-captura");
try {
  chrome.storage.local.get("mdm_modo_captura", (d) => {
    selModo.value = (d && d.mdm_modo_captura === "preguntar") ? "preguntar" : "auto";
  });
  selModo.addEventListener("change", () => {
    chrome.storage.local.set({ mdm_modo_captura: selModo.value });
  });
} catch (_e) { /* storage opcional */ }

$("btn-recargar").addEventListener("click", () => {
  const t = $("btn-reload-text");
  try {
    chrome.runtime.reload();
    t.textContent = "✓ Extensión recargada";
  } catch (_) {
    // no-op: reload() no lanza error en MV3 cuando falla
  }
  // Siempre abrir chrome://extensions como respaldo,
  // porque reload() puede fallar silenciosamente
  setTimeout(() => {
    chrome.tabs.create({ url: "chrome://extensions" });
  }, 200);
  t.textContent = "Abriendo chrome://extensions…";
});
