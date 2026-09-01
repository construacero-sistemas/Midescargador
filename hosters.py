# -*- coding: utf-8 -*-
"""
MiDescargador - Extractores de enlaces de "file hosters" (estilo JDownloader).
Convierte una página de descarga (rootz.so, etc.) en la URL directa del archivo,
para que el motor segmentado pueda descargarla con Range, pausa y reanudación.

Flujo rootz.so (descubierto experimentalmente, rediseño 2026):
  Enlaces nuevos (formato /download/<uuid> y carpetas /folder/<id>):
    1. GET /api/files/download/<fileId> -> JSON con fileName, size, status...
    2. GET /api/files/proxy-download/<fileId> -> redirige a la URL firmada
       (Cloudflare R2, ~24 h de validez). No requiere token.
  Enlaces clásicos (formato /d/<shortId>, siguen circulando):
    1. GET https://www.rootz.so/d/<shortId>  -> el HTML trae el "pageToken" en
       el payload RSC de Next.js (regex sobre 'pageToken\\":\\"<token>')
    2. GET /api/files/download-by-short?shortId=<id>  con cabecera X-Page-Token
       -> JSON con fileName, size (bytes), fileId, etc.
    3. GET /api/files/proxy-download/<fileId>  con X-Page-Token
       -> redirige a la URL firmada (Cloudflare R2)
  Carpetas (formato /folder/<id>):
    1. GET /api/folders/share/<id> -> JSON con la lista de archivos
    2. Cada archivo se descarga como /download/<fileId> (o /d/<shortId>)
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import ssl

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 40
_CTX = ssl.create_default_context()

# dominios que este módulo sabe resolver (extensible)
SOPORTADOS = ("rootz.so", "www.rootz.so", "fireload.com", "www.fireload.com",
              "megaup.net", "www.megaup.net", "gofile.io", "www.gofile.io",
              "mediafire.com", "www.mediafire.com", "fuckingfast.net",
              "www.fuckingfast.net", "1fichier.com", "www.1fichier.com",
              "lolaup.com", "www.lolaup.com", "rapidshare.co", "www.rapidshare.co",
              "upto.cash", "www.upto.cash", "solred.app", "www.solred.app",
              "drive.marketcat.io", "drive.google.com", "drive.usercontent.google.com")

# etiquetas amigables por dominio (para la lista del panel)
ETIQUETAS = {
    "rootz.so": "Rootz", "fireload.com": "Fireload", "megaup.net": "MegaUp",
    "gofile.io": "GoFile", "mediafire.com": "MediaFire",
    "fuckingfast.net": "FuckingFast", "1fichier.com": "1Fichier",
    "lolaup.com": "LolaUp", "rapidshare.co": "RapidShare",
    "upto.cash": "UpToCash", "solred.app": "Solred",
    "drive.marketcat.io": "MarketCat", "google.com": "Google Drive",
}


def hosters_soportados():
    """Lista de {dominio, nombre} para mostrar en el panel."""
    vistos = set()
    out = []
    for d in SOPORTADOS:
        # dominio base: las dos últimas etiquetas (rootz.so, mediafire.com...)
        partes = d.split(".")
        base = ".".join(partes[-2:]) if len(partes) > 1 else d
        if base in vistos:
            continue
        vistos.add(base)
        out.append({"dominio": base, "nombre": ETIQUETAS.get(base, base)})
    return out


def _pedir(url, cabeceras=None, metodo="GET"):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if cabeceras:
        h.update(cabeceras)
    req = urllib.request.Request(url, headers=h, method=metodo)
    return urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX)


def _rootz_metadatos(ident):
    """Pide los metadatos de un archivo rootz.so por su identificador (UUID).
    Devuelve (file_id, datos) o lanza RuntimeError con mensaje claro."""
    try:
        with _pedir(
            f"https://www.rootz.so/api/files/download/{urllib.parse.quote(ident)}",
        ) as r:
            datos = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"rootz.so no encontró el archivo (HTTP {e.code}); "
            f"¿enlace caducado o borrado?") from e
    except Exception as e:
        raise RuntimeError(f"rootz.so rechazó la consulta del archivo: {e}") from e
    if not datos.get("success"):
        raise RuntimeError("rootz.so: " + str(datos.get("error") or "error desconocido"))
    d = datos.get("data") or {}
    if d.get("status") == "deleted":
        raise RuntimeError("rootz.so: el archivo fue borrado o el enlace caducó")
    if d.get("passwordProtected"):
        raise RuntimeError("rootz.so: el archivo está protegido con contraseña "
                           "(ábrelo en el navegador y copia el enlace directo)")
    if not d.get("downloadAllowed"):
        raise RuntimeError("rootz.so: la descarga no está permitida para este archivo")
    return ident, d


def _rootz_directa(file_id, cabeceras=None):
    """Pide la URL firmada de descarga (la API redirige)."""
    try:
        with _pedir(
            f"https://www.rootz.so/api/files/proxy-download/{file_id}",
            cabeceras=cabeceras,
        ) as r:
            return r.geturl()
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"rootz.so falló al generar la descarga (HTTP {e.code}); "
            f"¿archivo borrado o caducado?") from e
    except Exception:
        raise RuntimeError("rootz.so no entregó la URL directa del archivo")


def _extraer_rootz(url):
    """Resuelve un enlace rootz.so a su URL directa firmada.

    Acepta los tres formatos actuales:
      /d/<shortId>      (clásico, sigue circulando)
      /download/<uuid>  (nuevo)
      /folder/<id>      (carpeta: se resuelve el primer archivo)

    Devuelve dict con: url (directa), nombre, tamano (bytes), pagina.
    Lanza RuntimeError con mensaje claro si algo falla.
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in SOPORTADOS:
        raise RuntimeError(f"dominio no soportado: {host}")

    # ---- carpeta: lista de archivos, resolvemos el primero ----
    fm = re.search(r"/folder/([A-Za-z0-9_-]+)", url)
    if fm:
        try:
            with _pedir(
                "https://www.rootz.so/api/folders/share/"
                + urllib.parse.quote(fm.group(1)),
            ) as r:
                datos = json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"rootz.so no encontró la carpeta (HTTP {e.code})") from e
        except Exception as e:
            raise RuntimeError(f"rootz.so rechazó la consulta de la carpeta: {e}") from e
        archivos = ((datos.get("data") or {}).get("files")) or []
        if not archivos:
            raise RuntimeError("rootz.so: la carpeta está vacía")
        # cada archivo: /d/<short_id> si lo tiene, si no /download/<id>
        f = archivos[0]
        enlace = (f"/d/{f.get('short_id')}" if f.get("short_id")
                  else f"/download/{f.get('id')}")
        nombre = f.get("name") or f.get("fileName") or "descarga"
        tamano = f.get("size")
        ident, _d = _rootz_metadatos(f.get("id") or f.get("fileId"))
        directa = _rootz_directa(ident)
        return {"url": directa, "nombre": nombre, "tamano": tamano,
                "pagina": url}

    # ---- archivo individual: /download/<uuid> (nuevo) o /d/<shortId> (clásico) ----
    m = re.search(r"/(?:d|download|file)/([A-Za-z0-9_-]+)", url)
    if not m:
        raise RuntimeError("enlace rootz.so sin código de archivo "
                           "(formatos: /d/XXX, /download/XXX o /folder/XXX)")
    ident = m.group(1)
    directa = None
    nombre = "descarga"
    tamano = None

    if "/d/" in url:
        # flujo clásico: pageToken de la página + download-by-short
        try:
            with _pedir(url) as r:
                html = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"rootz.so bloqueó la página (HTTP {e.code}); "
                               f"prueba abrirla antes en el navegador") from e
        except Exception as e:
            raise RuntimeError(f"no se pudo abrir la página rootz.so: {e}") from e
        tm = re.search(r'pageToken\\":\\"([^\\]+)\\"', html)
        if not tm:
            raise RuntimeError("no encontré el token de la página rootz.so "
                               "(¿cambió el sitio?)")
        token = tm.group(1)
        try:
            with _pedir(
                "https://www.rootz.so/api/files/download-by-short"
                f"?shortId={urllib.parse.quote(ident)}",
                cabeceras={"X-Page-Token": token},
            ) as r:
                datos = json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception as e:
            raise RuntimeError(f"rootz.so rechazó la consulta del archivo: {e}") from e
        if not datos.get("success"):
            raise RuntimeError("rootz.so: " + str(datos.get("error") or "error desconocido"))
        d = datos.get("data") or {}
        if d.get("status") == "deleted":
            raise RuntimeError("rootz.so: el archivo fue borrado o el enlace caducó")
        if d.get("passwordProtected"):
            raise RuntimeError("rootz.so: el archivo está protegido con contraseña "
                               "(ábrelo en el navegador y copia el enlace directo)")
        file_id = d.get("fileId")
        if not file_id:
            raise RuntimeError("rootz.so no devolvió el identificador del archivo")
        nombre = d.get("fileName") or "descarga"
        tamano = d.get("size")
        directa = _rootz_directa(file_id, cabeceras={"X-Page-Token": token})
    else:
        # flujo nuevo: la API resuelve el UUID directamente, sin token
        _ident, d = _rootz_metadatos(ident)
        nombre = d.get("fileName") or "descarga"
        tamano = d.get("size")
        directa = _rootz_directa(ident)

    return {
        "url": directa,
        "nombre": nombre,
        "tamano": tamano,
        "pagina": url,
    }


