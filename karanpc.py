# -*- coding: utf-8 -*-
"""Extractor tolerante para posts individuales de karanpc.com.

KaranPC publica reseñas de software y sus enlaces pueden aparecer como
anchors, atributos data-* o botones que llevan a un gateway/intermediario
(como GloTorrents). Este módulo usa el Chrome/CDP ya empleado por ZonaLeros,
pero no depende de la estructura de series ni de sus acortadores.
"""
import json
import re
import time
import urllib.parse

import zonaleros_copia as zonaleros

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_HOSTERS = (
    ("mediafire.com", "MediaFire"),
    ("mega.nz", "Mega"), ("mega.co.nz", "Mega"),
    ("gofile.io", "GoFile"),
    ("pixeldrain.com", "PixelDrain"),
    ("qiwi.gg", "Qiwi"),
    ("1fichier.com", "1Fichier"),
    ("megaup.net", "MegaUp"),
    ("fireload.com", "Fireload"),
    ("drive.google.com", "Google Drive"),
    ("dropbox.com", "Dropbox"),
    ("glodls.online", "GloTorrents"),
)


def _es_karanpc(url):
    """Acepta posts de KaranPC, no el índice /posts/ como descarga."""
    try:
        p = urllib.parse.urlparse(url or "")
        host = (p.hostname or "").lower()
        path = p.path.rstrip("/") or "/"
        return host == "karanpc.com" or host.endswith(".karanpc.com")
    except Exception:
        return False


def _es_indice(url):
    try:
        path = urllib.parse.urlparse(url or "").path.rstrip("/")
        return path == "/posts" or path.startswith("/posts/page/")
    except Exception:
        return False


def _nombre_hoster(url):
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    for fragmento, nombre in _HOSTERS:
        if host == fragmento or host.endswith("." + fragmento):
            return nombre
    if host and host != "karanpc.com" and not host.endswith(".karanpc.com"):
        return host.replace("www.", "")
    return "Servidor por confirmar"


def _es_intermedio(url):
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return host == "glodls.online" or host.endswith(".glodls.online")


def _es_url_util(url, origen):
    if not re.match(r"^https?://", url or "", re.I):
        return False
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not host or host == origen or host.endswith("." + origen):
        return False
    if host in {"google.com", "gstatic.com", "facebook.com", "twitter.com",
                "x.com", "youtube.com", "instagram.com", "t.me"}:
        return False
    if re.search(r"wp-content|schema.org|wordpress.org|gravatar.com", url, re.I):
        return False
    return True


def _extraer_urls_dom(cdp, origen):
    """Extrae enlaces candidatos del DOM, incluyendo data-url/onclick/texto."""
    expr = r'''(() => {
      const out = [];
      const attrs = ['href','data-href','data-url','data-download','data-link','onclick'];
      for (const el of document.querySelectorAll('a,button,[data-href],[data-url],[data-download],[data-link]')) {
        const texto = (el.innerText || el.textContent || el.title || '').trim().replace(/\s+/g, ' ');
        for (const a of attrs) {
          const v = el.getAttribute(a) || '';
          if (!v) continue;
          const found = v.match(/https?:\\/\\/[^\s"'<>\\)]+/ig) || [];
          for (const u of found) out.push({url:u, texto:texto});
        }
      }
      const body = document.body && document.body.innerText || '';
      for (const u of body.match(/https?:\\/\\/[^\s"'<>\\)]+/ig) || []) {
        out.push({url:u, texto:''});
      }
      return out;
    })()'''
    try:
        crudas = cdp.eval(expr) or []
    except Exception:
        crudas = []
    resultado = []
    vistos = set()
    for e in crudas:
        url = (e.get("url") or "").strip().rstrip(".,;)")
        if not _es_url_util(url, origen):
            continue
        # ignorar recursos del propio post, redes y enlaces editoriales; sí
        # conservar GloTorrents y hosters conocidos.
        hoster = _nombre_hoster(url)
        texto = e.get("texto") or ""
        es_descarga = (hoster != "Servidor por confirmar" or
                       _es_intermedio(url) or
                       re.search(r"download|descarg|mirror|portable|setup|torrent|direct", texto, re.I))
        if not es_descarga or url in vistos:
            continue
        vistos.add(url)
        resultado.append({"url": url, "texto": texto})
    return resultado


