/*
 * Lógica del panel de análisis / extracción de series: temporadas + servidores.
 * Extraída del <script> inline de index.html para que sea importable y testeable
 * en Node (jsdom) sin cargar toda la página.
 *
 * Se expone como factory UMD: en el navegador queda en
 * `window.MiDescargadorSeleccion.crearSeleccion(...)`; en Node, en
 * `require("./seleccion.js").crearSeleccion(...)`.
 *
 * Las dependencias se inyectan en `ctx` (así la prueba las sustituye):
 *   - ctx.$            : selector CSS -> Element         (obligatorio)
 *   - ctx.document     : el DOM (para crear/consultar)   (alto)
 *   - ctx.escapeHtml   : función de escape de texto      (obligatorio)
 *   - ctx.t            : (key, vars) -> string i18n
 *   - ctx.fetch        : fetch (alta => window.fetch)
 *   - ctx.alert        : alert (alta => window.alert)
 *   - ctx.mostrarEnlaces: (datos, parcial?) -> void (renderiza el panel final)
 *   - ctx.verEnlaces    : () -> void (extracción normal, ZonaLeros)
 */
(function (global, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    global.MiDescargadorSeleccion = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function crearSeleccion(ctx) {
    ctx = ctx || {};
    const $ = ctx.$ || function (s) { return (ctx.document || document).querySelector(s); };
    const document = ctx.document || (typeof window !== "undefined" ? window.document : null);
    const escapeHtml = ctx.escapeHtml || function (s) { return String(s == null ? "" : s); };
    const t = ctx.t || function (k) { return k; };
    const fetch = ctx.fetch || (typeof window !== "undefined" ? window.fetch : function () {
      return Promise.reject(new Error("fetch no disponible"));
    });
    const alert = ctx.alert || (typeof window !== "undefined" ? window.alert : function () {});
    const verEnlaces = ctx.verEnlaces || function () {};
    const mostrarEnlaces = ctx.mostrarEnlaces || function () {};

    let tareaAnalisisEnlaces = null;
    let datosAnalisisEnlaces = null;

    function renderAnalisisEnlaces(d) {
      const panel = $("#panel-analisis-enlaces");
      if (!panel || !d) return;
      if (d.error) {
        panel.innerHTML = `<div class="error-banner">${escapeHtml(d.error)}</div>`;
        return;
      }
      if (d.tipo !== "serie") {
        panel.innerHTML = `<div class="smart-notice info">${escapeHtml(d.mensaje || "Este enlace se procesará con el flujo normal.")}</div>`;
        return;
      }
      const temporadas = d.temporadas || [];
      // Solo los servidores que la serie usa de verdad (los manda el backend desde
      // el catálogo). Si aún no están detectados, no se pintan casillas: filtro
      // desactivado equivale a "todos los servidores".
      const servidoresPosibles = (d.servidores_posibles && d.servidores_posibles.length) ? d.servidores_posibles : [];
      const servidoresHtml = servidoresPosibles.map(s =>
        `<label style="font-size:12px;"><input type="checkbox" class="analisis-servidor" value="${escapeHtml(s)}"> ${escapeHtml(s)}</label>`
      ).join("");
      const avisoServidores = servidoresPosibles.length
        ? `<span style="font-size:11px;color:var(--text-secondary);">Sin marcar: todos los servidores</span>`
        : `<span style="font-size:11px;color:var(--text-secondary);">Sin servidores detectados: al resolver los enlaces se sabrá cuáles usa esta serie</span>`;
      panel.innerHTML = `
      <div class="links-panel-header">
        <div>
          <div class="links-game-title">${escapeHtml(d.titulo || "Serie")}</div>
          <div style="font-size:12px;color:var(--text-secondary);margin-top:4px;">
            ${temporadas.length} temporadas · ${d.total_episodios || 0} episodios
          </div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button class="btn btn-ghost btn-sm" id="btn-analisis-cancelar">Cancelar</button>
          <button class="btn btn-secondary btn-sm" id="btn-analisis-resolver">Resolver selección</button>
        </div>
      </div>
      <div class="smart-notice info" style="margin-top:10px;">
        Seleccioná las temporadas y los servidores. El filtro se aplicará antes de mostrar y encolar los enlaces finales.
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px;">
        <strong style="font-size:12px;">Servidores:</strong>
        ${servidoresHtml}
        ${avisoServidores}
      </div>
      <div id="analisis-temporadas" style="display:grid;gap:8px;margin-top:12px;"></div>
    `;
      const lista = $("#analisis-temporadas");
      temporadas.forEach((temp, idx) => {
        const id = `analisis-temp-${idx}`;
        const eps = temp.episodios || [];
        const bloque = document.createElement("details");
        bloque.open = idx === 0;
        bloque.innerHTML = `
        <summary style="cursor:pointer;display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--bg-surface-elevated);border:1px solid var(--border-subtle);border-radius:var(--radius-md);">
          <input type="checkbox" class="analisis-temporada" data-id="${escapeHtml(temp.id)}" checked>
          <strong>${escapeHtml(temp.nombre)}</strong><span style="color:var(--text-secondary);">${eps.length} episodios</span>
        </summary>
        <div style="padding:8px 12px 4px 34px;display:grid;gap:5px;max-height:220px;overflow:auto;">
          ${eps.map(e => `<label style="font-size:12px;color:var(--text-secondary);"><input type="checkbox" class="analisis-episodio" data-temp="${escapeHtml(temp.id)}" data-url="${encodeURIComponent(e.url || "")}" checked> ${escapeHtml(e.label || "Episodio")}</label>`).join("")}
        </div>`;
        const casillaTemp = bloque.querySelector(".analisis-temporada");
        casillaTemp.addEventListener("change", () => {
          bloque.querySelectorAll(".analisis-episodio").forEach(e => { e.checked = casillaTemp.checked; });
        });
        lista.appendChild(bloque);
      });
      $("#btn-analisis-cancelar").onclick = cancelarAnalisisEnlaces;
      $("#btn-analisis-resolver").onclick = resolverSeleccionAnalisis;
    }

    async function cancelarAnalisisEnlaces() {
      if (tareaAnalisisEnlaces) {
        try { await fetch("/api/enlaces/escaneo/cancelar", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({tarea:tareaAnalisisEnlaces})}); } catch (_) {}
      }
      tareaAnalisisEnlaces = null;
      $("#panel-analisis-enlaces").style.display = "none";
      $("#btn-enlaces").disabled = false;
    }

    async function resolverSeleccionAnalisis() {
      const episodios = [...document.querySelectorAll(".analisis-episodio:checked")].map(x => decodeURIComponent(x.dataset.url || "")).filter(Boolean);
      if (!episodios.length) { alert("Seleccioná al menos un episodio."); return; }
      const servidores = [...document.querySelectorAll(".analisis-servidor:checked")].map(x => x.value).filter(Boolean);
      $("#panel-analisis-enlaces").style.display = "none";
      datosAnalisisEnlaces = null;
      await verEnlacesSeleccionados(episodios, servidores);
    }

    async function verEnlacesSeleccionados(urls, servidores) {
      const panel = $("#panel-enlaces");
      panel.style.display = "block";
      panel.innerHTML = `<div class="empty-state" style="padding:30px 20px"><div class="empty-title">Resolviendo selección…</div><div class="empty-desc">Se procesarán ${urls.length} episodios y solo los servidores elegidos.</div></div>`;
      try {
        const r = await fetch("/api/enlaces", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({url: urls[0], seleccion:{urls, servidores}})});
        const d = await r.json();
        if (d.error) { panel.innerHTML = `<div class="error-banner">${escapeHtml(d.error)}</div>`; return; }
        if (d.servidores) { mostrarEnlaces(d); return; }
        if (!d.tarea) { panel.innerHTML = `<div class="error-banner">${t("enlaces.respuesta")}</div>`; return; }
        const ini = Date.now();
        while (true) {
          await new Promise(res => setTimeout(res, 1500));
          const rr = await fetch("/api/enlaces/estado?tarea=" + encodeURIComponent(d.tarea));
          const e = await rr.json();
          if (e.parcial && e.parcial.servidores) mostrarEnlaces(e.parcial, true);
          if (e.estado === "listo") { if (e.resultado && !e.resultado.error) mostrarEnlaces(e.resultado); else panel.innerHTML = `<div class="error-banner">${escapeHtml((e.resultado || {}).error || t("enlaces.sinresultados"))}</div>`; break; }
        }
      } catch (e) { panel.innerHTML = `<div class="error-banner">${escapeHtml(e.message)}</div>`; }
    }

    async function analizarEnlaceNormal() {
      const url = $("#url").value.trim();
      if (!url) return verEnlaces();
      const panel = $("#panel-analisis-enlaces");
      panel.style.display = "block";
      panel.innerHTML = `<div class="empty-state" style="padding:24px"><div class="empty-title">Analizando enlace…</div><div class="empty-desc">Obteniendo temporadas y episodios sin resolver todavía todos los servidores.</div></div>`;
      try {
        const r = await fetch("/api/enlaces/escaneo", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({url})});
        const d = await r.json();
        if (d.error) { renderAnalisisEnlaces(d); return; }
        if (d.tipo === "flujo_actual") return verEnlaces();
        tareaAnalisisEnlaces = d.tarea;
        while (tareaAnalisisEnlaces) {
          await new Promise(res => setTimeout(res, 1200));
          const rr = await fetch("/api/enlaces/estado?tarea=" + encodeURIComponent(tareaAnalisisEnlaces));
          const e = await rr.json();
          if (e.estado === "listo") { tareaAnalisisEnlaces = null; datosAnalisisEnlaces = e.resultado; renderAnalisisEnlaces(e.resultado); break; }
          if (e.estado === "cancelando") break;
        }
      } catch (e) {
        renderAnalisisEnlaces({error: "No se pudo completar el análisis: " + e.message});
      }
    }

    return {
      renderAnalisisEnlaces,
      cancelarAnalisisEnlaces,
      resolverSeleccionAnalisis,
      verEnlacesSeleccionados,
      analizarEnlaceNormal,
    };
  }

  return { crearSeleccion };
});