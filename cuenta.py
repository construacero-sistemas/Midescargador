# -*- coding: utf-8 -*-
"""Sesiones de plataforma (YouTube/Google y TikTok) para yt-dlp.

Los videos que YouTube bloquea sin sesión ('Sign in to confirm you're not a
bot') y los de TikTok que exigen cuenta (mayor calidad, menos bloqueos) pasan
si yt-dlp lleva las cookies de una cuenta iniciada. La app abre Chrome con el
perfil real del usuario en una ventana VISIBLE; el usuario inicia sesión con
su cuenta, y la sesión (cookies en vivo, ya descifradas por Chrome) se lee
por CDP y se guarda como cookies.txt (formato Netscape) para yt-dlp. Las
cookies nunca salen de esta máquina y la sesión se refresca al navegar.

Nota: requiere que Chrome esté cerrado al iniciar (como en la extracción de
zona-leros), porque la app lanza una instancia con el perfil real.
"""
import os
import json
import time
import subprocess
import tempfile
import urllib.request

import zonaleros

try:
    import websocket as _ws
except ImportError:
    _ws = None

_TEMP = os.path.join(tempfile.gettempdir(), "MiDescargador")

# configuración por plataforma: puerto CDP propio (9224 youtube, 9226 tiktok),
# URL de login, archivo de cookies, dominios a exportar y cookies que indican
# que la sesión está realmente iniciada
_SESIONES = {
    "youtube": {
        "etiqueta": "YouTube (Google)",
        "puerto": 9224,
        "login_url": "https://accounts.google.com/",
        "ruta": os.path.join(_TEMP, "yt_cookies.txt"),
        "dominios": ("google.com", "youtube.com", "googleapis.com"),
        # cookies que DEMUESTRAN la sesión (lo que yt-dlp exige en
        # youtube.py/_has_auth_cookies): LOGIN_INFO (se borra al salir o al
        # rotar) y al menos un SAPISID. Las 3P (__Secure-3PSID...) aparecen a
        # mitad del login y NO prueban nada: capturar con ellas solas guardaba
        # una sesión incompleta que yt-dlp trataba como anónima.
        "requeridos": ("LOGIN_INFO",),
        "alternativos": ("SAPISID", "__Secure-1PAPISID",
                          "__Secure-3PAPISID"),
        "ttl": 20 * 3600,          # 20 horas; YouTube renueva al navegar
    },
    "tiktok": {
        "etiqueta": "TikTok",
        "puerto": 9226,
        "login_url": "https://www.tiktok.com/login",
        "ruta": os.path.join(_TEMP, "tt_cookies.txt"),
        "dominios": ("tiktok.com",),
        "requeridos": (),
        "alternativos": ("sessionid", "sid_tt", "uid_tt"),
        "ttl": 20 * 3600,
    },
}

# compatibilidad: la sesión de YouTube es la que había antes
_RUTA_COOKIES = _SESIONES["youtube"]["ruta"]
_TTL_SESION = _SESIONES["youtube"]["ttl"]
_NOMBRES_SESION = _SESIONES["youtube"]["alternativos"]


def _hay_sesion_cookies(cookies, cfg):
    """True si una lista de cookies (dicts con 'name') representa una sesión
    REAL de la plataforma, no solo cookies sueltas. Espejo de lo que yt-dlp
    exige para considerar la sesión iniciada (youtube.py/_has_auth_cookies:
    LOGIN_INFO + al menos un SAPISID); TikTok con sessionid/sid_tt/uid_tt."""
    nombres = {c.get("name") for c in cookies}
    if not all(n in nombres for n in cfg.get("requeridos", ())):
        return False
    alt = cfg.get("alternativos", ())
    if not alt:
        return True
    return any(n in nombres for n in alt)


def plataformas():
    return list(_SESIONES.keys())


def _config(plataforma):
    return _SESIONES.get(plataforma or "youtube", _SESIONES["youtube"])


def _ruta_cookies(plataforma="youtube"):
    return _config(plataforma)["ruta"]