def _extraer_fireload(url):
    """Fireload: la URL directa firmada viene en window.Fl dentro del HTML.
    La pedimos con cookies de sesión + token CSRF y seguimos el redirect
    hasta la URL del servidor de descarga.
    """
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA)]
    try:
        with op.open(url, timeout=TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"fireload bloqueó la página (HTTP {e.code})") from e
    except Exception as e:
        raise RuntimeError(f"no se pudo abrir la página fireload: {e}") from e

    tm = re.search(r"name='authenticity_token' value='([^']+)'", html)
    dm = re.search(r'"dlink": "([^"]+)"', html)
    if not dm:
        raise RuntimeError("no encontré el enlace de descarga en fireload "
                           "(¿archivo borrado?)")
    token = tm.group(1) if tm else ""
    dlink = dm.group(1)

    # pedir el dlink con sesión + token CSRF -> 206 con URL real
    cab = {"User-Agent": UA, "Referer": url, "Accept": "*/*",
           "Range": "bytes=0-0"}
    if token:
        cab["X-CSRF-Token"] = token
    req = urllib.request.Request(dlink, headers=cab)
    try:
        r = op.open(req, timeout=TIMEOUT)
        r.read(1)
        directa = r.geturl()
        nombre = None
        cd = r.headers.get("Content-Disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.I)
        if m:
            nombre = urllib.parse.unquote(m.group(1))
        tam = None
        cr = r.headers.get("Content-Range", "")
        m = re.search(r"/(\d+)$", cr)
        if m:
            tam = int(m.group(1))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"fireload falló al generar la descarga (HTTP {e.code})") from e
    except Exception as e:
        raise RuntimeError(f"fireload no entregó la URL directa: {e}") from e

    return {"url": directa, "nombre": nombre or "descarga",
            "tamano": tam, "pagina": url}


