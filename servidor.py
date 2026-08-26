# -*- coding: utf-8 -*-
"""
MiDescargador - Servidor local.
Sirve la interfaz web en http://127.0.0.1:17890 y la API REST que la
extensión de Chrome usa para añadir descargas.

API:
  POST /api/descargar   {"url", "segmentos"?, "carpeta"?}
  GET  /api/estado
  POST /api/pausar      {"id"}
  POST /api/reanudar    {"id"}
  POST /api/cancelar    {"id"}
  POST /api/borrar      {"id"}        (quita de la lista)
  POST /api/abrir       {"id"}        (abre la carpeta en el explorador)
  POST /api/carpeta                  (abre la carpeta de descargas en el
                                      explorador de archivos del sistema)
  GET  /api/media/<id>               (sirve el archivo con soporte de Range,
                                      para el reproductor integrado del panel)
"""

import hmac
import json
import os
import re
import secrets
import sys
import time
import uuid
import tempfile
import subprocess
import threading
import urllib.parse
import concurrent.futures as _futuros
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import motor
import hosters
import mega
import zonaleros_copia as zonaleros   # variante copia de perfil (Chrome abierto, sin taskkill destructivo)
import pivigames
import cuenta
import descomprimir
import torrents

PUERTO = 17890
CARPETA_DEFECTO = os.path.join(os.path.expanduser("~"), "Downloads", "MiDescargador")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _base_dir():
    """Dónde viven los recursos del programa. En modo empaquetado
    (PyInstaller) los binarios y el panel se extraen a sys._MEIPASS."""
    if getattr(sys, "_MEIPASS", None):
        return sys._MEIPASS
    return BASE_DIR


def _version_app():
    r"""Versión de la app. La escribe Electron al arrancar en
    %LOCALAPPDATA%\MiDescargador\version.json (app.getVersion()); en dev
    (sin Electron) cae a electron/package.json. Si no hay nada, fallback."""
    import json as _json
    candidatos = [
        # version.json compartido, escrito por la app de escritorio
        os.path.join(_dir_datos(), "version.json"),
        # en dev: electron/package.json junto al proyecto
        os.path.join(BASE_DIR, "electron", "package.json"),
        os.path.join(BASE_DIR, "..", "electron", "package.json"),
    ]
    for c in candidatos:
        try:
            with open(c, encoding="utf-8") as f:
                v = _json.load(f).get("version")
            if v:
                return v
        except Exception:
            continue
    return "2.0"


def _dir_datos():
    r"""Carpeta de datos persistentes (logs): en el empaquetado el _MEIPASS es
    temporal, así que los logs van a %LOCALAPPDATA%\MiDescargador."""
    if getattr(sys, "_MEIPASS", None):
        d = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
                         "MiDescargador")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d
    return BASE_DIR


# --- token de la API local ------------------------------------------------
# El puerto es accesible desde cualquier proceso local; el token distingue a
# los clientes de confianza (panel, extensión) del resto. Se genera una vez
# y se persiste para que no cambie en cada reinicio. La API lo exige salvo
# en /api/token (bootstrap de la extensión, con verificación de Host) y en
# /api/media (el <video> del reproductor no puede mandar cabeceras y el
# endpoint solo sirve archivos ya descargados, sin efectos secundarios).
def _ruta_token():
    return os.path.join(_dir_datos(), "token.txt")


def _cargar_o_crear_token():
    ruta = _ruta_token()
    try:
        with open(ruta, encoding="utf-8") as f:
            t = f.read().strip()
        if t and len(t) >= 16:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(32)
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(t)
    except OSError:
        pass
    return t


TOKEN_API = _cargar_o_crear_token()


LOG_RUTA = os.path.join(_dir_datos(), "errores.log")
LOG_SERVIDOR = os.path.join(_dir_datos(), "servidor.log")
LOG_MAX = 400          # máximo de líneas que se guardan
_RUTA_COLA = os.path.join(_dir_datos(), "cola.json")   # cola persistida

# --- reintentos automáticos (scheduler estilo IDM) -------------------------
# Cuando una descarga falla con un error transitorio (red, servidor ocupado,
# timeout), el hilo _hilo_reintentos la vuelve a lanzar sola con backoff
# exponencial en vez de dejar la tarjeta en error para siempre.
REINTENTOS_MAX = 5            # reintentos automáticos por tarea
REINTENTOS_BASE = 30          # 30 s → 60 s → 120 s → 240 s → 480 s
REINTENTOS_MAX_ESPERA = 600   # tope: 10 minutos


def _es_reintentable(error):
    """True si el error parece transitorio (vale la pena reintentar solo).
    Los errores permanentes (404, formato inexistente, sesión vencida…) NO
    se reintentan: reintentar solo repetiría el mismo fallo."""
    e = (error or "").lower()
    # 404/410 y avisos explícitos de recurso inexistente = permanente
    if any(p in e for p in ("404", "410", "not found", "no existe",
                            "requested format is not available",
                            "no longer valid", "expir", "sesión vencida",
                            "no se encontró", "no encontrado")):
        return False
    return True