def _lanzar_visible(url, plataforma):
    """Lanza Chrome (perfil real, ventana visible) y devuelve (ws_url, err)."""
    if _ws is None:
        return None, "falta la librería websocket-client"
    if zonaleros._chrome_corriendo():
        return None, ("Chrome está abierto: ciérralo para poder iniciar "
                      "sesión con el perfil real")
    ruta = zonaleros._ruta_chrome()
    perfil = zonaleros._crear_junction()
    if not ruta or not perfil:
        return None, "no se pudo preparar Chrome"
    cmd = [
        ruta,
        "--user-data-dir=" + perfil,
        "--profile-directory=Default",
        "--remote-debugging-port=%d" % _config(plataforma)["puerto"],
        "--remote-allow-origins=*",
        "--disable-gpu",
        "--no-first-run",
        "--disable-background-networking",
        url,
    ]
    zonaleros._respaldar_cookies()
    subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    fin = time.time() + 50
    while time.time() < fin:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list"
                    % _config(plataforma)["puerto"], timeout=2) as r:
                for t in json.loads(r.read().decode("utf-8", "replace")):
                    if t.get("type") == "page":
                        return t["webSocketDebuggerUrl"], None
        except Exception:
            pass
        time.sleep(1)
    return None, "Chrome no respondió"


def _leer_y_guardar(ws, plataforma):
    """Lee las cookies de la plataforma del perfil en vivo y las guarda en
    formato Netscape para yt-dlp. Devuelve (guardadas, hay_sesion)."""
    cfg = _config(plataforma)
    id_ = [0]

    def cmd(method, params=None):
        id_[0] += 1
        ws.send(json.dumps({
            "id": id_[0], "method": method, "params": params or {}}))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == id_[0]:
                return r

    cmd("Network.enable")
    r = cmd("Network.getAllCookies")
    cookies = (r.get("result") or {}).get("cookies") or []
    relevantes = [c for c in cookies
                  if any(d in (c.get("domain") or "")
                         for d in cfg["dominios"])]
    if not relevantes:
        return False, False
    # sesión CONFIRMADA solo con el set completo (LOGIN_INFO + SAPISID para
    # YouTube): si capturamos apenas aparecen las 3P, el login sigue en curso
    # y el archivo quedaría incompleto (yt-dlp lo trataría como anónimo)
    hay_sesion = _hay_sesion_cookies(relevantes, cfg)
    os.makedirs(os.path.dirname(cfg["ruta"]), exist_ok=True)
    lineas = ["# Netscape HTTP Cookie File"]
    for c in relevantes:
        # Formato Netscape: si el dominio no lleva punto inicial es una cookie
        # host-only y su flag de dominio debe ser FALSE; si lleva punto (cookie
        # de subdominio) se conserva el punto y el flag es TRUE. Antes se
        # quitaba el punto y se escribía TRUE siempre: http.cookiejar de Python
        # (que usa yt-dlp) rechaza ese archivo como inválido y la sesión 🔑
        # nunca llegaba a usarse.
        d = c.get("domain") or ""
        subdominio = d.startswith(".")
        if not subdominio:
            d = d.lstrip(".")
        lineas.append("\t".join([
            d, "TRUE" if subdominio else "FALSE", c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(int(c.get("expires") or 0)), c.get("name", ""),
            c.get("value", "")]))
    with open(cfg["ruta"], "w", encoding="utf-8") as f:
        f.write("\n".join(lineas) + "\n")
    # el archivo cambió: forzar re-validación en vivo la próxima vez
    _VALIDACION_CACHE.pop(plataforma, None)
    return True, hay_sesion


def _sesion_activa(plataforma="youtube"):
    """Sesión válida = archivo fresco, en formato Netscape REAL (si está
    roto, MozillaCookieJar lo rechaza -> inactiva) y con las cookies de
    sesión completas. Antes bastaba con encontrar un nombre suelto en el
    texto: el archivo malformado (dominio sin punto + TRUE) pasaba el check
    y la UI decía 'Activa' aunque yt-dlp nunca pudiera cargarlo."""
    cfg = _config(plataforma)
    try:
        if not os.path.exists(cfg["ruta"]):
            return False
        if time.time() - os.path.getmtime(cfg["ruta"]) > cfg["ttl"]:
            return False
        import http.cookiejar
        cj = http.cookiejar.MozillaCookieJar()
        cj.load(cfg["ruta"])
        return _hay_sesion_cookies(
            [{"name": c.name} for c in cj], cfg)
    except Exception:
        return False


# validación EN VIVO de la sesión: las cookies se guardan bien pero YouTube
# las rota al tiempo ("The provided YouTube account cookies are no longer
# valid"). El check local no lo ve; el badge diría 'Activa' con cookies
# muertas. Validamos de verdad contra YouTube, con caché (10 min) y
# re-validación si el archivo cambió (p. ej. al iniciar sesión de nuevo).
_VALIDACION_CACHE = {}      # plataforma -> (ts, mtime, resultado)
_VALIDACION_TTL = 600       # 10 minutos


