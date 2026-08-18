# -*- coding: utf-8 -*-
"""Extractor de enlaces de descarga de pivigames.blog.

Pivigames usa playpaste.net para mantener sus enlaces (igual que zona-leros
usa zpaste.net): cada botón de la página del juego es una imagen que apunta
a https://playpaste.net/pivi/?v=<codigo>, y cada paste está protegido con un
reto de Turnstile de Cloudflare ("Comprueba que eres humano" -> Continuar).

Dos particularidades de pivigames que este extractor maneja:

1. PASTES ENCADENADOS. Un botón puede revelar enlaces a OTROS pastes de
   playpaste (p. ej. el botón "Mega, Mediafire y Gofile" revela el enlace de
   Gofile + dos pastes más, uno con el enlace de Mega y otro con el de
   MediaFire). Se resuelven en cadena hasta agotarlos.

2. UN SERVIDOR = UN HOSTER. Al final los enlaces se agrupan por el servidor
   real (Mega, MediaFire, Gofile, Qiwi, PixelDrain, Torrent...), no por la
   etiqueta del botón, para que cada servidor aparezca por separado.

Se reutiliza la infraestructura de zonaleros (Chrome real vía CDP, clic en el
widget de Turnstile y el clasificador de multipartes).
"""
import json
import re
import time
import urllib.parse
import urllib.request

import torrents   # _url_torrent_directa (madiashare -> .torrent)
import zonaleros_copia as zonaleros  # CDP, _lanzar, _clasificar_enlaces, _leer_enlaces_paste...

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# (fragmento de dominio, nombre de servidor) para agrupar por hoster real
_HOSTERS = (
    ("mega.nz", "Mega"), ("mega.co.nz", "Mega"),
    ("mediafire.com", "MediaFire"),
    ("gofile.io", "Gofile"),
    ("qiwi.gg", "Qiwi"),
    ("pixeldrain.com", "PixelDrain"),
    ("madiashare.com", "Torrent"),
    ("drive.google.com", "Google Drive"),
    ("megaup.net", "MegaUp"),
    ("1fichier.com", "1Fichier"),
    ("fuckingfast.net", "FuckingFast"),
    ("rootz.so", "Rootz"), ("www.rootz.so", "Rootz"),
    ("fireload.com", "Fireload"),
)


def _es_pivigames(url):
    return "pivigames.blog" in (url or "").lower()