def _titulo(cdp):
    try:
        titulo = (cdp.eval("document.title") or "").strip()
    except Exception:
        titulo = ""
    titulo = re.sub(r"\s*[|\u2013-]\s*KaranPC\s*$", "", titulo, flags=re.I).strip()
    # descartar títulos genéricos de error (404, Cloudflare…): nunca
    # presentarlos como nombre del programa
    if not titulo or zonaleros._es_titulo_error(titulo):
        return "Programa KaranPC"
    return titulo


def _navegar_intermedio(cdp, url, fin_global):
    """Abre GloTorrents y recoge sus enlaces de descarga visibles."""
    if time.time() >= fin_global:
        return []
    try:
        if not cdp.navegar(url, condicion="document.readyState === 'complete'", tiempo_max=40):
            return []
        time.sleep(2)
        return _extraer_urls_dom(cdp, "glodls.online")
    except Exception:
        return []


def _clasificar(enlaces):
    por_hoster = {}
    for e in enlaces:
        url = e.get("url") or ""
        hoster = _nombre_hoster(url)
        if hoster == "KaranPC" or hoster == "Servidor por confirmar":
            hoster = "Servidor por confirmar"
        grupo = por_hoster.setdefault(hoster, [])
        if not any(x.get("url") == url for x in grupo):
            grupo.append(e)
    salida = []
    for hoster, items in por_hoster.items():
        entradas = [{"url": e["url"], "texto": e.get("texto") or "",
                     "nombre": zonaleros._nombre_de_url(e["url"])} for e in items]
        grupo = zonaleros._clasificar_enlaces(entradas)
        grupo["servidor"] = hoster
        grupo["hoster"] = hoster
        salida.append(grupo)
    return salida


def extraer(url, on_progreso=None, hosters_permitidos=None, episodios_permitidos=None):
    """Extrae un post individual de KaranPC.

    La página de índice /posts/ se rechaza para evitar tratar un listado como
    un programa. GloTorrents se abre con Chrome y se inspeccionan únicamente
    enlaces visibles/candidatos; no se ejecutan scripts externos ni se saltan
    controles de acceso.
    """
    if not _es_karanpc(url):
        return {"error": "la URL no pertenece a KaranPC"}
    if _es_indice(url):
        return {"error": "pega la URL del post individual de KaranPC, no el índice /posts/"}
    ws_url, err = zonaleros._lanzar(url)
    if err:
        return {"error": err}
    cdp = None
    fin = time.time() + 300
    try:
        cdp = zonaleros._Cdp(ws_url)
        cond = ("document.readyState === 'complete' || " + zonaleros._js_reto_cloudflare())
        if not cdp.navegar(url, condicion=cond, tiempo_max=120):
            if not cdp.navegar(url, condicion=cond, tiempo_max=120):
                return {"error": "KaranPC no dejó cargar el post"}
        titulo = _titulo(cdp)
        origen = (urllib.parse.urlparse(url).hostname or "karanpc.com").lower()
        candidatos = _extraer_urls_dom(cdp, origen)
        # Resolver intermediarios con una misma pestaña; no se conserva el
        # enlace GloTorrents como descarga final si devuelve destinos reales.
        finales = []
        vistos = set()
        cola = list(candidatos)
        max_intermedios = 8
        while cola and time.time() < fin:
            actual = cola.pop(0)
            u = actual["url"]
            if u in vistos:
                continue
            vistos.add(u)
            if _es_intermedio(u) and max_intermedios > 0:
                max_intermedios -= 1
                hijos = _navegar_intermedio(cdp, u, fin)
                if hijos:
                    cola.extend(hijos)
                else:
                    finales.append(actual)
            else:
                finales.append(actual)
        # Dedupe y aplicar filtro exacto por hoster real.
        permitidos = {str(x).lower() for x in (hosters_permitidos or [])}
        finales = [e for e in finales
                   if not permitidos or _nombre_hoster(e["url"]).lower() in permitidos]
        servidores = _clasificar(finales)
        if not servidores:
            return {"error": "no se encontraron enlaces de descarga en el post de KaranPC",
                    "titulo": titulo}
        resultado = {"servidores": servidores, "titulo": titulo}
        if on_progreso:
            try:
                on_progreso(servidores, 1, 1, titulo)
            except TypeError:
                on_progreso(servidores, 1, 1, titulo)
        return resultado
    except Exception as e:
        if not zonaleros._nuestro_chrome_vivo():
            return {"error": "la instancia de Chrome de la extracción se cerró"}
        return {"error": "error extrayendo KaranPC: %s" % e}
    finally:
        if cdp:
            try:
                cdp.cerrar()
            except Exception:
                pass
        zonaleros._finalizar()