class _LogAvisos:
    """Logger de yt-dlp que recolecta warnings (para detectar 'cookies are
    no longer valid' sin ensuciar la consola)."""

    def __init__(self):
        self.avisos = []

    def debug(self, _m):
        pass

    def info(self, _m):
        pass

    def warning(self, m):
        self.avisos.append(m or "")

    def error(self, _m):
        pass

    def cookies_rotadas(self):
        return any("no longer valid" in a.lower() for a in self.avisos)


def _validar_en_vivo(plataforma):
    """Comprueba que YouTube acepta de verdad las cookies del archivo.
    True = sesión válida; False = cookies rechazadas (rotadas); None = no se
    pudo determinar (sin red / sin yt_dlp / otro error)."""
    cfg = _config(plataforma)
    if plataforma != "youtube":
        return None
    try:
        import yt_dlp
        from yt_dlp.extractor.youtube import YoutubeIE
    except Exception:
        return None
    log = _LogAvisos()
    try:
        with yt_dlp.YoutubeDL({
                "quiet": True, "skip_download": True, "noplaylist": True,
                "cookiefile": cfg["ruta"], "socket_timeout": 15,
                "logger": log}) as ydl:
            ie = YoutubeIE(ydl)
            autenticado_local = bool(ie.is_authenticated)
            # extracción mínima (video corto) para que YouTube evalúe las
            # cookies; el warning de rotación solo aparece en red
            ydl.extract_info("https://www.youtube.com/watch?v=jNQXAC9IVRw",
                             download=False)
        if not autenticado_local:
            return False
        if log.cookies_rotadas():
            return False
        return True
    except Exception:
        return None


def _validar_en_vivo_cached(plataforma):
    """Versión cacheada de _validar_en_vivo: repite la validación solo si
    pasó el TTL o si el archivo de cookies cambió desde la última vez."""
    cfg = _config(plataforma)
    try:
        mtime = os.path.getmtime(cfg["ruta"])
    except OSError:
        return None
    ahora = time.time()
    c = _VALIDACION_CACHE.get(plataforma)
    if c and ahora - c[0] < _VALIDACION_TTL and c[1] == mtime:
        return c[2]
    resultado = _validar_en_vivo(plataforma)
    _VALIDACION_CACHE[plataforma] = (ahora, mtime, resultado)
    return resultado


def estado(plataforma="youtube"):
    cfg = _config(plataforma)
    if not _sesion_activa(plataforma):
        return {"activa": False, "plataforma": plataforma}
    # validación en vivo: si YouTube rechaza las cookies (rotadas), la
    # sesión NO está activa aunque el archivo esté perfecto
    vivo = _validar_en_vivo_cached(plataforma)
    if vivo is False:
        return {"activa": False, "plataforma": plataforma,
                "rotada": True, "edad_min": _edad_min(cfg)}
    try:
        edad = _edad_min(cfg)
    except OSError:
        edad = 0
    return {"activa": True, "edad_min": int(edad),
            "ruta": cfg["ruta"], "plataforma": plataforma}


def _edad_min(cfg):
    return int((time.time() - os.path.getmtime(cfg["ruta"])) / 60)


def iniciar_sesion(plataforma="youtube", espera_max=240):
    """Abre Chrome visible en la página de login de la plataforma y espera a
    que el usuario inicie sesión. Cuando detecta la sesión, guarda las
    cookies y cierra."""
    cfg = _config(plataforma)
    if _sesion_activa(plataforma):
        return {"ok": True, "ya_activa": True}
    ws_url, err = _lanzar_visible(cfg["login_url"], plataforma)
    if err:
        return {"error": err}
    ws = None
    try:
        ws = _ws.create_connection(ws_url, timeout=30)
        fin = time.time() + espera_max
        while time.time() < fin:
            try:
                guardadas, hay_sesion = _leer_y_guardar(ws, plataforma)
                if guardadas and hay_sesion:
                    return {"ok": True, "ruta": cfg["ruta"]}
            except Exception:
                pass
            time.sleep(5)
        return {"error": ("No se detectó el inicio de sesión. Cierra la "
                          "ventana de Chrome y revisa que entraste con tu "
                          "cuenta en %s." % cfg["etiqueta"])}
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        zonaleros._matar_chrome()
        zonaleros._restaurar_cookies_si_danadas()


def borrar(plataforma="youtube"):
    cfg = _config(plataforma)
    try:
        if os.path.exists(cfg["ruta"]):
            os.remove(cfg["ruta"])
        return {"ok": True}
    except OSError as e:
        return {"ok": False, "error": str(e)}