def _extraer_megaup(url):
    """Megaup: la URL directa real está detrás de dos capas.

    Flujo (descubierto experimentalmente):
      1. La página del archivo trae un href a download.megaup.net/?url=<tok>,
         que responde 403 a clientes HTTP normales (Cloudflare).
      2. Abierta en Chrome (sesión real), download.megaup.net muestra un
         countdown de ~6 s; al pulsar el botón, el JS genera la URL real:
         megadl.boats/download/<nombre>?download_token=<token>.
      3. Esa URL soporta Range y se descarga con el motor segmentado. El
         token caduca rápido (rotación), así que se genera en el momento.
    """
    import zonaleros_copia as zonaleros  # módulo único; evita dependencia circular

    if zonaleros._chrome_corriendo():
        raise RuntimeError(
            "megaup exige el navegador real para resolver el enlace: "
            "cierra Chrome del todo y reintenta.")
    ws_url, err = zonaleros._lanzar(url)
    if err:
        raise RuntimeError(err)
    cdp = None
    try:
        cdp = zonaleros._Cdp(ws_url)
        # 1) página del archivo: espera el href a download.megaup.net
        fin = time.time() + 45
        href = None
        while time.time() < fin:
            try:
                html = cdp.eval("document.documentElement.outerHTML") or ""
            except Exception:
                html = ""
            m = re.search(r"href='([^']*download\.megaup\.net[^']*)'", html)
            if m:
                href = m.group(1)
                break
            time.sleep(3)
        if not href:
            raise RuntimeError(
                "megaup no expuso la página de descarga (¿archivo borrado?)")
        # 2) navega a download.megaup.net (deja pasar Cloudflare) y espera
        #    el countdown (~6 s) hasta que el botón esté habilitado
        import json as _json
        cdp._ws.send(_json.dumps({
            "id": 9006, "method": "Page.navigate", "params": {"url": href}}))
        fin = time.time() + 60
        while time.time() < fin:
            try:
                listo = cdp.eval(
                    "(() => { const b = document.getElementById('btndownload'); "
                    "return b ? !b.disabled : false; })()")
            except Exception:
                listo = False
            if listo:
                break
            time.sleep(2)
        # 3) pulsa el botón y lee la URL real (megadl.boats) que se revela
        cdp.eval("document.getElementById('btndownload').click()")
        fin = time.time() + 30
        directa = None
        while time.time() < fin:
            try:
                directa = cdp.eval(
                    "(() => { const a = document.querySelector("
                    "'#afterdownload a[href]'); return a ? a.href : null; })()")
            except Exception:
                directa = None
            if directa:
                break
            time.sleep(2)
        if not directa:
            raise RuntimeError(
                "megaup no generó el enlace de descarga (¿cuenta o límite?)")
        # nombre desde el <title> de la página del archivo
        nombre = None
        tm = re.search(r"<title>([^<]+)</title>", html or "", re.I)
        if tm:
            nombre = re.sub(r"\s*-\s*MegaUp\s*$", "", tm.group(1).strip(),
                            flags=re.I)
        tam = None
        mm = re.search(r"\(([\d.]+)\s*(GB|MB|KB)\)", html or "", re.I)
        if mm:
            try:
                n = float(mm.group(1))
                u = mm.group(2).upper()
                tam = int(n * (1024 ** {"KB": 1, "MB": 2, "GB": 3}[u]))
            except Exception:
                tam = None
        return {"url": directa, "nombre": nombre, "tamano": tam, "pagina": url}
    finally:
        if cdp:
            try:
                cdp.cerrar()
            except Exception:
                pass


