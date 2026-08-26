# -*- coding: utf-8 -*-
"""Extractor de enlaces de descarga de zona-leros.com - MODO HÍBRIDO.

Mismo flujo CDP + Turnstile que zonaleros.py, pero elige automáticamente el
mecanismo de perfil según el estado de Chrome en el momento de lanzar:

  - Chrome CERRADO  -> junction al perfil REAL: cookies del usuario => máxima
    confianza ante Cloudflare (la vía clásica de zonaleros.py).
  - Chrome ABIERTO  -> COPIA temporal del perfil: no molesta al usuario ni
    toca el perfil real; el reto se resuelve desde cero en la copia.

En ambos modos al terminar solo se mata la instancia lanzada (por PID y por
su user-data-dir) y el Chrome del usuario queda intacto. El perfil real solo
se toca en modo junction, con respaldo/restauración de cookies por si un
cierre forzado lo daña.

Para probar:  python zonaleros_copia.py [url]
"""
import os
import re
import json
import time
import shutil
import subprocess
import tempfile
import urllib.request

try:
    import websocket as _ws
except ImportError:
    _ws = None

_PUERTO_CDP = 9223
_RUTA_CHROME = None

_PERFIL_ACTIVO = None   # user-data-dir en uso (junction real o copia temporal)
_PID_ACTIVO = None      # PID del Chrome que lanzamos (solo ese se mata)
_MODO_ACTIVO = None     # "junction" (Chrome cerrado) o "copia" (Chrome abierto)
_LOG = None             # hook opcional: servidor.py lo conecta a errores.log


def _log(msg):
    """Registra un evento del modo híbrido (qué rama se usó y por qué) si el
    hook está conectado. Sin hook no hace nada (p. ej. en el CLI de prueba)."""
    if _LOG is not None:
        try:
            _LOG(msg)
        except Exception:
            pass


def _ruta_chrome():
    global _RUTA_CHROME
    if _RUTA_CHROME:
        return _RUTA_CHROME
    candidatos = (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    )
    for p in candidatos:
        if os.path.exists(p):
            _RUTA_CHROME = p
            return p
    return None


def _chrome_corriendo():
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
            capture_output=True, text=True, errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "chrome.exe" in r.stdout and "No tasks" not in r.stdout
    except Exception:
        return False


def _crear_junction():
    """Crea (si falta) un junction temporal al perfil real de Chrome.
    Devuelve la ruta o None si no se pudo."""
    try:
        import _winapi
        origen = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
        dst = os.path.join(tempfile.gettempdir(), "midesc-chrome-perfil")
        if not os.path.exists(origen):
            return None
        if not os.path.lexists(dst):
            _winapi.CreateJunction(origen, dst)
        if os.path.exists(os.path.join(dst, "Local State")):
            return dst
    except Exception:
        pass
    return None


def _copiar_archivo_tolerante(origen, destino, reintentos=3):
    """Copia un archivo tolerando bloqueos temporales (Chrome abierto puede
    tener el archivo en uso). Devuelve True si se copió, False si no."""
    for i in range(reintentos + 1):
        try:
            shutil.copy2(origen, destino)
            return True
        except PermissionError:
            if i < reintentos:
                time.sleep(1)
            else:
                _log("[copia] aviso: %s está bloqueado, se omite" %
                     os.path.basename(origen))
        except OSError:
            return False
    return False


