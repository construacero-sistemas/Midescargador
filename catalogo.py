# -*- coding: utf-8 -*-
"""Catálogo ZonaLeros: descarga las fichas de TODO el sitio (juegos,
películas y series) en carpetas por ítem, con reanudación automática.

Estructura en disco:
  <root>/juegos/<Juego>/        portada.<ext> + descripcion.txt + enlaces.txt + requisitos.txt
  <root>/peliculas/<Película>/  portada.<ext> + descripcion.txt + enlaces.txt
  <root>/series/<Serie>/<Episodio>/  portada.<ext> + descripcion.txt + enlaces.txt

Reanudación: progreso.json guarda el estado de cada ítem
(pendiente/hecho/error/descartado). Al relanzar solo se procesan los
pendientes y los errores con reintentos disponibles; los hechos se saltan
(no se repite contenido). Cada carpeta completa lleva un marcador .hecho.
"""
import os
import re
import time
import json
import queue
import base64
import threading
import urllib.parse

import zonaleros_copia as zc

CATEGORIAS = ("juegos", "peliculas", "series")
INDICES = {
    "juegos": "https://www.zona-leros.com/juegos-espanhol-m",
    "peliculas": "https://www.zona-leros.com/peliculas-hd-online-lati",
    "series": "https://www.zona-leros.com/series-hd",
}
_PESTANAS = 3            # pestanas que procesan items a la vez
_MAX_PAGINA = 300
_MAX_REINTENTOS = 3      # reintentos antes de descartar un item que falla

# Orden en que la cola procesa categorías: los episodios (baratos, muchos) y
# las series primero, los juegos (el grueso) al final. Antes la cola era el
# orden de inserción y miles de juegos dejaban películas/series sin tocar
# durante días.
_PRIORIDAD_COLA = {"series_ep": 0, "series": 1, "peliculas": 2, "juegos": 3}

# Bitácora persistente: además de la memoria, cada línea se agrega a
# <root>/catalogo.log para que el registro sobreviva reinicios de la app
# (antes era solo en memoria y el panel mostraba "no hay registros" tras
# abrir de nuevo).
_RUTA_LOG = None
_RUTA_LOG_MAX = 1_000_000   # recorte del log en disco (bytes)


def _sanitizar(nombre):
    """Nombre de carpeta valido en Windows."""
    nombre = re.sub(r'[\/:*?"<>|]+', " ", nombre or "")
    nombre = re.sub(r"\s+", " ", nombre).strip(" .")
    return nombre or "sin-titulo"


_JS_ENLACES_INDICE = {
    "juegos": r'''(() => {
        const s = new Set();
        for (const a of document.querySelectorAll("a[href]")) {
            const m = (a.href || "").match(/zona-leros\.com\/juegos-pc\/([^\/?#]+)/);
            if (m && !/^(genero|nivel)$/.test(m[1])) s.add(a.href.split("#")[0]);
        }
        return [...s];
    })()''',
    "peliculas": r'''(() => {
        const s = new Set();
        for (const a of document.querySelectorAll("a[href]")) {
            const m = (a.href || "").match(/zona-leros\.com\/peliculas\/([^\/?#]+)/);
            if (m && m[1] !== "genero") s.add(a.href.split("#")[0]);
        }
        return [...s];
    })()''',
    "series": r'''(() => {
        const s = new Set();
        for (const a of document.querySelectorAll("a[href]")) {
            const m = (a.href || "").match(/zona-leros\.com\/series\/([^\/?#]+)/);
            if (m && !/^(genero|episode)$/.test(m[1]))
                s.add(a.href.split("#")[0]);
        }
        return [...s];
    })()''',
}


_BITACORA = []
_BITACORA_LOCK = threading.Lock()
_MAX_BITACORA = 500