def _nombre_hoster(url):
    """Nombre de servidor del enlace según su dominio real."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for frag, nombre in _HOSTERS:
        if frag in host:
            return nombre
    return host.replace("www.", "") or "?"


def _leer_enlaces_paste(cdp):
    """Lee los enlaces visibles del paste (reutiliza el de zonaleros, que ya
    filtra el acortador y normaliza). Incluye los playpaste encadenados."""
    return zonaleros._leer_enlaces_paste(cdp)


def _separar_enlaces(crudo, pastes_hijos=None):
    """Separa los enlaces de descarga de los que hay que ignorar:
    playpaste encadenados (se siguen), pivigames.blog (el footer del paste)
    y el acortador. Devuelve (enlaces_finales, pastes_hijos_actualizados)."""
    pastes_hijos = pastes_hijos if pastes_hijos is not None else []
    enlaces = []
    for e in crudo:
        u = e.get("url", "")
        if "playpaste.net" in u:
            if u not in pastes_hijos:
                pastes_hijos.append(u)
            continue
        if "pivigames.blog" in u or "zshorte" in u or "anomizador" in u:
            continue
        enlaces.append(e)
    return enlaces, pastes_hijos


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


def _recargar_paste(cdp):
    """Recarga el paste actual (nueva sesión de reto Turnstile). Devuelve
    True si el paste volvió a cargar."""
    paste_url = cdp.eval("location.href") or ""
    if not paste_url or "playpaste.net" not in paste_url:
        return False
    return bool(cdp.navegar(paste_url,
                            condicion="location.href.indexOf('playpaste.net') !== -1",
                            tiempo_max=35))


def _revelar_pestanas(cdp):
    """Algunos pastes de playpaste muestran PESTAÑAS (p. ej. MEGA /
    MEDIAFIRE / ROOTZ): solo la activa está visible y el resto se carga
    al pulsarlas. Pulsa cada pestaña y devuelve los enlaces de todas
    (sin duplicados). Devuelve [] si el paste no tiene pestañas."""
    expr_tabs = r'''(() => [...document.querySelectorAll('a,button,div,li,span')].filter(x => {
        const cls = ((x.className||'').toString() + ' ' + ((x.parentElement&&x.parentElement.className)||'').toString()).toLowerCase();
        if (cls.indexOf('tab') === -1) return false;
        const t = (x.innerText || x.title || '').trim();
        return t && t.length < 40 && !/^https?:/i.test(t);
    }).map(x => (x.innerText || x.title || '').trim()))()'''
    nombres = cdp.eval(expr_tabs) or []
    extra = []
    vistos = set()
    for nombre in dict.fromkeys(nombres):
        ok = cdp.eval('''(() => {
            for (const x of document.querySelectorAll('a,button,div,li,span')) {
                const cls = ((x.className||'').toString() + ' ' + ((x.parentElement&&x.parentElement.className)||'').toString()).toLowerCase();
                if (cls.indexOf('tab') === -1) continue;
                const t = (x.innerText || x.title || '').trim();
                if (t === %s) { x.click(); return true; }
            }
            return false;
        })()''' % json.dumps(nombre))
        if not ok:
            continue
        time.sleep(1.2)
        for e in _leer_enlaces_paste(cdp):
            u = e.get("url") or ""
            if u and u not in vistos:
                vistos.add(u)
                extra.append(e)
    return extra


def _resolver_paste(cdp, fin_global):
    """Resuelve UN paste de playpaste: espera el token invisible de
    Turnstile, envía 'Continuar' y espera a que aparezcan los enlaces.
    Reintenta una vez recargando el paste. Devuelve (enlaces_finales,
    pastes_hijos): enlaces_finales son los enlaces de descarga (sin
    playpaste) y pastes_hijos los playpaste encadenados que hay que seguir.
    """
    enlaces = []
    pastes_hijos = []
    for intento in range(2):
        if time.time() >= fin_global:
            break
        # 0) hay pastes SIN protección (el contenido ya está visible, sin
        #    formulario ni reto): devolver lo que haya sin esperar nada
        crudo = _leer_enlaces_paste(cdp)
        enlaces, pastes_hijos = _separar_enlaces(crudo, pastes_hijos)
        if enlaces:
            return enlaces, pastes_hijos
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
            crudo = _leer_enlaces_paste(cdp)
            enlaces, pastes_hijos = _separar_enlaces(crudo, pastes_hijos)
            if enlaces:
                return enlaces, pastes_hijos
            # 'Captcha incorrecto' = el token no era válido: reintenta
            if (cdp.eval("(document.body.innerText || '').indexOf('Captcha incorrecto') !== -1") or False):
                break
            time.sleep(3)
        if enlaces or time.time() >= fin_global or intento == 1:
            break
        # reintenta: recarga el paste (nueva sesión de reto)
        if not _recargar_paste(cdp):
            break
    return enlaces, pastes_hijos


def extraer(url):
    """Flujo completo: abre la página del juego de pivigames, recoge los
    botones (playpaste), resuelve la cadena de pastes (Turnstile) y agrupa
    los enlaces por servidor real. Devuelve {"servidores", "titulo"}."""
    # la página del juego responde sin navegador: la usamos para los botones
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept-Language": "es-ES,es;q=0.9"})
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return {"error": "no se pudo abrir la página de pivigames: %s" % e}

    # botones: cada <a href> a playpaste con la etiqueta del strong/h4 más
    # cercano hacia atrás (la estructura del post es: etiqueta -> imagen).
    # OJO: los enlaces de playpaste aparecen en 3 formas distintas:
    #   playpaste.net/pivi/?v=X   playpaste.net/pivi?v=X   playpaste.net/?v=X
    botones = []
    vistos = set()
    enlaces = [(m.start(), m.group(1)) for m in
               re.finditer(r'<a href="(https://playpaste\.net/(?:pivi/?)?\?v=[A-Za-z0-9_-]+)"',
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

    # resuelve la cadena de pastes con Chrome (Turnstile). Cada botón puede
    # revelar enlaces directos + otros pastes: se siguen en una cola.
    ws_url, err = zonaleros._lanzar("about:blank")
    if err:
        return {"error": err}
    cdp = None
    fin_global = time.time() + 600   # tope total ~10 min (cadena de pastes)
    try:
        cdp = zonaleros._Cdp(ws_url)
        # cola: (url del paste, etiqueta del botón que lo originó)
        cola = [(b["h"], b.get("t") or "") for b in botones]
        resueltos = set()
        # acumula por hoster: hoster -> {"enlaces": [...], "etiquetas": set()}
        acumulado = {}
        limite_pastes = 12   # cota de seguridad por si la cadena se dispara
        while cola and time.time() < fin_global and len(resueltos) < limite_pastes:
            href, etiqueta = cola.pop(0)
            if href in resueltos:
                continue
            resueltos.add(href)
            if not cdp.navegar(href,
                               condicion="location.href.indexOf('playpaste.net') !== -1",
                               tiempo_max=40):
                continue
            enlaces, pastes_hijos = _resolver_paste(cdp, fin_global)
            # pastes con pestañas (MEGA/MEDIAFIRE/ROOTZ...): las pestañas
            # inactivas están ocultas hasta pulsarlas; recoge sus enlaces
            revelados, pastes_hijos = _separar_enlaces(
                _revelar_pestanas(cdp), pastes_hijos)
            conocidos = {e["url"] for e in enlaces}
            for e in revelados:
                if e["url"] not in conocidos:
                    conocidos.add(e["url"])
                    enlaces.append(e)
            # los playpaste encadenados se siguen con la misma etiqueta
            for ph in pastes_hijos:
                if ph not in resueltos:
                    cola.append((ph, etiqueta))
            for e in enlaces:
                # madiashare (torrent): la URL visible es una página HTML; la
                # directa /Link/downloads/<id> es la que entrega el .torrent
                u = e["url"]
                if "madiashare.com" in (urllib.parse.urlparse(u).hostname or "").lower():
                    e["url"] = torrents._url_torrent_directa(u)
                hoster = _nombre_hoster(e["url"])
                grupo = acumulado.setdefault(hoster, {"enlaces": [], "etiquetas": set()})
                grupo["enlaces"].append(e)
                if etiqueta:
                    grupo["etiquetas"].add(etiqueta)

        # construye los servidores agrupados por hoster real
        servidores = []
        for hoster, grupo in acumulado.items():
            enlaces = grupo["enlaces"]
            # nombre del servidor: el hoster; si la etiqueta del botón aporta
            # contexto distinto (UPDATE, CRACK...), se añade entre paréntesis
            nombre = hoster
            extras = [t for t in grupo["etiquetas"]
                      if t.lower() not in hoster.lower()
                      and hoster.lower() not in t.lower()]
            if extras:
                nombre = "%s (%s)" % (hoster, extras[0])
            # clasifica multipartes del mismo archivo vs sueltos
            clasificado = zonaleros._clasificar_enlaces([
                {"url": e["url"], "texto": e.get("texto") or "",
                 "nombre": zonaleros._nombre_de_url(e["url"])} for e in enlaces])
            clasificado["servidor"] = nombre
            servidores.append(clasificado)
        return {"servidores": servidores, "titulo": titulo_juego[:150]}
    except Exception as e:
        if not zonaleros._nuestro_chrome_vivo():
            return {"error": ("La instancia de Chrome de la extracción se "
                              "cerró a mitad (quizá se actualizó o el "
                              "sistema la cerró). Vuelve a intentar.")}
        return {"error": "error extrayendo: %s" % e}
    finally:
        if cdp:
            try:
                cdp.cerrar()
            except Exception:
                pass
        zonaleros._finalizar()   # solo nuestra instancia; el Chrome del usuario intacto