def _log_servidor(msg):
    """Anota arranque/parada/fallos del servidor en servidor.log, para que
    'no arranca' deje rastro aunque la ventana de consola se cierre."""
    try:
        with open(LOG_SERVIDOR, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _init_reintentos(t, reinientos=0, proximo=None):
    """Campos del scheduler de reintentos en cualquier tipo de tarea.
    _reintentos_auto: cuántos reintentos automáticos ya se hicieron.
    _proximo_reintento: timestamp del próximo intento (0 = pendiente ya)."""
    t._reintentos_auto = reinientos
    t._proximo_reintento = proximo if proximo is not None else 0


def _reset_reintentos(t):
    """Vuelve a cero el contador de reintentos (al completar o al reintentar
    a mano desde el panel)."""
    try:
        _init_reintentos(t)
    except Exception:
        pass


def _registrar_error(trabajo):
    """Escribe un error en errores.log con hora, id, url y detalle."""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        url = getattr(trabajo, "url", "?")
        tid = getattr(trabajo, "id", "?")
        err = (getattr(trabajo, "error", None) or "?").replace("\n", " ")
        linea = f"[{ts}] id={tid} url={url}\n    ERROR: {err}\n"
        with open(LOG_RUTA, "a", encoding="utf-8") as f:
            f.write(linea)
        _recortar_log()
    except Exception:
        pass


motor.on_error = _registrar_error        # el motor avisa aquí cuando algo falla


def _recortar_log():
    try:
        with open(LOG_RUTA, "r", encoding="utf-8") as f:
            lineas = f.readlines()
        if len(lineas) > LOG_MAX * 2:
            with open(LOG_RUTA, "w", encoding="utf-8") as f:
                f.writelines(lineas[-LOG_MAX * 2:])
    except Exception:
        pass


def _leer_log(n=200):
    try:
        with open(LOG_RUTA, "r", encoding="utf-8", errors="replace") as f:
            lineas = f.read().splitlines()
        return lineas[-n:]
    except OSError:
        return []


def _log_hibrido(msg):
    """Anota en errores.log qué rama del híbrido (junction/copia) usó cada
    extracción y por qué, para dejar rastro cuando algo falle."""
    try:
        with open(LOG_RUTA, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


zonaleros._LOG = _log_hibrido   # el extractor híbrido avisa aquí qué rama usó

# Dominios de páginas que casi siempre necesitan yt-dlp (videos, mediafire...)
DOMINIOS_YTDLP = (
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com", "instagr.am",
    "facebook.com", "fb.watch", "twitter.com", "x.com", "twitch.tv",
    "vimeo.com", "dailymotion.com", "mediafire.com", "soundcloud.com",
    "bilibili.com", "reddit.com", "pinterest.com", "threads.net",
    "vk.com", "rumble.com", "kick.com",
)


def _usar_ytdlp(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in DOMINIOS_YTDLP)


def _es_cdn_directo(url):
    """URLs tipo downloadN.host.com/... son CDNs de archivos directos
    (MediaFire, etc.): van al motor segmentado, no a yt-dlp."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return (host.startswith("download")
            or host.startswith("srv")
            or host.startswith("dl")
            or "userstorage" in host)


def _es_mega(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d)
               for d in ("mega.nz", "mega.co.nz", "mega.io"))


# ---------------------------------------------------------- servidor origen
# nombre amigable del servidor desde el que se descarga
SERVIDORES = [
    ("youtube.com", "YouTube"), ("youtu.be", "YouTube"),
    ("tiktok.com", "TikTok"), ("instagram.com", "Instagram"),
    ("instagr.am", "Instagram"), ("facebook.com", "Facebook"),
    ("fb.watch", "Facebook"), ("twitter.com", "Twitter"),
    ("x.com", "X"), ("twitch.tv", "Twitch"), ("vimeo.com", "Vimeo"),
    ("dailymotion.com", "Dailymotion"), ("soundcloud.com", "SoundCloud"),
    ("bilibili.com", "Bilibili"), ("reddit.com", "Reddit"),
    ("pinterest.com", "Pinterest"), ("threads.net", "Threads"),
    ("vk.com", "VK"), ("rumble.com", "Rumble"), ("kick.com", "Kick"),
    ("mediafire.com", "MediaFire"), ("mega.nz", "Mega"),
    ("mega.co.nz", "Mega"), ("rootz.so", "Rootz"),
    ("fireload.com", "Fireload"), ("megaup.net", "MegaUp"),
    ("gofile.io", "GoFile"), ("drive.google.com", "Google Drive"),
    ("drive.usercontent.google.com", "Google Drive"),
]


def _nombre_servidor(url):
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    if not host:
        return None
    for d, nombre in SERVIDORES:
        if host == d or host.endswith("." + d):
            return nombre
    # dominio base genérico (ej. download1234.mediafire.com -> mediafire.com)
    partes = host.split(".")
    if len(partes) > 2:
        return ".".join(partes[-2:])
    return host


def _candidatos_ytdlp():
    """Rutas candidatas a yt-dlp, en orden de prioridad. Devuelve SOLO
    archivos reales: al empaquetar con PyInstaller a veces el yt-dlp.exe
    queda como directorio anidado (venv/Scripts/yt-dlp.exe/yt-dlp.exe);
    os.path.exists() ve el directorio y lo devolvería como comando,
    rompiendo todas las consultas de calidades en silencio."""
    base = _base_dir()
    candidatos = [
        os.path.join(base, "venv", "Scripts", "yt-dlp.exe"),
        os.path.join(base, "venv", "bin", "yt-dlp"),
    ]
    # directorio anidado que PyInstaller a veces crea
    for c in list(candidatos):
        candidatos.append(os.path.join(c, "yt-dlp.exe"))
        candidatos.append(os.path.join(c, "yt-dlp"))
    import shutil
    for nombre in ("yt-dlp", "ytdlp"):
        p = shutil.which(nombre)
        if p:
            candidatos.append(p)
    return candidatos


def _candidatos_deno():
    """Rutas candidatas al runtime JS de yt-dlp (deno), en orden de
    prioridad. En dev vive en venv/Scripts/deno.exe (paquete pip 'deno');
    al empaquetar con PyInstaller se copia a bin/deno.exe (y a veces queda
    el directorio anidado venv/Scripts/deno.exe/deno.exe, igual que
    yt-dlp).

    yt-dlp moderno resuelve el desafío JS de YouTube (PO token) con un
    runtime externo: sin deno no se genera el token y la variante con
    cookies de sesión falla con 'The page needs to be reloaded'."""
    base = _base_dir()
    candidatos = [
        os.path.join(base, "bin", "deno.exe"),
        os.path.join(base, "venv", "Scripts", "deno.exe"),
        os.path.join(base, "venv", "bin", "deno"),
    ]
    # directorio anidado que PyInstaller a veces crea
    for c in list(candidatos):
        candidatos.append(os.path.join(c, "deno.exe"))
        candidatos.append(os.path.join(c, "deno"))
    import shutil
    p = shutil.which("deno")
    if p:
        candidatos.append(p)
    return [c for c in candidatos if os.path.isfile(c)]


def _ruta_deno():
    """Primera ruta real de deno, o None si no hay runtime JS disponible."""
    c = _candidatos_deno()
    return c[0] if c else None


def _ytdlp_disponible():
    return any(os.path.isfile(p) for p in _candidatos_ytdlp())


def _cmd_ytdlp():
    """Devuelve cómo invocar yt-dlp (prioriza el venv del proyecto)."""
    for p in _candidatos_ytdlp():
        if os.path.isfile(p):
            return [p]
    return ["yt-dlp"]


# ---------------------------------------------------------- organización
# Categorías por extensión de archivo (para carpetas automáticas)
CATEGORIAS = [
    ("Videos", (".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv",
                 ".m4v", ".mpg", ".mpeg", ".ts", ".3gp", ".ogv")),
    ("Musica", (".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus",
                 ".wma", ".aiff")),
    ("Imagenes", (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
                   ".svg", ".heic", ".tiff", ".ico")),
    ("Comprimidos", (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2",
                      ".xz", ".zst", ".iso", ".cab", ".tgz")),
    ("Documentos", (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt",
                     ".pptx", ".txt", ".md", ".csv", ".epub", ".odt")),
    ("Programas", (".exe", ".msi", ".apk", ".bat", ".cmd", ".deb",
                    ".rpm", ".dmg", ".jar")),
    ("Otros", ()),
]

ORGANIZAR_POR_TIPO = True   # se puede cambiar desde el panel
DESCOMPRESION_AUTO = False  # descomprimir automáticamente al terminar
PASSWORD_DESCOMPRESION = ""  # contraseña para comprimidos protegidosCONFIG_RUTA = os.path.join(_dir_datos(), "config.json")
LIMITE_VELOCIDAD_KBPS = 0  # límite GLOBAL de velocidad en KB/s (0 = sin límite)


def _aplicar_limite_global():
    """Propaga el límite global al motor segmentado y a Mega (aplica en vivo
    a las descargas en curso; las de yt-dlp toman el límite al arrancar)."""
    try:
        motor.LIMITE_GLOBAL_BPS = LIMITE_VELOCIDAD_KBPS * 1024
        mega.LIMITE_GLOBAL_BPS = LIMITE_VELOCIDAD_KBPS * 1024
    except Exception:
        pass


def _cargar_config():
    """Carga la configuración persistida (organizar, simultáneas,
    descompresión, contraseña y límite de velocidad) desde config.json."""
    global ORGANIZAR_POR_TIPO, MAX_SIMULTANEAS
    global DESCOMPRESION_AUTO, PASSWORD_DESCOMPRESION
    global LIMITE_VELOCIDAD_KBPS
    try:
        with open(CONFIG_RUTA, encoding="utf-8") as f:
            c = json.load(f)
        ORGANIZAR_POR_TIPO = bool(c.get("organizar", ORGANIZAR_POR_TIPO))
        MAX_SIMULTANEAS = max(1, min(int(c.get(
            "max_simultaneas", MAX_SIMULTANEAS)), 10))
        DESCOMPRESION_AUTO = bool(c.get(
            "descompresion_auto", DESCOMPRESION_AUTO))
        PASSWORD_DESCOMPRESION = str(c.get(
            "password_descompresion", "") or "")
        try:
            LIMITE_VELOCIDAD_KBPS = max(0, int(c.get(
                "limite_kbps", LIMITE_VELOCIDAD_KBPS)))
        except (TypeError, ValueError):
            pass
    except Exception:
        pass
    _aplicar_limite_global()


def _guardar_config():
    """Persiste la configuración actual a config.json."""
    try:
        with open(CONFIG_RUTA, "w", encoding="utf-8") as f:
            json.dump({
                "organizar": ORGANIZAR_POR_TIPO,
                "max_simultaneas": MAX_SIMULTANEAS,
                "descompresion_auto": DESCOMPRESION_AUTO,
                "password_descompresion": PASSWORD_DESCOMPRESION,
                "limite_kbps": LIMITE_VELOCIDAD_KBPS,
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _categoria(nombre):
    ext = os.path.splitext(nombre or "")[1].lower()
    for cat, exts in CATEGORIAS:
        if ext in exts:
            return cat
    return "Otros"


def _organizar(trabajo):
    """Mueve el archivo descargado a su subcarpeta por tipo (Videos, etc.)."""
    if not ORGANIZAR_POR_TIPO:
        return
    nombre = getattr(trabajo, "nombre", None) or getattr(trabajo, "_nombre", None)
    carpeta = getattr(trabajo, "carpeta", None)
    if not nombre or not carpeta or not os.path.isdir(carpeta):
        return
    origen = os.path.join(carpeta, nombre)
    if not os.path.exists(origen):
        # yt-dlp pudo nombrar el archivo distinto. Buscar por TÍTULO
        # (el template es %(title).120s.%(ext)s) y NUNCA tocar .part de
        # otra descarga en curso
        try:
            candidatos = [os.path.join(carpeta, f) for f in os.listdir(carpeta)
                          if os.path.isfile(os.path.join(carpeta, f))
                          and not f.endswith(".part")]
            if not candidatos:
                return
            nombre_base = (nombre or "").strip()[:120]
            por_titulo = [c for c in candidatos
                          if nombre_base and os.path.basename(c).startswith(nombre_base)]
            if por_titulo:
                origen = max(por_titulo, key=os.path.getmtime)
            else:
                origen = max(candidatos, key=os.path.getmtime)
        except Exception:
            return
    sub = os.path.join(carpeta, _categoria(os.path.basename(origen)))
    # registrar la categoría real en el trabajo para el panel
    try:
        trabajo.categoria = os.path.basename(sub)
    except Exception:
        pass
    if sub == carpeta:
        try:
            trabajo._archivo_final = origen
        except Exception:
            pass
        return
    try:
        os.makedirs(sub, exist_ok=True)
        destino = os.path.join(sub, os.path.basename(origen))
        if os.path.abspath(origen) != os.path.abspath(destino):
            os.replace(origen, destino)
        try:
            trabajo._archivo_final = destino   # donde quedó el archivo
        except Exception:
            pass
    except Exception:
        pass


motor.on_completada = _organizar        # y aquí cuando termina bien
mega.on_error = _registrar_error        # mega avisa aquí cuando algo falla
mega.on_completada = _organizar


def _al_error(trabajo):
    try:
        _registrar_error(trabajo)
    except Exception:
        pass
    _procesar_cola()   # un fallo libera hueco: entra la siguiente
    _guardar_cola()    # el estado cambió: persistir


# -------------------------------------------------- scheduler de reintentos
# Estilo IDM: cuando una descarga falla con un error transitorio, se vuelve
# a lanzar sola con backoff exponencial (30s, 60s, 120s, …). El contador se
# persiste en cola.json, así que el ciclo sobrevive al reinicio del servidor.

def _espera_reintento(n):
    """Backoff exponencial para el reintento n (0 = primer reintento)."""
    return min(REINTENTOS_BASE * (2 ** n), REINTENTOS_MAX_ESPERA)


def _tick_reintentos():
    """Una pasada del scheduler (testeable): mira si hay tareas en error que
    deban reintentarse solas (error transitorio, dentro del tope y con el
    backoff ya cumplido). Devuelve los ids reintentados en esta pasada."""
    ahora = time.time()
    candidatas = []
    with GESTOR._lock:
        for t in list(GESTOR.trabajos.values()):
            if t.estado != "error":
                continue
            if not _es_reintentable(getattr(t, "error", "")):
                continue
            n = getattr(t, "_reintentos_auto", 0)
            if n >= REINTENTOS_MAX:
                continue
            prox = getattr(t, "_proximo_reintento", 0) or 0
            if prox and prox > ahora:
                continue   # todavía en el backoff
            candidatas.append((t, n))
    reintentados = []
    for t, n in candidatas:
        try:
            if t.estado != "error":
                continue   # pudo cambiar (cancelada/manual) mientras tanto
            prox = getattr(t, "_proximo_reintento", 0) or 0
            if prox == 0:
                # primer fallo (o restaurado sin programar): arranca el
                # backoff desde el primer intento, sin reintentar ya
                t._proximo_reintento = ahora + _espera_reintento(n)
                _guardar_cola()
                continue
            espera = _espera_reintento(n)
            t._reintentos_auto = n + 1
            t._proximo_reintento = ahora + espera
            _log_servidor("reintento %d/%d de %s en %ds"
                          % (n + 1, REINTENTOS_MAX,
                             getattr(t, "id", "?"), espera))
            t.reintentar()
            _guardar_cola()
            reintentados.append(t.id)
        except Exception:
            pass
    return reintentados


def _hilo_reintentos():
    """Bucle del scheduler: cada 10 s corre _tick_reintentos()."""
    while True:
        time.sleep(10)
        try:
            _tick_reintentos()
        except Exception:
            pass


def _cookies_sesion_activas():
    """Argumentos --cookies de las sesiones activas (YouTube y TikTok).
    Cada elemento es una lista de argumentos extra para yt-dlp."""
    extras = []
    for p in ("youtube", "tiktok"):
        if cuenta._sesion_activa(p):
            extras.append(["--cookies", cuenta._ruta_cookies(p)])
    return extras


def _plataforma_sesion_en(args):
    """Qué plataforma de sesión 🔑 lleva un comando/extra de yt-dlp (por la
    ruta exacta de su cookiefile), o None si no lleva cookies de la app."""
    for p in ("youtube", "tiktok"):
        ruta = cuenta._ruta_cookies(p)
        if any(str(a) == ruta or ruta in str(a) for a in (args or [])):
            return p
    return None


def _invalidar_sesion_detectada(plataforma):
    """Marca la sesión 🔑 como vencida al instante cuando yt-dlp reportó que
    sus cookies ya no son válidas ('no longer valid'), sin esperar a que el
    panel consulte /api/sesion. Registra el motivo en servidor.log."""
    if cuenta.invalidar(plataforma):
        _log_servidor("sesión %s marcada como vencida: yt-dlp reportó "
                      "cookies no válidas (rotadas) durante una descarga o "
                      "consulta" % plataforma)


def _variantes_ytdlp():
    """Estrategias de extractor para evitar el 403 anti-bot de YouTube.
    Cada elemento es una lista de argumentos extra para yt-dlp.
    """
    variantes = [
        # cliente android: funciona sin cookies y esquiva el 403 anti-bot
        ["--extractor-args", "youtube:player_client=android"],
        # cliente web + android con fallback (el más compatible)
        ["--extractor-args", "youtube:player_client=default,android"],
        # cliente tv (otro camino, a veces esquiva el bloqueo)
        ["--extractor-args", "youtube:player_client=tv"],
    ]
    # sesiones iniciadas (YouTube/TikTok) como respaldo
    variantes.extend(_cookies_sesion_activas())
    return variantes


def _ruta_ffmpeg():
    """Localiza ffmpeg como ffmpeg.exe (copia local con el nombre que yt-dlp
    espera). Al empaquetar con PyInstaller el exe puede quedar como directorio
    anidado (bin/ffmpeg.exe/ffmpeg.exe): os.path.exists() ve el directorio y
    lo devolvería como comando, rompiendo la verificación en silencio."""
    base = _base_dir()
    local = os.path.join(base, "bin", "ffmpeg.exe")
    if os.path.isfile(local):
        return local
    # directorio anidado que PyInstaller a veces crea
    anidado = os.path.join(local, "ffmpeg.exe")
    if os.path.isfile(anidado):
        return anidado
    try:
        import imageio_ffmpeg
        origen = imageio_ffmpeg.get_ffmpeg_exe()
        os.makedirs(os.path.dirname(local), exist_ok=True)
        import shutil
        shutil.copy2(origen, local)
        return local
    except Exception:
        import shutil
        return shutil.which("ffmpeg")


# caché de calidades por video: consultar el mismo enlace otra vez es instantáneo
_FORMATOS_CACHE = {}        # clave -> (timestamp, lista, titulo)
_FORMATOS_CACHE_TTL = 1800  # 30 minutos
_FORMATOS_LOCK = threading.Lock()
_FORMATOS_CACHE_ARCHIVO = os.path.join(_dir_datos(), "formatos_cache.json")

# caché de TAMAÑOS SIMULADOS por video (disco, TTL largo): los tamaños de
# los formatos cambian poco, así que se calculan UNA vez por video con
# --simulate y el resto de las consultas abren al instante sin las ~30 s
# de simulaciones. Estructura: {clave_video: {selector: [tamano, ts]}}
_FORMATOS_TAM_CACHE = {}
_FORMATOS_TAM_TTL = 7 * 24 * 3600   # 7 días
_FORMATOS_TAM_ARCHIVO = os.path.join(_dir_datos(), "formatos_tamanos_cache.json")
_FORMATOS_TAM_LOCK = threading.Lock()


def _cargar_tamanos_cache():
    """Restaura la caché de tamaños simulados desde disco."""
    try:
        with open(_FORMATOS_TAM_ARCHIVO, encoding="utf-8") as f:
            datos = json.load(f)
        ahora = time.time()
        with _FORMATOS_TAM_LOCK:
            for k, v in (datos or {}).items():
                if isinstance(v, dict):
                    v = {s: x for s, x in v.items()
                         if x and isinstance(x, list) and len(x) == 2
                         and ahora - x[1] < _FORMATOS_TAM_TTL}
                    if v:
                        _FORMATOS_TAM_CACHE[k] = v
    except Exception:
        pass


def _guardar_tamanos_cache():
    """Persiste la caché de tamaños simulados a disco."""
    try:
        with _FORMATOS_TAM_LOCK:
            datos = dict(_FORMATOS_TAM_CACHE)
        with open(_FORMATOS_TAM_ARCHIVO, "w", encoding="utf-8") as f:
            json.dump(datos, f)
    except Exception:
        pass
# single-flight: si dos peticiones piden el mismo video a la vez (p. ej. el
# prefetch del hover y el clic), una sola corre yt-dlp y la otra espera.
_FORMATOS_EN_CURSO = {}     # clave -> threading.Event
_FORMATOS_EN_CURSO_LOCK = threading.Lock()


def _cargar_formatos_cache():
    """Restaura la caché de calidades desde disco (sobrevive reinicios)."""
    try:
        with open(_FORMATOS_CACHE_ARCHIVO, encoding="utf-8") as f:
            datos = json.load(f)
        ahora = time.time()
        with _FORMATOS_LOCK:
            for k, (ts, lista, titulo) in (datos or {}).items():
                if ahora - ts < _FORMATOS_CACHE_TTL:
                    _FORMATOS_CACHE[k] = (ts, lista, titulo)
    except Exception:
        pass


def _guardar_formatos_cache():
    """Persiste la caché de calidades a disco (se llama al llenar una clave)."""
    try:
        with _FORMATOS_LOCK:
            datos = dict(_FORMATOS_CACHE)
        with open(_FORMATOS_CACHE_ARCHIVO, "w", encoding="utf-8") as f:
            json.dump(datos, f)
    except Exception:
        pass


def _clave_video(url):
    """Clave de caché: el ID del video para YouTube, la URL completa si no."""
    import re as _re
    m = _re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", url)
    return m.group(1) if m else url


def _ytdlp_info(url, extra, timeout=30):
    """Una consulta -J a yt-dlp con un cliente concreto. Devuelve
    (json, stderr) o (None, stderr) si falla/expira.

    Vía rápida: yt_dlp EN PROCESO (el paquete lo trae como módulo), que
    evita arrancar el yt-dlp.exe externo (onefile: ~1,4 s de arranque por
    consulta). Si el módulo no está disponible o falla, cae al exe."""
    # 1) en proceso: sin arrancar un proceso nuevo
    info = _ytdlp_info_proceso(url, extra, timeout)
    if info is not None:
        return info, ""
    # 2) respaldo: el exe externo (onefile autocontenido)
    cmd = _cmd_ytdlp() + [
        "--no-playlist", "--no-warnings", "--skip-download", "-J",
    ] + extra + [url]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            return None, r.stderr or ""
        return json.loads(r.stdout), ""
    except Exception as e:
        return None, str(e)


def _ytdlp_info_proceso(url, extra, timeout=30):
    """Consulta yt-dlp en el mismo proceso (sin subproceso) usando la API
    de yt_dlp. Devuelve el dict de info (igual que -J) o None si no se
    puede (módulo ausente, timeout, error). Los extra se traducen de
    argumentos CLI a opciones de YoutubeDL: --extractor-args, --cookies y
    --cookies-from-browser."""
    try:
        import yt_dlp
    except Exception:
        return None
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "socket_timeout": 15,
            "logger": cuenta.LogAvisos()}
    # runtime JS (deno) para el desafío de YouTube: sin él, la variante con
    # cookies de sesión no genera el PO token y falla con 'The page needs
    # to be reloaded'. Si no hay deno, yt-dlp cae a lo que encuentre en
    # PATH (o anónimo, como antes).
    _deno = _ruta_deno()
    if _deno:
        opts["js_runtimes"] = {"deno": {"path": _deno}}
    # jar en memoria para --cookies (ver abajo): la opción "cookiejar" del
    # constructor de YoutubeDL NO existe en yt-dlp moderno, así que el jar se
    # asigna a ydl.cookiejar directamente tras crear el YoutubeDL
    _jar_memoria = None
    i = 0
    while i < len(extra):
        a = extra[i]
        if a == "--extractor-args" and i + 1 < len(extra):
            # formato: youtube:player_client=default,android
            texto = extra[i + 1]
            if ":" in texto:
                sitio, _, par = texto.partition(":")
                claves = par.split(",")
                d = opts.setdefault("extractor_args", {}).setdefault(sitio, {})
                for c in claves:
                    if "=" in c:
                        k, _, v = c.partition("=")
                        d.setdefault(k, []).append(v)
            i += 2
        elif a == "--cookies" and i + 1 < len(extra):
            ruta_cj = extra[i + 1]
            # jar en memoria: con cookiefile, yt-dlp REESCRIBE el archivo al
            # terminar (guarda el jar con las Set-Cookie de la respuesta) y
            # eso podía corromper la sesión 🔑 en cada consulta de calidades
            try:
                from yt_dlp.cookies import YoutubeDLCookieJar as _YDCJ
                _jar_memoria = _YDCJ(ruta_cj)
                _jar_memoria.load(ruta_cj, ignore_discard=True,
                                  ignore_expires=True)
            except Exception:
                opts["cookiefile"] = ruta_cj
            i += 2
        elif a == "--cookies-from-browser" and i + 1 < len(extra):
            opts["cookiefrombrowser"] = (extra[i + 1], None, None, None)
            i += 2
        else:
            i += 1
    ex = _futuros.ThreadPoolExecutor(max_workers=1)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            if _jar_memoria is not None:
                ydl.cookiejar = _jar_memoria
            # corre en un hilo para poder cortar por timeout (la API no
            # acepta timeout propio; socket_timeout corta sockets colgados)
            fut = ex.submit(lambda: ydl.extract_info(url, download=False))
            info = fut.result(timeout=timeout)
        # yt-dlp avisó que las cookies de la sesión 🔑 ya no son válidas:
        # invalidar al instante (el panel no tiene que estar abierto)
        if (isinstance(opts.get("logger"), cuenta.LogAvisos)
                and opts["logger"].cookies_rotadas()):
            p = _plataforma_sesion_en(extra)
            if p:
                _invalidar_sesion_detectada(p)
        return info or None
    except _futuros.TimeoutError:
        return None
    except Exception:
        return None
    finally:
        ex.shutdown(wait=False)


def _variante_con_cookies(extra):
    """True si la variante de descarga lleva cookies (Chrome o sesión 🔑)."""
    return any(a.startswith("--cookies") for a in (extra or []))


def _variante_chrome(extra):
    """True si la variante lee las cookies del PERFIL de Chrome
    (--cookies-from-browser = el junction)."""
    return any(str(a).startswith("--cookies-from-browser")
               for a in (extra or []))


def _quitar_variantes_chrome(variantes):
    """Quita de la cadena las variantes --cookies-from-browser chrome."""
    return [v for v in variantes if not _variante_chrome(v)]


def _es_error_dpapi(detalle):
    """True si yt-dlp no pudo descifrar las cookies de Chrome (v20/App-Bound,
    Chrome 2025+; yt-dlp #10927). Cuando pasa, la variante que lee el perfil
    (junction) está muerta y solo agrega ruido al error."""
    d = (detalle or "").lower()
    return ("failed to decrypt with dpapi" in d
            or "app-bound" in d or "app bound" in d
            or "10927" in d)


def _formato_sin_marca(selector):
    """Preferir formatos SIN marca de agua (TikTok marca el suyo con
    format_note='watermarked'): aplica el filtro a cada alternativa del
    selector y deja el selector original como último recurso, para que si
    solo existe la versión con marca se descargue igual en vez de fallar."""
    if not selector:
        return selector
    ramas = [r for r in selector.split("/") if r.strip()]
    return ("/".join(r + "[format_note!=watermarked]" for r in ramas)
            + "/" + selector)


def _fusionar_formatos(info, mejores):
    """Vuelca los formatos de un JSON de yt-dlp en el diccionario de mejores
    por altura, prefiriendo mp4 y el tamaño mayor."""
    for f in info.get("formats") or []:
        if (f.get("vcodec") or "none") == "none":
            continue
        h = f.get("height") or 0
        if h <= 0:
            continue
        ext = f.get("ext") or "?"
        tam = f.get("filesize") or f.get("filesize_approx")
        actual = mejores.get(h)
        if actual is None:
            mejores[h] = (ext, tam)
        elif ext == "mp4" and actual[0] != "mp4":
            mejores[h] = (ext, tam)
        elif ext == actual[0] and tam and (not actual[1] or tam > actual[1]):
            mejores[h] = (ext, tam)


def _simular_tamano(url, selector, extra, timeout=25, extra_args=None):
    """Tamaño EXACTO (bytes) que descargaría un selector para un video, con
    yt-dlp --simulate --print. El selector (y args como -S) son los MISMOS
    que usa la descarga, así el tamaño coincide con el archivo real
    (video+audio del codec que de verdad elige yt-dlp).
    Devuelve int o None si falla/expira (se usa el estimado como respaldo)."""
    cmd = _cmd_ytdlp() + [
        "--no-playlist", "--no-warnings", "--simulate",
        "--print", "%(filesize_approx)s", "-f", selector,
    ] + list(extra_args or []) + extra + [url]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            return None
        for linea in r.stdout.splitlines():
            s = linea.strip()
            if s.isdigit():
                return int(s)
        return None
    except Exception:
        return None


def _selector_mejor():
    """El selector EXACTO que usa la descarga con 'Mejor calidad disponible':
    bv*+ba sin marca de agua (con respaldo), y la preferencia de contenedor
    mp4/m4a. Devuelve (selector, args_extra) para --simulate."""
    return ("bv*+ba[format_note!=watermarked]"
            "/b[format_note!=watermarked]/bv*+ba/b",
            ["-S", "res,ext:mp4:m4a"])


def _calcular_formatos(url):
    """Ejecuta la consulta real a yt-dlp (sin caché ni single-flight).
    Devuelve [{"altura", "etiqueta", "ext", "tamano", "formato"}], un
    dict {"error": ...} o [] si falla."""
    variantes = [
        ["--extractor-args", "youtube:player_client=default,android"],
        ["--extractor-args", "youtube:player_client=android"],
        ["--extractor-args", "youtube:player_client=tv"],
        ["--cookies-from-browser", "chrome",
         "--extractor-args", "youtube:player_client=default,android"],
    ]
    # sesión 🔑 activa: la sesión exportada por la extensión es la fuente de
    # confianza; las cookies del perfil de Chrome (junction) son redundantes
    # y con Chrome 2025+ fallan con DPAPI (App-Bound) — se omiten (sin
    # sesión quedan como último recurso y el DPAPI se detecta en pleno
    # intento y se quitan solas)
    if cuenta._sesion_activa("youtube"):
        variantes = _quitar_variantes_chrome(variantes)
    # si hay sesión de Google iniciada (botón 🔑), pruébala ANTES que los
    # clientes sin sesión — pasa los videos bloqueados por el anti-bot y
    # expone las alturas que los clientes anónimos ocultan (p. ej. videos
    # que sin sesión solo muestran 360p y con sesión llegan a 1080p+).
    if cuenta._sesion_activa("youtube"):
        # web_embedded (no tv_downgraded): el cliente por defecto para
        # sesiones logueadas quedó roto con 'The page needs to be reloaded'
        # (yt-dlp #17389); el maintainer recomienda default,web_embedded
        # para usar las cookies sin que YouTube pida recargar.
        variantes.insert(0, ["--cookies", cuenta._ruta_cookies("youtube"),
                             "--extractor-args",
                             "youtube:player_client=default,web_embedded"])
    if cuenta._sesion_activa("tiktok"):
        variantes.append(["--cookies", cuenta._ruta_cookies("tiktok")])
    mejores = {}   # altura -> (ext, tamano)
    titulo = None
    errores = []

    def _correr(extras):
        nonlocal titulo
        info, err = _ytdlp_info(url, extras)
        if err:
            errores.append(err)
        if info:
            _fusionar_formatos(info, mejores)
            if not titulo:
                titulo = info.get("title") or None

    # 1) vía rápida: la sesión 🔑 (si hay) + los clientes más fiables, en
    #    paralelo. Nos vamos en cuanto el PRIMERO responda con un set de
    #    calidades completo (≥ 3 alturas); si el primero es un cliente pobre
    #    (1-2 alturas, p. ej. android solo), seguimos esperando al otro para
    #    fusionar — así nunca se pierden resoluciones por querer ir rápido.
    #    Con sesión activa corre de entrada (posición 0), no como respaldo.
    ex = _futuros.ThreadPoolExecutor(max_workers=3)
    futuros = [ex.submit(_correr, v) for v in variantes[:3]]
    while futuros:
        hechos, futuros = _futuros.wait(
            futuros, timeout=20, return_when=_futuros.FIRST_COMPLETED)
        if len(mejores) >= 3:      # set completo → no esperar al segundo
            break
        if not hechos and not futuros:
            break
    ex.shutdown(wait=False)

    # 2) respaldo (solo si no salió nada): el resto (tv, cookies de Chrome)
    if not mejores:
        ex2 = _futuros.ThreadPoolExecutor(max_workers=2)
        futuros2 = [ex2.submit(_correr, v) for v in variantes[3:]]
        while futuros2:
            hechos2, futuros2 = _futuros.wait(
                futuros2, timeout=25, return_when=_futuros.FIRST_COMPLETED)
            if len(mejores) >= 3 or (not hechos2 and not futuros2):
                break
        ex2.shutdown(wait=False)

    if not mejores:
        # si YouTube pide iniciar sesión, dímoselo claro (no es un error
        # de red ni nuestro)
        texto = "\n".join(errores)
        if "Sign in to confirm" in texto or "confirm you" in texto:
            return {"error": ("YouTube pide iniciar sesión para este video "
                              "('Sign in to confirm you're not a bot'). "
                              "Pulsa el botón 🔑 Sesión y entra con tu "
                              "cuenta de Google, o abre el video en Chrome "
                              "e inicia sesión; luego reintenta.")}, None
        return [], None
    lista = []
    for h in sorted(mejores, reverse=True):
        ext, tam = mejores[h]
        lista.append({
            "altura": h,
            "etiqueta": f"{h}p · {ext}",
            "ext": ext,
            "tamano": tam,
            # altura EXACTA primero (para que no baje de resolución en
            # silencio), y si ese cliente no la expone, cae a la mejor <=h:
            # así una 4K no falla solo porque el cliente android no la traiga
            "formato": _formato_sin_marca(
                f"bv[height={h}]+ba/bv[height<={h}]+ba/b"),
        })
    lista.append({"altura": 0, "etiqueta": "Solo audio (mp3)",
                  "ext": "mp3", "tamano": None, "formato": "audio"})
    # tamaño EXACTO por altura: el menú muestra lo que de verdad va a pesar
    # la descarga (video+audio del codec que elige yt-dlp), no un estimado
    # de un codec distinto. --simulate con el MISMO selector del menú, en
    # paralelo; si una simulación falla o expira, queda el estimado.
    # (Solo en la primera consulta: el resultado se cachea 30 min.)
    extras_sim = variantes[0] if variantes else []
    clave = _clave_video(url)
    # tamaños ya simulados en caché de disco (7 días): esos selectores NO se
    # vuelven a simular — la consulta abre al instante
    with _FORMATOS_TAM_LOCK:
        tam_cache = dict(_FORMATOS_TAM_CACHE.get(clave) or {})
    pendientes = [o for o in lista
                  if o["altura"] > 0 and o["formato"] not in tam_cache]
    for o in lista:
        if o["altura"] > 0 and o["formato"] in tam_cache:
            o["tamano"] = tam_cache[o["formato"]][0]
    sel_mejor, args_mejor = _selector_mejor()
    if sel_mejor not in tam_cache:
        pendientes.append({"altura": -1, "formato": sel_mejor,
                           "tamano": None, "args": args_mejor})
    # simulamos SOLO lo que falta, en paralelo (máx 4 workers)
    if pendientes:
        exs = _futuros.ThreadPoolExecutor(max_workers=min(4, len(pendientes)))
        futs = {exs.submit(
            _simular_tamano, url, o["formato"], extras_sim,
            extra_args=o.get("args")): o for o in pendientes}
        nuevos = {}
        try:
            for fut in _futuros.as_completed(futs, timeout=45):
                o = futs[fut]
                try:
                    tam = fut.result()
                except Exception:
                    tam = None
                if tam:
                    o["tamano"] = tam
                    nuevos[o["formato"]] = [tam, time.time()]
        except _futuros.TimeoutError:
            pass
        exs.shutdown(wait=False)
        if nuevos:
            with _FORMATOS_TAM_LOCK:
                _FORMATOS_TAM_CACHE.setdefault(clave, {}).update(nuevos)
            _guardar_tamanos_cache()
    # la opción "mejor" va PRIMERO en el menú (como en el panel): le
    # adjuntamos el tamaño simulado (del cache o recién calculado)
    mejor_tam = tam_cache.get(sel_mejor, [None])[0]
    for o in pendientes:
        if o["altura"] == -1 and o.get("tamano"):
            mejor_tam = o["tamano"]
    lista.insert(0, {"altura": None, "etiqueta": "Mejor calidad (recomendada)",
                     "ext": "", "tamano": mejor_tam,
                     "formato": None, "mejor": True})
    return lista, titulo


def _formatos(url):
    """Calidades de un video: caché (30 min, persistida a disco) + single-
    flight (dos peticiones simultáneas del mismo video comparten la consulta
    yt-dlp). Devuelve [{"altura", ...}] o {"error": ...} o [] si falla."""
    clave = _clave_video(url)
    with _FORMATOS_LOCK:
        if clave in _FORMATOS_CACHE:
            ts, lista, _titulo = _FORMATOS_CACHE[clave]
            if time.time() - ts < _FORMATOS_CACHE_TTL:
                return lista
    # single-flight: si otra petición ya está consultando este video,
    # esperamos su resultado en vez de lanzar otro yt-dlp
    with _FORMATOS_EN_CURSO_LOCK:
        ev = _FORMATOS_EN_CURSO.get(clave)
        if ev is None:
            ev = threading.Event()
            _FORMATOS_EN_CURSO[clave] = ev
            soy_el_primero = True
        else:
            soy_el_primero = False
    if not soy_el_primero:
        ev.wait(timeout=35)
        with _FORMATOS_LOCK:
            if clave in _FORMATOS_CACHE:
                return _FORMATOS_CACHE[clave][1]
        return []
    try:
        resultado, titulo = _calcular_formatos(url)
        if isinstance(resultado, list):
            # guarda también el TÍTULO real: así una tarea nueva que se
            # descarga justo después de consultar calidades nace con el
            # nombre correcto (sin "watch") leyendo la caché
            with _FORMATOS_LOCK:
                _FORMATOS_CACHE[clave] = (time.time(), resultado, titulo)
            _guardar_formatos_cache()
        return resultado
    finally:
        with _FORMATOS_EN_CURSO_LOCK:
            _FORMATOS_EN_CURSO.pop(clave, None)
        ev.set()


# ---------------------------------------------------- enlaces (zona-leros/pivigames)
# URLs que claramente no son una página de juego (imágenes, comprimidos...):
# extraer enlaces de ellas lanzaría Chrome para nada.
_EXT_ARCHIVO = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
                ".avif", ".ico", ".zip", ".rar", ".7z", ".tar", ".gz",
                ".exe", ".mp4", ".mkv", ".pdf")


def _es_url_archivo(url):
    p = urllib.parse.urlparse(url).path.lower()
    return p.endswith(_EXT_ARCHIVO)


def _pagina_enlaces(url):
    """Devuelve el módulo extractor adecuado para la URL, o None."""
    if "zona-leros.com" in url:
        return zonaleros
    if pivigames._es_pivigames(url):
        return pivigames
    return None


_HILO_SESION = None          # hilo del inicio de sesión de Google
_ENLACES_CACHE = {}          # url -> (timestamp, resultado)
_ENLACES_TTL = 12 * 3600     # 12 horas: los enlaces rara vez cambian
_ENLACES_LOCK = threading.Lock()
_ENLACES_TAREAS = {}         # id -> {url, estado, resultado, ts} (en curso)


def _enlaces_extraer(url):
    """(síncrono) Lista los enlaces de descarga de una página de juego
    (zona-leros.com o pivigames.blog). Usa el Chrome real del usuario para
    pasar Cloudflare; el resultado se guarda en caché 12 horas (la
    extracción tarda ~1-3 minutos)."""
    modulo = _pagina_enlaces(url)
    if modulo is None:
        return {"error": "no parece un enlace de zona-leros.com ni de pivigames.blog"}
    with _ENLACES_LOCK:
        if url in _ENLACES_CACHE:
            ts, r = _ENLACES_CACHE[url]
            if time.time() - ts < _ENLACES_TTL:
                return r
        resultado = modulo.extraer(url)
        # solo se cachea si salió al menos un enlace (una extracción vacía
        # no merece quedar guardada) y el resultado no quedó incompleto
        # (las series parciales se reintentan para completar episodios)
        if (not resultado.get("error") and not resultado.get("incompleto")
                and any(s.get("enlaces")
                        for s in resultado.get("servidores", []))):
            _ENLACES_CACHE[url] = (time.time(), resultado)
        return resultado


def _enlaces_lanzar(url):
    """Devuelve la extracción si ya está en caché, o la arranca en un hilo
    y devuelve un id de tarea para sondearla. Así el panel no mantiene una
    conexión HTTP de 1-3 minutos que se cae con 'Failed to fetch' cuando
    el servidor se reinicia o la red hace un corte transitorio."""
    if _pagina_enlaces(url) is None:
        return {"error": "no parece un enlace de zona-leros.com ni de pivigames.blog"}
    if _es_url_archivo(url):
        return {"error": ("esa URL es un archivo o imagen, no una página de "
                          "juego. Pega la URL de la página "
                          "(ej. .../juegos-pc/<nombre-del-juego>).")}
    with _ENLACES_LOCK:
        if url in _ENLACES_CACHE:
            ts, r = _ENLACES_CACHE[url]
            if time.time() - ts < _ENLACES_TTL:
                return r
    # si ya hay una extracción en curso de esta misma url, reusa la tarea
    for tid, t in list(_ENLACES_TAREAS.items()):
        if t.get("url") == url and t["estado"] == "trabajando":
            return {"tarea": tid}
    tid = uuid.uuid4().hex[:8]
    _ENLACES_TAREAS[tid] = {"url": url, "estado": "trabajando",
                            "resultado": None, "ts": time.time()}

    def _trabajo():
        try:
            _ENLACES_TAREAS[tid]["resultado"] = _enlaces_extraer(url)
        except Exception as e:
            _ENLACES_TAREAS[tid]["resultado"] = {
                "error": "error extrayendo: %s" % e}
        finally:
            _ENLACES_TAREAS[tid]["estado"] = "listo"
            _ENLACES_TAREAS[tid]["ts"] = time.time()

    threading.Thread(target=_trabajo, daemon=True).start()
    return {"tarea": tid}


def _estado_enlaces_tarea(tid):
    """Devuelve (datos, codigo) del estado de una extracción en curso."""
    t = _ENLACES_TAREAS.get(tid)
    if not t:
        return {"error": "tarea no encontrada"}, 404
    if t["estado"] == "trabajando":
        return {"estado": "trabajando"}, 200
    # limpieza de tareas viejas (más de 1 hora) para no acumular memoria
    for k, v in list(_ENLACES_TAREAS.items()):
        if time.time() - v["ts"] > 3600:
            _ENLACES_TAREAS.pop(k, None)
    return {"estado": "listo", "resultado": t["resultado"]}, 200


_UA_VERIF = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _verificar_un_enlace(url):
    """Clasifica un enlace de descarga: 'activo', 'caido', 'navegador'
    (responde pero exige navegador real: no se puede confirmar sin Chrome)
    o 'error'. Sin abrir Chrome: peticiones ligeras con timeout corto."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    try:
        # Mega: la página responde 200 siempre (SPA); se comprueba la API
        if "mega.nz" in host or "mega.co.nz" in host:
            m = re.search(r"/file/([A-Za-z0-9_-]{8})", url)
            if m:
                try:
                    r = mega._api([{"a": "g", "g": 1, "p": m.group(1)}])
                    if isinstance(r, list) and r and isinstance(r[0], dict):
                        return "activo"
                except Exception:
                    pass
            return "caido"
        # Gofile: API pública
        if "gofile.io" in host:
            m = re.search(r"gofile\.io/d/([A-Za-z0-9]+)", url)
            if m:
                try:
                    req = urllib.request.Request(
                        "https://api.gofile.io/getContent?contentId=" + m.group(1),
                        headers={"User-Agent": _UA_VERIF})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        d = json.loads(r.read().decode("utf-8", "replace"))
                    ok = (d.get("status") == "ok"
                          and bool(d.get("data", {}).get("contents")))
                    return "activo" if ok else "caido"
                except Exception:
                    return "caido"
        # MediaFire y 1fichier: responden 200 aunque el archivo no exista;
        # se busca en el HTML la señal de archivo borrado/eliminado
        if "mediafire.com" in host or "1fichier.com" in host:
            try:
                req = urllib.request.Request(url,
                                             headers={"User-Agent": _UA_VERIF})
                with urllib.request.urlopen(req, timeout=12) as r:
                    body = r.read(60000).decode("utf-8", "replace").lower()
                senales = ("not found", "no longer available", "deleted",
                           "removed", "suppressed", "no se ha encontrado",
                           "archivo no disponible", "file has been")
                for s in senales:
                    if s in body:
                        return "caido"
                return "activo"
            except urllib.error.HTTPError as e:
                return ("caido" if e.code in (404, 410, 451)
                        else "navegador" if e.code == 403 else "caido")
            except Exception:
                return "caido"
        # resto: HEAD con timeout (los servidores que exigen navegador
        # devuelven 403 a un cliente normal: no es que esté caído)
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": _UA_VERIF})
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                cod = r.getcode()
                return ("activo" if cod < 400
                        else "navegador" if cod == 403 else "caido")
        except urllib.error.HTTPError as e:
            return ("navegador" if e.code == 403
                    else "caido" if e.code in (404, 410, 451) else "caido")
        except Exception:
            # sin soporte HEAD: probar GET de un rango de 1 byte
            req2 = urllib.request.Request(
                url, headers={"User-Agent": _UA_VERIF, "Range": "bytes=0-0"})
            try:
                with urllib.request.urlopen(req2, timeout=12) as r:
                    return "activo" if r.getcode() < 400 else "caido"
            except urllib.error.HTTPError as e:
                return "navegador" if e.code == 403 else "caido"
            except Exception:
                return "caido"
    except Exception:
        return "error"


def _verificar_enlaces(urls):
    """Verifica varias URLs en paralelo. Devuelve {url: estado}."""
    with _futuros.ThreadPoolExecutor(max_workers=8) as ex:
        estados = list(ex.map(_verificar_un_enlace, urls))
    return dict(zip(urls, estados))


class _TrabajoYtdlp:
    """Descarga vía yt-dlp (videos, MediaFire, etc.) con progreso por líneas."""

    def __init__(self, url, carpeta, formato=None, conexiones=None,
                 limite_kbps=0):
        self.id = uuid.uuid4().hex[:8]
        self.url = url
        self.carpeta = carpeta
        self.formato = formato or "mejor"
        self.conexiones = conexiones or 8
        self.limite_kbps = max(0, int(limite_kbps or 0))
        self.nombre = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or "video"
        # si el video ya se consultó (calidades en caché), el título real está
        # disponible al instante: la tarjeta nace con el nombre correcto y no
        # muestra "watch" ni un instante (el hilo lo resolvería igual, pero
        # tarda unos segundos en arrancar)
        try:
            with _FORMATOS_LOCK:
                _clave_c = _clave_video(url)
                if _clave_c in _FORMATOS_CACHE:
                    _ts_c, _lst_c, _tit_c = _FORMATOS_CACHE[_clave_c]
                    if _tit_c:
                        self.nombre = _tit_c[:120]
        except Exception:
            pass
        self.total = None
        self.descargado = 0
        self.velocidad = 0.0
        self.estado = "esperando"
        self.error = None
        self._aviso = None   # aviso visible en la tarjeta (p. ej. anti-cuelgue)
        self._proc = None
        self._hilo = None
        self._cancelar = threading.Event()
        self._salida = []
        self._cmd_activo = None     # comando que se está/reanudará usando
        self._reanudando = False
        self._pausado = False
        # acumulación de progreso multi-stream (video + audio)
        self._archivo_progreso = None
        self._descargado_archivo = 0
        self._total_archivo = 0
        self._acum_descargado = 0
        self._acum_total = 0
        self.calidad = None
        self.calidad_real = None   # resolución REAL leída con ffmpeg al terminar
        self._archivo_final = None

    def iniciar(self):
        self._hilo = threading.Thread(target=self._run, daemon=True)
        self._hilo.start()
        return self

    def _obtener_titulo(self):
        """Pide el título real a yt-dlp sin descargar nada (rápido)."""
        # si ya se consultaron las calidades de este video, el título ya está
        # en caché (viene en el mismo JSON) -> cero segundos
        with _FORMATOS_LOCK:
            clave = _clave_video(self.url)
            if clave in _FORMATOS_CACHE:
                ts, _lista, titulo = _FORMATOS_CACHE[clave]
                if time.time() - ts < _FORMATOS_CACHE_TTL and titulo:
                    return titulo[:120]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        for extra in _variantes_ytdlp():
            try:
                cmd = _cmd_ytdlp() + [
                    "--no-playlist", "--no-warnings", "--skip-download",
                    "--print", "%(title)s",
                ] + extra + [self.url]
                r = subprocess.run(
                    cmd, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=90, env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip().splitlines()[0][:120]
            except Exception:
                continue
        return None

    def _run(self):
        os.makedirs(self.carpeta, exist_ok=True)
        # Al reanudar, reutilizar el comando que ya funcionaba; con
        # --continue yt-dlp continúa el archivo .part donde quedó.
        # Si falla (p. ej. YouTube bloquea de nuevo), cae a un intento fresco.
        if self._cmd_activo and self._reanudando:
            self._reanudando = False
            self._salida = []
            cmd = self._cmd_activo + ["--continue", "--no-overwrites"]
            codigo, detalle = self._ejecutar(cmd)
            if codigo == 0:
                return
            errores_resume = [f"reanudar: {detalle or codigo}"]
            # intento fresco desde cero si el reanudado no funcionó
            variantes = self._variantes()
            intento = 0
            while intento < len(variantes):
                if self._cancelar.is_set() or self._pausado:
                    return
                extra = variantes[intento]
                cmd = self._cmd_base(extra)
                self._cmd_activo = cmd
                codigo, detalle = self._ejecutar(cmd)
                if codigo == 0:
                    return
                if codigo == -2:
                    return
                # cookies de Chrome sin descifrar: omitir las restantes
                if _es_error_dpapi(detalle):
                    restantes = variantes[intento + 1:]
                    if any(_variante_chrome(v) for v in restantes):
                        variantes[intento + 1:] = _quitar_variantes_chrome(restantes)
                # misma priorización de cookies que en el flujo principal
                if (self._altura_pedida()
                        and detalle
                        and "Requested format is not available" in detalle
                        and any(_variante_con_cookies(v)
                               for v in variantes[intento + 1:])):
                    restantes = variantes[intento + 1:]
                    variantes[intento + 1:] = (
                        [v for v in restantes if _variante_con_cookies(v)]
                        + [v for v in restantes if not _variante_con_cookies(v)])
                errores_resume.append(f"intento {intento + 1}: {detalle or codigo}")
                intento += 1
            self.estado = "error"
            self._aviso = None   # el banner de error explica el fallo final
            self.error = "yt-dlp falló: " + " // ".join(errores_resume[-3:])
            _registrar_error(self)
            return
        # título real para mostrarlo en el panel en lugar de "watch"
        titulo = self._obtener_titulo()
        if titulo:
            self.nombre = titulo
        self.estado = "descargando"
        self._salida = []

        errores = []
        variantes = self._variantes()
        intento = 0
        while intento < len(variantes):
            if self._cancelar.is_set():
                return
            if self._pausado:
                return  # quedó pausado a mitad de intento
            extra = variantes[intento]
            cmd = self._cmd_base(extra)
            self._cmd_activo = cmd
            # mismo cliente con reintento: si falla a mitad (caída de red,
            # 403 puntual), se vuelve a lanzar CON --continue y retoma el
            # .part donde quedó en lugar de descargar los GB otra vez
            for reintento in range(3):
                if self._cancelar.is_set() or self._pausado:
                    return
                reint_cmd = cmd
                if reintento:
                    reint_cmd = cmd + ["--continue", "--no-overwrites"]
                codigo, detalle = self._ejecutar(reint_cmd)
                if codigo == 0:
                    return
                if codigo == -2:
                    return  # pausado por el usuario
                if reintento < 2:
                    time.sleep(5)   # respiro antes de retomar el .part
            # cookies de Chrome sin descifrar (v20/App-Bound): las variantes
            # --cookies-from-browser restantes están muertas — quitarlas ya
            # para no gastar intentos ni ensuciar el error con DPAPI
            if _es_error_dpapi(detalle):
                restantes = variantes[intento + 1:]
                if any(_variante_chrome(v) for v in restantes):
                    variantes[intento + 1:] = _quitar_variantes_chrome(restantes)
                    _log_servidor(
                        "cookies de Chrome no descifrables (DPAPI/App-Bound, "
                        "yt-dlp #10927): se omiten las variantes del perfil "
                        "de Chrome; exportá la sesión con 'Exportar sesión "
                        "🔑' de la extensión para descargar autenticado")
            # Pedido de altura concreta y el cliente no la expone
            # ("Requested format is not available"): en vez de seguir con
            # clientes anónimos que van a fallar igual, priorizamos los
            # intentos CON cookies (Chrome / sesión 🔑), que exponen todas
            # las calidades y desbloquean la altura pedida.
            if (self._altura_pedida()
                    and detalle and "Requested format is not available" in detalle
                    and any(_variante_con_cookies(v) for v in variantes[intento + 1:])):
                restantes = variantes[intento + 1:]
                con_cookies = [v for v in restantes if _variante_con_cookies(v)]
                sin_cookies = [v for v in restantes if not _variante_con_cookies(v)]
                variantes[intento + 1:] = con_cookies + sin_cookies
                _log_servidor("calidad concreta: el cliente no tiene la altura, "
                              "se priorizan las cookies para reintentar")
            errores.append(f"intento {intento + 1}: {detalle or 'código ' + str(codigo)}")
            intento += 1
        # todos los intentos fallaron
        self.estado = "error"
        self._aviso = None   # el banner de error explica el fallo final
        resumen = " // ".join(errores[-3:])
        altura = self._altura_pedida()
        # Cookies de Chrome ilegibles (App-Bound/DPAPI): aunque el resto de
        # la cadena haya fallado, el mensaje debe explicar CÓMO destrabar
        # (exportar la sesión con la extensión), no solo mostrar el ruido.
        if (_es_error_dpapi(resumen)
                and not cuenta._sesion_activa("youtube")):
            self.error = (
                "Chrome no deja leer sus cookies (cifrado App-Bound de "
                "2025+): yt-dlp no puede descifrarlas con DPAPI. Abrí la "
                "extensión y pulsa 'Exportar sesión 🔑' con tu cuenta de "
                "YouTube para descargar autenticado y sin este error. "
                "Detalle: " + resumen)
        # Video DRM/restringido: YouTube corta los streams altos con 403 y
        # los clientes anónimos solo llegan a baja calidad (o a nada). En vez
        # de un error genérico, decimos qué pasó y qué desbloquearía la altura.
        # (elif: si ya se explicó el DPAPI arriba, no lo pisamos — el fix de
        # cookies es el mensaje accionable y el DRM solo suma ruido.)
        elif (altura and ("403" in resumen or "DRM" in resumen
                          or "Requested format is not available" in resumen)):
            self.error = (
                f"YouTube no permite descargar {altura}p de este video sin "
                "sesión iniciada (DRM): cortó la descarga con error 403 en "
                "todos los clientes. Activá la sesión 🔑 con tu cuenta de "
                "YouTube o iniciá sesión en YouTube dentro de Chrome y "
                "reintentá. Para bajar algo igual ahora, elegí 'Mejor "
                "calidad disponible' (puede quedar en 360p). "
                f"Detalle: {resumen}")
        else:
            self.error = "yt-dlp falló en todos los intentos: " + resumen
        _registrar_error(self)

    def _altura_pedida(self):
        """La altura exacta que pidió el usuario (de un selector bv[height=N]),
        o None si pidió 'mejor'/'audio'/otro selector."""
        m = re.search(r"bv\[height=(\d+)\]", self.formato or "")
        return int(m.group(1)) if m else None

    def _nombre_generico(self):
        """True si el nombre es el placeholder inicial de la URL (p. ej.
        'watch' de YouTube) y no el título real del video."""
        n = (self.nombre or "").strip().lower()
        if not n:
            return True
        # basename de la URL sin extensión (watch, video, download, ...) o
        # placeholder corto genérico: NO es un título real
        if n in ("watch", "video", "download", "descarga", "shorts",
                 "embed", "playlist", "v"):
            return True
        # nombre que parece un basename de URL (sin extensión ni espacios,
        # corto): p. ej. 'watch' cae arriba; 'archivo123' también es genérico
        if (len(n) < 12 and "." not in n and " " not in n
                and not any(c.isdigit() for c in n)):
            return True
        return False

    def _variantes(self):
        """Estrategias de descarga en cadena, de la más completa a la más simple.
        default,android expone TODAS las calidades (web primero, android de
        respaldo contra el 403); las cookies de la sesión 🔑 van antes que
        las de chrome (que fallan con DPAPI si el navegador está abierto).

        Con una altura CONCRETA pedida, los clientes con cookies van primero:
        los anónimos (android, tv) no siempre exponen la altura pedida, así
        que un fallback a ellos perdería calidad o fallaría; los autenticados
        sí la tienen."""
        variantes = [
            ["--extractor-args", "youtube:player_client=default,android"],
            ["--extractor-args", "youtube:player_client=android"],
            ["--extractor-args", "youtube:player_client=tv"],
            ["--cookies-from-browser", "chrome",
             "--extractor-args", "youtube:player_client=default,android"],
            [],
        ]
        # sesión 🔑 activa: la sesión exportada por la extensión es la fuente
        # de confianza (sesión PRIMERO, junction solo como último recurso y
        # sin sesión). Con Chrome 2025+ el junction falla con DPAPI
        # (App-Bound, yt-dlp #10927): se omite para no gastar intentos ni
        # ensuciar el error; si no hay sesión queda y el DPAPI se detecta y
        # se quita en pleno intento
        if cuenta._sesion_activa("youtube"):
            variantes = _quitar_variantes_chrome(variantes)
        if cuenta._sesion_activa("youtube"):
            # web_embedded (ver _calcular_formatos): el cliente por defecto
            # para logueados (tv_downgraded) falla con 'The page needs to be
            # reloaded' (yt-dlp #17389); web_embedded usa las cookies bien.
            # Va PRIMERO (posición 0), igual que en _calcular_formatos: los
            # clientes anónimos expuestos con un selector tolerante ("mejor")
            # "tienen éxito" bajando 360p y la sesión que ve 1080p+ nunca
            # correría. Con la sesión primero, los videos restringidos bajan
            # la mejor calidad real; si web_embedded falla (anti-bot), la
            # cadena cae a los anónimos como antes.
            variantes.insert(0, ["--cookies",
                                 cuenta._ruta_cookies("youtube"),
                                 "--extractor-args",
                                 "youtube:player_client=default,web_embedded"])
        if cuenta._sesion_activa("tiktok"):
            variantes.append(["--cookies", cuenta._ruta_cookies("tiktok")])
        if self._altura_pedida():
            primero = variantes[0]   # la sesión 🔑 (si hay) o default,android
            con_cookies = [v for v in variantes[1:]
                           if _variante_con_cookies(v)]
            sin_cookies = [v for v in variantes[1:]
                           if not _variante_con_cookies(v)]
            variantes = [primero] + con_cookies + sin_cookies
        return variantes

    def _cmd_base(self, extra):
        cmd = _cmd_ytdlp() + [
            "--newline", "--no-playlist", "--no-warnings",
            # alta calidad = archivos de GB con miles de fragmentos:
            # más reintentos para aguantar caídas puntuales
            "--retries", "10", "--fragment-retries", "10",
            "--file-access-retries", "5",
            # anti-cuelgue (las de alta resolución se quedaban colgadas a
            # mitad con la velocidad congelada):
            #  - socket muerto se corta a los 15 s (antes el default de 20 s
            #    con 10 reintentos por fragmento = minutos sin progreso)
            #  - si YouTube empieza a ESTRANGULAR (trickle de bytes que nunca
            #    dispara el socket timeout), --throttled-rate aborta y
            #    re-extrae URLs frescas en vez de quedar congelado para siempre
            "--socket-timeout", "15",
            "--throttled-rate", "128K",
            # descarga fragmentos en paralelo (como hace el reproductor de
            # YouTube) — acelera mucho; 8+ parece bot y dispara el
            # estrangulamiento de YouTube, así que se topea en 4
            "--concurrent-fragments", str(min(self.conexiones or 8, 4)),
            # progreso en JSON por línea: archivo, bajado, total, altura, velocidad
            "--progress-template",
            "download:{'f':'%(info.filename)s','d':%(progress.downloaded_bytes)s,"
            "'t':%(progress.total_bytes)s,'h':'%(info.height)s',"
            "'s':%(progress.speed)s}",
        ]
        # runtime JS (deno) también en la descarga: el PO token puede
        # pedirse a mitad de archivo (re-extracción por throttling) y sin
        # él la sesión/cookies fallan con 'page needs to be reloaded'
        _deno = _ruta_deno()
        if _deno:
            cmd += ["--js-runtimes", "deno:" + _deno]
        # límite de velocidad: por tarea o el global (KB/s)
        _lim = self.limite_kbps if self.limite_kbps else LIMITE_VELOCIDAD_KBPS
        if _lim:
            if _lim >= 1024:
                cmd += ["--limit-rate", "%.1fM" % (_lim / 1024.0)]
            else:
                cmd += ["--limit-rate", "%dK" % max(1, _lim)]
        if self.formato == "audio":
            cmd += ["-f", "ba/b", "-x", "--audio-format", "mp3"]
        elif self.formato == "mejor":
            # prefiere el formato limpio (TikTok etiqueta el suyo como
            # 'watermarked') y cae al con marca solo si no hay otra opción
            cmd += ["-f", "bv*+ba[format_note!=watermarked]"
                    "/b[format_note!=watermarked]/bv*+ba/b",
                    # mp4 en vez de webm: misma resolución y codec, pero el
                    # contenedor que reproducen Windows, TV, editores y
                    # WhatsApp sin problemas (webm/AV1 no lo reproducen todos)
                    "-S", "res,ext:mp4:m4a"]
        else:
            # Altura CONCRETA pedida por el usuario: selector ESTRICTO, sin
            # el fallback bv[height<=N] que degrada silenciosamente. Si el
            # cliente de turno no expone la altura exacta (YouTube DRM, el
            # cliente android solo trae 360p...), yt-dlp FALLA ese intento y
            # el código pasa al siguiente cliente en vez de completar con
            # menos calidad de la pedida. Si ningún cliente la tiene, marca
            # error claro en lugar de entregar 360p por 2160p.
            m = re.search(r"bv\[height=(\d+)\]", self.formato or "")
            if m:
                sel = "bv[height=%s]+ba" % m.group(1)
            else:
                sel = self.formato
            cmd += ["-f", _formato_sin_marca(sel)]
            # formatos concretos: también prefiere mp4 si el servidor lo da
            cmd += ["-S", "res,ext:mp4:m4a"]
        ff = _ruta_ffmpeg()
        if ff:
            cmd += ["--ffmpeg-location", os.path.dirname(ff)]
        # contenedor final mp4: cuando yt-dlp fusiona video+audio, que el
        # resultado sea .mp4 (el -S de arriba prefiere fuentes mp4; esto
        # asegura que la FUSIÓN también quede en mp4 y no vuelva a webm)
        if self.formato != "audio":
            cmd += ["--merge-output-format", "mp4"]
        cmd += extra + [
            "-o", os.path.join(self.carpeta, "%(title).120s.%(ext)s"),
            self.url,
        ]
        return cmd

    def _ejecutar(self, cmd):
        """Ejecuta un comando yt-dlp, actualizando progreso. Devuelve (código, detalle)."""
        self.estado = "descargando"
        # reiniciar acumuladores por si se reintenta
        self._archivo_progreso = None
        self._descargado_archivo = 0
        self._total_archivo = 0
        self._acum_descargado = 0
        self._acum_total = 0
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            return -1, f"yt-dlp no disponible: {e}"
        # centinela anti-cuelgue (red de seguridad por si yt-dlp se queda
        # mudo pese a --socket-timeout/--throttled-rate: p. ej. extracción
        # o fusión eterna). Si no llega NADA durante 180 s con el proceso
        # vivo, se mata y el bucle de reintentos retoma el .part con
        # --continue en vez de dejar la tarjeta congelada para siempre.
        _avance = {"ts": time.time()}
        _proc_actual = self._proc

        def _centinela():
            while True:
                time.sleep(15)
                if _proc_actual.poll() is not None:
                    return
                if time.time() - _avance["ts"] > 180:
                    try:
                        _log_servidor(
                            "anti-cuelgue: %s sin progreso, se corta yt-dlp "
                            "para retomar el .part" % self.id)
                    except Exception:
                        pass
                    # aviso visible en la tarjeta: el usuario ve que no se
                    # colgó, que se está reanudando el .part donde quedó
                    try:
                        self._aviso = (
                            "YouTube cortó la conexión a mitad de archivo — "
                            "se está reanudando donde quedó (no vuelve a "
                            "empezar).")
                    except Exception:
                        pass
                    try:
                        _proc_actual.kill()
                    except Exception:
                        pass
                    return

        threading.Thread(target=_centinela, daemon=True).start()
        while True:
            linea = self._proc.stdout.readline()
            if not linea:
                break
            _avance["ts"] = time.time()
            linea = linea.strip()
            if linea.startswith("{'f':") and "'t':" in linea:
                self._parsear_json(linea)
            elif linea.startswith("ERROR"):
                self._salida.append(linea)
            # cookies de la sesión 🔑 rotadas: yt-dlp lo avisa aquí, en plena
            # descarga, sin que el panel esté abierto -> invalidar YA
            if "no longer valid" in linea.lower():
                p = _plataforma_sesion_en(cmd)
                if p:
                    _invalidar_sesion_detectada(p)
        codigo = self._proc.wait()
        if self._pausado:
            return -2, "pausada"
        if codigo == 0:
            self.estado = "completa"
            self._aviso = None   # ya terminó: el aviso ya no aplica
            self._verificar_y_organizar()
            return 0, ""
        detalle = " | ".join(self._salida[-3:])
        return codigo, detalle

    def _verificar_y_organizar(self):
        """Lee el archivo final con ffmpeg: tamaño y resolución REALES.
        Así comprobamos que YouTube entregó la calidad elegida (o cuál dio).
        Los fallos quedan en servidor.log (nada en silencio)."""
        import re as _re
        ff = _ruta_ffmpeg()
        if not ff:
            _log_servidor("verificar calidad: no hay ffmpeg, se omite")
            _al_completar(self)
            return
        if not os.path.isdir(self.carpeta):
            _log_servidor("verificar calidad: carpeta inexistente %s"
                          % self.carpeta)
            _al_completar(self)
            return
        try:
            cand = [os.path.join(self.carpeta, f)
                    for f in os.listdir(self.carpeta)
                    if os.path.isfile(os.path.join(self.carpeta, f))]
        except OSError as e:
            _log_servidor("verificar calidad: no se pudo listar %s: %s"
                          % (self.carpeta, e))
            _al_completar(self)
            return

        def _es_final(nombre):
            """Archivo final: sin .part ni marcador .fNNN de fragmento DASH."""
            n = nombre.lower()
            if n.endswith(".part"):
                return False
            if _re.search(r"\.f\d+\.", n):
                return False
            return True

        def _base_ytdlp(nombre):
            """'titulo.f399.mp4.part' -> 'titulo': quita los marcadores
            temporales con los que yt-dlp nombra los streams (la barra |
            del título se convierte en ｜ fullwidth al grabar, así que el
            inicio del nombre es la parte fiable)."""
            n = nombre
            if n.lower().endswith(".part"):
                n = n[:-5]
            n = _re.sub(r"\.f\d+\.[^.]*$", "", n)
            return n

        ruta = None
        # 1) por el TÍTULO del video: el template de salida es
        #    "%(title).120s.%(ext)s", así que el archivo empieza por el
        #    nombre de la descarga. yt-dlp sanea algunos caracteres del
        #    título al grabar, por eso comparamos el inicio del nombre
        #    (prefiriendo el archivo final, sin fragmentos)
        nombre_base = (self.nombre or "").strip()[:120]
        if nombre_base:
            coinciden = [c for c in cand
                         if _base_ytdlp(os.path.basename(c))
                         .startswith(nombre_base)]
            if coinciden:
                finales = [c for c in coinciden
                           if _es_final(os.path.basename(c))]
                ruta = max(finales or coinciden, key=os.path.getmtime)
        # 2) por el archivo que vimos en el progreso (normalizado)
        if ruta is None and self._archivo_progreso:
            prog = _base_ytdlp(os.path.basename(self._archivo_progreso))
            coinciden = [c for c in cand
                         if _base_ytdlp(os.path.basename(c)) == prog]
            if coinciden:
                finales = [c for c in coinciden
                           if _es_final(os.path.basename(c))]
                ruta = max(finales or coinciden, key=os.path.getmtime)
        # 3) último recurso: el archivo final más reciente (nunca un
        #    fragmento .part: ese daría una resolución falsa o ninguna)
        if ruta is None:
            finales = [c for c in cand if _es_final(os.path.basename(c))]
            if finales:
                ruta = max(finales, key=os.path.getmtime)
        if ruta is None:
            _log_servidor("verificar calidad: no se encontró archivo final "
                          "en %s" % self.carpeta)
            _al_completar(self)
            return
        try:
            self._archivo_final = ruta
            self.total = os.path.getsize(ruta)
            self.descargado = self.total
            # resolución real con ffmpeg -i (stderr trae los streams)
            r = subprocess.run(
                [ff, "-i", ruta], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            m = _re.search(r"(\d{3,4})x(\d{3,4})", r.stderr)
            if m:
                h = int(m.group(2))
                self.calidad_real = f"{h}p"
                _log_servidor("verificar calidad: %s -> %s"
                              % (os.path.basename(ruta), self.calidad_real))
                # aviso si YouTube entregó MENOS de lo pedido: el badge del
                # panel ya lo muestra, pero también queda en errores.log
                pedida = re.search(r"bv\[height=(\d+)\]", self.formato or "")
                if pedida and h < int(pedida.group(1)):
                    tmp = self.error
                    self.error = ("calidad menor de la pedida: se pidió %sp "
                                  "y YouTube entregó %sp (DRM o cliente sin "
                                  "esa altura). Reintentá con la sesión 🔑 "
                                  "abierta o el video en Chrome."
                                  % (pedida.group(1), h))
                    _registrar_error(self)
                    self.error = tmp
            else:
                _log_servidor("verificar calidad: sin resolución en %s"
                              % os.path.basename(ruta))
        except Exception as e:
            _log_servidor("verificar calidad FALLÓ para %s: %s" % (ruta, e))
        _al_completar(self)

    def _parsear_json(self, linea):
        """Procesa la línea de progreso JSON de yt-dlp.
        Como YouTube descarga video y audio por separado, acumulamos los bytes
        de cada stream para que la barra muestre el progreso TOTAL del archivo.
        Usamos regex para extraer los valores (las rutas de Windows y los
        apóstrofes en títulos romperían eval/json.loads directo).
        El total puede venir como None (DASH de alta calidad no siempre
        anuncia el tamaño): se tolera para que el progreso no se congele.
        """
        import re as _re
        m = _re.search(
            r"'f':'(.*?)','d':(\d+),'t':([^,]+),'h':'(.*?)','s':([^,}]+)", linea)
        if not m:
            return
        fname = m.group(1)
        try:
            d = int(m.group(2))
            t_raw = m.group(3).strip()
            t = int(t_raw) if t_raw not in ("None", "NA", "") else 0
            h = m.group(4)
            s_raw = m.group(5).strip()
            s = float(s_raw) if s_raw not in ("NA", "None", "") else 0.0
        except (TypeError, ValueError):
            return
        if h:
            try:
                self.calidad = str(int(h)) + "p"
            except (TypeError, ValueError):
                pass
        # categoría real en vivo: el archivo que se está bajando ya tiene
        # extensión (ej. .webm), así la tarjeta dice "Videos" desde el inicio
        try:
            base = os.path.basename(fname)
            if base and "." in base:
                self.categoria = _categoria(base)
        except Exception:
            pass
        # Detectar cambio de stream (video -> audio): cambia el nombre del
        # archivo o el contador de bytes se reinicia (d baja). En ese caso
        # sumamos lo ya descargado al acumulado para mostrar el TOTAL real.
        if (self._archivo_progreso is not None
                and (fname != self._archivo_progreso
                     or d < self._descargado_archivo)):
            self._acum_descargado += self._descargado_archivo
            self._acum_total += self._total_archivo
            self._descargado_archivo = 0
            self._total_archivo = 0
        self._archivo_progreso = fname
        self._descargado_archivo = max(self._descargado_archivo, d)
        self._total_archivo = max(self._total_archivo, t)
        self.descargado = self._acum_descargado + self._descargado_archivo
        self.total = (self._acum_total + self._total_archivo) if t else None
        self.velocidad = float(s or 0)

    def pausar(self):
        self._pausado = True
        self.estado = "pausada"
        self._terminar()

    def reanudar(self):
        if self.estado == "pausada":
            self._pausado = False
            self._reanudando = True
            self.iniciar()

    def cancelar(self):
        self._cancelar.set()
        self.estado = "cancelada"
        self._terminar()

    def reintentar(self):
        """Vuelve a lanzar la descarga desde cero (tras cancelar/error)."""
        self._cancelar.clear()
        self._pausado = False
        self._reanudando = False
        self._cmd_activo = None
        self._salida = []
        self.error = None
        self.descargado = 0
        self.total = None
        self.estado = "esperando"
        self.iniciar()

    def _terminar(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def progreso(self):
        calidad = None
        if self.formato == "audio":
            calidad = "mp3"
        elif self.formato == "mejor":
            calidad = "Mejor"
        else:
            import re
            # altura EXACTA (height=1080) o el respaldo "mejor" (height<=1080)
            m = re.search(r"height(?:<=|=)(\d+)", self.formato or "")
            if m:
                calidad = m.group(1) + "p"
        # tareas completas con nombre genérico ("watch", "video"): el título
        # real no se pudo fijar al iniciar (yt-dlp falló o se restauró la cola
        # con el nombre inicial). Lo recuperamos de la caché de formatos o de
        # la consulta rápida a yt-dlp, para que la tarjeta muestre el título
        # verdadero en vez de "watch".
        if (self.estado == "completa" and self._nombre_generico()
                and not getattr(self, "_titulo_intentado", False)):
            self._titulo_intentado = True   # solo un intento por tarea
            try:
                titulo = self._obtener_titulo()
                if titulo:
                    self.nombre = titulo
                    # Marca que el nombre cambió para que estado() persista
                    # la cola FUERA del lock (progreso() corre bajo
                    # GESTOR._lock y _guardar_cola() toma ese mismo lock ->
                    # deadlock si se llama acá).
                    self._nombre_corregido = True
            except Exception:
                pass
        # si el archivo ya se guardó, la categoría real está en self.categoria
        categoria = getattr(self, "categoria", None) or _categoria(self.nombre)
        total = self.total
        descargado = self.descargado
        if self.estado == "completa":
            # tamaño REAL del archivo final en disco (video+audio fusionado),
            # no el estimado de YouTube: la tarjeta muestra lo que pesa
            # realmente el archivo que quedó guardado
            try:
                ruta = _archivo_real(self)
                if ruta and os.path.isfile(ruta):
                    total = os.path.getsize(ruta)
                    descargado = total
            except Exception:
                pass
        return {
            "id": self.id, "url": self.url, "nombre": self.nombre,
            "estado": self.estado, "total": total,
            "descargado": descargado, "velocidad": self.velocidad,
            "eta": None, "error": self.error, "aviso": self._aviso,
            "tipo": "yt-dlp",
            "categoria": categoria,
            "calidad": calidad,
            "calidad_real": self.calidad_real,
        }


def _archivo_real(trabajo):
    """Devuelve la ruta del archivo final de una descarga (donde quedó en
    disco tras organizar), o None si no se puede determinar."""
    ruta = getattr(trabajo, "_archivo_final", None)
    if ruta and os.path.isfile(ruta):
        return ruta
    carpeta = getattr(trabajo, "carpeta", None)
    nombre = getattr(trabajo, "nombre", None) or getattr(trabajo, "_nombre", None)
    if carpeta and nombre and os.path.isdir(carpeta):
        # 1) directo en la carpeta base
        ruta = os.path.join(carpeta, nombre)
        if os.path.isfile(ruta):
            return ruta
        # 2) en las subcarpetas de organización (Videos/, Musica/, mp3/,
        #    Comprimidos/…): las tareas restauradas de la cola no tienen
        #    _archivo_final y el archivo ya fue movido a su categoría
        try:
            nombre_base = (nombre or "").strip()[:120]
            subcarpetas = [d for d in os.listdir(carpeta)
                           if os.path.isdir(os.path.join(carpeta, d))]
            for sub in subcarpetas:
                dir_sub = os.path.join(carpeta, sub)
                try:
                    lista = os.listdir(dir_sub)
                except Exception:
                    continue
                for f in lista:
                    if not os.path.isfile(os.path.join(dir_sub, f)):
                        continue
                    if f.endswith(".part"):
                        continue
                    # nombre exacto o por TÍTULO (yt-dlp puede nombrar distinto)
                    if f == nombre or (nombre_base and f.startswith(nombre_base)):
                        return os.path.join(dir_sub, f)
        except Exception:
            pass
        # 3) fallback: tareas restauradas con nombre genérico ("watch"): el
        #    archivo real quedó en alguna subcarpeta. Si el nombre tiene
        #    extensión, matchea por tipo (Videos/, Musica/…) y mtime; si es
        #    genérico sin extensión, busca el archivo de video más reciente.
        try:
            ext = (nombre or "").rsplit(".", 1)[-1].lower()
            tiene_ext = "." in (nombre or "") and len(ext) in (3, 4)
            for sub in ["Videos", "Musica", "mp3", "Imagenes",
                        "Comprimidos", "Documentos", "Otros"]:
                dir_sub = os.path.join(carpeta, sub)
                if not os.path.isdir(dir_sub):
                    continue
                try:
                    lista = os.listdir(dir_sub)
                except Exception:
                    continue
                for f in lista:
                    p = os.path.join(dir_sub, f)
                    if not os.path.isfile(p) or f.endswith(".part"):
                        continue
                    if tiene_ext:
                        if f.lower().endswith("." + ext):
                            return p
                    elif sub == "Videos" and f.lower().endswith(
                            (".mp4", ".mkv", ".webm", ".avi", ".mov")):
                        # nombre genérico ("watch"): inequívoco cuando el
                        # video NO tiene su MP3 extraído todavía (el resto ya
                        # se convirtió) o es el único de la carpeta
                        base = os.path.splitext(f)[0]
                        mp3dir = os.path.join(carpeta, "mp3")
                        tiene_mp3 = (os.path.isdir(mp3dir) and any(
                            x.lower().endswith(".mp3")
                            and os.path.splitext(x)[0].startswith(base)
                            for x in os.listdir(mp3dir)))
                        videos = [x for x in lista
                                  if x.lower().endswith((".mp4", ".mkv",
                                                         ".webm", ".avi",
                                                         ".mov"))]
                        if not tiene_mp3 and (len(videos) == 1
                                              or sum(1 for v in videos
                                                     if not _tiene_mp3_ya(
                                                         carpeta, v)) == 1):
                            return p
        except Exception:
            pass
    return None


def _tiene_mp3_ya(carpeta, video):
    """True si un video ya tiene su MP3 extraído en la carpeta mp3/."""
    try:
        base = os.path.splitext(video)[0]
        mp3dir = os.path.join(carpeta, "mp3")
        if not os.path.isdir(mp3dir):
            return False
        return any(os.path.splitext(x)[0].startswith(base)
                   for x in os.listdir(mp3dir)
                   if x.lower().endswith(".mp3"))
    except Exception:
        return False


_TIPOS_MEDIA = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv",
    ".ts": "video/mp2t", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".flac": "audio/flac", ".wma": "audio/x-ms-wma",
}


def _tipo_media(ruta):
    """Content-Type para el reproductor según la extensión del archivo."""
    ext = os.path.splitext(ruta)[1].lower()
    return _TIPOS_MEDIA.get(ext, "application/octet-stream")


def _ruta_vlc():
    """Localiza vlc.exe en las rutas de instalación habituales de Windows.
    Devuelve None si VLC no está instalado."""
    candidatos = [
        os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"),
                     "VideoLAN", "VLC", "vlc.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)",
                                    "C:\\Program Files (x86)"),
                     "VideoLAN", "VLC", "vlc.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Programs", "VideoLAN", "VLC", "vlc.exe"),
    ]
    for p in candidatos:
        if os.path.isfile(p):
            return p
    import shutil
    return shutil.which("vlc")


def _dir_extension():
    """Carpeta fuente de la extensión de Chrome (la que contiene manifest.json).
    En el empaquetado vive en <app>/resources/extension (extraResources); en dev
    es <repo>/extension (servidor.py vive en la raíz del repo). Devuelve None si
    no está disponible."""
    candidatos = []
    if getattr(sys, "_MEIPASS", None):
        # onedir: sys.executable = <app>/resources/backend/servidor/servidor.exe
        base = os.path.dirname(sys.executable)
        candidatos.append(os.path.normpath(os.path.join(base, "..", "..", "extension")))
        candidatos.append(os.path.normpath(os.path.join(sys._MEIPASS, "..", "..", "..", "extension")))
    # dev: BASE_DIR = <repo> (servidor.py está en la raíz del repo)
    candidatos.append(os.path.normpath(os.path.join(BASE_DIR, "extension")))
    # dev con layout antiguo (servidor.py en backend/servidor/)
    candidatos.append(os.path.normpath(os.path.join(BASE_DIR, "..", "extension")))
    for c in candidatos:
        if os.path.isfile(os.path.join(c, "manifest.json")):
            return c
    return None


def _abrir_carpeta(ruta):
    """Abre una carpeta en el explorador de archivos del sistema.
    Windows usa os.startfile; macOS y Linux caen a open/xdg-open.
    Devuelve (ok, error|None)."""
    if not ruta or not os.path.isdir(ruta):
        return False, "la carpeta no existe: %s" % ruta
    try:
        if os.name == "nt":
            os.startfile(ruta)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", ruta])
        else:
            subprocess.Popen(["xdg-open", ruta])
        return True, None
    except Exception as e:
        return False, "no se pudo abrir la carpeta: %s" % e


class Gestor:
    def __init__(self):
        self.trabajos = {}
        self._lock = threading.Lock()

    def agregar(self, url, segmentos=8, carpeta=None, formato=None,
                iniciar_auto=True, origen=None, limite_kbps=None):
        carpeta = carpeta or CARPETA_DEFECTO
        # límite de ESTA descarga (KB/s; 0 = usa el límite global)
        kbps = max(0, int(limite_kbps or 0))
        bps = kbps * 1024
        # Anti-duplicados: si esta misma URL ya tiene un trabajo vivo
        # (descargando, esperando, en cola o pausada), devolvemos ese id en
        # lugar de crear otro. Evita que pulsar varias veces 'Descargar'
        # (o doble clic) encuele la misma descarga repetida.
        vivos = {"descargando", "esperando", "en cola", "uniendo", "pausada"}
        # Normaliza madiashare (downloads?d=<id> y /Link/downloads/<id> son
        # la misma descarga) para que el anti-duplicado los vea iguales y no
        # se lancen dos aria2c del mismo torrent.
        def _norm(u):
            return torrents._url_torrent_directa(u) if torrents.es_torrent(u) else u
        url_norm = _norm(url)
        with self._lock:
            for t in self.trabajos.values():
                if t.estado in vivos:
                    t_url = getattr(t, "pagina", None) or getattr(t, "url", "")
                    if _norm(t_url) == url_norm:
                        return t.id
        # file hosters (rootz.so, etc.): resuelve primero a la URL directa
        # para que el motor segmentado baje con Range, pausa y reanudación
        resuelto = None
        try:
            resuelto = hosters.resolver(url)
        except Exception as e:
            resuelto = {"error": str(e)}
        if resuelto and isinstance(resuelto, dict) and "carpeta_drive" in resuelto:
            # carpeta compartida de Google Drive: encolar cada archivo
            tids = []
            for f in resuelto.get("carpeta_drive") or []:
                f_id = self.agregar(f.get("url"), segmentos=segmentos,
                                    carpeta=carpeta, formato=formato,
                                    iniciar_auto=False, origen=origen)
                if f_id:
                    tids.append(f_id)
            if iniciar_auto:
                _procesar_cola()  # arranca los que quepan en MAX_SIMULTANEAS
            return tids[0] if tids else None
        if resuelto and resuelto.get("error"):
            t = motor.Descarga(url, carpeta, segmentos=segmentos,
                               limite_bps=bps)
            t.nombre = "⚠ " + resuelto["error"]
            t.error = resuelto["error"]
            t.estado = "error"
            _registrar_error(t)
        elif resuelto:
            t = motor.Descarga(resuelto["url"], carpeta, segmentos=segmentos,
                               nombre=resuelto.get("nombre"),
                               total=resuelto.get("tamano"),
                               post=resuelto.get("post"),
                               cookies=resuelto.get("cookies"),
                               unico=resuelto.get("unico"),
                               limite_bps=bps)
            t.nombre = resuelto.get("nombre") or t.nombre
            t.pagina = resuelto.get("pagina") or url
        elif _es_mega(url):
            # mega.nz: descarga cifrada (AES-CTR) con el motor propio de Mega
            try:
                info = mega.resolver(url)
            except Exception as e:
                t = motor.Descarga(url, carpeta, segmentos=segmentos,
                                   limite_bps=bps)
                t.nombre = "⚠ " + str(e)[:80]
                t.error = str(e)
                t.estado = "error"
                _registrar_error(t)
                info = None
            if info is not None:
                t = mega.Descarga(info, carpeta, segmentos=segmentos,
                                  limite_bps=bps)
                t.pagina = url
        elif torrents.es_torrent(url):
            # magnet / .torrent / zetrrent.com -> BitTorrent con aria2c
            t = torrents.TrabajoTorrent(url, carpeta)
            t.pagina = url
        elif _usar_ytdlp(url) and not _es_cdn_directo(url) and _ytdlp_disponible():
            t = _TrabajoYtdlp(url, carpeta, formato=formato,
                              conexiones=segmentos, limite_kbps=kbps)
        else:
            t = motor.Descarga(url, carpeta, segmentos=segmentos,
                               limite_bps=bps)
        # límite por tarea uniforme (para persistencia en la cola)
        try:
            t.limite_kbps = kbps
        except Exception:
            pass
        t.id = uuid.uuid4().hex[:8]
        t.origen = origen or ""   # "zonaleros"/"pivigames": contraseña automática
        _init_reintentos(t)
        with self._lock:
            self.trabajos[t.id] = t
        if iniciar_auto:
            t.iniciar()
        else:
            t.estado = "en cola"
        _guardar_cola()
        return t.id

    def estado(self):
        with self._lock:
            lista = []
            for t in self.trabajos.values():
                p = t.progreso()
                # si el motor directo no calculó categoría, usa la extensión
                if not p.get("categoria"):
                    p["categoria"] = _categoria(p.get("nombre") or "")
                # servidor de origen: la URL original del usuario si existe
                url = getattr(t, "pagina", None) or p.get("url") or ""
                p["servidor"] = _nombre_servidor(url)
                p["origen"] = getattr(t, "origen", "") or ""
                if getattr(t, "descompresion_estado", None):
                    p["descompresion"] = t.descompresion_estado
                    p["descompresion_msg"] = getattr(
                        t, "descompresion_msg", "") or ""
                # scheduler de reintentos: lo que le falta al panel para
                # mostrar "reintento automático en Xs"
                p["reintentos_auto"] = getattr(t, "_reintentos_auto", 0)
                p["proximo_reintento"] = getattr(t, "_proximo_reintento", 0)
                p["reintentos_max"] = REINTENTOS_MAX
                lista.append(p)
        # persiste nombres corregidos FUERA del lock: progreso() marcó
        # _nombre_corregido en tareas completadas con nombre genérico
        try:
            if any(getattr(t, "_nombre_corregido", False)
                   for t in self.trabajos.values()):
                for t in self.trabajos.values():
                    t._nombre_corregido = False
                _guardar_cola()
        except Exception:
            pass
        return lista

    def accion(self, tid, accion):
        with self._lock:
            t = self.trabajos.get(tid)
        if not t:
            return False
        getattr(t, accion)()
        if accion == "reintentar":
            _reset_reintentos(t)   # manual: el usuario toma el control
        _guardar_cola()
        return True

    def borrar(self, tid):
        with self._lock:
            t = self.trabajos.get(tid)
            if t:
                t.cancelar()
                del self.trabajos[tid]
        _guardar_cola()
        # depuración inteligente: borra los fragmentos de la descarga borrada
        nombre = getattr(t, "nombre", None)
        if nombre:
            motor.borrar_parts_de(nombre)
            try:
                destino = os.path.join(getattr(t, "carpeta", ""), nombre)
                if destino.endswith(".part") and os.path.exists(destino):
                    os.remove(destino)
            except Exception:
                pass
        motor.depurar()
        return t is not None

    def abrir(self, tid):
        with self._lock:
            t = self.trabajos.get(tid)
        if not t:
            return False
        carpeta = getattr(t, "carpeta", None)
        if carpeta and os.path.isdir(carpeta):
            os.startfile(carpeta)  # Windows
        return True

    def reproducir(self, tid):
        """Abre el archivo descargado en VLC si está instalado, o en el
        reproductor por defecto de Windows si no. Devuelve (ok, error)."""
        with self._lock:
            t = self.trabajos.get(tid)
        if not t:
            return False, "descarga no encontrada"
        ruta = _archivo_real(t)
        if not ruta or not os.path.isfile(ruta):
            return False, "el archivo no existe en disco"
        vlc = _ruta_vlc()
        if vlc:
            # lanzar VLC sin bloquear el servidor ni abrir ventana de consola
            try:
                subprocess.Popen([vlc, ruta],
                                 creationflags=getattr(subprocess,
                                                      "CREATE_NO_WINDOW", 0))
                return True, None
            except Exception as e:
                return False, "no se pudo lanzar VLC: %s" % e
        try:
            os.startfile(ruta)  # reproductor por defecto de Windows
            return True, None
        except Exception as e:
            return False, "no se pudo abrir el archivo: %s" % e

    def extraer_audio(self, tid):
        """Extrae el audio de un video descargado a MP3 (yt-dlp -x).
        Devuelve (ok, ruta_del_mp3 | mensaje_de_error)."""
        with self._lock:
            t = self.trabajos.get(tid)
        if not t:
            return False, "descarga no encontrada"
        ruta = _archivo_real(t)
        if not ruta or not os.path.isfile(ruta):
            return False, "el archivo no existe en disco"
        ext = os.path.splitext(ruta)[1].lower()
        if ext in (".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus"):
            return False, "el archivo ya es audio (%s)" % ext.lstrip(".")
        base = getattr(t, "carpeta", None) or os.path.dirname(ruta)
        # el MP3 extraído va SIEMPRE a una subcarpeta "mp3" dentro de la
        # carpeta de descargas (independiente de Organizar por Tipo)
        destino = os.path.join(base, "mp3")
        os.makedirs(destino, exist_ok=True)
        # yt-dlp no acepta rutas Windows con letra de unidad: se pasa la ruta
        # como file:/// (codificada) y se habilita con --enable-file-urls
        ff = _ruta_ffmpeg()
        url_local = "file:///" + urllib.parse.quote(
            ruta.replace("\\", "/"), safe="/:")
        # extraer en un temp y mover el mp3 al final: si la extracción falla,
        # no quedan copias del video en la carpeta Musica
        import shutil
        tmpdir = os.path.join(tempfile.gettempdir(),
                              "midesc-mp3-%s" % tid)
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
            os.makedirs(tmpdir, exist_ok=True)
        except Exception:
            pass
        cmd = _cmd_ytdlp() + [
            "--enable-file-urls",
            "-x", "--audio-format", "mp3", "--audio-quality", "0",
            "--no-playlist", "--no-warnings",
            "-o", os.path.join(tmpdir, "%(title).120s.%(ext)s"),
        ]
        if ff:
            cmd += ["--ffmpeg-location", os.path.dirname(ff)]
        cmd += [url_local]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=1800,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return False, "la extracción tardó demasiado (archivo muy grande)"
        except Exception as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return False, "no se pudo ejecutar yt-dlp: %s" % e
        if r.returncode != 0:
            detalle = (r.stderr or r.stdout or "").strip()
            shutil.rmtree(tmpdir, ignore_errors=True)
            return False, ((detalle.splitlines()[-1] if detalle
                            else "error de yt-dlp")[:200])
        # mover el mp3 generado al destino y limpiar el temp
        try:
            candidatos = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)
                          if f.lower().endswith(".mp3")]
            mp3 = max(candidatos, key=os.path.getmtime) if candidatos else None
        except Exception:
            mp3 = None
        if not mp3 or not os.path.isfile(mp3):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return False, "no se encontró el mp3 generado"
        destino_mp3 = os.path.join(destino, os.path.basename(mp3))
        try:
            if os.path.abspath(mp3) != os.path.abspath(destino_mp3):
                os.replace(mp3, destino_mp3)
        except OSError as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return False, "no se pudo mover el mp3: %s" % e
        shutil.rmtree(tmpdir, ignore_errors=True)
        _log_servidor("extraer audio: %s -> %s"
                      % (os.path.basename(ruta), os.path.basename(destino_mp3)))
        return True, destino_mp3

    def ruta_archivo(self, tid):
        """Ruta del archivo final de una descarga, o None si no existe.
        (La usa el endpoint /api/media/<id> del reproductor integrado.)"""
        with self._lock:
            t = self.trabajos.get(tid)
        if not t:
            return None
        return _archivo_real(t)


GESTOR = Gestor()


def _guardar_cola():
    """Persiste la cola en disco (cola.json) para restaurarla si el servidor
    se reinicia. Se llama en cada cambio de estado relevante (agregar, borrar,
    pausar/reanudar/cancelar/reintentar, completar, error)."""
    try:
        datos = []
        with GESTOR._lock:
            for t in list(GESTOR.trabajos.values()):
                try:
                    p = t.progreso()
                except Exception:
                    continue
                e = {
                    "tipo": p.get("tipo"),
                    "id": t.id,
                    "url": getattr(t, "pagina", None) or getattr(t, "url", ""),
                    "pagina": getattr(t, "pagina", None),
                    "carpeta": getattr(t, "carpeta", None),
                    "nombre": p.get("nombre"),
                    "total": p.get("total"),
                    "estado": p.get("estado"),
                    "error": p.get("error"),
                    "origen": getattr(t, "origen", "") or "",
                    "categoria": p.get("categoria"),
                    "servidor": p.get("servidor"),
                    "calidad": p.get("calidad"),
                    "calidad_real": p.get("calidad_real"),
                    "limite_kbps": getattr(t, "limite_kbps", 0) or 0,
                    "reintentos_auto": getattr(t, "_reintentos_auto", 0),
                    "proximo_reintento": getattr(t, "_proximo_reintento", 0),
                }
                # estado interno necesario para REANUDAR tras el reinicio
                if isinstance(t, motor.Descarga):
                    e["url_directa"] = t.url          # URL ya resuelta
                    e["segmentos"] = t.segmentos_max
                    e["post"] = t.post
                    e["cookies"] = t.cookies
                    e["unico"] = bool(t._forzar_unico)
                elif isinstance(t, mega.Descarga):
                    e["info"] = t.info                # {url, nombre, tamano, clave...}
                    e["segmentos"] = t.segmentos_max
                elif isinstance(t, _TrabajoYtdlp):
                    e["formato"] = t.formato
                    e["conexiones"] = t.conexiones
                datos.append(e)
        with open(_RUTA_COLA, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False)
    except Exception:
        pass


def _pausar_restaurada(t):
    """Marca una tarea restaurada como pausada sin arrancar su hilo."""
    try:
        ev = getattr(t, "_pausa", None)
        if ev is not None:
            ev.set()
        if getattr(t, "_pausado", None) is not None:
            t._pausado = True
    except Exception:
        pass


def _restaurar_cola():
    """Recarga la cola persistida tras un reinicio. Las tareas vivas
    (descargando/esperando/en cola/uniendo) se reanudan — el motor retoma
    los fragmentos .part en disco; las pausadas/completas/error/canceladas
    se restauran tal cual."""
    try:
        with open(_RUTA_COLA, "r", encoding="utf-8") as f:
            datos = json.load(f)
    except Exception:
        return
    if not isinstance(datos, list):
        return
    for d in datos:
        try:
            tipo = d.get("tipo")
            carpeta = d.get("carpeta") or CARPETA_DEFECTO
            url = d.get("url") or ""
            lim_kbps = d.get("limite_kbps") or 0
            if tipo == "directa":
                t = motor.Descarga(d.get("url_directa") or url, carpeta,
                                   segmentos=d.get("segmentos") or 8,
                                   nombre=d.get("nombre"),
                                   total=d.get("total"),
                                   post=d.get("post"),
                                   cookies=d.get("cookies"),
                                   unico=d.get("unico"),
                                   limite_bps=lim_kbps * 1024)
            elif tipo == "mega":
                t = mega.Descarga(d.get("info") or {}, carpeta,
                                  segmentos=d.get("segmentos") or 8,
                                  limite_bps=lim_kbps * 1024)
            elif tipo == "torrent":
                t = torrents.TrabajoTorrent(url, carpeta)
            elif tipo == "yt-dlp":
                t = _TrabajoYtdlp(url, carpeta, formato=d.get("formato"),
                                  conexiones=d.get("conexiones"),
                                  limite_kbps=lim_kbps)
            else:
                continue
            t.id = d.get("id") or uuid.uuid4().hex[:8]
            t.pagina = d.get("pagina") or None
            t.origen = d.get("origen") or ""
            try:
                t.limite_kbps = lim_kbps
            except Exception:
                pass
            if t.nombre is None:
                t.nombre = d.get("nombre")
            _init_reintentos(t, d.get("reintentos_auto") or 0,
                             d.get("proximo_reintento") or 0)
            estado = d.get("estado")
            if estado == "completa":
                t.estado = "completa"
                if d.get("total"):
                    t.total = d["total"]
            elif estado == "error":
                t.estado = "error"
                t.error = d.get("error")
            elif estado == "cancelada":
                t.estado = "cancelada"
            elif estado == "pausada":
                t.estado = "pausada"
                _pausar_restaurada(t)
            elif estado == "en cola":
                # espera su turno: la arranca _procesar_cola() respetando
                # MAX_SIMULTANEAS al final de la restauración
                t.estado = "en cola"
            else:
                # viva (descargando/esperando/uniendo): reanudar ya
                t.estado = "esperando"
                t.iniciar()
            with GESTOR._lock:
                GESTOR.trabajos[t.id] = t
        except Exception:
            continue
    try:
        _procesar_cola()   # arranca las "en cola" restauradas que quepan
    except Exception:
        pass


# ---------------------------------------------------------- lote (cola)
# Descargas en SIMULTÁNEO: la cola arranca hasta MAX_SIMULTANEAS a la vez.
MAX_SIMULTANEAS = 3        # por defecto; cambiable desde el panel


def _contar_activas():
    return sum(1 for t in GESTOR.trabajos.values()
               if t.estado in ("descargando", "uniendo", "esperando"))


def _hay_activa():
    return _contar_activas() > 0


_cola_lock = threading.Lock()


def _en_cola():
    return [t for t in GESTOR.trabajos.values() if t.estado == "en cola"]


def _procesar_cola():
    """Arranca de la cola tantas descargas como permita MAX_SIMULTANEAS.
    Los encolados son tarjetas visibles con estado "en cola"; aquí se
    lanzan los que quepan en el máximo. Se marca "esperando" antes de
    arrancar para que el hilo en probe no se cuente dos veces."""
    with _cola_lock:
        con = _contar_activas()
        for t in _en_cola():
            if con >= MAX_SIMULTANEAS:
                break
            t.estado = "esperando"   # sale de la cola: ya no se re-arranca
            t.iniciar()
            con += 1


def _encolar_lote(urls, segmentos, origen=None, limite_kbps=0):
    """Cada URL se convierte en una tarjeta visible con estado "en cola".
    Si hay hueco en el máximo de simultáneas, arrancan ya; si no, esperan
    su turno. Devuelve el número de URLs aceptadas."""
    for url in urls:
        GESTOR.agregar(url, segmentos=segmentos, iniciar_auto=False,
                       origen=origen, limite_kbps=limite_kbps)
    _procesar_cola()
    return len(urls)


# ------------------------------------------------- descompresión automática
# Cuando una descarga termina y DESCOMPRESION_AUTO está activo, los
# comprimidos (.rar/.zip/.7z/...) se extraen solos. Los enlaces que vienen
# de zona-leros llevan origen "zonaleros" y usan su contraseña automática.
_PENDIENTES_DESCOMPRESION = {}   # ruta -> (trabajo, contraseña)


def _marcar_descompresion(trabajo, estado, msg=""):
    try:
        trabajo.descompresion_estado = estado
        trabajo.descompresion_msg = msg
    except Exception:
        pass


def _en_descarga(nombre):
    """True si algún trabajo activo va a producir ese nombre de archivo
    (una parte del conjunto que todavía está bajando)."""
    for t in GESTOR.trabajos.values():
        n = getattr(t, "nombre", None) or getattr(t, "_nombre", None)
        if n == nombre and t.estado in ("descargando", "uniendo",
                                        "esperando", "en cola"):
            return True
    return False


def _faltan_partes(ruta):
    """True si falta una parte del conjunto multiparte que aún se está
    descargando (entonces conviene esperar antes de extraer)."""
    import re as _re
    nombre = os.path.basename(ruta)
    carpeta = os.path.dirname(ruta)
    # el número de parte es el ÚLTIMO antes de la extensión
    m = _re.search(r"\.part(\d+)\.[^.]*$", nombre)
    if m:
        base = _re.sub(r"\.part\d+\.[^.]*$", "", nombre)
        n = int(m.group(1)) + 1
        while n <= 99:
            parte = "%s.part%d%s" % (base, n, os.path.splitext(nombre)[1])
            if not os.path.exists(os.path.join(carpeta, parte)):
                return _en_descarga(parte)
            n += 1
        return False
    m = _re.search(r"\.r(\d{2,3})$", nombre)
    if m:
        base = _re.sub(r"\.r\d{2,3}$", "", nombre)
        n = int(m.group(1)) + 1
        for suf in ("%02d" % n, "%03d" % n):
            parte = "%s.r%s" % (base, suf)
            if not os.path.exists(os.path.join(carpeta, parte)):
                return _en_descarga(parte)
    return False


def _descomprimir_si_aplica(trabajo):
    """Encola la extracción del archivo si aplica (toggle activo, archivo
    comprimido, no es parte de continuación). Corre en segundo plano."""
    if not DESCOMPRESION_AUTO:
        return
    ruta = getattr(trabajo, "_archivo_final", None)
    carpeta = getattr(trabajo, "carpeta", None)
    nombre = getattr(trabajo, "nombre", None) or getattr(trabajo, "_nombre", None)
    if not ruta or not os.path.exists(ruta):
        # _organizar pudo mover el archivo a su subcarpeta por tipo
        r = None
        if carpeta and nombre:
            r = os.path.join(carpeta, nombre)
            if not os.path.exists(r):
                r = os.path.join(carpeta, _categoria(nombre), nombre)
        if not r or not os.path.exists(r):
            return
        ruta = r
    if not ruta or not os.path.exists(ruta):
        return
    nombre = os.path.basename(ruta)
    if not descomprimir.es_comprimido(nombre):
        return
    if descomprimir.es_parte_secundaria(nombre):
        return
    # contraseña automática según el sitio de origen
    if getattr(trabajo, "origen", "") == "zonaleros":
        password = "zonaleros"
    elif getattr(trabajo, "origen", "") == "pivigames":
        password = "pivigames"
    else:
        password = PASSWORD_DESCOMPRESION
    if _faltan_partes(ruta):
        # el resto del conjunto sigue bajando: se reintenta al completar
        _PENDIENTES_DESCOMPRESION[ruta] = (trabajo, password)
        _marcar_descompresion(trabajo, "esperando",
                              "Esperando el resto de partes…")
        return
    threading.Thread(target=_ejecutar_descompresion,
                     args=(trabajo, ruta, password), daemon=True).start()


def _ejecutar_descompresion(trabajo, ruta, password):
    _marcar_descompresion(trabajo, "extrayendo", "")
    try:
        ok, msg = descomprimir.descomprimir(ruta, password)
        _marcar_descompresion(trabajo, "ok" if ok else "error", msg)
    except Exception as e:
        _marcar_descompresion(trabajo, "error", str(e)[:200])
    finally:
        _PENDIENTES_DESCOMPRESION.pop(ruta, None)


def _reintentar_descompresiones():
    """Cuando completa otra parte del conjunto, reintenta las extracciones
    que estaban esperando por el resto de partes."""
    for ruta, (trabajo, password) in list(_PENDIENTES_DESCOMPRESION.items()):
        if os.path.exists(ruta) and not _faltan_partes(ruta):
            _PENDIENTES_DESCOMPRESION.pop(ruta, None)
            threading.Thread(target=_ejecutar_descompresion,
                             args=(trabajo, ruta, password),
                             daemon=True).start()


def _al_completar(trabajo):
    _reset_reintentos(trabajo)   # terminó bien: el contador vuelve a cero
    try:
        _organizar(trabajo)
    except Exception:
        pass
    try:
        _descomprimir_si_aplica(trabajo)
        _reintentar_descompresiones()
    except Exception:
        pass
    # una descarga terminó (o falló): entra la siguiente de la cola
    _procesar_cola()
    _guardar_cola()    # el estado cambió: persistir


motor.on_completada = _al_completar
mega.on_completada = _al_completar
torrents.on_completada = _al_completar
motor.on_error = _al_error
mega.on_error = _al_error
torrents.on_error = _al_error


class Manejador(BaseHTTPRequestHandler):
    def _token_ok(self):
        t = self.headers.get("X-MiDescargador-Token") or ""
        return bool(t) and hmac.compare_digest(t, TOKEN_API)

    def _host_local(self):
        h = (self.headers.get("Host") or "").lower()
        return h.split(":", 1)[0].strip().strip("[]") in (
            "127.0.0.1", "localhost", "::1")

    def _json(self, datos, codigo=200):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _leer_cuerpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        # sin CORS: un preflight de una web remota no recibe cabeceras de
        # permiso y el navegador bloquea la petición real
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        ruta = urllib.parse.urlparse(self.path).path
        # API protegida por token. /api/token es el bootstrap de la extensión
        # y solo responde a hosts locales (bloquea DNS rebinding: una web que
        # re-resuelva su dominio a 127.0.0.1 llega con Host propio).
        if ruta.startswith("/api/"):
            if ruta == "/api/token":
                if not self._host_local():
                    self._json({"error": "no autorizado"}, 401)
                else:
                    self._json({"token": TOKEN_API})
                return
            if not ruta.startswith("/api/media/") and not self._token_ok():
                self._json({"error": "no autorizado"}, 401)
                return
        if ruta in ("/", "/index.html"):
            self._servir_ui()
        elif ruta.startswith("/static/") or ruta in ("/logo.svg", "/favicon.ico", "/favicon.svg"):
            base = _base_dir()
            if ruta.startswith("/static/"):
                fpath = os.path.abspath(
                    os.path.join(base, "static", ruta[len("/static/"):]))
                # anti path traversal: el archivo debe quedar DENTRO de
                # static/ — un /static/../config.json no debe servirse
                raiz = os.path.abspath(os.path.join(base, "static"))
                if fpath != raiz and not fpath.startswith(raiz + os.sep):
                    fpath = None
            else:
                fpath = os.path.join(base, "static",
                                     os.path.basename(ruta))
            if fpath and os.path.isfile(fpath):
                ctype = "image/svg+xml" if fpath.endswith(".svg") else "image/x-icon" if fpath.endswith(".ico") else "application/octet-stream"
                try:
                    with open(fpath, "rb") as f:
                        cuerpo = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(cuerpo)))
                    self.end_headers()
                    self.wfile.write(cuerpo)
                    return
                except OSError:
                    pass
            self._json({"error": "no encontrado"}, 404)
        elif ruta == "/api/estado":
            self._json(GESTOR.estado())
        elif ruta == "/api/version":
            self._json({"nombre": "MiDescargador", "version": _version_app()})
        elif ruta == "/api/log":
            self._json({"lineas": _leer_log(200), "ruta": LOG_RUTA})
        elif ruta == "/api/config":
            self._json({"organizar": ORGANIZAR_POR_TIPO,
                        "max_simultaneas": MAX_SIMULTANEAS,
                        "descompresion_auto": DESCOMPRESION_AUTO,
                        "password_descompresion": PASSWORD_DESCOMPRESION,
                        "limite_kbps": LIMITE_VELOCIDAD_KBPS})
        elif ruta == "/api/hosters":
            self._json({"hosters": hosters.hosters_soportados()})
        elif ruta == "/api/lote":
            self._json({"pendientes": len(_en_cola()),
                        "descargando": _hay_activa()})
        elif ruta == "/api/sesion":
            self._json({"youtube": cuenta.estado("youtube"),
                        "tiktok": cuenta.estado("tiktok")})
        elif ruta == "/api/sesion/perfiles":
            # perfiles de Chrome donde hay una sesión de YouTube detectada
            # (por nombres/dominios de cookies): avisa al usuario dónde
            # exportar con la extensión sin volver a iniciar sesión
            self._json({"perfiles": cuenta.perfiles_con_sesion()})
        elif ruta == "/api/enlaces/estado":
            qs = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            tid = (qs.get("tarea") or [""])[0]
            datos, codigo = _estado_enlaces_tarea(tid)
            self._json(datos, codigo)
        elif ruta.startswith("/api/media/"):
            tid = urllib.parse.unquote(ruta[len("/api/media/"):])
            self._servir_media(tid)
        else:
            self._json({"error": "no encontrado"}, 404)

    def do_HEAD(self):
        """El reproductor del navegador puede pedir cabeceras con HEAD antes
        de reproducir; respondemos igual que GET pero sin cuerpo."""
        ruta = urllib.parse.urlparse(self.path).path
        if ruta.startswith("/api/media/"):
            tid = urllib.parse.unquote(ruta[len("/api/media/"):])
            self._servir_media(tid)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        ruta = urllib.parse.urlparse(self.path).path
        if not self._token_ok():
            self._json({"error": "no autorizado"}, 401)
            return
        try:
            self._do_post_inner(ruta)
        except Exception as e:
            try:
                self._json({"error": str(e)[:200]}, 500)
            except Exception:
                pass

    def _do_post_inner(self, ruta):
        datos = self._leer_cuerpo()
        if ruta == "/api/descargar":
            url = (datos.get("url") or "").strip()
            if not url:
                self._json({"error": "falta url"}, 400)
                return
            try:
                seg = int(datos.get("segmentos") or 8)
            except (TypeError, ValueError):
                seg = 8
            try:
                lim = max(0, int(datos.get("limite_kbps") or 0))
            except (TypeError, ValueError):
                lim = 0
            tid = GESTOR.agregar(url, seg, datos.get("carpeta"),
                                 datos.get("formato"),
                                 origen=datos.get("origen"),
                                 limite_kbps=lim)
            self._json({"id": tid})
        elif ruta == "/api/formatos":
            url = (datos.get("url") or "").strip()
            if not url:
                self._json({"error": "falta url"}, 400)
                return
            try:
                _log_servidor("/api/formatos url=%s" % url[:200])
            except Exception:
                pass
            res = _formatos(url)
            try:
                _log_servidor("/api/formatos res=%d formatos" % (len(res) if isinstance(res, list) else 0))
            except Exception:
                pass
            if isinstance(res, dict) and res.get("error"):
                self._json(res, 422)
            else:
                self._json({"formatos": res})
        elif ruta == "/api/enlaces":
            url = (datos.get("url") or "").strip()
            if not url:
                self._json({"error": "falta url"}, 400)
                return
            self._json(_enlaces_lanzar(url))
        elif ruta == "/api/verificar-enlaces":
            urls = datos.get("urls") or []
            if isinstance(urls, str):
                urls = [u.strip() for u in urls.splitlines() if u.strip()]
            urls = [u for u in urls
                    if isinstance(u, str) and u.startswith(("http://", "https://"))]
            if not urls:
                self._json({"error": "no hay enlaces para verificar"}, 400)
                return
            self._json({"resultados": _verificar_enlaces(urls)})
        elif ruta == "/api/lote":
            urls = datos.get("urls") or []
            if isinstance(urls, str):
                urls = [u.strip() for u in urls.splitlines() if u.strip()]
            urls = [u.strip() for u in urls if u and u.strip().startswith(("http://", "https://"))]
            if not urls:
                self._json({"error": "no hay enlaces válidos"}, 400)
                return
            try:
                seg = int(datos.get("segmentos") or 8)
            except (TypeError, ValueError):
                seg = 8
            try:
                lim_lote = max(0, int(datos.get("limite_kbps") or 0))
            except (TypeError, ValueError):
                lim_lote = 0
            n = _encolar_lote(urls, seg, origen=datos.get("origen"),
                              limite_kbps=lim_lote)
            self._json({"ok": True, "encolados": n,
                        "pendientes": len(_en_cola()),
                        "descargando": _hay_activa()})
        elif ruta == "/api/pausar":
            self._json({"ok": GESTOR.accion(datos.get("id"), "pausar")})
        elif ruta == "/api/reanudar":
            self._json({"ok": GESTOR.accion(datos.get("id"), "reanudar")})
        elif ruta == "/api/reintentar":
            self._json({"ok": GESTOR.accion(datos.get("id"), "reintentar")})
        elif ruta == "/api/cancelar":
            self._json({"ok": GESTOR.accion(datos.get("id"), "cancelar")})
        elif ruta == "/api/borrar":
            self._json({"ok": GESTOR.borrar(datos.get("id"))})
        elif ruta == "/api/abrir":
            self._json({"ok": GESTOR.abrir(datos.get("id"))})
        elif ruta == "/api/carpeta":
            # acceso rápido desde el popup de la extensión: abre la carpeta
            # de descargas por defecto en el explorador de archivos
            try:
                os.makedirs(CARPETA_DEFECTO, exist_ok=True)
            except OSError:
                pass
            ok, err = _abrir_carpeta(CARPETA_DEFECTO)
            if ok:
                self._json({"ok": True, "ruta": CARPETA_DEFECTO})
            else:
                self._json({"ok": False, "error": err}, 404)
        elif ruta == "/api/carpeta-extension":
            # abre la carpeta de la extensión de Chrome (para cargarla con
            # «Cargar descomprimida» en chrome://extensions)
            dir_ext = _dir_extension()
            if not dir_ext:
                self._json({"ok": False,
                            "error": "La carpeta de la extensión no está disponible en esta instalación."}, 404)
                return
            ok, err = _abrir_carpeta(dir_ext)
            if ok:
                self._json({"ok": True, "ruta": dir_ext})
            else:
                self._json({"ok": False, "error": err}, 404)
        elif ruta == "/api/reproducir":
            ok, err = GESTOR.reproducir(datos.get("id"))
            if ok:
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": err}, 404)
        elif ruta == "/api/extraer-audio":
            ok, res = GESTOR.extraer_audio(datos.get("id"))
            if ok:
                self._json({"ok": True, "ruta": res})
            else:
                self._json({"ok": False, "error": res}, 404)
        elif ruta == "/api/sesion/iniciar":
            # corre en segundo plano: la ventana de Chrome queda esperando
            # a que el usuario inicie sesión; el panel consulta /api/sesion
            global _HILO_SESION
            plataforma = datos.get("plataforma") or "youtube"
            if plataforma not in cuenta.plataformas():
                self._json({"ok": False,
                            "error": "plataforma desconocida: "
                                      + str(plataforma)}, 400)
            elif not _HILO_SESION or not _HILO_SESION.is_alive():
                def _trabajo():
                    try:
                        cuenta.iniciar_sesion(plataforma)
                    except Exception:
                        pass
                _HILO_SESION = threading.Thread(target=_trabajo, daemon=True)
                _HILO_SESION.start()
            self._json({"ok": True})
        elif ruta == "/api/sesion/exportar":
            # cookies del perfil donde corre la extensión (chrome.cookies):
            # Chrome las descifra por nosotros (incluidas las v20/ABE), así
            # que la sesión se regenera sin volver a iniciar sesión
            plataforma = datos.get("plataforma") or "youtube"
            if plataforma not in cuenta.plataformas():
                self._json({"ok": False,
                            "error": "plataforma desconocida: "
                                      + str(plataforma)}, 400)
            else:
                self._json(cuenta.exportar(datos.get("cookies"), plataforma))
        elif ruta == "/api/sesion/borrar":
            plataforma = datos.get("plataforma") or "youtube"
            if plataforma not in cuenta.plataformas():
                self._json({"ok": False,
                            "error": "plataforma desconocida: "
                                      + str(plataforma)}, 400)
            else:
                self._json(cuenta.borrar(plataforma))
        elif ruta == "/api/log/limpiar":
            try:
                with open(LOG_RUTA, "w", encoding="utf-8") as f:
                    f.write("")
                self._json({"ok": True})
            except OSError as e:
                self._json({"ok": False, "error": str(e)}, 500)
        elif ruta == "/api/config":
            global ORGANIZAR_POR_TIPO, MAX_SIMULTANEAS
            global DESCOMPRESION_AUTO, PASSWORD_DESCOMPRESION
            global LIMITE_VELOCIDAD_KBPS
            if "organizar" in datos:
                ORGANIZAR_POR_TIPO = bool(datos["organizar"])
            if "max_simultaneas" in datos:
                try:
                    MAX_SIMULTANEAS = max(1, min(int(datos["max_simultaneas"]), 10))
                except (TypeError, ValueError):
                    pass
                _procesar_cola()   # si subimos el límite, arranca más
            if "descompresion_auto" in datos:
                DESCOMPRESION_AUTO = bool(datos["descompresion_auto"])
            if "password_descompresion" in datos:
                PASSWORD_DESCOMPRESION = str(datos["password_descompresion"] or "")
            if "limite_kbps" in datos:
                try:
                    LIMITE_VELOCIDAD_KBPS = max(0, int(datos["limite_kbps"]))
                    _aplicar_limite_global()   # aplica en vivo al motor/Mega
                except (TypeError, ValueError):
                    pass
            _guardar_config()
            self._json({"ok": True, "organizar": ORGANIZAR_POR_TIPO,
                        "max_simultaneas": MAX_SIMULTANEAS,
                        "descompresion_auto": DESCOMPRESION_AUTO,
                        "password_descompresion": PASSWORD_DESCOMPRESION,
                        "limite_kbps": LIMITE_VELOCIDAD_KBPS})
        else:
            self._json({"error": "no encontrado"}, 404)

    def _servir_ui(self):
        base = _base_dir()
        ruta = os.path.join(base, "static", "index.html")
        try:
            with open(ruta, "rb") as f:
                cuerpo = f.read()
        except OSError:
            self._json({"error": "interfaz no encontrada"}, 404)
            return
        # inyecta el token en el panel (mismo origen): el JS de index.html lo
        # adjunta a sus peticiones /api sin exponerlo a webs remotas
        cuerpo = cuerpo.replace(b"__MDM_TOKEN_PLACEHOLDER__", TOKEN_API.encode())
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _servir_media(self, tid):
        """Sirve el archivo de una descarga terminada con soporte de Range,
        para que el <video> del panel pueda reproducir y hacer seek."""
        ruta = GESTOR.ruta_archivo(tid)
        if not ruta:
            self._json({"error": "archivo no encontrado"}, 404)
            return
        try:
            tam = os.path.getsize(ruta)
        except OSError:
            self._json({"error": "archivo no accesible"}, 404)
            return
        if tam <= 0:
            self._json({"error": "archivo vacío"}, 404)
            return

        inicio, fin, es_parcial = 0, tam - 1, False
        rango = self.headers.get("Range")
        if rango:
            m = re.match(r"bytes=(\d*)-(\d*)$", rango.strip())
            if not m or not (m.group(1) or m.group(2)):
                # rango malformado o no soportado: 416 con el tamaño total
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % tam)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            a, b = m.group(1), m.group(2)
            if a:
                inicio = int(a)
                fin = int(b) if b else tam - 1
            else:
                # rango de sufijo (bytes=-N): últimos N bytes
                n = int(b)
                inicio = max(tam - n, 0)
                fin = tam - 1
            if inicio < 0 or inicio > fin or inicio >= tam:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % tam)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            fin = min(fin, tam - 1)
            es_parcial = True

        longitud = fin - inicio + 1
        self.send_response(206 if es_parcial else 200)
        self.send_header("Content-Type", _tipo_media(ruta))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(longitud))
        if es_parcial:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (inicio, fin, tam))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(ruta, "rb") as f:
                f.seek(inicio)
                resto = longitud
                while resto > 0:
                    bloque = f.read(min(262144, resto))
                    if not bloque:
                        break
                    self.wfile.write(bloque)
                    resto -= len(bloque)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass   # el cliente cerró la conexión (seek, cerrar modal…)

    def log_message(self, fmt, *args):
        pass