def _log(mensaje):
    """Registro en memoria + append a catalogo.log (si ya hay carpeta)."""
    linea = "[%s] %s" % (time.strftime("%H:%M:%S"), mensaje)
    with _BITACORA_LOCK:
        _BITACORA.append(linea)
        if len(_BITACORA) > _MAX_BITACORA:
            del _BITACORA[:len(_BITACORA) - _MAX_BITACORA]
    try:
        if _RUTA_LOG:
            if os.path.exists(_RUTA_LOG) and \
                    os.path.getsize(_RUTA_LOG) > _RUTA_LOG_MAX:
                with open(_RUTA_LOG, "w", encoding="utf-8") as f:
                    f.write(linea + "\n")
            else:
                with open(_RUTA_LOG, "a", encoding="utf-8") as f:
                    f.write(linea + "\n")
    except Exception:
        pass   # el log en disco es lo de menos
    return linea

class Catalogo:
    def __init__(self, root):
        self.root = root
        self._lock = threading.Lock()
        self._progreso = {"items": {}}
        self._ruta_progreso = os.path.join(root, "progreso.json")
        self._detener = False
        self._hilo = None
        self._estado = "inactivo"  # inactivo|enumerando|procesando|pausado|terminado|error
        self._ultimo_item = ""
        self._t_media_item = 60.0  # media movil de segundos por item (estimar)
        global _RUTA_LOG
        _RUTA_LOG = os.path.join(root, "catalogo.log")
        self._cargar()

    # ---------------- progreso / estado ----------------
    def _cargar(self):
        try:
            with open(self._ruta_progreso, encoding="utf-8") as f:
                p = json.load(f)
            if isinstance(p, dict) and isinstance(p.get("items"), dict):
                self._progreso = p
        except Exception:
            self._progreso = {"items": {}}
        self._progreso.setdefault("items", {})
        self._progreso.setdefault("hosters_serie", {})
        self._progreso.setdefault("enumerado", {})

    # ---------------- hosters detectados por serie ----------------
    def registrar_hosters(self, url, hosters):
        """Acumula los hosters reales detectados de una serie (por URL de su
        página) en progreso.json. Se fusiona: repasar la serie con otro
        hoster agrega, nunca borra. Persiste en disco."""
        if not url or not hosters:
            return
        con = [str(x).strip() for x in hosters if x and str(x).strip()]
        if not con:
            return
        with self._lock:
            mapa = self._progreso.setdefault("hosters_serie", {})
            actual = set(mapa.get(url, []) or [])
            actual.update(con)
            mapa[url] = sorted(actual)
            self._guardar()

    def hosters_de(self, url):
        """Lista de hosters detectados de la serie (URL de su página) o None
        si aún no se han registrado hosters para ella."""
        if not url:
            return None
        with self._lock:
            mapa = self._progreso.get("hosters_serie") or {}
            datos = mapa.get(url) or []
            return list(datos) if datos else None

    def _guardar(self):
        try:
            os.makedirs(self.root, exist_ok=True)
            tmp = self._ruta_progreso + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._progreso, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._ruta_progreso)
        except Exception:
            pass

    def _es_pendiente(self, it):
        st = it.get("estado", "pendiente")
        if st == "pendiente":
            return True
        if st == "error":
            return it.get("reintentos", 0) < _MAX_REINTENTOS
        return False

    def corriendo(self):
        return self._hilo is not None and self._hilo.is_alive()

    def estado_publico(self):
        with self._lock:
            items = self._progreso.get("items", {})

            def conteo(cat):
                total = hecho = err = desc = 0
                for it in items.values():
                    if it.get("cat") != cat:
                        continue
                    total += 1
                    st = it.get("estado", "pendiente")
                    if st == "hecho":
                        hecho += 1
                    elif st == "error":
                        err += 1
                    elif st == "descartado":
                        desc += 1
                return {"total": total, "hecho": hecho,
                        "error": err, "descartado": desc}

            j = conteo("juegos")
            p = conteo("peliculas")
            s = conteo("series")
            ep = conteo("series_ep")
            pend = sum(1 for it in items.values() if self._es_pendiente(it))
            r = {
                "estado": self._estado,
                "juegos": j,
                "peliculas": p,
                "series": {"total": s["total"], "hecho": s["hecho"],
                           "episodios": ep},
                "pendientes": pend,
                "estimado_seg": int(pend * self._t_media_item),
                "ultimo_item": self._ultimo_item,
                "ruta": self.root,
            }
        con = dict(r)
        with _BITACORA_LOCK:
            con["logs"] = list(_BITACORA)
        return con

    # ---------------- control ----------------
    def iniciar(self, revisar=False):
        if self.corriendo():
            _log("ya hay una corrida en curso")
            return
        self._detener = False
        _log("iniciando catálogo")
        if revisar:
            # revisar re-recorre TODOS los índices: limpiar las marcas para
            # que ninguna categoría se salte aunque ya estuviera enumerada
            with self._lock:
                self._progreso["enumerado"] = {}
                self._guardar()
        self._hilo = threading.Thread(
            target=self._correr, kwargs={"revisar": bool(revisar)}, daemon=True)
        self._hilo.start()

    def pausar(self):
        self._detener = True
        _log("pausa solicitada")

    def _correr(self, revisar):
        self._estado = "enumerando"
        try:
            ws, err = zc._lanzar(INDICES["juegos"], tiempo_max=60)
            if err:
                self._estado = "error"
                _log("ERROR al lanzar Chrome: %s" % err)
                return
            cdp = None
            try:
                cdp = zc._Cdp(ws)
                # enumerar SIEMPRE las categorías que falten: antes solo se
                # enumeraba si no había items, así que una corrida cortada a
                # mitad (p. ej. antes de llegar a series) dejaba esa categoría
                # en 0 para siempre aunque reanudaras mil veces.
                self._enumerar(cdp, self._categorias_a_enumerar(revisar))
                if not self._detener:
                    self._procesar(cdp, ws)
            finally:
                if cdp:
                    try:
                        cdp.cerrar()
                    except Exception:
                        pass
                zc._finalizar()
        except Exception as e:
            self._estado = "error"
            _log("ERROR: %s: %s" % (type(e).__name__, e))
            self._guardar()
            return
        if self._detener:
            self._estado = "pausado"
            _log("catálogo pausado")
        else:
            self._estado = "terminado"
            _log("catálogo terminado")
        self._guardar()

    # ---------------- enumeracion del catalogo ----------------
    def _categorias_a_enumerar(self, revisar=False):
        """Categorías cuyo índice falta recorrer: las ya marcadas como
        enumeradas se saltan (reanudar es barato); con revisar=True se
        re-recorren todas. La marca se guarda en progreso.json."""
        if revisar:
            return list(CATEGORIAS)
        with self._lock:
            hechos = self._progreso.get("enumerado") or {}
            faltantes = [c for c in CATEGORIAS if not hechos.get(c)]
        # las categorías que interesan primero: si una corrida anterior quedó
        # cortada, series se enumera antes que re-escanear todo el índice de
        # juegos
        return sorted(faltantes, key=lambda c: _PRIORIDAD_COLA.get(c, 9))

    def _enumerar(self, cdp, categorias=None):
        for cat in (categorias or list(CATEGORIAS)):
            if self._detener:
                break
            base = INDICES[cat]
            pagina = 1
            sin_nuevos = 0
            completa = False
            while pagina <= _MAX_PAGINA:
                if self._detener:
                    break
                url_idx = base + ("?page=%d" % pagina if pagina > 1 else "")
                if not cdp.navegar(
                        url_idx,
                        condicion="location.href.indexOf('zona-leros.com') !== -1"
                                  " && !/un momento/i.test(document.title)",
                        tiempo_max=60):
                    sin_nuevos += 1
                    if sin_nuevos >= 2:
                        completa = True   # el índice se acabó (no hay más páginas)
                        break
                    pagina += 1
                    continue
                fin = time.time() + 120
                while time.time() < fin and not cdp.eval(
                        "document.querySelectorAll('a[href]').length > 30"):
                    time.sleep(3)
                enlaces = cdp.eval(_JS_ENLACES_INDICE[cat]) or []
                nuevos = self._agregar_items(cat, enlaces)
                self._ultimo_item = "%s · página %d: +%d ítems" % (
                    cat, pagina, nuevos)
                self._guardar()
                if not enlaces or nuevos == 0:
                    sin_nuevos += 1
                    if sin_nuevos >= 2:
                        completa = True   # dos páginas sin items nuevos: fin
                        break
                else:
                    sin_nuevos = 0
                pagina += 1
                time.sleep(1)   # ritmo suave en los indices
            if completa and not self._detener:
                # solo se marca la categoría COMPLETA (recorrió su índice
                # entero sin pausa): una pausa a mitad vuelve a enumerarla
                with self._lock:
                    self._progreso.setdefault("enumerado", {})[cat] = True
                    self._guardar()
                _log("enumeración de %s completa" % cat)

    def _agregar_items(self, cat, enlaces):
        nuevos = 0
        with self._lock:
            for u in enlaces:
                if not u or u in self._progreso["items"]:
                    continue
                slug = _sanitizar(u.rstrip("/").split("/")[-1])
                self._progreso["items"][u] = {
                    "cat": cat, "estado": "pendiente",
                    "carpeta": os.path.join(self.root, cat, slug),
                    "reintentos": 0,
                }
                nuevos += 1
        return nuevos

    # ---------------- procesamiento en paralelo ----------------
    def _procesar(self, cdp, ws_maestro):
        self._estado = "procesando"
        pestanas = zc._crear_pestanas(_PESTANAS)
        conexiones = [(ws_maestro, None)] + pestanas
        q = queue.Queue()
        for u in self._pendientes_ordenados():
            q.put(u)
        hilos = []
        for ws_url, tid in conexiones:
            h = threading.Thread(target=self._trabajador,
                                 args=(ws_url, tid, q), daemon=True)
            h.start()
            hilos.append(h)
        for h in hilos:
            h.join()
        # sobras encoladas por expansiones tardias de series: la pestana
        # maestra las termina (no se pierde ningun item)
        while not q.empty() and not self._detener:
            try:
                u = q.get_nowait()
            except queue.Empty:
                break
            self._procesar_uno(cdp, u, q)

    def _pendientes_ordenados(self):
        """URLs pendientes ordenadas por prioridad de categoría: episodios →
        series → películas → juegos. Antes era el orden de inserción y el
        grueso de juegos ahogaba el resto."""
        with self._lock:
            pend = [u for u, it in self._progreso["items"].items()
                    if self._es_pendiente(it)]

            def prioridad(u):
                it = self._progreso["items"].get(u) or {}
                return _PRIORIDAD_COLA.get(it.get("cat"), 9)

        return sorted(pend, key=prioridad)

    def _trabajador(self, ws_url, target_id, q):
        cdp = None
        try:
            cdp = zc._Cdp(ws_url, target_id=target_id)
        except Exception:
            return
        try:
            while not self._detener:
                try:
                    u = q.get_nowait()
                except queue.Empty:
                    break
                self._procesar_uno(cdp, u, q)
        finally:
            if cdp:
                try:
                    cdp.cerrar()
                except Exception:
                    pass

    def _procesar_uno(self, cdp, url, q):
        with self._lock:
            it = self._progreso["items"].get(url)
            if not it or not self._es_pendiente(it):
                return
            cat = it.get("cat")
            carpeta = it.get("carpeta") or os.path.join(self.root, "otros")
        t0 = time.time()
        ok = False
        error = ""
        titulo = ""
        try:
            if cat == "series":
                titulo, error, nuevos = self._expandir_serie(cdp, url, carpeta)
                ok = not error
                if ok and nuevos:
                    for u in nuevos:
                        q.put(u)
            else:
                titulo, error = self._extraer_ficha(cdp, url, cat, carpeta)
                ok = not error
        except Exception as e:
            error = "error interno: %s" % e
        duracion = time.time() - t0
        self._t_media_item = self._t_media_item * 0.9 + duracion * 0.1
        with self._lock:
            it = self._progreso["items"].get(url)
            if it is None:
                return
            if ok:
                it["estado"] = "hecho"
                _log("hecho: %s" % (titulo or url))
                if titulo:
                    it["carpeta"] = os.path.join(
                        os.path.dirname(carpeta), _sanitizar(titulo))
            else:
                it["estado"] = "error"
                it["reintentos"] = it.get("reintentos", 0) + 1
                it["error"] = (error or "")[:200]
                _log("error (%d/%d): %s" % (it["reintentos"], _MAX_REINTENTOS, error))
                if it["reintentos"] >= _MAX_REINTENTOS:
                    it["estado"] = "descartado"
            self._ultimo_item = url
            self._guardar()

    # ---------------- ficha de un juego / pelicula ----------------
    def _extraer_ficha(self, cdp, url, cat, carpeta):
        cond = ("document.querySelectorAll('a[id=\"download-link\"]').length > 0"
                " || /un momento/i.test(document.title)")
        if not cdp.navegar(url, condicion=cond, tiempo_max=60):
            return "", "no se pudo abrir la pagina"
        fin = time.time() + 150
        while time.time() < fin and not cdp.eval(
                "document.querySelectorAll('a[id=\"download-link\"]').length > 0"):
            time.sleep(3)
        if not cdp.eval(
                "document.querySelectorAll('a[id=\"download-link\"]').length > 0"):
            return "", "sin botones de descarga"
        datos = self._scrape_ficha(cdp, "games_tumbl|movies_tumbl")
        datos["_portada_bytes"] = self._portada_bytes(cdp, datos.get("portada"))
        botones = zc._extraer_botones(cdp) or []
        servidores = []
        fin_item = time.time() + 120 + 40 * len(botones)
        for i, b in enumerate(botones):
            if time.time() >= fin_item:
                break
            cl = zc._resolver_enlace_episodio(cdp, b, fin_item)
            if cl is None:
                break
            cl["servidor"] = ((b.get("t") or "").strip()
                              or ("Opcion %d" % (i + 1)))
            servidores.append(cl)
        if not servidores:
            return "", "sin enlaces resueltos"
        titulo = (datos.get("titulo") or
                  os.path.basename(carpeta) or "sin-titulo").strip()
        final = os.path.join(os.path.dirname(carpeta), _sanitizar(titulo))
        final = self._carpeta_unica(final)
        ok, err = self._guardar_ficha(cdp, final, url, datos, servidores)
        if not ok:
            return "", err
        return titulo, None

    # ---------------- ficha de un episodio ----------------
    def _extraer_ficha_episodio(self, cdp, url, carpeta):
        cond = ("document.querySelectorAll('a[href*=\"anomizador\"]').length > 0"
                " || /un momento/i.test(document.title)")
        if not cdp.navegar(url, condicion=cond, tiempo_max=60):
            return "", "no se pudo abrir el episodio"
        fin = time.time() + 150
        while time.time() < fin and not cdp.eval(
                "document.querySelectorAll('a[href*=\"anomizador\"]').length > 0"):
            time.sleep(3)
        if not cdp.eval(
                "document.querySelectorAll('a[href*=\"anomizador\"]').length > 0"):
            return "", "sin botones de descarga"
        datos = self._scrape_ficha(cdp, "episodes_tumbl")
        datos["_portada_bytes"] = self._portada_bytes(cdp, datos.get("portada"))
        label = (datos.get("titulo") or os.path.basename(carpeta)
                 or "episodio").strip() or "episodio"
        botones = zc._extraer_botones_episodio(cdp) or []
        servidores = []
        fin_item = time.time() + 120 + 40 * max(len(botones), 1)
        for i, b in enumerate(botones):
            if time.time() >= fin_item:
                break
            cl = zc._resolver_enlace_episodio(cdp, b, fin_item)
            if cl is None:
                break
            cl["servidor"] = label + (" · Opcion %d" % (i + 1)
                                      if len(botones) > 1 else "")
            servidores.append(cl)
        if not servidores:
            return "", "sin enlaces resueltos"
        final = os.path.join(os.path.dirname(carpeta), _sanitizar(label))
        final = self._carpeta_unica(final)
        ok, err = self._guardar_ficha(cdp, final, url, datos, servidores)
        if not ok:
            return "", err
        return label, None

    # ---------------- expansion de una serie ----------------
    def _expandir_serie(self, cdp, url, carpeta):
        cond = ("document.querySelectorAll('a[href*=\"/series/episode/\"]').length > 0"
                " || /un momento/i.test(document.title)")
        if not cdp.navegar(url, condicion=cond, tiempo_max=60):
            return "", "no se pudo abrir la serie", []
        fin = time.time() + 150
        while time.time() < fin and not cdp.eval(
                "document.querySelectorAll('a[href*=\"/series/episode/\"]').length > 0"):
            time.sleep(3)
        episodios = zc._extraer_episodios(cdp) or []
        if not episodios:
            return "", "sin episodios", []
        datos = self._scrape_ficha(cdp, "series_tumbl")
        titulo = (datos.get("titulo") or os.path.basename(carpeta)
                  or "serie").strip() or "serie"
        serie_dir = os.path.join(os.path.dirname(carpeta), _sanitizar(titulo))
        serie_dir = self._carpeta_unica(serie_dir)
        try:
            os.makedirs(serie_dir, exist_ok=True)
            with open(os.path.join(serie_dir, "descripcion.txt"),
                      "w", encoding="utf-8") as f:
                f.write("Titulo: %s\nURL: %s\n\n=== DESCRIPCION ===\n\n%s\n"
                        % (titulo, url, datos.get("descripcion") or ""))
            if datos.get("portada"):
                self._descargar_portada(cdp, datos["portada"], serie_dir)
            with open(os.path.join(serie_dir, "episodios.txt"),
                      "w", encoding="utf-8") as f:
                for ep in episodios:
                    f.write("%s | %s\n" % (ep.get("label") or "?",
                                           ep.get("url") or ""))
        except Exception:
            pass
        nuevos = []
        with self._lock:
            for ep in episodios:
                eurl = ep.get("url") or ""
                if not eurl or eurl in self._progreso["items"]:
                    continue
                self._progreso["items"][eurl] = {
                    "cat": "series_ep", "estado": "pendiente",
                    "carpeta": os.path.join(
                        serie_dir, _sanitizar(ep.get("label") or "episodio")),
                    "reintentos": 0,
                }
                nuevos.append(eurl)
        return titulo, None, nuevos

    # ---------------- scraping de la pagina ----------------
    def _scrape_ficha(self, cdp, tumbl):
        js = r'''(() => {
            const out = {titulo: "", descripcion: "", portada: "", requisitos: []};
            out.titulo = (document.title || "")
                .replace(/\s*[|\u2013]\s*ZonaLeRoS.*$/i, "")
                .replace(/^(ver|descargar)\s+/i, "")
                .trim();
            for (const p of document.querySelectorAll("p")) {
                const t = (p.innerText || "").trim();
                if (t.length > 40) { out.descripcion = t; break; }
            }
            if (!out.descripcion) {
                out.descripcion = (document.querySelector('meta[name="description"]') || {}).content || "";
            }
            const pat = /__TUMBL__/i;
            let mejor = "";
            for (const i of document.querySelectorAll("img")) {
                const s = i.src || "";
                if (!pat.test(s)) continue;
                if (!mejor) mejor = s;
                if (/cover/i.test(s)) { mejor = s; break; }
            }
            out.portada = mejor || "";
            for (const h of document.querySelectorAll("h1,h2,h3,h4")) {
                const t = (h.innerText || "").trim();
                if (!/^(minimos|recomendados|requisitos?)/i.test(t)) continue;
                const div = h.parentElement;
                const texto = (div ? div.innerText : "").replace(new RegExp("^" + t), "").trim();
                const lineas = texto.split("\n").map(x => x.trim()).filter(Boolean);
                if (lineas.length) out.requisitos.push({titulo: t, lineas: lineas});
            }
            return out;
        })()'''.replace("__TUMBL__", tumbl)
        return cdp.eval(js) or {}

    # ---------------- guardado ----------------
    def _carpeta_unica(self, carpeta):
        if not os.path.exists(carpeta):
            return carpeta
        base, n = carpeta, 2
        while os.path.exists(base + "-%d" % n):
            n += 1
        return base + "-%d" % n

    def _guardar_ficha(self, cdp, carpeta, url, datos, servidores):
        try:
            os.makedirs(carpeta, exist_ok=True)
            titulo = (datos.get("titulo") or "").strip()
            with open(os.path.join(carpeta, "descripcion.txt"),
                      "w", encoding="utf-8") as f:
                f.write("Titulo: %s\nURL: %s\n\n=== DESCRIPCION ===\n\n%s\n"
                        % (titulo, url, datos.get("descripcion") or ""))
            lineas = ["Titulo: %s" % titulo, "URL: %s" % url,
                      "", "=== ENLACES DE DESCARGA ==="]
            for s in servidores:
                lineas.append("")
                lineas.append("[%s]" % (s.get("servidor") or "Servidor"))
                if s.get("error"):
                    lineas.append("  (error: %s)" % s["error"])
                for e in s.get("enlaces", []):
                    nombre = e.get("nombre") or ""
                    parte = e.get("parte") or 0
                    total = e.get("total") or 0
                    etiqueta = ""
                    if parte:
                        etiqueta = ("Parte %d de %d" % (parte, total)
                                    if total else "Parte %d" % parte)
                    elif nombre:
                        etiqueta = nombre
                    if etiqueta:
                        lineas.append("  %s: %s" % (etiqueta, e.get("url") or ""))
                    else:
                        lineas.append("  %s" % (e.get("url") or ""))
            with open(os.path.join(carpeta, "enlaces.txt"),
                      "w", encoding="utf-8") as f:
                f.write("\n".join(lineas) + "\n")
            if datos.get("requisitos"):
                with open(os.path.join(carpeta, "requisitos.txt"),
                          "w", encoding="utf-8") as f:
                    f.write("=== REQUISITOS DEL SISTEMA ===\n\n")
                    for r in datos["requisitos"]:
                        f.write("%s\n%s\n\n"
                                % (r.get("titulo", "").upper(),
                                   "\n".join(r.get("lineas", []))))
            if datos.get("_portada_bytes"):
                ext = os.path.splitext(urllib.parse.urlparse(
                    datos.get("portada") or "").path)[1]
                ext = re.sub(r"[^a-zA-Z0-9.]", "", ext) or ".jpg"
                try:
                    with open(os.path.join(carpeta, "portada" + ext),
                              "wb") as f:
                        f.write(datos["_portada_bytes"])
                except Exception:
                    pass
            with open(os.path.join(carpeta, ".hecho"),
                      "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
            return True, None
        except Exception as e:
            return False, "no se pudo guardar la ficha: %s" % e

    def _portada_bytes(self, cdp, url_img):
        """Baja la portada por fetch MIENTRAS estamos en la pagina
        (mismo origen); tras resolver enlaces la pestana ya no esta
        en zona-leros y el fetch cruzado fallaria por CORS."""
        if not url_img:
            return None
        try:
            b64 = cdp.eval(
                "fetch(%s).then(function(r){return r.ok?r.arrayBuffer():null;})"
                ".then(function(b){if(!b)return null;var s='',CH=0x8000;"
                "for(var i=0;i<b.byteLength;i+=CH){s+=String.fromCharCode.apply(null,"
                "new Uint8Array(b).subarray(i,i+CH));}return btoa(s);})"
                ".catch(function(){return null;})" % json.dumps(url_img))
            if not b64:
                return None
            return base64.b64decode(b64)
        except Exception:
            return None

    def _descargar_portada(self, cdp, url_img, carpeta):
        try:
            b64 = cdp.eval(
                "fetch(%s).then(function(r){return r.ok?r.arrayBuffer():null;})"
                ".then(function(b){if(!b)return null;var s='',CH=0x8000;"
                "for(var i=0;i<b.byteLength;i+=CH){s+=String.fromCharCode.apply(null,"
                "new Uint8Array(b).subarray(i,i+CH));}return btoa(s);})"
                ".catch(function(){return null;})" % json.dumps(url_img))
            if not b64:
                return
            ext = os.path.splitext(urllib.parse.urlparse(url_img).path)[1]
            ext = re.sub(r"[^a-zA-Z0-9.]", "", ext) or ".jpg"
            with open(os.path.join(carpeta, "portada" + ext), "wb") as f:
                f.write(base64.b64decode(b64))
        except Exception:
            pass