def _crear_copia():
    """Copia lo mínimo necesario del perfil real de Chrome a una carpeta
    temporal ÚNICA: Local State (contiene la clave maestra que permite a
    Chrome descifrar las cookies), las Preferences de Default (fingerprint
    ligero: idioma, etc.) y la base de cookies de Default (Network/Cookies).

    Funciona con Chrome ABIERTO: los archivos bloqueados se reintentan y,
    si no se pueden copiar, se omiten (el reto de Cloudflare decidirá si la
    confianza alcanza). Devuelve la ruta de la copia o None.
    """
    origen = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
    if not os.path.exists(origen):
        return None
    dst = os.path.join(tempfile.gettempdir(),
                       "midesc-chrome-copia-%d" % int(time.time() * 1000))
    try:
        os.makedirs(os.path.join(dst, "Default", "Network"))
        _copiar_archivo_tolerante(
            os.path.join(origen, "Local State"),
            os.path.join(dst, "Local State"))
        _copiar_archivo_tolerante(
            os.path.join(origen, "Default", "Preferences"),
            os.path.join(dst, "Default", "Preferences"))
        # la base de cookies puede estar bloqueada (Chrome abierto): solo se
        # copian los sufijos (-wal/-journal) si la base principal entró
        ok_cookies = _copiar_archivo_tolerante(
            os.path.join(origen, "Default", "Network", "Cookies"),
            os.path.join(dst, "Default", "Network", "Cookies"))
        if ok_cookies:
            for suf in ("-journal", "-wal"):
                o = os.path.join(origen, "Default", "Network", "Cookies" + suf)
                d = os.path.join(dst, "Default", "Network", "Cookies" + suf)
                if os.path.exists(o):
                    _copiar_archivo_tolerante(o, d)
        else:
            _log("[copia] sin cookies del usuario (Chrome abierto): "
                 "el reto se resolverá desde cero en la copia")
        # marca de primer arranque para que Chrome no abra el asistente
        open(os.path.join(dst, "First Run"), "w").close()
        if os.path.exists(os.path.join(dst, "Local State")):
            return dst
    except Exception:
        pass
    try:
        shutil.rmtree(dst, ignore_errors=True)
    except Exception:
        pass
    return None


def _modo():
    """Modo de lanzamiento según el estado de Chrome:
    - 'junction' si Chrome está cerrado (perfil real, cookies => confianza),
    - 'copia' si Chrome está abierto (perfil copiado, no molesta al usuario)."""
    return "junction" if not _chrome_corriendo() else "copia"


def _lanzar(url, tiempo_max=50):
    """Lanza Chrome con el mecanismo que corresponda (híbrido) y devuelve
    (ws_url_cdp, error). NO exige Chrome cerrado: si está abierto usa una
    copia temporal del perfil; si está cerrado, el perfil real vía junction."""
    global _PERFIL_ACTIVO, _PID_ACTIVO, _MODO_ACTIVO
    if _ws is None:
        return None, "falta la librería websocket-client en el venv"
    ruta = _ruta_chrome()
    if not ruta:
        return None, "no se encontró Chrome instalado"
    modo = _modo()
    abierto = _chrome_corriendo()
    _log("extracción de enlaces: modo=%s (Chrome %s), url=%s" % (
        modo, "abierto" if abierto else "cerrado", url))
    if modo == "junction":
        # Chrome cerrado: junction al perfil REAL (cookies del usuario).
        # El perfil se puede tocar con un cierre forzado: respaldo previo.
        # Protección v20: el junction sobre un perfil con App-Bound lo
        # destruye (Chrome no descifra v20 fuera de la ruta real). Con
        # cookies v20 se cae a la COPIA temporal en vez de bloquear: es
        # segura (el perfil real nunca se toca) y el reto de Cloudflare se
        # resuelve desde cero (las cookies v10 sí se descifran con la clave
        # de Local State que se copia; las v20 de la copia simplemente se
        # descartan, sin dañar el perfil real).
        if _perfil_usa_abe("Default"):
            _log("modo junction -> copia: perfil Default con cookies "
                 "App-Bound (v20), el junction las destruiría")
            modo = "copia"
    if modo == "junction":
        perfil = _crear_junction()
        if not perfil:
            return None, "no se pudo preparar el perfil temporal de Chrome"
        _respaldar_cookies()
    else:
        # Chrome abierto (o perfil con cookies v20): copia mínima del
        # perfil (el real no se toca).
        perfil = _crear_copia()
        if not perfil:
            return None, "no se pudo preparar la copia del perfil de Chrome"
    cmd = [
        ruta,
        "--user-data-dir=" + perfil,
        "--profile-directory=Default",
        "--remote-debugging-port=%d" % _PUERTO_CDP,
        "--remote-allow-origins=*",
        "--window-position=-32000,-32000",   # fuera de pantalla
        "--disable-gpu",                      # evita caídas del proceso GPU
        "--no-first-run",
        "--disable-background-networking",
        url,
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _PERFIL_ACTIVO = perfil
    _MODO_ACTIVO = modo
    try:
        proc = subprocess.Popen(cmd, creationflags=flags)
    except Exception:
        _PERFIL_ACTIVO = None
        _MODO_ACTIVO = None
        return None, "no se pudo lanzar Chrome"
    _PID_ACTIVO = proc.pid
    fin = time.time() + tiempo_max
    while time.time() < fin:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % _PUERTO_CDP,
                    timeout=2) as r:
                for t in json.loads(r.read().decode("utf-8", "replace")):
                    if t.get("type") == "page":
                        return t["webSocketDebuggerUrl"], None
        except Exception:
            pass
        time.sleep(1)
    return None, "Chrome no respondió a tiempo"


