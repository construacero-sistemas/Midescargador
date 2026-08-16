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
        "nombres": (
            "SAPISID", "__Secure-1PSID", "SID", "HSID", "APISID",
            "LOGIN_INFO", "__Secure-3PSID", "SSID"),
        "ttl": 20 * 3600,          # 20 horas; YouTube renueva al navegar
    },
    "tiktok": {
        "etiqueta": "TikTok",
        "puerto": 9226,
        "login_url": "https://www.tiktok.com/login",
        "ruta": os.path.join(_TEMP, "tt_cookies.txt"),
        "dominios": ("tiktok.com",),
        "nombres": ("sessionid", "sid_tt", "uid_tt"),
        "ttl": 20 * 3600,
    },
}

# compatibilidad: la sesión de YouTube es la que había antes
_RUTA_COOKIES = _SESIONES["youtube"]["ruta"]
_TTL_SESION = _SESIONES["youtube"]["ttl"]
_NOMBRES_SESION = _SESIONES["youtube"]["nombres"]


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
    hay_sesion = any(c.get("name") in cfg["nombres"] for c in relevantes)
    os.makedirs(os.path.dirname(cfg["ruta"]), exist_ok=True)
    lineas = ["# Netscape HTTP Cookie File"]
    for c in relevantes:
        d = (c.get("domain") or "").lstrip(".")
        lineas.append("\t".join([
            d, "TRUE", c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(int(c.get("expires") or 0)), c.get("name", ""),
            c.get("value", "")]))
    with open(cfg["ruta"], "w", encoding="utf-8") as f:
        f.write("\n".join(lineas) + "\n")
    return True, hay_sesion


def _sesion_activa(plataforma="youtube"):
    cfg = _config(plataforma)
    try:
        if not os.path.exists(cfg["ruta"]):
            return False
        if time.time() - os.path.getmtime(cfg["ruta"]) > cfg["ttl"]:
            return False
        with open(cfg["ruta"], encoding="utf-8", errors="replace") as f:
            contenido = f.read()
        return any(n in contenido for n in cfg["nombres"])
    except Exception:
        return False


def estado(plataforma="youtube"):
    cfg = _config(plataforma)
    if not _sesion_activa(plataforma):
        return {"activa": False, "plataforma": plataforma}
    try:
        edad = (time.time() - os.path.getmtime(cfg["ruta"])) / 60
    except OSError:
        edad = 0
    return {"activa": True, "edad_min": int(edad),
            "ruta": cfg["ruta"], "plataforma": plataforma}


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