def _extraer_mediafire(url):
    """MediaFire: genera la URL directa del CDN.

    MediaFire cambió su página (2026): ya no incrusta download<N>.mediafire.com
    en el HTML. Ahora el botón de descarga lleva un JWT (data-security-token)
    y, al pulsarlo, el navegador llama a POST /download_link.php, que devuelve
    la URL firmada del CDN. Los archivos marcados como sospechosos exigen
    además un POST con pass=<bdpass> (extraído del propio JWT); esos POST
    soportan Range, así que el motor puede descargar en segmentos.
    """
    import http.cookiejar
    import base64
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA)]
    try:
        with op.open(url, timeout=TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"mediafire bloqueó la página (HTTP {e.code})") from e
    except Exception as e:
        raise RuntimeError(f"no se pudo abrir la página mediafire: {e}") from e

    # 1) enlace clásico: si todavía viene en el HTML, se usa directo
    m = re.search(r"https://download[0-9]+\.mediafire\.com/[^\"'< ]+", html)
    directa = None
    post = None
    if m:
        directa = m.group(0)
    else:
        # 2) flujo nuevo: JWT del botón + POST /download_link.php
        m = re.search(
            r'<button[^>]*deferredDownloadButton[^>]*data-security-token="([^"]+)"',
            html)
        if not m:
            raise RuntimeError("mediafire no mostró el botón de descarga "
                               "(¿archivo borrado?)")
        jwt = m.group(1)
        bdpass = None
        try:
            payload = jwt.split(".")[1] + "==="
            bdpass = (json.loads(base64.urlsafe_b64decode(payload))
                      .get("bdpass") or None)
        except Exception:
            pass
        resp = None
        for _intento in range(3):   # el sitio responde "delay" a veces
            try:
                datos = urllib.parse.urlencode({"security_token": jwt}).encode()
                req = urllib.request.Request(
                    "https://www.mediafire.com/download_link.php",
                    data=datos,
                    headers={"X-Requested-With": "XMLHttpRequest",
                             "Content-Type": "application/x-www-form-urlencoded",
                             "Referer": url})
                with op.open(req, timeout=TIMEOUT) as r:
                    resp = json.loads(r.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as e:
                raise RuntimeError(
                    f"mediafire rechazó el enlace (HTTP {e.code})") from e
            except Exception as e:
                raise RuntimeError(f"mediafire no generó el enlace: {e}") from e
            if resp and resp.get("result") == "success" \
                    and resp.get("status") == "success":
                break
            espera = 0
            try:
                espera = int(resp.get("retry_after") or 0)
            except (TypeError, ValueError):
                pass
            if resp.get("status") == "delay" and _intento < 2:
                time.sleep(min(espera or 3, 10))
                continue
            raise RuntimeError("mediafire: " + str(
                resp.get("reason") or resp.get("error")
                or "no pudo generar el enlace de descarga"))
        directa = (resp or {}).get("download_url")
        if not directa:
            raise RuntimeError("mediafire no entregó la URL directa")
        # los archivos marcados como sospechosos exigen POST con pass
        if bdpass:
            post = "pass=" + bdpass

    # nombre: el elemento fileName de la página (el <title> ya es genérico)
    nombre = None
    tm = re.search(r"fileName[^>]*>\s*([^<]{2,120}?)\s*<", html)
    if tm:
        nombre = tm.group(1).strip()
    seg = [s for s in urllib.parse.urlparse(url).path.split("/") if s]
    base_url = seg[-2] if len(seg) >= 2 and seg[-1] == "file" else (seg[-1] if seg else "")
    ext = os.path.splitext(urllib.parse.unquote(base_url))[1]
    if nombre and ext and not nombre.lower().endswith(ext.lower()):
        nombre += ext
    if not nombre:
        nombre = urllib.parse.unquote(base_url) or "descarga"
    # tamaño: el botón trae "Download Anyway (8.7 MB)"
    tam = None
    mm = re.search(r"Download[^<(]*\(([\d.]+)\s*(MB|GB|KB)\)", html)
    if mm:
        try:
            n = float(mm.group(1))
            u = mm.group(2).upper()
            tam = int(n * (1024 ** {"KB": 1, "MB": 2, "GB": 3}[u]))
        except Exception:
            tam = None
    return {"url": directa, "nombre": nombre, "tamano": tam,
            "pagina": url, "post": post}


def _extraer_lolaup(url):
    """LolaUp: la página trae un enlace directo al CDN firmado
    (/en/download/<id>/<token>/<archivo>). Sin retos ni captcha: basta con
    seguirlo (redirige a cache.lolaup.com con el archivo real)."""
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA)]
    try:
        with op.open(url, timeout=TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"lolaup bloqueó la página (HTTP {e.code})") from e
    except Exception as e:
        raise RuntimeError(f"no se pudo abrir la página lolaup: {e}") from e

    # el enlace directo /en/download/<id>/<token>/<nombre>
    m = re.search(r'href="(https?://[^"]*/en/download/[^"]+)"', html)
    if not m:
        raise RuntimeError("lolaup no mostró el enlace de descarga "
                           "(¿archivo borrado o caducado?)")
    directa = m.group(1).replace("&amp;", "&")

    # nombre y tamaño
    nombre = None
    tm = re.search(r"<title>([^<]*)</title>", html, re.I)
    if tm:
        t = tm.group(1).strip()
        mm = re.search(r"—\s*Download\s*—\s*(.+)$", t)
        if mm:
            nombre = mm.group(1).strip()
    if not nombre:
        base = urllib.parse.unquote(
            urllib.parse.urlparse(directa).path.rsplit("/", 1)[-1] or "")
        nombre = base or "descarga"
    tam = None
    mm2 = re.search(r"([\d.]+)\s*(GB|MB|KB)", html)
    if mm2:
        try:
            n = float(mm2.group(1))
            u = mm2.group(2).upper()
            tam = int(n * (1024 ** {"KB": 1, "MB": 2, "GB": 3}[u]))
        except Exception:
            tam = None
    return {"url": directa, "nombre": nombre, "tamano": tam, "pagina": url}