def _nuestro_chrome_vivo():
    """True si la instancia que lanzamos sigue viva (no el Chrome del
    usuario, que puede estar abierto aparte)."""
    pid = _PID_ACTIVO
    if not pid:
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % pid],
            capture_output=True, text=True, errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return str(pid) in r.stdout and "No tasks" not in r.stdout
    except Exception:
        return False


def _finalizar():
    """Cierra SOLO la instancia de Chrome que lanzamos (por PID y, de
    respaldo, por su user-data-dir en la línea de comandos) y deja el
    entorno limpio según el modo usado:

    - 'copia': borra la copia temporal del perfil (el real nunca se tocó).
    - 'junction': restaura el respaldo de cookies si el perfil real quedó
      dañado por el cierre forzado.

    NUNCA toca el Chrome del usuario."""
    global _PERFIL_ACTIVO, _PID_ACTIVO, _MODO_ACTIVO
    pid = _PID_ACTIVO
    if pid:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    perfil = _PERFIL_ACTIVO
    if perfil:
        try:
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                  "Where-Object { $_.CommandLine -like '*%s*' } | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                  "-ErrorAction SilentlyContinue }" % perfil)
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
        if _MODO_ACTIVO == "copia":
            try:
                shutil.rmtree(perfil, ignore_errors=True)
            except Exception:
                pass
        elif _MODO_ACTIVO == "junction":
            _restaurar_cookies_si_danadas()
    _PID_ACTIVO = None
    _PERFIL_ACTIVO = None
    _MODO_ACTIVO = None


# ---------------- red de seguridad del perfil ----------------
# Lanzar Chrome con el perfil real es lo que permite pasar los retos, pero
# un cierre forzado puede dejar la base de cookies tocada. Antes de lanzar
# se respaldan Local State y las cookies de TODOS los perfiles y, si al
# terminar alguno quedó con muchas menos cookies, se restaura su respaldo.
_RESPALDO_DIR = os.path.join(tempfile.gettempdir(), "midesc-respaldo-perfil")


# ---------------- protección App-Bound (cookies v20) ----------------
# Chrome 2025+ migra sus cookies al formato v20/App-Bound: abrir un perfil
# así con el JUNCTION es destructivo (Chrome no descifra v20 fuera de su ruta
# real y elimina las cookies al cerrar). El prefijo v10/v20 del valor
# encriptado está en claro en la base, así que se detecta ANTES de lanzar.
_MENSAJE_ABE = (
    "El perfil «Default» de Chrome usa cookies protegidas App-Bound (v20) "
    "de Chrome 2025+: abrirlo con el junction las destruiría (Chrome no "
    "puede descifrarlas fuera de su ruta real). Para tu sesión de YouTube, "
    "instalá la extensión en ese perfil y pulsá «Exportar sesión 🔑» en "
    "vez de iniciar sesión aquí.")


def _perfil_usa_abe(nombre="Default"):
    """True si el perfil tiene alguna cookie encriptada con App-Bound (v20)."""
    db = os.path.join(os.path.expandvars(
        r"%LOCALAPPDATA%\Google\Chrome\User Data"),
        nombre, "Network", "Cookies")
    if not os.path.isfile(db):
        return False
    import shutil, sqlite3, tempfile
    tmp = tempfile.mktemp(prefix="md_abe_", suffix=".db")
    try:
        shutil.copy2(db, tmp)
        con = sqlite3.connect(tmp)
        row = con.execute(
            "SELECT count(*) FROM cookies "
            "WHERE substr(encrypted_value, 1, 3) = X'763230'").fetchone()
        con.close()
        return bool(row and row[0] > 0)
    except Exception:
        return False
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _perfiles_chrome_con_cookies():
    """Perfiles de Chrome (Default y Profile N) que tienen base de cookies."""
    base = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
    if not os.path.isdir(base):
        return []
    salida = []
    for nombre in os.listdir(base):
        if nombre in ("Guest Profile", "System Profile"):
            continue
        if os.path.isfile(os.path.join(base, nombre, "Network", "Cookies")):
            salida.append(nombre)
    return salida


