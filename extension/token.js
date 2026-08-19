// token.js — token de la API local de MiDescargador.
// Lo comparten el service worker (importScripts), el popup (<script>) y el
// content script (manifest). El servidor genera el token al arrancar y lo
// persiste; aquí se pide una vez (GET /api/token — solo accesible a
// procesos locales: sin CORS remoto + verificación de Host) y se cachea
// en chrome.storage.local para sobrevivir recargas del service worker.

const MDM_SERVIDOR = "http://127.0.0.1:17890";
const MDM_TOKEN_KEY = "mdm_api_token";

let _tokenPromise = null;

function _mdmTokenDeStorage() {
  try {
    const p = chrome.storage.local.get(MDM_TOKEN_KEY);
    if (p && p.then) {
      return p.then((d) => (d && d[MDM_TOKEN_KEY]) || null);
    }
  } catch (_e) { /* storage opcional */ }
  return null;
}

function _mdmPedirToken() {
  return fetch(MDM_SERVIDOR + "/api/token", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      const t = (d && d.token) || null;
      if (t) {
        try {
          const p = chrome.storage.local.set({ [MDM_TOKEN_KEY]: t });
          if (p && p.catch) p.catch(() => {});
        } catch (_e) { /* storage opcional */ }
      }
      return t;
    })
    .catch(() => null);
}

function mdmObtenerToken() {
  if (!_tokenPromise) {
    _tokenPromise = _mdmTokenDeStorage().then((t) => {
      if (t) return t;
      return _mdmPedirToken();
    }).catch(() => null);
  }
  return _tokenPromise;
}

// fetch con la cabecera X-MiDescargador-Token puesta. Si el servidor
// responde 401 (token cambiado/borrado), pide uno fresco y reintenta una
// sola vez.
async function mdmFetch(url, opts, _reintento) {
  opts = opts || {};
  const token = await mdmObtenerToken();
  const headers = new Headers(opts.headers || {});
  if (token) headers.set("X-MiDescargador-Token", token);
  const r = await fetch(url, Object.assign({}, opts, { headers }));
  if (r.status === 401 && !_reintento && token) {
    _tokenPromise = null;
    try {
      const p = chrome.storage.local.remove(MDM_TOKEN_KEY);
      if (p && p.catch) p.catch(() => {});
    } catch (_e) { /* storage opcional */ }
    return mdmFetch(url, opts, true);
  }
  return r;
}