def _extraer_rapidshare(url):
    """RapidShare.co: el botón de descarga hace POST a
    /<lang>/d/<id>/single/request con el file id y el CSRF, y responde
    JSON con el enlace directo (download_link). Sin captcha."""
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA)]
    try:
        with op.open(url, timeout=TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"rapidshare bloqueó la página (HTTP {e.code})") from e
    except Exception as e:
        raise RuntimeError(f"no se pudo abrir la página rapidshare: {e}") from e

    tok = re.search(r'name="_token" value="([^"]+)"', html)
    ids = re.findall(r'class="download-btn"[^>]*data-id="([^"]+)"', html)
    if not ids:
        ids = re.findall(r'data-id="([^"]+)"[^>]*class="[^"]*download-btn', html)
    if not tok or not ids:
        raise RuntimeError("rapidshare no mostró el botón de descarga "
                           "(¿archivo borrado o caducado?)")
    # url base de la página (sin /d/<id>): POST a /<lang>/d/<id>/single/request
    base = url.rstrip("/")
    file_id = ids[0]
    try:
        datos = urllib.parse.urlencode({"_token": tok.group(1),
                                        "id": file_id}).encode()
        req = urllib.request.Request(
            base + "/single/request", data=datos,
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": url, "Accept": "application/json"})
        with op.open(req, timeout=TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"rapidshare rechazó la descarga (HTTP {e.code})") from e
    except Exception as e:
        raise RuntimeError(f"rapidshare no generó el enlace: {e}") from e
    directa = (resp or {}).get("download_link")
    if not directa:
        raise RuntimeError("rapidshare: " + str(
            resp.get("error") or "no entregó la URL directa"))
    # el enlace de descarga exige la sesión (cookie) obtenida al resolverlo
    cookies = "; ".join("%s=%s" % (c.name, c.value) for c in cj)

    # nombre y tamaño: la página no expone el tamaño del archivo (el
    # número grande que aparece es el límite de la cuenta, no el archivo),
    # así que se deja None para que el motor lo detecte al empezar.
    nombre = None
    tm = re.search(r"<title>([^<]*)</title>", html, re.I)
    if tm:
        t = tm.group(1).strip()
        mm = re.search(r"—\s*Download\s*—\s*(.+)$", t, re.I)
        if mm:
            nombre = mm.group(1).strip()
    # OJO: el enlace de descarga de rapidshare es de UN SOLO USO (el probe
    # del motor lo consumiría y la descarga fallaría con 401): forzamos
    # descarga en una sola conexión, sin probe.
    return {"url": directa, "nombre": nombre, "tamano": None,
            "pagina": url, "cookies": cookies, "unico": True}


def _extraer_solred(url):
    """Solred.app: el botón de descarga se carga por AJAX y lleva la URL
    directa del CDN en su onclick (window.location='<url>?download_token=...').

      1. GET la página -> extrae showFile(<fileId>, 'true')
      2. POST /account/ajax/file_details_2 con u=<fileId>
         -> JSON {html} con el botón y su URL directa (descarga segmentada
         con el token: el servidor redirige al CDN real).
    """
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Accept-Language", "es-ES,es;q=0.9")]
    try:
        with op.open(url, timeout=TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"solred bloqueó la página (HTTP {e.code})") from e
    except Exception as e:
        raise RuntimeError(f"no se pudo abrir la página solred: {e}") from e

    # fileId real del archivo (showFile(<id>, 'true'))
    m = re.search(r"showFile\s*\(\s*(\d+)\s*,", html)
    if not m:
        raise RuntimeError("solred no expuso el identificador del archivo "
                           "(¿enlace borrado o caducado?)")
    file_id = m.group(1)
    # el botón con la URL directa se carga por AJAX
    try:
        datos = urllib.parse.urlencode({"u": file_id, "isfront": "true"}).encode()
        req = urllib.request.Request(
            "https://solred.app/account/ajax/file_details_2", data=datos,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "X-Requested-With": "XMLHttpRequest", "Referer": url,
                     "Accept": "application/json"})
        with op.open(req, timeout=TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8", errors="replace"))
        html_det = (resp or {}).get("html") or ""
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"solred rechazó la consulta del archivo (HTTP {e.code})") from e
    except Exception as e:
        raise RuntimeError(f"solred no cargó los detalles: {e}") from e
    m2 = re.search(r"window\.location='(https?://[^']+)'", html_det)
    if not m2:
        m2 = re.search(r'href="(https?://[^"]*download_token=[^"]+)"', html_det)
    if not m2:
        raise RuntimeError("solred no mostró el enlace de descarga "
                           "(¿archivo borrado o caducado?)")
    directa = m2.group(1).replace("&amp;", "&")

    nombre = None
    tm = re.search(r"<title>([^<]*)</title>", html, re.I)
    if tm:
        nombre = tm.group(1).strip()
    tam = None
    mm = re.search(r"Download\s*\(([\d.]+)\s*(GB|MB|KB)\)", html_det
                   + " " + html, re.I)
    if mm:
        try:
            n = float(mm.group(1))
            u = mm.group(2).upper()
            tam = int(n * (1024 ** {"KB": 1, "MB": 2, "GB": 3}[u]))
        except Exception:
            tam = None
    cookies = "; ".join("%s=%s" % (c.name, c.value) for c in cj)
    return {"url": directa, "nombre": nombre, "tamano": tam,
            "pagina": url, "cookies": cookies}


def _extraer_marketcat(url):
    """drive.marketcat.io (MarketCat Drive, también usado por solred.app):
    el enlace compartido /drive/s/<hash> expone una API REST pública:

      1. GET /api/v1/shareable-links/<hash>?loader=shareableLink
         -> JSON con link.id y link.entry_id
      2. token = base64("<entry_id>|padd")
      3. GET /api/v1/file-entries/download/<token>?shareable_link=<link.id>
         -> redirige a la URL firmada del CDN (access.marketcat.io)
    """
    import base64
    import http.cookiejar
    m = re.search(r"/drive/s/([A-Za-z0-9_-]+)", url)
    hash_id = m.group(1) if m else None
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA)]
    if not hash_id:
        raise RuntimeError("marketcat: enlace compartido no reconocido")
    try:
        req = urllib.request.Request(
            f"https://drive.marketcat.io/api/v1/shareable-links/"
            f"{urllib.parse.quote(hash_id)}?loader=shareableLink",
            headers={"Accept": "application/json",
                     "X-Requested-With": "XMLHttpRequest",
                     "Referer": url})
        with op.open(req, timeout=TIMEOUT) as r:
            datos = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"marketcat no encontró el enlace (HTTP {e.code}); "
            f"¿enlace caducado o borrado?") from e
    except Exception as e:
        raise RuntimeError(f"marketcat rechazó la consulta: {e}") from e
    link = (datos or {}).get("link") or {}
    entry = link.get("entry") or {}
    entry_id = link.get("entry_id")
    link_id = link.get("id")
    if not entry_id or not link_id:
        raise RuntimeError("marketcat: el enlace no tiene archivo descargable")
    if not link.get("allow_download"):
        raise RuntimeError("marketcat: la descarga no está permitida para "
                           "este enlace")
    tok = base64.b64encode(f"{entry_id}|padd".encode()).decode().rstrip("=")
    directa = (f"https://drive.marketcat.io/api/v1/file-entries/download/"
               f"{urllib.parse.quote(tok)}?shareable_link={link_id}")
    nombre = entry.get("name") or None
    tam = entry.get("size")
    try:
        tam = int(tam) if tam else None
    except (TypeError, ValueError):
        tam = None
    # el enlace de descarga exige la sesión (cookie) obtenida al resolverlo
    cookies = "; ".join("%s=%s" % (c.name, c.value) for c in cj)
    return {"url": directa, "nombre": nombre, "tamano": tam,
            "pagina": url, "cookies": cookies}