def _respaldar_cookies():
    """Respalda Local State y las cookies de TODOS los perfiles. No pisa un
    respaldo anterior que tenga MÁS cookies que la base actual."""
    try:
        import shutil
        base = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
        os.makedirs(_RESPALDO_DIR, exist_ok=True)
        ori = os.path.join(base, "Local State")
        if os.path.exists(ori):
            shutil.copy2(ori, os.path.join(_RESPALDO_DIR, "Local State"))
        for nombre in _perfiles_chrome_con_cookies():
            ori = os.path.join(base, nombre, "Network", "Cookies")
            dst = os.path.join(_RESPALDO_DIR,
                               nombre.replace(os.sep, "_") + "_Network_Cookies")
            if os.path.exists(dst):
                actual = _contar_cookies(ori)
                previo = _contar_cookies(dst)
                if (actual is not None and previo is not None
                        and actual < previo):
                    continue   # el respaldo previo es mejor: no pisarlo
            shutil.copy2(ori, dst)
        return True
    except Exception:
        return False


def _contar_cookies(ruta_db):
    import shutil
    import sqlite3
    tmp = ruta_db + ".cuenta.tmp"
    try:
        shutil.copy2(ruta_db, tmp)
        con = sqlite3.connect(tmp)
        n = con.execute("select count(*) from cookies").fetchone()[0]
        con.close()
        try:
            os.remove(tmp)
        except OSError:
            pass
        return n
    except Exception:
        return None


def _restaurar_cookies_si_danadas():
    """Si algún perfil perdió más de la mitad de sus cookies durante la
    extracción, restaura el respaldo tomado antes de lanzar Chrome."""
    try:
        import shutil
        base = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
        restaurado = False
        for nombre in _perfiles_chrome_con_cookies():
            db_vivo = os.path.join(base, nombre, "Network", "Cookies")
            db_resp = os.path.join(
                _RESPALDO_DIR, nombre.replace(os.sep, "_") + "_Network_Cookies")
            if not os.path.exists(db_resp) or not os.path.exists(db_vivo):
                continue
            vivas = _contar_cookies(db_vivo)
            respaldo = _contar_cookies(db_resp)
            if (vivas is not None and respaldo is not None
                    and vivas < respaldo * 0.5):
                shutil.copy2(db_resp, db_vivo)
                restaurado = True
        if restaurado:
            ls_res = os.path.join(_RESPALDO_DIR, "Local State")
            if os.path.exists(ls_res):
                shutil.copy2(ls_res, os.path.join(base, "Local State"))
        return restaurado
    except Exception:
        return False


    except Exception:
        return False