def _detener_trabajos_al_salir():
    """Al salir el backend (cierre o crash), cancela todos los trabajos
    activos para que sus subprocesos (aria2c, yt-dlp) no queden huérfanos
    descargando en segundo plano."""
    for t in list(GESTOR.trabajos.values()):
        try:
            t.cancelar()
        except Exception:
            pass


import atexit
atexit.register(_detener_trabajos_al_salir)


def main():
    os.makedirs(CARPETA_DEFECTO, exist_ok=True)
    _cargar_formatos_cache()   # calidades en disco sobreviven al reinicio
    _cargar_tamanos_cache()     # y los tamaños simulados (7 días)
    motor.limpiar_restos()   # quita .partes viejas y fragmentos huérfanos

    def _depurar_periodico():
        while True:
            time.sleep(600)      # cada 10 minutos
            try:
                motor.depurar()
            except Exception:
                pass

    threading.Thread(target=_depurar_periodico, daemon=True).start()

    _cargar_config()   # restaura toggle/contraseña guardados en config.json
    _restaurar_cola()  # recarga la cola persistida (reanuda lo vivo)
    threading.Thread(target=_hilo_reintentos, daemon=True).start()

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PUERTO), Manejador)
    except OSError as e:
        _log_servidor("FALLO al arrancar (¿puerto %d ocupado?): %s"
                      % (PUERTO, e))
        raise
    _log_servidor("Servidor en http://127.0.0.1:%d (carpeta: %s)"
                  % (PUERTO, CARPETA_DEFECTO))
    print(f"MiDescargador en http://127.0.0.1:{PUERTO}")
    print(f"Carpeta de descargas: {CARPETA_DEFECTO}")
    print("Presiona Ctrl+C para detener.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _log_servidor("Servidor detenido.")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