def _extraer_gofile(url):
    """Gofile: la API necesita un token de cuenta (los invitados lo obtienen
    al subir; el listado directo es Premium). Extraemos el guest token de la
    página si está presente; si no, error claro.
    """
    m = re.search(r"/d/([A-Za-z0-9_-]+)", url)
    cid = m.group(1) if m else None
    if not cid:
        raise RuntimeError("enlace gofile sin código de carpeta (/d/XXX)")

    # la API pública exige token y el listado directo es Premium:
    # pedimos el token de invitado generado por la propia web
    token = None
    try:
        with _pedir(url) as r:
            html = r.read().decode("utf-8", errors="replace")
        tm = re.search(r'"guestToken"\s*:\s*"([^"]+)"', html)
        if tm:
            token = tm.group(1)
    except Exception:
        pass
    if not token:
        raise RuntimeError(
            "gofile cambió su API: ahora exige cuenta (el listado directo es "
            "Premium). Abre el enlace en tu navegador y copia el enlace "
            "directo del archivo (botón 'Direct link')")

    try:
        with _pedir(
            f"https://api.gofile.io/contents/{cid}?token={urllib.parse.quote(token)}",
        ) as r:
            datos = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        raise RuntimeError(f"gofile rechazó la consulta: {e}") from e
    if datos.get("status") != "ok":
        raise RuntimeError("gofile: " + str(datos.get("status") or "error"))
    d = datos.get("data") or {}
    hijos = d.get("children") or {}
    if not hijos:
        raise RuntimeError("gofile: la carpeta está vacía o es Premium")
    primero = next(iter(hijos.values()))
    return {"url": primero.get("link"), "nombre": primero.get("name"),
            "tamano": primero.get("size"), "pagina": url}


def _extraer_fuckingfast(url):
    """Resuelve un enlace fuckingfast.net a su URL directa firmada.

    La página responde 403 a cualquier cliente HTTP normal (exige navegador
    real con cookies de sesión). Se abre con Chrome vía CDP (el mismo perfil
    que usa el extractor de ZonaLeros), se lee el token firmado del botón
    'Copy download link' (/<id>/download?t=<token>) y se hace fetch de esa
    URL con la sesión del navegador: el servidor redirige a la URL real
    (ts.fuckingfast.net/d/<id>?v=<firma>) con nombre y tamaño del archivo.
    """
    import zonaleros_copia as zonaleros  # módulo único; evita dependencia circular con servidor.py

    if zonaleros._chrome_corriendo():
        raise RuntimeError(
            "fuckingfast exige el navegador real para resolver el enlace: "
            "cierra Chrome del todo y reintenta.")
    ws_url, err = zonaleros._lanzar(url)
    if err:
        raise RuntimeError(err)
    cdp = None
    try:
        cdp = zonaleros._Cdp(ws_url)
        # espera a que cargue y aparezca el botón con el token
        fin = time.time() + 45
        path = None
        while time.time() < fin:
            try:
                html = cdp.eval("document.documentElement.outerHTML") or ""
            except Exception:
                html = ""
            m = re.search(r"copyDownloadLink\('\\/([^']+)'\)", html)
            if m:
                path = m.group(1).replace("\\/", "/")
                break
            time.sleep(3)
        if not path:
            raise RuntimeError(
                "fuckingfast no expuso el botón de descarga (¿reto de "
                "Cloudflare no resuelto?)")
        # fetch de /download?t=<token> dentro del contexto del navegador:
        # la respuesta redirige a la URL firmada real del archivo
        expr = (
            "fetch('/" + path + "', {method:'GET', credentials:'include', "
            "redirect:'follow'})"
            ".then(r => ({status: r.status, url: r.url, "
            "ctype: r.headers.get('content-type'), "
            "clen: r.headers.get('content-length'), "
            "disp: r.headers.get('content-disposition')}))"
        )
        r = cdp._cmd("Runtime.evaluate", {
            "expression": expr, "awaitPromise": True, "returnByValue": True})
        info = (r.get("result") or {}).get("result", {}).get("value")
        if not info or not info.get("url") or info.get("status") != 200:
            raise RuntimeError(
                "fuckingfast no entregó la URL directa (status " +
                str((info or {}).get("status")) + ")")
        # nombre real desde Content-Disposition (si viene)
        nombre = None
        disp = info.get("disp") or ""
        m2 = re.search(r"filename\*?=(?:UTF-8''|\"?)([^\";]+)", disp)
        if m2:
            nombre = urllib.parse.unquote(m2.group(1).strip('"'))
        return {"url": info["url"], "nombre": nombre,
                "tamano": info.get("clen"), "pagina": url}
    finally:
        if cdp:
            try:
                cdp.cerrar()
            except Exception:
                pass


def _extraer_1fichier(url):
    """1fichier: página con countdown (60 s para invitados) y POST.

    Flujo (descubierto experimentalmente):
      1. GET la página -> guarda cookies y lee el countdown (var ct = N).
      2. Esperar N segundos (el servidor valida el tiempo transcurrido).
      3. POST a la misma URL con las mismas cookies (dl_no_ssl/dlinline).
      4. La respuesta trae el enlace real: http(s)://a-<n>.1fichier.com/<id>
         (botón 'Start your download'), que soporta Range.
    """
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Accept-Language", "es-ES,es;q=0.9")]
    try:
        with op.open(url, timeout=TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"1fichier bloqueó la página (HTTP {e.code})") from e
    except Exception as e:
        raise RuntimeError(f"no se pudo abrir la página 1fichier: {e}") from e

    # countdown (60 s para invitados)
    m = re.search(r"var ct\s*=\s*(\d+)", html)
    espera = int(m.group(1)) if m else 60
    # el servidor valida que pasó el tiempo; esperamos un margen extra
    time.sleep(espera + 2)

    data = urllib.parse.urlencode(
        {"dl_no_ssl": "on", "dlinline": "on"}).encode("utf-8")
    try:
        with op.open(urllib.request.Request(url, data=data), timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"1fichier rechazó la descarga (HTTP {e.code})") from e
    except Exception as e:
        raise RuntimeError(f"1fichier falló al pedir la descarga: {e}") from e

    # enlace real: a-<n>.1fichier.com/<id>[?inline]
    m = re.search(r"href=\"(https?://a-\d+\.1fichier\.com/[^\"'\s]+)\"",
                  body, re.I)
    if not m:
        if "temporarily limited" in body.lower() or "high demand" in body.lower():
            raise RuntimeError(
                "1fichier limita las descargas gratuitas por alta demanda "
                "en este momento; intenta en unos minutos.")
        raise RuntimeError(
            "1fichier no entregó el enlace de descarga "
            "(¿archivo borrado o requiere cuenta?)")
    directa = m.group(1)
    if not directa.startswith("https"):
        directa = "https:" + directa  # forzar TLS

    # nombre y tamaño de la página inicial (los trae en el título)
    nombre = None
    tm = re.search(r"<span[^>]*>([^<]+\.(?:rar|zip|7z|001|r00|mp4))</span>",
                   html, re.I)
    if tm:
        nombre = tm.group(1).strip()
    tam = None
    mm = re.search(r"([\d.]+)\s*(GB|MB|KB)", html)
    if mm:
        try:
            n = float(mm.group(1))
            u = mm.group(2).upper()
            tam = int(n * (1024 ** {"KB": 1, "MB": 2, "GB": 3}[u]))
        except Exception:
            tam = None
    return {"url": directa, "nombre": nombre, "tamano": tam, "pagina": url}