class _Cdp:
    """Cliente mínimo del protocolo de depuración de Chrome, con
    reconexión automática si Chrome reinicia un proceso (pasa a veces:
    el renderer se cae y el navegador lo relanza)."""

    def __init__(self, ws_url):
        self._ws_url = ws_url
        self._ws = _ws.create_connection(ws_url, timeout=30)
        self._id = 0

    def _reconectar(self):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % _PUERTO_CDP,
                    timeout=3) as r:
                for t in json.loads(r.read().decode("utf-8", "replace")):
                    if t.get("type") == "page":
                        self._ws_url = t["webSocketDebuggerUrl"]
                        break
            self._ws = _ws.create_connection(self._ws_url, timeout=30)
            return True
        except Exception:
            return False

    def _cmd(self, metodo, params=None):
        """Envía cualquier comando CDP y espera su respuesta por id."""
        self._id += 1
        i = self._id
        self._ws.send(json.dumps({"id": i, "method": metodo,
                                  "params": params or {}}))
        while True:
            r = json.loads(self._ws.recv())
            if r.get("id") == i:
                return r

    def _eval_una(self, expr):
        self._id += 1
        i = self._id
        self._ws.send(json.dumps({
            "id": i, "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True}}))
        while True:
            r = json.loads(self._ws.recv())
            if r.get("id") == i:
                return (r.get("result") or {}).get("result", {}).get("value")

    def clic_widget_turnstile(self):
        """Turnstile a veces exige clic real en su widget (checkbox dentro
        del iframe de Cloudflare): envía un clic de ratón real por CDP en el
        centro del iframe. Devuelve True si encontró y pulsó el widget."""
        pos = self.eval('''(() => {
            const f = document.querySelector('iframe[src*="challenges.cloudflare.com"], iframe[title*="challenge"], iframe[title*="robot"], iframe[src*="turnstile"]');
            if (!f) return null;
            const r = f.getBoundingClientRect();
            if (r.width < 5 || r.height < 5) return null;
            return {x: r.left + r.width / 2, y: r.top + r.height / 2};
        })()''')
        if not pos or pos.get("x") is None:
            return False
        x, y = int(pos["x"]), int(pos["y"])
        for tipo in ("mouseMoved", "mousePressed", "mouseReleased"):
            params = {"type": tipo, "x": x, "y": y, "button": "left",
                      "clickCount": 1}
            if tipo == "mouseMoved":
                params.pop("button")
                params.pop("clickCount")
            self._cmd("Input.dispatchMouseEvent", params)
        return True

    def eval(self, expr, reintentos=2):
        for intento in range(reintentos + 1):
            try:
                return self._eval_una(expr)
            except Exception:
                if intento < reintentos and self._reconectar():
                    continue
                raise

    def navegar(self, url, condicion=None, tiempo_max=45):
        """Navega y espera a que se cumpla la condición (JS que devuelve
        verdadero). Pasa por alto el 'Un momento…' de Cloudflare."""
        self._ws.send(json.dumps({
            "id": 9000, "method": "Page.navigate", "params": {"url": url}}))
        fin = time.time() + tiempo_max
        while time.time() < fin:
            time.sleep(3)
            try:
                if condicion and self.eval(condicion):
                    return self.eval("location.href") or url
            except Exception:
                pass
        return None

    def cerrar(self):
        try:
            self._ws.close()
        except Exception:
            pass


def _extraer_botones(cdp):
    """Los botones de descarga de la página del juego."""
    return cdp.eval('''(() => {
        const res = [];
        for (const a of document.querySelectorAll('a[id="download-link"]')) {
            const t = (a.title || a.innerText || '').trim();
            res.push({h: a.href, t: t.replace(/Clic aqui para ver los Enlaces (en|de) /i, '')});
        }
        return res.filter(x => x.h && x.t);
    })()''') or []


def _url_real(entrada):
    """De {h, t} (href + texto de un ancla) saca la URL de descarga real.
    zPaste a veces esconde el enlace detrás de su acortador zshorte.net
    (el href es zshorte.net/...&url=<base64>) y muestra la URL real como
    texto del ancla; se prefiere el texto cuando es una URL y si no se
    decodifica el parámetro 'url' (base64) del acortador."""
    import base64 as _b64
    texto = (entrada.get("t") or "").strip()
    if re.match(r"^https?://", texto, re.I):
        return texto
    href = entrada.get("h") or ""
    if "zshorte.net" in href or "anomizador" in href:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            dest = _b64.b64decode(q["url"][0] + "==").decode("utf-8", "replace")
            if re.match(r"^https?://", dest, re.I):
                return dest
        except Exception:
            pass
    return href


def _leer_enlaces_paste(cdp):
    """Tras resolver la verificación, lee los enlaces de descarga visibles
    junto con su etiqueta (el texto del enlace suele decir 'Parte 1' o el
    nombre del archivo, clave para distinguir multipartes). Normaliza los
    enlaces que van por el acortador zshorte.net a su URL real.
    Devuelve [{"url", "texto"}] sin duplicados."""
    expr = r'''(() => {
        const malos = /zpaste\.net|zona-leros|cloudflare|traffmovie|ggpicky|instant-gaming|plus\.google|t\.me|javascript/i;
        const vistos = new Map();
        for (const a of document.querySelectorAll('a[href]')) {
            const u = a.href;
            if (!/^https?:\\/\\//.test(u) || malos.test(u)) continue;
            if (vistos.has(u)) continue;
            const t = (a.innerText || a.title || '').trim().replace(/\s+/g, ' ');
            if (t || !vistos.has(u)) vistos.set(u, t);   // prefiere texto no vacío
        }
        return [...vistos].map(([url, texto]) => ({url, texto}));
    })()'''
    anclas = cdp.eval(expr) or []
    reales = []
    vistos = set()
    for e in anclas:
        # el JS devuelve {url, texto}; _url_real espera {h, t}
        u = _url_real({"h": e.get("url") or "", "t": e.get("texto") or ""})
        if not re.match(r"^https?://", u):
            continue
        # nada de zpaste ni del acortador: solo el destino final
        if re.search(r"zpaste\.net|zona-leros|cloudflare|zshorte|anomizador", u):
            continue
        if u in vistos:
            continue
        vistos.add(u)
        reales.append({"url": u, "texto": (e.get("texto") or "")})
    # si no salieron anchors, busca texto con aspecto de URL
    if not reales:
        cuerpo = cdp.eval("document.body.innerText") or ""
        for u in re.findall(r"https?://[^\s\"'<>]+", cuerpo):
            if re.search(r"zpaste\.net|zona-leros|cloudflare|zshorte|anomizador", u):
                continue
            if u in vistos:
                continue
            vistos.add(u)
            reales.append({"url": u, "texto": ""})
    return reales


def _nombre_de_url(url):
    """Nombre de archivo que sugiere la URL (mediafire lo lleva en la ruta)."""
    import urllib.parse
    seg = [urllib.parse.unquote(s)
           for s in urllib.parse.urlparse(url).path.split("/") if s]
    nombre = seg[-1] if seg else ""
    # mediafire: .../file/<id>/<Nombre.part1.rar>/file -> usar el penúltimo
    if (not nombre or nombre.lower() in ("file", "download", "view", "d")) \
            and len(seg) >= 2:
        nombre = seg[-2]
    return nombre or ""


def _clave_parte(nombre):
    """De un nombre de archivo saca (clave_de_grupo, numero_de_parte).
    La clave identifica al archivo sin sus marcadores de parte, de modo que
    'Juego.rar', 'Juego.part1.rar' y 'Juego.r00' comparten la clave 'juego'.
    Marcadores reconocidos: .partN.ext, .rNN/.zNN/.aNN (rar/7z partido) y
    .NNN (.001, .002...)."""
    n = (nombre or "").lower()
    num = None
    m = re.search(r"\.part(\d{1,4})(?=\.)", n)   # file.part1.rar -> 1
    if m:
        num = int(m.group(1))
        n = n[:m.start()] + n[m.end():]
    m = re.search(r"\.([a-z0-9]{1,5})$", n)        # extensión final
    if m:
        ext = m.group(1)
        n = n[:m.start()]
        if num is None:
            if re.fullmatch(r"r\d{2}", ext):       # .r00 .r01: el .rar es la 1
                num = int(ext[1:]) + 2
            elif re.fullmatch(r"z\d{2}|a\d{2}", ext):
                num = int(ext[1:]) + 2
            elif re.fullmatch(r"\d{3}", ext):      # .001 .002 (7z partido)
                num = int(ext)
    return n, num


def _clasificar_enlaces(entradas):
    """Reconoce si los enlaces de un servidor son partes de un MISMO archivo
    (multipartes: Juego.part1.rar ... part6.rar) o archivos independientes
    (crack.rar, parche.rar...), y los ordena por número de parte.

    entrada: {"url", "texto", "nombre"}. Devuelve:
      enlaces: [{url, nombre, parte, total}]  (parte 0 = archivo suelto)
      es_multipartes, total_partes, nombre_base
    """
    grupos = {}      # clave de archivo -> [miembro, ...]
    sueltos = []     # archivos individuales sin marcador de parte
    for e in entradas:
        nombre = e.get("nombre") or ""
        clave, num = _clave_parte(nombre)
        texto = e.get("texto") or ""
        m_texto = re.search(
            r"(?:parte|part)\s*(\d{1,3})(?:\s*(?:de|/)\s*(\d{1,3}))?",
            texto, re.I)
        # un nombre tipo ID (rootz /d/1OC8iu, mega ...#clave) no describe el
        # archivo: si la etiqueta dice 'Parte N', se usa la etiqueta
        nombre_id = bool(nombre) and "." not in nombre and len(nombre) <= 40
        if m_texto and (not clave or nombre_id):
            clave, num = "__etiquetados__", int(m_texto.group(1))
        elif not clave:
            sueltos.append({"url": e["url"], "nombre": nombre,
                            "parte": 0, "texto": texto})
            continue
        grupos.setdefault(clave, []).append(
            {"url": e["url"], "nombre": nombre, "num": num,
             "texto": texto})

    # el multipartes es el grupo con más enlaces numerados
    orden = [(len([m for m in ms if m["num"] is not None]), clave, ms)
             for clave, ms in grupos.items()]
    orden.sort(key=lambda t: -t[0])
    es_multi = False
    nombre_base = None
    resto = []
    multi_miembros = []
    if orden and orden[0][0] >= 2:
        _, clave, miembros = orden[0]
        es_multi = True
        multi_miembros = miembros
        total = len(miembros)
        # asigna parte a los sin marcar (.rar de un split .r00/.r01)
        usados = sorted(m["num"] for m in miembros if m["num"] is not None)
        libre = 1
        for m in sorted(miembros,
                        key=lambda m: (m["num"] is None, m["num"] or 0)):
            if m["num"] is None:
                while libre in usados:
                    libre += 1
                m["num"] = libre
                usados.append(libre)
            m["parte"] = m["num"]
            m["total"] = total
            m.pop("num", None)
        miembros.sort(key=lambda m: m["parte"])
        nombre_base = max((m["nombre"] for m in miembros if m["nombre"]),
                          key=len, default="")
        if nombre_base:
            nombre_base = re.sub(
                r"\.(?:part\d{1,4}|\d{3})(?=\.)", "", nombre_base,
                count=1, flags=re.I)
        if nombre_base and "." not in nombre_base:
            nombre_base = None   # era un ID de enlace, no un nombre real
        resto = [m for k, ms in grupos.items() if k != clave for m in ms]
        resto.sort(key=lambda m: m["num"] or 0)
        for m in resto:
            m["parte"] = 0
            m.pop("num", None)
        sueltos.extend(resto)
    else:
        # no hay multipartes: cada enlace es un archivo independiente
        for ms in grupos.values():
            for m in ms:
                m["parte"] = 0
                m.pop("num", None)
            sueltos.extend(ms)

    sueltos.sort(key=lambda m: (m.get("nombre") or m["url"]).lower())
    salida = multi_miembros + sueltos
    for m in salida:
        m.setdefault("total", 0)
    return {"enlaces": salida, "es_multipartes": es_multi,
            "total_partes": len(salida) if es_multi else 0,
            "nombre_base": nombre_base or None}


_JS_CLICK_DOWNLOAD = '''(() => {
    const e = [...document.querySelectorAll('button')]
        .find(x => /download\\s*file/i.test(x.innerText || ''));
    if (e) { e.click(); return true; }
    return false;
})()'''

_JS_CLICK_VERIFICAR = '''(() => {
    const e = [...document.querySelectorAll('button,a,input[type=button]')]
        .find(x => (x.innerText || x.value || '').toUpperCase().includes('VERIFICAR'));
    if (e && !e.disabled && e.offsetParent !== null) { e.click(); return true; }
    return false;
})()'''


def extraer(url):
    """Flujo completo: abre la página del juego con el perfil real de Chrome,
    resuelve el reto y saca los enlaces de cada servidor.
    Devuelve {"servidores": [{"servidor", "enlaces" (con parte/total),
    "es_multipartes", "nombre_base"}], "titulo"} o {"error": ...}.
    """
    ws_url, err = _lanzar(url)
    if err:
        return {"error": err}
    cdp = None
    fin_global = time.time() + 300   # tope total: ~5 min como máximo
    try:
        cdp = _Cdp(ws_url)
        # 1) la página del juego: espera a que Cloudflare pase y aparezcan
        #    los botones de descarga
        cond = ("document.querySelectorAll('a[id=\"download-link\"]').length > 0"
                " || /un momento/i.test(document.title)")
        if not cdp.navegar(url, condicion=cond, tiempo_max=60):
            return {"error": "Cloudflare no dejó pasar la página"}
        # espera extra a que terminen de cargar los botones
        while time.time() < fin_global and not cdp.eval(
                "document.querySelectorAll('a[id=\"download-link\"]').length > 0"):
            time.sleep(3)
        titulo_juego = cdp.eval("document.title") or ""
        botones = _extraer_botones(cdp)
        if not botones:
            return {"error": "no se encontraron botones de descarga en la página"}

        # 2) cada botón: acortador -> zpaste -> verificación -> enlaces
        servidores = []
        for b in botones:
            servidor = (b.get("t") or "?").strip() or "?"
            if time.time() >= fin_global:
                break   # se acabó el presupuesto: entregamos lo extraído
            if not cdp.navegar(
                    b["h"],
                    condicion="location.href.indexOf('zpaste.net') !== -1",
                    tiempo_max=35):
                servidores.append({"servidor": servidor, "enlaces": [],
                                   "error": "no se pudo abrir el enlace"})
                continue
            # El paste llega tras DOS retos encadenados:
            #  1) el reto exterior de Cloudflare ('Un momento…') en el propio
            #     zpaste.net, que se resuelve solo en unos segundos (a veces
            #     hasta ~60 s). Hay que esperar a que desaparezca y aparezca
            #     el contenido del paste (el botón VERIFICAR).
            #  2) la verificación humana del paste: el botón 'Download File'
            #     es lo que dispara Turnstile y rellena cf-turnstile-response;
            #     con el token presente, 'VERIFICAR & CONTINUAR' revela los
            #     enlaces. Pulsar VERIFICAR con el token vacío mata el reto.
            enlaces = []
            for intento in range(2):
                if time.time() >= fin_global:
                    break
                # 1) esperar a que se resuelva el reto exterior de Cloudflare
                fin_reto = min(time.time() + 90, fin_global)
                while time.time() < fin_reto:
                    titulo_reto = (cdp.eval("document.title") or "").lower()
                    hay_verif = cdp.eval(
                        "!![...document.querySelectorAll('button,a')]"
                        ".find(x => /VERIFICAR/i.test(x.innerText || x.value || ''))")
                    if "un momento" not in titulo_reto and hay_verif:
                        break
                    time.sleep(3)
                # 2) 'Download File' arranca Turnstile; cuando el token está,
                #    pulsar VERIFICAR revela los enlaces
                fin = min(time.time() + 40, fin_global)
                while time.time() < fin and not enlaces:
                    enlaces = _leer_enlaces_paste(cdp)
                    if enlaces:
                        break
                    token = (cdp.eval(
                        "(document.querySelector('input[name=\"cf-turnstile-response\"]') || {}).value || ''")
                        or "")
                    if not token:
                        # sin token: dispara Turnstile con 'Download File' y
                        # si el reto pide interacción (checkbox visible) dale
                        # un clic real en su iframe
                        cdp.eval(_JS_CLICK_DOWNLOAD)
                        cdp.clic_widget_turnstile()
                    else:
                        cdp.eval(_JS_CLICK_VERIFICAR)
                    time.sleep(3)
                if enlaces or time.time() >= fin_global or intento == 1:
                    break
                # reintenta: recarga el paste (nueva sesión de reto)
                paste_url = cdp.eval("location.href") or b["h"]
                if not cdp.navegar(
                        paste_url,
                        condicion="location.href.indexOf('zpaste.net') !== -1",
                        tiempo_max=35):
                    break
            # clasifica: multipartes del mismo archivo vs archivos sueltos,
            # y ordena por número de parte (la extracción exige el orden)
            clasificado = _clasificar_enlaces([
                {"url": e["url"], "texto": e.get("texto") or "",
                 "nombre": _nombre_de_url(e["url"])} for e in enlaces])
            clasificado["servidor"] = servidor
            servidores.append(clasificado)
        return {"servidores": servidores, "titulo": titulo_juego[:150]}
    except Exception as e:
        # si nuestra instancia de Chrome murió a mitad, dilo claro
        if not _nuestro_chrome_vivo():
            return {"error": ("La instancia de Chrome de la extracción se "
                              "cerró a mitad (quizá se actualizó o el "
                              "sistema la cerró). Vuelve a intentar.")}
        return {"error": "error extrayendo: %s" % e}
    finally:
        if cdp:
            cdp.cerrar()
        _finalizar()   # solo nuestra instancia; el Chrome del usuario intacto


# ---------------------------------------------------------------- CLI de prueba
# python zonaleros_copia.py [url]   (por defecto la página de Kaiserpunk)
if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    url = (sys.argv[1] if len(sys.argv) > 1
           else "https://www.zona-leros.com/juegos-pc/kaiserpunk-pc-e")
    print("URL:", url)
    print("Chrome abierto ahora mismo:", _chrome_corriendo())
    t0 = time.time()
    r = extraer(url)
    print("duracion: %.0fs" % (time.time() - t0))
    if "error" in r:
        print("ERROR:", r["error"])
        sys.exit(1)
    print("TITULO:", r.get("titulo"))
    for s in r.get("servidores", []):
        print("\n### SERVIDOR:", s.get("servidor"))
        if s.get("error"):
            print("   error:", s["error"])
        print("   enlaces:", len(s.get("enlaces", [])))
        for e in s.get("enlaces", []):
            print("   - parte %s de %s | %s | %s" %
                  (e.get("parte") or "-", e.get("total") or "-",
                   e.get("nombre") or "(sin nombre)", e.get("url", "")[:70]))
        if s.get("es_multipartes"):
            print("   -> MULTIPARTES:", s.get("total_partes"),
                  "| base:", s.get("nombre_base"))
