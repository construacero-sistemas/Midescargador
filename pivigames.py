# -*- coding: utf-8 -*-
"""Extractor de enlaces de descarga de pivigames.blog.

Pivigames usa playpaste.net para mantener sus enlaces (igual que zona-leros
usa zpaste.net): cada botón de la página del juego es una imagen que apunta
a https://playpaste.net/pivi/?v=<codigo>, y cada paste está protegido con un
reto de Turnstile de Cloudflare ("Comprueba que eres humano" -> Continuar).

Se reutiliza la infraestructura de zonaleros (Chrome real vía CDP, clic en el
widget de Turnstile y el clasificador de multipartes), cambiando solo la
página de origen y el flujo del paste.
"""
import re
import time
import urllib.request

import zonaleros  # CDP, _lanzar, _clasificar_enlaces, _leer_enlaces_paste...

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _es_pivigames(url):
    return "pivigames.blog" in (url or "").lower()


def _leer_enlaces_paste(cdp):
    """Lee los enlaces de descarga visibles del paste (reutiliza el de
    zonaleros, que ya filtra el acortador y normaliza)."""
    return zonaleros._leer_enlaces_paste(cdp)


def _token_turnstile(cdp):
    """Devuelve el token de Turnstile del input oculto (o '' si aún no
    está). El Turnstile de playpaste es INVISIBLE: el token se rellena solo
    en ~10-20 s, sin necesidad de pulsar nada."""
    try:
        return (cdp.eval(
            "(document.querySelector('input[name=\"cf-turnstile-response\"]') || {}).value || ''")
            or "")
    except Exception:
        return ""


def _pulsar_continuar(cdp):
    """Envía el formulario del paste (botón 'Continuar'), solo con token."""
    cdp.eval('''(() => {
        const f = document.querySelector('form');
        if (f) { f.submit(); return true; }
        const b = [...document.querySelectorAll('button,input[type=submit]')]
            .find(x => /continuar/i.test((x.innerText || x.value || '')));
        if (b) { b.click(); return true; }
        return false;
    })()''')
    time.sleep(3)


def _resolver_paste(cdp, fin_global):
    """Resuelve UN paste de playpaste: espera el token invisible de
    Turnstile, envía 'Continuar' y espera a que aparezcan los enlaces.
    Reintenta una vez recargando el paste (como hace zonaleros con zpaste).
    Devuelve la lista de enlaces (puede ser vacía)."""
    enlaces = []
    for intento in range(2):
        if time.time() >= fin_global:
            break
        # 1) espera el token de Turnstile (invisible, a veces tarda 25-60 s).
        #    NUNCA pulsar 'Continuar' antes: el submit con token vacío mata
        #    el reto y devuelve 'Captcha incorrecto'.
        fin_token = min(time.time() + 70, fin_global)
        while time.time() < fin_token:
            if _token_turnstile(cdp):
                break
            # si el widget no es invisible, dale un clic real a su iframe
            cdp.clic_widget_turnstile()
            time.sleep(2)
        if not _token_turnstile(cdp):
            # sin token en este intento: recarga (nueva sesión de reto)
            if intento == 1:
                break
            if not _recargar_paste(cdp):
                break
            continue
        # 2) con token listo: envía el formulario
        _pulsar_continuar(cdp)
        # 3) espera a que aparezcan los enlaces (la respuesta del POST
        #    revela la página con los botones de descarga)
        fin_links = min(time.time() + 30, fin_global)
        while time.time() < fin_links:
            enlaces = _leer_enlaces_paste(cdp)
            # descarta enlaces a otros pastes de playpaste (los botones de
            # la página del juego quedan visibles en el HTML revelado)
            enlaces = [e for e in enlaces
                       if "playpaste.net" not in e.get("url", "")]
            if enlaces:
                return enlaces
            # 'Captcha incorrecto' = el token no era válido: reintenta
            if (cdp.eval("(document.body.innerText || '').indexOf('Captcha incorrecto') !== -1") or False):
                break
            time.sleep(3)
        if enlaces or time.time() >= fin_global or intento == 1:
            break
        # reintenta: recarga el paste (nueva sesión de reto)
        if not _recargar_paste(cdp):
            break
    return enlaces