def _drive_nombre(cd):
    """Extrae el nombre de archivo de un Content-Disposition."""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.I)
    if m:
        return urllib.parse.unquote(m.group(1).strip())
    return None


def _mensaje_drive(html):
    """Convierte la página de error de Google Drive en un mensaje claro."""
    h = html.lower()
    if "too large" in h or "exceeded" in h or "cannot scan" in h:
        return ("Google Drive no pudo escanear el archivo (muy grande o con "
                "virus). Probá descargarlo manualmente desde el navegador.")
    if "accounts.google.com" in h or "sign in" in h:
        return ("Google Drive pide iniciar sesión: el archivo no está "
                "compartido como 'cualquier persona con el enlace'.")
    return ("Google Drive respondió con una página de verificación y no se "
            "pudo obtener el token de descarga. Probá más tarde.")


def _extraer_google_drive(url):
    """Google Drive: enlace compartido -> URL directa de descarga.

    El enlace /file/d/<ID>/view es una PÁGINA, no el archivo. Resolvemos el
    ID y apuntamos al endpoint de descarga drive.usercontent.google.com. Si
    Google responde con la página de confirmación (archivos grandes o aviso
    de virus), extraemos el token 'confirm' del formulario y reintentamos.
    Devuelve {"url", "nombre", "tamano", "pagina"} o lanza RuntimeError.
    """
    m = re.search(r"/file/d/([^/?#]+)|[?&]id=([^&#]+)", url)
    fid = (m.group(1) or m.group(2)) if m else None
    if not fid:
        raise RuntimeError(
            "no es un enlace de archivo de Google Drive: usá uno del tipo "
            ".../file/d/<ID>/view (los enlaces de carpeta no se soportan)")
    fid = re.sub(r"[^A-Za-z0-9_-]", "", fid)
    if not fid:
        raise RuntimeError("el enlace de Google Drive no tiene un ID válido")

    base = "https://drive.usercontent.google.com/download"
    token = "t"
    uuid = ""
    for intento in range(3):
        directa = "%s?id=%s&export=download&confirm=%s" % (base, fid, token)
        if uuid:
            directa += "&uuid=%s" % uuid
        try:
            req = urllib.request.Request(directa, headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Range": "bytes=0-0",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT,
                                        context=_CTX) as r:
                final = r.geturl()
                tipo = (r.headers.get("Content-Type") or "").lower()
                if "text/html" in tipo:
                    # página de confirmación o "no se puede escanear": el
                    # formulario trae confirm (+ uuid para el 'Download
                    # anyway') -> re-solicitar con esos valores
                    html = r.read(300000).decode("utf-8", errors="replace")
                    m2 = re.search(r'name="confirm"\s+value="([^"]+)"',
                                   html)
                    m3 = re.search(r'name="uuid"\s+value="([^"]+)"',
                                   html)
                    nuevo_token = m2.group(1) if m2 else None
                    nuevo_uuid = m3.group(1) if m3 else None
                    if (nuevo_token or nuevo_uuid) and intento < 2:
                        if nuevo_token:
                            token = nuevo_token
                        if nuevo_uuid:
                            uuid = nuevo_uuid
                        continue
                    raise RuntimeError(_mensaje_drive(html))
                # binario: el rango 0-0 alcanza; cerrar sin bajar el resto
                r.read(1)
                cr = r.headers.get("Content-Range", "")
                m3 = re.search(r"/(\d+)\s*$", cr)
                tamano = int(m3.group(1)) if m3 else None
                if tamano is None:
                    cl = r.headers.get("Content-Length")
                    tamano = int(cl) if cl and cl.isdigit() else None
                nombre = _drive_nombre(
                    r.headers.get("Content-Disposition", ""))
                if not nombre:
                    nombre = "descarga_drive_%s" % fid[:8]
                return {"url": final, "nombre": nombre, "tamano": tamano,
                        "pagina": url}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError(
                    "Google Drive no encontró el archivo (¿borrado o enlace "
                    "mal copiado?)") from e
            if e.code in (403, 429):
                raise RuntimeError(
                    "Google Drive bloqueó la descarga (HTTP %d). El archivo "
                    "debe estar compartido como 'cualquier persona con el "
                    "enlace'." % e.code) from e
            raise RuntimeError(
                "Google Drive respondió HTTP %d" % e.code) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                "no se pudo conectar con Google Drive: %s" % e.reason) from e
    raise RuntimeError("no se pudo resolver el enlace de Google Drive")


