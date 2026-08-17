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
import re
import time
import urllib.parse
import urllib.request

import zonaleros  # CDP, _lanzar, _clasificar_enlaces, _leer_enlaces_paste...

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
            # separa los enlaces de descarga de los playpaste encadenados
            enlaces = [e for e in crudo if "playpaste.net" not in e.get("url", "")]
            for e in crudo:
                u = e.get("url", "")
                if "playpaste.net" in u and u not in pastes_hijos:
                    pastes_hijos.append(u)
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
            # los playpaste encadenados se siguen con la misma etiqueta
            for ph in pastes_hijos:
                if ph not in resueltos:
                    cola.append((ph, etiqueta))
            for e in enlaces:
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