def _recargar_paste(cdp):
    """Recarga el paste actual (nueva sesión de reto Turnstile). Devuelve
    True si el paste volvió a cargar."""
    paste_url = cdp.eval("location.href") or ""
    if not paste_url or "playpaste.net" not in paste_url:
        return False
    return bool(cdp.navegar(paste_url,
                            condicion="location.href.indexOf('playpaste.net') !== -1",
                            tiempo_max=35))


def extraer(url):
    """Flujo completo: abre la página del juego de pivigames, recoge los
    botones (playpaste), resuelve el Turnstile de cada paste y lee los
    enlaces finales de cada servidor. Devuelve {"servidores", "titulo"}."""
    # la página del juego responde sin navegador: la usamos para los botones
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept-Language": "es-ES,es;q=0.9"})
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return {"error": "no se pudo abrir la página de pivigames: %s" % e}

    # botones: cada <a href> a playpaste con la etiqueta del strong/h4 más
    # cercano hacia atrás (la estructura del post es: etiqueta -> imagen)
    botones = []
    vistos = set()
    enlaces = [(m.start(), m.group(1)) for m in
               re.finditer(r'<a href="(https://playpaste\.net/pivi/\?v=[^"]+)"',
                           html, re.I)]
    etiquetas = list(re.finditer(
        r'<(?:strong|h4)[^>]*>(.*?)</(?:strong|h4)>', html, re.S | re.I))
    for pos, href in enlaces:
        if href in vistos:
            continue
        # strong/h4 más cercano ANTES del enlace
        mejor = None
        for m in etiquetas:
            if m.end() <= pos:
                txt = re.sub(r'<[^>]+>', ' ', m.group(1))
                txt = re.sub(r'\s+', ' ', txt).strip()
                if txt and (mejor is None or m.end() > mejor[0]):
                    mejor = (m.end(), txt)
        etiqueta = mejor[1] if mejor else ""
        vistos.add(href)
        botones.append({"h": href, "t": etiqueta})

    if not botones:
        return {"error": "no se encontraron botones de descarga en la página"}

    # título del juego
    tm = re.search(r"<title>([^<]+)</title>", html, re.I)
    titulo_juego = tm.group(1).strip() if tm else ""
    titulo_juego = re.sub(r"^\s*[▷▶•]+\s*", "", titulo_juego)
    titulo_juego = re.sub(r"\s*\|\s*PiviGames\s*$", "", titulo_juego)

    # ahora resuelve cada paste con Chrome (Turnstile)
    ws_url, err = zonaleros._lanzar("about:blank")
    if err:
        return {"error": err}
    cdp = None
    fin_global = time.time() + 420   # tope total ~7 min (4 pastes x ~60 s)
    servidores = []
    try:
        cdp = zonaleros._Cdp(ws_url)
        for b in botones:
            servidor = (b.get("t") or "?").strip()[:60] or "?"
            if time.time() >= fin_global:
                break
            if not cdp.navegar(b["h"],
                               condicion="location.href.indexOf('playpaste.net') !== -1",
                               tiempo_max=40):
                servidores.append({"servidor": servidor, "enlaces": [],
                                   "error": "no se pudo abrir el paste"})
                continue
            enlaces = _resolver_paste(cdp, fin_global)
            # clasifica multipartes del mismo archivo vs sueltos
            clasificado = zonaleros._clasificar_enlaces([
                {"url": e["url"], "texto": e.get("texto") or "",
                 "nombre": zonaleros._nombre_de_url(e["url"])} for e in enlaces])
            clasificado["servidor"] = servidor
            servidores.append(clasificado)
        return {"servidores": servidores, "titulo": titulo_juego[:150]}
    except Exception as e:
        if not zonaleros._chrome_corriendo():
            return {"error": ("Chrome se cerró a mitad de la extracción "
                              "(quizá lo abriste o se actualizó). Ciérralo "
                              "del todo y vuelve a intentar.")}
        return {"error": "error extrayendo: %s" % e}
    finally:
        if cdp:
            try:
                cdp.cerrar()
            except Exception:
                pass
        zonaleros._matar_chrome()
        zonaleros._restaurar_cookies_si_danadas()