def _drive_ivd_entradas(cid):
    """Devuelve [(nombre, id, es_carpeta), ...] de una carpeta compartida de
    Google Drive, leyendo el JSON embebido en la página de la carpeta
    (window['_DRIVE_ivd']). Soporta el formato actual (escapado con \\xNN)
    y el clásico (JSON plano)."""
    pagina = "https://drive.google.com/drive/folders/%s" % cid
    try:
        req = urllib.request.Request(pagina, headers={
            "User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as r:
            html = r.read(2000000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            "Google Drive no encontró la carpeta (HTTP %d)" % e.code) from e
    except Exception as e:
        raise RuntimeError(
            "no se pudo abrir la carpeta de Google Drive: %s" % e) from e
    datos = None
    # formato actual: string con escapes \xNN
    m = re.search(r"window\['_DRIVE_ivd'\]\s*=\s*'([^']+)';", html)
    if m:
        try:
            datos = json.loads(
                m.group(1).encode("utf-8").decode("unicode_escape"))
        except Exception:
            datos = None
    # formato clásico: JSON plano
    if datos is None:
        m = re.search(r"window\['_DRIVE_ivd'\]\s*=\s*(\[.*?\]);", html, re.S)
        if m:
            try:
                datos = json.loads(m.group(1))
            except Exception:
                datos = None
    if not isinstance(datos, list):
        raise RuntimeError(
            "no se pudo leer el contenido de la carpeta (¿vacía o sin "
            "permiso?)")

    def es_nuevo(info):
        # [id, [padres], nombre, mime, ...]
        return (isinstance(info, list) and len(info) > 3
                and isinstance(info[0], str)
                and re.match(r"[A-Za-z0-9_-]{25,}", info[0] or "")
                and isinstance(info[1], list)
                and isinstance(info[2], str)
                and isinstance(info[3], str))

    def es_clasico(info):
        # [nombre, id, tipo]
        return (isinstance(info, list) and len(info) > 2
                and isinstance(info[0], str)
                and isinstance(info[1], str)
                and re.match(r"[A-Za-z0-9_-]{25,}", info[1] or "")
                and isinstance(info[2], int))

    def colectar(nodo, salida, profundidad):
        # Google anida las entradas de forma distinta según la respuesta:
        # a veces data[0] es la lista, a veces cada data[i] envuelve una.
        # Recorremos en profundidad (acotado) recogiendo toda entrada que
        # tenga forma de archivo/carpeta.
        if profundidad > 5:
            return
        if es_nuevo(nodo):
            salida.append((str(nodo[2]), str(nodo[0]),
                           (nodo[3] or "") ==
                           "application/vnd.google-apps.folder"))
            return
        if es_clasico(nodo):
            salida.append((str(nodo[0]), str(nodo[1]), nodo[2] == 2))
            return
        if isinstance(nodo, list):
            for x in nodo:
                colectar(x, salida, profundidad + 1)

    salida = []
    colectar(datos, salida, 0)
    # quitar duplicados por id (mismo archivo puede aparecer varias veces)
    vistos = set()
    unicos = []
    for nombre, fid, es_carpeta in salida:
        if fid not in vistos:
            vistos.add(fid)
            unicos.append((nombre, fid, es_carpeta))
    return unicos


def _lista_carpeta_drive(url):
    """Carpeta compartida de Drive -> lista de archivos
    [{"url", "nombre"}...]. Recorre subcarpetas hasta 3 niveles."""
    m = re.search(
        r"/drive/(?:u/\d+/)?folders/([^/?#]+)"
        r"|[?&]folder=([^&#]+)"
        r"|folderview\?id=([^&#]+)", url)
    cid = next((g for g in m.groups() if g), None) if m else None
    if not cid:
        return None
    archivos = []

    def expandir(carpeta_id, prof):
        if prof > 3:
            return
        for nombre, fid, es_carpeta in _drive_ivd_entradas(carpeta_id):
            if es_carpeta:
                expandir(fid, prof + 1)
            else:
                archivos.append({
                    "url": "https://drive.google.com/file/d/%s/view" % fid,
                    "nombre": nombre,
                })

    expandir(cid, 0)
    if not archivos:
        raise RuntimeError("la carpeta está vacía o sin archivos accesibles")
    return archivos


def resolver(url):
    """Intenta convertir un enlace de file hoster en su URL directa.

    Devuelve dict {"url", "nombre", "tamano", "pagina"}, {"carpeta_drive":
    [...]} para carpetas de Google Drive, o None si el dominio no está
    soportado.
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in ("rootz.so", "www.rootz.so"):
        return _extraer_rootz(url)
    if host in ("fireload.com", "www.fireload.com"):
        return _extraer_fireload(url)
    if host in ("megaup.net", "www.megaup.net"):
        return _extraer_megaup(url)
    if host in ("gofile.io", "www.gofile.io"):
        return _extraer_gofile(url)
    if host in ("mediafire.com", "www.mediafire.com"):
        return _extraer_mediafire(url)
    if host in ("fuckingfast.net", "www.fuckingfast.net"):
        return _extraer_fuckingfast(url)
    if host in ("1fichier.com", "www.1fichier.com"):
        return _extraer_1fichier(url)
    if host in ("lolaup.com", "www.lolaup.com"):
        return _extraer_lolaup(url)
    if host in ("rapidshare.co", "www.rapidshare.co"):
        return _extraer_rapidshare(url)
    if host in ("solred.app", "www.solred.app"):
        return _extraer_solred(url)
    if host in ("drive.marketcat.io",):
        return _extraer_marketcat(url)
    if host in ("drive.google.com", "www.drive.google.com",
                "drive.usercontent.google.com"):
        if host != "drive.usercontent.google.com" and (
                "/folders/" in url or "folderview" in url
                or "?folder=" in url):
            return {"carpeta_drive": _lista_carpeta_drive(url)}
        return _extraer_google_drive(url)
    if host in ("upto.cash", "www.upto.cash"):
        raise RuntimeError(
            "upto.cash exige resolver un captcha en el navegador. "
            "Abre el enlace manualmente y descarga el archivo.")
    return None
