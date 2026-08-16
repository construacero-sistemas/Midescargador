// MiDescargador 2.0 - Content Script
// Detecta elementos <video> en la página e inyecta un botón flotante con estilo Glassmorphism 2.0.

(() => {
  if (window.__midescargador) return;
  window.__midescargador = true;

  const SERVIDOR = "http://127.0.0.1:17890";
  const ESTILO_BOTON = `
    position: fixed;
    z-index: 2147483647;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: rgba(15, 21, 35, 0.88);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    color: #f8fafc;
    border: 1px solid rgba(59, 130, 246, 0.4);
    border-radius: 9999px;
    padding: 8px 14px;
    font: 600 12.5px "Plus Jakarta Sans", "Segoe UI", system-ui, sans-serif;
    cursor: pointer;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45), 0 0 16px rgba(59, 130, 246, 0.25);
    transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    user-select: none;
  `;

  const SVG_ICON = `
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
      <polyline points="7 10 12 15 17 10"></polyline>
      <line x1="12" y1="15" x2="12" y2="3"></line>
    </svg>
  `;

  const videos = new Set();
  let avisoActual = null;

  function avisar(texto, ok) {
    if (avisoActual) avisoActual.remove();
    avisoActual = document.createElement("div");
    avisoActual.innerHTML = `
      <div style="display:flex; align-items:center; gap:8px;">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="${ok ? '#10b981' : '#ef4444'}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          ${ok ? '<polyline points="20 6 9 17 4 12"></polyline>' : '<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line>'}
        </svg>
        <span>${texto}</span>
      </div>
    `;
    Object.assign(avisoActual.style, {
      position: "fixed",
      top: "18px",
      right: "18px",
      zIndex: "2147483647",
      background: "rgba(15, 21, 35, 0.94)",
      backdropFilter: "blur(12px)",
      color: "#f8fafc",
      border: ok ? "1px solid rgba(16, 185, 129, 0.35)" : "1px solid rgba(239, 68, 68, 0.35)",
      borderRadius: "12px",
      padding: "12px 18px",
      font: "600 13px 'Plus Jakarta Sans', 'Segoe UI', sans-serif",
      boxShadow: "0 12px 32px rgba(0, 0, 0, 0.5)",
      maxWidth: "360px",
      animation: "fadeIn 0.2s ease-out"
    });
    document.documentElement.appendChild(avisoActual);
    setTimeout(() => avisoActual && avisoActual.remove(), 4000);
  }

  function urlReal(video) {
    return video.currentSrc || video.src || "";
  }

  function posicionar(boton, video) {
    const r = video.getBoundingClientRect();
    if (!r.width && !r.height) { boton.style.display = "none"; return; }
    boton.style.display = "inline-flex";
    boton.style.left = Math.max(12, r.left + r.width - 165) + "px";
    boton.style.top = Math.max(12, r.top + 14) + "px";
  }

  function botonPara(video) {
    const b = document.createElement("button");
    b.type = "button";
    b.innerHTML = `${SVG_ICON} <span>Descargar</span>`;
    b.setAttribute("style", ESTILO_BOTON);

    b.onmouseenter = () => {
      b.style.transform = "translateY(-2px)";
      b.style.borderColor = "#3b82f6";
      b.style.boxShadow = "0 12px 28px rgba(0, 0, 0, 0.5), 0 0 20px rgba(59, 130, 246, 0.4)";
    };
    b.onmouseleave = () => {
      b.style.transform = "none";
      b.style.borderColor = "rgba(59, 130, 246, 0.4)";
      b.style.boxShadow = "0 8px 24px rgba(0, 0, 0, 0.45), 0 0 16px rgba(59, 130, 246, 0.25)";
    };

    b.onclick = async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const src = urlReal(video);
      b.disabled = true;
      try {
        if (!src) throw new Error("El video no tiene fuente directa accesible");
        const r = await fetch(SERVIDOR + "/api/descargar", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            url: src,
            segmentos: 8,
            carpeta: null,
          }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.error || "El servidor rechazó la descarga");
        avisar("Enviado a MiDescargador: " + (d.id || ""), true);
      } catch (err) {
        try {
          const ok = await chrome.runtime.sendMessage({ tipo: "descargar", url: src });
          if (ok && ok.ok) avisar("Descargado con respaldo del navegador", true);
          else throw new Error("Servidor apagado y navegador no pudo");
        } catch (e2) {
          avisar("No se pudo iniciar la descarga. ¿Servidor encendido? " + err.message, false);
        }
      } finally {
        b.disabled = false;
      }
    };

    document.addEventListener("scroll", () => posicionar(b, video), { passive: true });
    window.addEventListener("resize", () => posicionar(b, video));
    document.documentElement.appendChild(b);
    posicionar(b, video);
    return b;
  }

  function procesar() {
    document.querySelectorAll("video").forEach(v => {
      if (videos.has(v)) return;
      videos.add(v);
      const b = botonPara(v);
      const obs = new ResizeObserver(() => posicionar(b, v));
      obs.observe(v);
      v.addEventListener("loadedmetadata", () => posicionar(b, v), { once: true });
    });
  }

  procesar();
  const mo = new MutationObserver(() => procesar());
  mo.observe(document.documentElement, { childList: true, subtree: true });
  setInterval(() => procesar(), 1500);
})();
