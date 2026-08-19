// MiDescargador 3.0 - Content Script
// Overlay estilo IDM: al pasar el ratón sobre un <video> aparece una pequeña
// pestaña con "Descargar"; al pulsarla consulta al servidor local las
// resoluciones disponibles (yt-dlp) y muestra un menú para elegir calidad.
//
// Detección por POSICIÓN del ratón (no por mouseenter del <video>): muchos
// sitios (Instagram Reels, TikTok...) cubren el video con overlays de clics
// o le ponen pointer-events:none, con lo que el video nunca recibe el
// evento. Comprobamos si el cursor cae dentro del rectángulo del video.

(() => {
  if (window.__midescargador) return;
  window.__midescargador = true;

  const SERVIDOR = "http://127.0.0.1:17890";
  // sitios cuyo <video> no sirve la URL real (YouTube, TikTok...): para esos
  // se usa la URL de la PÁGINA, que es la que yt-dlp sabe resolver con
  // todas las calidades.
  const SITIOS_PAGINA = /youtube\.com|youtu\.be|tiktok\.com|instagram\.com|instagr\.am|facebook\.com|fb\.watch|twitter\.com|x\.com|reddit\.com|pinterest\.com|threads\.net|vk\.com|rumble\.com|kick\.com|twitch\.tv|vimeo\.com|dailymotion\.com|soundcloud\.com|bilibili\.com|t\.co/i;
  // la captura de enlaces (takeover de clics, tecla Insert) solo aporta en
  // el frame principal: los iframes (ads, widgets) no la necesitan
  const esFrameTop = window.self === window.top;

  const ovs = new Map(); // video -> overlay
  let avisoActual = null;
  let videoActivo = null; // video cuya pestaña está visible ahora

  const CSS = `
    .mdm-ov {
      position: fixed;
      z-index: 2147483647;
      display: none;
      flex-direction: column;
      min-width: 198px;
      font: 500 13px "Segoe UI", system-ui, sans-serif;
      user-select: none;
      pointer-events: auto;
    }
    .mdm-ov.dentro { display: flex; }
    .mdm-btn {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: rgba(16, 22, 38, 0.92);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      color: #f2f5fa;
      border: 1px solid rgba(77, 141, 255, 0.55);
      border-radius: 9px;
      padding: 8px 16px;
      font: 600 13px "Segoe UI", system-ui, sans-serif;
      cursor: pointer;
      box-shadow: 0 6px 18px rgba(0,0,0,0.45);
      transition: background 0.15s, transform 0.15s;
      white-space: nowrap;
    }
    .mdm-btn:hover { background: rgba(26, 36, 62, 0.95); transform: translateY(-1px); }
    .mdm-btn:disabled { opacity: 0.6; cursor: wait; }
    .mdm-menu {
      margin-top: 5px;
      background: rgba(16, 22, 38, 0.97);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid rgba(77, 141, 255, 0.4);
      border-radius: 10px;
      box-shadow: 0 12px 32px rgba(0,0,0,0.55);
      overflow: hidden;
      max-height: 320px;
      overflow-y: auto;
    }
    .mdm-opc {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 9px 14px;
      color: #e8edf6;
      cursor: pointer;
      white-space: nowrap;
      border-bottom: 1px solid rgba(255,255,255,0.06);
    }
    .mdm-opc:last-child { border-bottom: none; }
    .mdm-opc:hover { background: rgba(77, 141, 255, 0.18); }
    .mdm-opc .mdm-tam { margin-left: auto; color: #8fa3c8; font-size: 12px; }
    .mdm-cab {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 9px 14px;
      color: #93a8cc;
      font-size: 12px;
      background: rgba(255,255,255,0.04);
    }
    .mdm-err {
      padding: 10px 12px;
      color: #ff8f8f;
      font-size: 11.5px;
      line-height: 1.4;
      max-width: 240px;
    }
    .mdm-svg { flex-shrink: 0; display: inline-flex; }
    .mdm-chip {
      position: fixed;
      z-index: 2147483647;
      top: 14px;
      left: 50%;
      transform: translateX(-50%);
      display: none;
      align-items: center;
      gap: 8px;
      background: rgba(16, 22, 38, 0.95);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      color: #f2f5fa;
      border: 1px solid rgba(77, 141, 255, 0.55);
      border-radius: 999px;
      padding: 8px 16px;
      font: 600 13px "Segoe UI", system-ui, sans-serif;
      box-shadow: 0 6px 18px rgba(0, 0, 0, 0.5);
      pointer-events: none;
      white-space: nowrap;
    }
  `;
  const estilo = document.createElement("style");
  estilo.textContent = CSS;
  (document.head || document.documentElement).appendChild(estilo);

  const SVG = (color) => `
    <svg class="mdm-svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
      <polyline points="7 10 12 15 17 10"></polyline>
      <line x1="12" y1="15" x2="12" y2="3"></line>
    </svg>`;

  function avisar(texto, ok) {
    if (avisoActual) avisoActual.remove();
    avisoActual = document.createElement("div");
    const fila = document.createElement("div");
    fila.style.cssText = "display:flex; align-items:center; gap:8px; color:#f8fafc;";
    const icono = document.createElement("span");
    icono.style.color = ok ? "#10b981" : "#ef4444";
    icono.style.fontWeight = "800";
    icono.textContent = ok ? "✓" : "✕";
    const msj = document.createElement("span");
    msj.textContent = texto;
    fila.appendChild(icono);
    fila.appendChild(msj);
    avisoActual.appendChild(fila);
    Object.assign(avisoActual.style, {
      position: "fixed", top: "18px", right: "18px", zIndex: "2147483647",
      background: "rgba(15, 21, 35, 0.95)", backdropFilter: "blur(12px)",
      border: ok ? "1px solid rgba(16,185,129,0.35)" : "1px solid rgba(239,68,68,0.35)",
      borderRadius: "10px", padding: "11px 16px",
      font: "600 13px 'Segoe UI', sans-serif",
      boxShadow: "0 12px 32px rgba(0,0,0,0.5)", maxWidth: "380px",
    });
    document.documentElement.appendChild(avisoActual);
    setTimeout(() => avisoActual && avisoActual.remove(), 4000);
  }

  // URL a enviar: la de la página si el sitio la necesita (YouTube...),
  // si no la fuente real del <video>.
  function urlParaEnviar(video) {
    const src = video.currentSrc || video.src || "";
    if (src && !/^blob:/.test(src)) return src;
    if (SITIOS_PAGINA.test(location.href)) return location.href;
    return src;
  }

  function fmtTam(n) {
    if (!n || n <= 0) return "";
    if (n >= 1073741824) return " · " + (n / 1073741824).toFixed(1) + " GB";
    if (n >= 1048576) return " · " + (n / 1048576).toFixed(0) + " MB";
    return "";
  }

  // --- detección por posición ---------------------------------------------

  function esVisible(v) {
    if (!v.isConnected) return false;
    const r = v.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    const cs = getComputedStyle(v);
    return cs.display !== "none" && cs.visibility !== "hidden" &&
           cs.opacity !== "0" && parseFloat(cs.opacity || "1") > 0.05;
  }

  // Devuelve el video bajo el punto (x, y), o null.
  function videoEnPunto(x, y) {
    let el = null;
    try { el = document.elementFromPoint(x, y); } catch (e) { el = null; }

    // si hay un diálogo/menú de la página abierto encima del video, no molestar
    if (el) {
      const dlg = el.closest("[role='dialog'], [role='menu'], [data-testid='dialog']");
      if (dlg && !dlg.querySelector("video")) return null;
    }

    // 1) el elemento real bajo el cursor es el video o un hijo suyo
    let n = el;
    while (n && n !== document.documentElement) {
      if (n.tagName === "VIDEO" && ovs.has(n)) return n;
      n = n.parentElement;
    }

    // 2) fallback por rectángulo: el cursor está dentro de un video visible
    //    aunque un overlay (click-catcher, pointer-events:none) lo cubra.
    let mejor = null, mejorArea = Infinity;
    for (const v of ovs.keys()) {
      if (!esVisible(v)) continue;
      const r = v.getBoundingClientRect();
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
        const a = r.width * r.height;
        if (a < mejorArea) { mejorArea = a; mejor = v; }
      }
    }
    return mejor;
  }

  function posicionar(ov, video) {
    const r = video.getBoundingClientRect();
    if (!r.width && !r.height) return;
    ov.style.left = Math.max(8, r.left + r.width - 215) + "px";
    ov.style.top = Math.max(8, r.top + 8) + "px";
  }

  // Prefetch: al pasar el cursor sobre el video, pedimos las calidades en
  // segundo plano (sin mostrar nada). Cuando el usuario pulsa "Calidad de
  // descarga", el servidor ya lo tiene en caché o en curso → respuesta
  // casi instantánea en vez de esperar los ~3-4 s de yt-dlp en ese momento.
  // La promesa se guarda para que el clic reutilice la misma consulta.
  const prefetched = new Map();   // url -> Promise<formatos>
  const prefetchTimers = new Map(); // url -> timeout
  function prefetchCalidades(v) {
    const url = urlParaEnviar(v);
    if (!url || prefetched.has(url)) return;
    // prefetch solo donde yt-dlp agrega valor (misma regla que
    // prefetchPrincipal): en un .mp4 directo no hay calidades que listar y
    // la consulta sería gasto inútil; el clic igual consulta bajo demanda.
    if (!SITIOS_PAGINA.test(location.href)) return;
    // debounce 350 ms: si el cursor solo pasa rozando el video (scroll por
    // feeds), no disparamos yt-dlp; solo se consulta si el usuario se
    // detiene sobre él.
    const prev = prefetchTimers.get(url);
    if (prev) clearTimeout(prev);
    prefetchTimers.set(url, setTimeout(() => {
      prefetchTimers.delete(url);
      if (prefetched.has(url)) return;
      // se guarda la promesa SIN tragar el error: el menú que la reutilice
      // verá el rechazo y mostrará el mensaje; el catch vacío evita el
      // "unhandled rejection" cuando el prefetch muere solo.
      const p = consultarFormatos(url);
      p.catch(() => {});
      prefetched.set(url, p);
    }, 350));
  }

  function mostrar(v) {
    const ov = ovs.get(v);
    if (!ov || !esVisible(v)) return;
    ov.style.display = "";
    posicionar(ov, v);
    ov.classList.add("dentro");
    prefetchCalidades(v);
  }

  function ocultar(v) {
    const ov = ovs.get(v);
    if (!ov) return;
    ov.classList.remove("dentro");
    const m = ov.querySelector(".mdm-menu");
    if (m) m.remove();
  }

  function ocultarTodos() {
    for (const v of ovs.keys()) ocultar(v);
    videoActivo = null;
  }

  let raf = null;
  function alMover(x, y) {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = null;

      // ¿el cursor está sobre nuestra propia pestaña/menú? → mantener visible
      let sobreOverlay = null;
      for (const [v, ov] of ovs) {
        const r = ov.getBoundingClientRect();
        if (r.width && r.height &&
            x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
          sobreOverlay = v;
          break;
        }
      }

      const destino = sobreOverlay || videoEnPunto(x, y);
      if (destino !== videoActivo) {
        if (videoActivo) ocultar(videoActivo);
        videoActivo = destino;
      }
      if (destino) mostrar(destino);
    });
  }

  // --- menú y descarga ----------------------------------------------------

  // Consultar formatos: primero vía background service worker (evita Chrome PNA),
  // luego fallback directo al servidor.
  async function consultarFormatos(url) {
    // 1) Intentar a través del service worker (bypass de Private Network Access)
    try {
      const resp = await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("timeout")), 38000);
        chrome.runtime.sendMessage({ tipo: "formatos", url }, (r) => {
          clearTimeout(timeout);
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
          } else {
            resolve(r);
          }
        });
      });
      if (resp && resp.error) throw new Error(resp.error);
      return (resp && resp.formatos) || [];
    } catch (e) {
      // 2) Fallback: fetch directo al servidor
      try {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 30000);
        const r = await fetch(SERVIDOR + "/api/formatos", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url }),
          signal: ctrl.signal,
        });
        clearTimeout(timer);
        const d = await r.json();
        if (d.error) throw new Error(d.error);
        return d.formatos || [];
      } catch (err2) {
        // Si ambos fallan, dar el error más informativo
        if (err2.name === "AbortError") {
          throw new Error("El servidor tardó demasiado en responder.");
        }
        if (err2.message === "Failed to fetch" || err2.message.includes("NetworkError")) {
          throw new Error("No se pudo conectar al servidor. Asegúrate de que MiDescargador esté abierto.");
        }
        throw err2;
      }
    }
  }

  // Fila de opción del menú construida SIN innerHTML para el texto: la
  // etiqueta puede venir del título del video (el servidor la interpola en
  // /api/formatos) y podría contener HTML. El SVG es una cadena estática
  // segura; todo texto dinámico va con textContent.
  function crearFilaOpcion(texto, tamano) {
    const opc = document.createElement("div");
    opc.className = "mdm-opc";
    opc.insertAdjacentHTML("afterbegin", SVG("#7dd3fc"));
    const et = document.createElement("span");
    et.textContent = texto;
    opc.appendChild(et);
    if (tamano) {
      const t = document.createElement("span");
      t.className = "mdm-tam";
      t.textContent = tamano;
      opc.appendChild(t);
    }
    return opc;
  }

  function construirMenu(ov, video, url) {
    const menu = document.createElement("div");
    menu.className = "mdm-menu";
    const cab = document.createElement("div");
    cab.className = "mdm-cab";
    cab.insertAdjacentHTML("afterbegin", SVG("#5b8cff"));
    const cabTexto = document.createElement("span");
    cabTexto.textContent = "Calidad de descarga";
    cab.appendChild(cabTexto);
    menu.appendChild(cab);
    const spinner = document.createElement("div");
    spinner.className = "mdm-opc";
    spinner.textContent = "Consultando calidades…";
    menu.appendChild(spinner);
    ov.appendChild(menu);

    // reutiliza el prefetch del hover/principal si sigue en curso; si ese
    // prefetch falló (servidor caído al cargar la página, etc.), reintenta
    // con una consulta nueva en lugar de mostrar el error viejo.
    const promesa = (prefetched.get(url) || consultarFormatos(url))
      .catch(() => consultarFormatos(url));
    promesa.then(lista => {
      menu.innerHTML = "";
      menu.appendChild(cab);
      // el servidor devuelve también el tamaño real simulado del "Mejor
      // calidad" (marcado con f.mejor); el resto son las alturas concretas
      const mejor = lista.find(f => f.mejor);
      const opciones = [
        { formato: null, etiqueta: "Mejor calidad (recomendada)",
          tamano: mejor ? mejor.tamano : null },
        ...lista.filter(f => !f.mejor).map(f => ({
          formato: f.formato, etiqueta: f.etiqueta, tamano: f.tamano,
        })),
      ];
      if (!lista.length) {
        const opc = crearFilaOpcion("Descargar video");
        opc.onclick = () => enviarDescarga(url, null, ov);
        menu.appendChild(opc);
        return;
      }
      opciones.forEach(o => {
        const opc = crearFilaOpcion(o.etiqueta, o.tamano ? fmtTam(o.tamano) : "");
        opc.onclick = () => enviarDescarga(url, o.formato, ov);
        menu.appendChild(opc);
      });
    }).catch(err => {
      menu.innerHTML = "";
      const e = document.createElement("div");
      e.className = "mdm-err";
      e.textContent = "No se pudieron listar calidades: " + err.message +
        (SITIOS_PAGINA.test(location.href) ? "" : " Se descargará el video directo.");
      menu.appendChild(e);
      // descarga directa como respaldo (el servidor decide con yt-dlp)
      const opc = crearFilaOpcion("Descargar de todos modos");
      opc.onclick = () => enviarDescarga(url, null, ov);
      menu.appendChild(opc);
    });
  }

  async function enviarDescarga(url, formato, ov) {
    ov.style.display = "none";
    // la pestaña se ocultó pero el ratón puede seguir sobre el video: si no
    // se limpia videoActivo, el próximo mousemove la ve "activa" y nunca
    // reaparece (hay que sacar y volver a meter el ratón).
    videoActivo = null;
    // 1) Intentar a través del service worker (evita Chrome PNA)
    try {
      const resp = await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("timeout")), 15000);
        chrome.runtime.sendMessage(
          { tipo: "descargar-formato", url, formato, segmentos: 8, carpeta: null },
          (r) => {
            clearTimeout(timeout);
            if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
            else resolve(r);
          });
      });
      if (resp && resp.ok) {
        avisar("Enviado a MiDescargador (" + (resp.id || "") + ")", true);
        return;
      }
      if (resp && resp.error) throw new Error(resp.error);
    } catch (e1) {
      // 2) Fallback: fetch directo
      try {
        const r = await fetch(SERVIDOR + "/api/descargar", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url, segmentos: 8, carpeta: null, formato }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.error || "El servidor rechazó la descarga");
        avisar("Enviado a MiDescargador (" + (d.id || "") + ")", true);
        return;
      } catch (err) {
        // 3) Último recurso: descarga directa del navegador
        try {
          const ok = await chrome.runtime.sendMessage({ tipo: "descargar", url });
          if (ok && ok.ok) avisar("Descargado con respaldo del navegador", true);
          else throw new Error("El servidor local no responde");
        } catch (e2) {
          avisar("No se pudo iniciar la descarga: " + err.message, false);
        }
      }
    }
  }

  function overlayPara(video) {
    const ov = document.createElement("div");
    ov.className = "mdm-ov";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mdm-btn";
    btn.innerHTML = `${SVG("#5b8cff")} <span>Descargar</span>`;

    btn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const url = urlParaEnviar(video);
      if (!url) { avisar("El video no tiene fuente accesible", false); return; }
      if (ov.querySelector(".mdm-menu")) { ov.querySelector(".mdm-menu").remove(); return; }
      construirMenu(ov, video, url);
    };

    ov.appendChild(btn);

    // reposicionar al hacer scroll/redimensionar mientras esté visible
    document.addEventListener("scroll", () => {
      if (ov.classList.contains("dentro")) posicionar(ov, video);
    }, { passive: true });
    window.addEventListener("resize", () => {
      if (ov.classList.contains("dentro")) posicionar(ov, video);
    });
    document.documentElement.appendChild(ov);
    return ov;
  }

  // Prefetch del video PRINCIPAL de la página (sin esperar el hover): el
  // video visible más grande. Cubre YouTube/SPA — al cambiar de video (o de
  // página) la URL cambia y el prefetch se dispara solo, sin repetir por el
  // mismo video. La guarda `prefetched`/`prefetchTimers` de prefetchCalidades
  // evita duplicados con el prefetch del hover.
  let ultimaPrincipalUrl = null;
  function prefetchPrincipal() {
    // solo donde yt-dlp agrega valor: en una página con un .mp4 directo no
    // hay calidades que listar y la consulta sería gasto inútil
    if (!SITIOS_PAGINA.test(location.href)) return;
    let mejor = null, mejorArea = 0;
    for (const v of ovs.keys()) {
      if (!esVisible(v)) continue;
      const r = v.getBoundingClientRect();
      const a = r.width * r.height;
      if (a > mejorArea) { mejorArea = a; mejor = v; }
    }
    if (!mejor) return;
    const url = urlParaEnviar(mejor);
    if (!url || url === ultimaPrincipalUrl) return;
    ultimaPrincipalUrl = url;
    prefetchCalidades(mejor);
  }

  function procesar() {
    document.querySelectorAll("video").forEach(v => {
      if (ovs.has(v)) return;
      const ov = overlayPara(v);
      ovs.set(v, ov);
      const obs = new ResizeObserver(() => {
        if (ov.classList.contains("dentro")) posicionar(ov, v);
      });
      obs.observe(v);
      v.addEventListener("loadedmetadata", () => {
        if (ov.classList.contains("dentro")) posicionar(ov, v);
      }, { once: true });
    });

    // limpiar videos que la página eliminó (SPA: YouTube, Instagram...)
    for (const [v, ov] of ovs) {
      if (!v.isConnected) { ov.remove(); ovs.delete(v); }
    }

    prefetchPrincipal();
  }

  // ================== arranque perezoso de overlays ==================
  // La maquinaria de overlays (listeners de ratón, MutationObserver y
  // sondeo) solo se activa cuando el frame tiene un <video>: en frames sin
  // video (ads, widgets, páginas de texto) no se crea ningún overlay ni se
  // escucha nada. Si el video aparece después (reproductor que carga tarde,
  // SPA), un chequeo barato la enciende sobre la marcha.
  let overlaysActivos = false;
  function arrancarOverlays() {
    if (overlaysActivos) return;
    overlaysActivos = true;
    document.addEventListener("mousemove", (e) => {
      alMover(e.clientX, e.clientY);
    }, { passive: true });
    document.addEventListener("mouseleave", ocultarTodos);
    procesar();
    const mo = new MutationObserver(() => procesar());
    mo.observe(document.documentElement, { childList: true, subtree: true });
    setInterval(() => procesar(), 1500);
  }
  function vigilarVideo() {
    if (document.querySelector("video")) { arrancarOverlays(); return; }
    const t = setInterval(() => {
      if (document.querySelector("video")) {
        clearInterval(t);
        arrancarOverlays();
      }
    }, 2000);
  }
  vigilarVideo();

  // ================== captura de enlaces (estilo IDM) ==================
  // Solo en el frame principal: los iframes no interceptan clics ni arman
  // el modo fuerza (Insert). Los overlays de video sí corren en iframes con
  // <video> (reproductores embebidos como el de YouTube).
  if (esFrameTop) {
  // - Clic en enlace de archivo (o host conocido): takeover automático hacia
  //   el servidor local, salvo que la URL esté en la lista de exclusiones.
  // - Tecla Insert: arma el modo "forzar takeover" (4 s o hasta el clic): el
  //   próximo clic en cualquier enlace/video se envía, aunque esté excluido.
  // - ALT + clic: bypass explícito (descarga normal del navegador).
  const EXT_DESCARGA = /\.(zip|rar|7z|tar|gz|bz2|xz|iso|exe|msi|apk|pdf|docx?|xlsx?|pptx?|epub|mp4|mkv|webm|avi|mov|mp3|flac|wav|m4a|aac|ogg|opus|torrent|dmg|deb|rpm|pkg)(\?|#|$)/i;
  // hosts que el servidor maneja aunque la URL no tenga extensión de archivo
  const HOSTS_DESCARGA = /(mediafire\.com\/file\/|mega\.nz\/file\/|mega\.co\.nz\/file\/|drive\.google\.com\/(file\/d\/|open\?id=|uc\?)|1fichier\.com)/i;

  function esEnlaceDescarga(url) {
    return EXT_DESCARGA.test(url) || HOSTS_DESCARGA.test(url);
  }

  let fuerzaActiva = false;
  let fuerzaTimer = null;
  let chip = null;

  function mostrarChip(texto) {
    if (!chip) {
      chip = document.createElement("div");
      chip.className = "mdm-chip";
      (document.body || document.documentElement).appendChild(chip);
    }
    chip.textContent = texto;
    chip.style.display = "flex";
  }
  function ocultarChip() {
    if (chip) chip.style.display = "none";
  }
  function desarmarFuerza() {
    fuerzaActiva = false;
    clearTimeout(fuerzaTimer);
    ocultarChip();
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Insert") {
      fuerzaActiva = true;
      mostrarChip("Descargar con MiDescargador — haz clic en el enlace (Esc cancela)");
      clearTimeout(fuerzaTimer);
      fuerzaTimer = setTimeout(desarmarFuerza, 4000);
    } else if (e.key === "Escape") {
      desarmarFuerza();
    }
  });

  function enviarCaptura(url, origen) {
    desarmarFuerza();
    chrome.runtime.sendMessage({ tipo: "capturar", url, origen }, (r) => {
      if (chrome.runtime.lastError) {
        mostrarChip("No se pudo contactar a MiDescargador");
        setTimeout(ocultarChip, 2500);
        return;
      }
      if (r && r.error) {
        mostrarChip("MiDescargador: " + recortarLocal(r.error, 60));
        setTimeout(ocultarChip, 3000);
      } else if (r && r.ok) {
        mostrarChip("✓ Enviado a MiDescargador");
        setTimeout(ocultarChip, 1800);
      }
    });
  }
  function recortarLocal(s, n) {
    s = String(s || "");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  document.addEventListener("click", (e) => {
    if (e.altKey) return; // bypass explícito: descarga normal del navegador
    const a = e.target.closest ? e.target.closest("a[href]") : null;
    const objetivo = a && a.href ? a.href : null;
    if (objetivo && objetivo !== location.href) {
      if (fuerzaActiva) {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        enviarCaptura(objetivo, "fuerza");
        return;
      }
      // takeover automático: solo enlaces de archivo/hosts, fuera de las
      // exclusiones y sin pisar los sitios de video (usan el overlay de
      // calidades)
      if (!SITIOS_PAGINA.test(objetivo)
          && esEnlaceDescarga(objetivo)
          && !mdmUrlExcluida(objetivo)) {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        enviarCaptura(objetivo, "auto");
      }
      return;
    }
    // video/audio directo de la página (solo con fuerza explícita)
    if (fuerzaActiva) {
      const m = e.target.closest ? e.target.closest("video,audio") : null;
      const src = m && (m.currentSrc || m.src);
      if (src) {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        enviarCaptura(src, "fuerza");
      }
    }
  }, true);
  } // fin: captura de enlaces (solo frame principal)
})();
