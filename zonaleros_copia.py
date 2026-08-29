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
_PARALELO_SERIE = 5   # pestañas de Chrome que resuelven episodios a la vez
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

    def __init__(self, ws_url, target_id=None):
        self._ws_url = ws_url
        self._target_id = target_id   # pestaña concreta (para el paralelo)
        self._ws = _ws.create_connection(ws_url, timeout=30)
        self._id = 0

    def _reconectar(self):
        try:
            nueva = None
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % _PUERTO_CDP,
                    timeout=3) as r:
                for t in json.loads(r.read().decode("utf-8", "replace")):
                    if t.get("type") == "page" and (
                            self._target_id is None
                            or t.get("id") == self._target_id):
                        nueva = t["webSocketDebuggerUrl"]
                        break
            if not nueva:
                return False
            self._ws_url = nueva
            self._ws = _ws.create_connection(nueva, timeout=30)
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
            "params": {"expression": expr, "returnByValue": True,
                            "awaitPromise": True}}))
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


def _es_pagina_serie(url):
    """True si la URL es la página de UNA SERIE de zona-leros (índice con la
    lista de episodios), no un episodio suelto ni una página de juego."""
    seg = [s for s in urllib.parse.urlparse(url).path.lower().split("/") if s]
    return len(seg) >= 2 and seg[0] == "series" and seg[1] != "episode"


def _es_pagina_episodio(url):
    """True si la URL es la página de UN episodio (/series/episode/...)."""
    seg = [s for s in urllib.parse.urlparse(url).path.lower().split("/") if s]
    return len(seg) >= 2 and seg[0] == "series" and seg[1] == "episode"


def _extraer_episodios(cdp):
    """Episodios de la página de serie: [{label, url}] desde los anclas a
    /series/episode/ (el texto suele ser '4x1 Ataque a los Titanes')."""
    anclas = cdp.eval('''(() => {
        const out = [];
        for (const a of document.querySelectorAll('a[href*="/series/episode/"]')) {
            const t = (a.innerText || a.title || '').trim().replace(/\\s+/g, ' ');
            const h = (a.href || '').split('#')[0];
            if (t && /^https?:\\/\\//.test(h)) out.push({t: t, h: h});
        }
        return out;
    })()''') or []
    vistos = set()
    salida = []
    for e in anclas:
        u = e.get("h") or ""
        if u in vistos:
            continue
        vistos.add(u)
        salida.append({"label": (e.get("t") or "?").strip() or "?", "url": u})
    return salida


def _temporadas(cdp):
    """Enlaces a las temporadas de una página de serie. El sitio organiza
    las series por temporada: `/series/ataque-a-los-titanes` lista los
    accesos a `/series/season/ataque-a-los-titanes-N`, y cada temporada
    contiene sus episodios. Devuelve la lista de URLs de temporadas."""
    return cdp.eval('''(() => {
        const out = [];
        for (const a of document.querySelectorAll('a[href*="/series/season/"]')) {
            const h = (a.href || '').split('#')[0];
            const t = (a.innerText || a.title || '').trim().replace(/\\s+/g, ' ');
            if (/^https?:\\/\\//.test(h) && out.indexOf(h) === -1) out.push({h: h, t: t});
        }
        return out;
    })()''') or []


def _extraer_episodios_de_temporada(cdp, url_temporada, fin_global):
    """Navega a una página de temporada (/series/season/...) y devuelve sus
    episodios. Devuelve (episodios, error)"""
    cond = ("document.querySelectorAll('a[href*=\\\"/series/episode/\\\"]').length > 0"
            " || " + _js_reto_cloudflare())
    if not cdp.navegar(url_temporada, condicion=cond, tiempo_max=90):
        if not cdp.navegar(url_temporada, condicion=cond, tiempo_max=90):
            return [], "Cloudflare no dejó pasar la página de la temporada"
    fin = min(time.time() + 120, fin_global)
    epis = []
    while time.time() < fin:
        epis = _extraer_episodios(cdp)
        if epis:
            break
        if _bloqueado_duro(cdp):
            return [], "Cloudflare bloqueó la página de la temporada"
        time.sleep(3)
    return epis, None


def _episodios_serie_completa(cdp, url, fin_global):
    """Lista de episodios de una serie, tolerante a la estructura del sitio:
    1) episodios directos en la página (a[href*="/series/episode/"]);
    2) si no hay, recorre las temporadas (a[href*="/series/season/"]) y
       junta los episodios de todas. Devuelve ([{label, url}], error)."""
    episodios = _extraer_episodios(cdp) or []
    if episodios:
        return episodios, None
    temporadas = _temporadas(cdp)
    if not temporadas:
        return [], None
    todos = []
    vistos = set()
    for temp in temporadas:
        if time.time() >= fin_global:
            break
        eps, err = _extraer_episodios_de_temporada(cdp, temp["h"], fin_global)
        if err:
            continue
        for e in eps:
            if e.get("url") and e["url"] not in vistos:
                vistos.add(e["url"])
                todos.append(e)
    return todos, None


def _extraer_botones_episodio(cdp):
    """Botones DESCARGAR de una página de episodio: anclas al acortador
    anomizador. No llevan title en el HTML; se etiquetan por opción."""
    return cdp.eval('''(() => {
        const res = [];
        for (const a of document.querySelectorAll('a[href*="anomizador"]')) {
            const t = (a.innerText || a.title || '').trim();
            const h = (a.href || '');
            if (/^descargar$/i.test(t)) res.push({h: h, t: t});
        }
        return res;
    })()''') or []


def _resolver_zpaste_actual(cdp, fin_global):
    """Estando ya en el paste (zpaste.net), resuelve los DOS retos
    encadenados y devuelve los enlaces visibles:
      1) el reto exterior de Cloudflare ('Un momento…'), que se resuelve
         solo en unos segundos (a veces hasta ~60 s): hay que esperar a que
         desaparezca y aparezca el contenido del paste (el botón VERIFICAR).
      2) la verificación humana del paste: el botón 'Download File' dispara
         Turnstile y rellena cf-turnstile-response; con el token presente,
         'VERIFICAR & CONTINUAR' revela los enlaces. Pulsar VERIFICAR con
         el token vacío mata el reto."""
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
        paste_url = cdp.eval("location.href")
        if not paste_url:
            break
        if not cdp.navegar(
                paste_url,
                condicion="location.href.indexOf('zpaste.net') !== -1",
                tiempo_max=35):
            break
    return enlaces


def _resolver_un_boton(cdp, boton, fin_global):
    """Juegos: acortador -> zpaste -> verificación -> enlaces de UN botón
    de descarga. Devuelve el dict clasificado (sin 'servidor'; lo pone el
    llamador) o None si se agotó el presupuesto."""
    if time.time() >= fin_global:
        return None
    if not cdp.navegar(
            boton["h"],
            condicion="location.href.indexOf('zpaste.net') !== -1",
            tiempo_max=35):
        return {"enlaces": [], "es_multipartes": False,
                "error": "no se pudo abrir el enlace"}
    enlaces = _resolver_zpaste_actual(cdp, fin_global)
    # clasifica: multipartes del mismo archivo vs archivos sueltos,
    # y ordena por número de parte (la extracción exige el orden)
    return _clasificar_enlaces([
        {"url": e["url"], "texto": e.get("texto") or "",
         "nombre": _nombre_de_url(e["url"])} for e in enlaces])


def _resolver_enlace_episodio(cdp, boton, fin_global):
    """Episodios: el acortador anomizador puede resolver DIRECTO al enlace
    final (Mega, MediaFire…) o pasar por zpaste (multipartes con reto).
    Espera a que el acortador termine de redirigir y, según dónde aterrice,
    toma la URL final como enlace o entra al flujo del paste. Devuelve el
    clasificado (sin 'servidor') o None si se agotó el presupuesto."""
    if time.time() >= fin_global:
        return None
    cond = ("location.href.indexOf('anomizador') === -1"
            " && !/un momento/i.test(document.title)"
            " && !/just a moment/i.test(document.title)")
    if not cdp.navegar(boton["h"], condicion=cond, tiempo_max=45):
        return {"enlaces": [], "es_multipartes": False,
                "error": "no se pudo abrir el enlace"}
    time.sleep(2)   # margen por si el acortador encadena una redirección JS
    destino = cdp.eval("location.href") or ""
    if "zpaste.net" in destino:
        # cayó en un paste: misma vía que los juegos (reto + verificación)
        enlaces = _resolver_zpaste_actual(cdp, fin_global)
        return _clasificar_enlaces([
            {"url": e["url"], "texto": e.get("texto") or "",
             "nombre": _nombre_de_url(e["url"])} for e in enlaces])
    # enlace directo: la URL final ya es el enlace de descarga. Se descartan
    # destinos inválidos (página de error de Chrome, página propia del sitio
    # tipo 404, o el acortador sin resolver).
    if (not re.match(r"^https?://", destino)
            or "zona-leros" in destino or "anomizador" in destino):
        return {"enlaces": [], "es_multipartes": False,
                "error": "enlace expirado o caído"}
    nombre = _nombre_de_url(destino)
    return {"enlaces": [{"url": destino, "nombre": nombre, "parte": 0,
                          "total": 0}],
            "es_multipartes": False, "total_partes": 0,
            "nombre_base": nombre or None}


def _nombre_hoster_url(url):
    """Nombre estable del hoster real para filtrar resultados de series."""
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    for frag, nombre in (("mediafire.com", "MediaFire"), ("mega.nz", "Mega"),
                         ("mega.co.nz", "Mega"), ("gofile.io", "GoFile"),
                         ("rootz.so", "Rootz"), ("1fichier.com", "1Fichier"),
                         ("megaup.net", "MegaUp"), ("fireload.com", "Fireload")):
        if host == frag or host.endswith("." + frag):
            return nombre
    return host or "Servidor"


# Lista canónica de hosters por los que se puede filtrar en series. El panel la
# pide al backend para pintar las opciones dinámicamente (no hardcodear con 5
# nombres). El orden también es el que muestra el extractor para dominios no
# reconocidos.
HOSTERES_CONOCIDOS = ("MediaFire", "Mega", "GoFile", "Rootz",
                      "1Fichier", "MegaUp", "Fireload",
                      "Servidor por confirmar")


def _hoster_para_grupo(urls_finales, label, n_opciones, opcion,
                       hosters_permitidos=None):
    """Resuelve el hoster real de un grupo de enlaces de episodio y aplica el
    filtro por servidores elegidos (función pura, testeable).

    Devuelve None si el grupo queda fuera del filtro, o un dict
    {servidor, episodio, hoster} con el nombre visible del grupo.

    La comparación de hosters es EXACTA (si no, "Mega" filtraría también
    "MegaUp"). Solo se descarta cuando se conoce el hoster y no coincide con
    ninguna opción elegida; un grupo sin hoster confirmado pasa a
    "Servidor por confirmar" y solo queda si el usuario lo eligió o no filtró."""
    hosters = {_nombre_hoster_url(u) for u in urls_finales if u}
    permitidos = {str(x).lower() for x in (hosters_permitidos or [])}
    hosters_lower = {h.lower() for h in hosters}
    if permitidos and hosters and not (hosters_lower & permitidos):
        return None
    nombre = next(iter(hosters), "Servidor por confirmar")
    sufijo = (" · Opción %d" % opcion) if n_opciones > 1 else ""
    return {"servidor": nombre + " · " + label + sufijo,
            "episodio": label, "hoster": nombre}


def _extraer_un_episodio(cdp, url, label, fin_global, hosters_permitidos=None):
    """Navega a la página de un episodio y resuelve sus botones DESCARGAR.
    Devuelve (servidores, incompleto, motivo). La etiqueta de cada grupo es
    el label del episodio + '· Opción N' cuando hay más de un botón."""
    cond = ("document.querySelectorAll('a[href*=\"anomizador\"]').length > 0"
            " || " + _js_reto_cloudflare())
    if not cdp.navegar(url, condicion=cond, tiempo_max=60):
        return ([], True, "no se pudo abrir el episodio")
    while time.time() < fin_global and not cdp.eval(
            "document.querySelectorAll('a[href*=\"anomizador\"]').length > 0"):
        if _bloqueado_duro(cdp):
            return ([], True, "Cloudflare bloqueó el episodio; intentá de nuevo")
        time.sleep(3)
    if not label:
        titulo = (cdp.eval("document.title") or "").strip()
        label = re.sub(r"\s*\|\s*ZonaLeRoS\s*$", "", titulo, flags=re.I).strip() or "?"
    botones = _extraer_botones_episodio(cdp)
    if not botones:
        return ([], True, "sin enlaces de descarga en el episodio")
    servidores = []
    incompleto = False
    for i, b in enumerate(botones):
        clasificado = _resolver_enlace_episodio(cdp, b, fin_global)
        if clasificado is None:
            incompleto = True
            break
        # El hoster solo se conoce tras resolver el acortador. Filtramos aquí,
        # antes de entregar el resultado al panel/cola, no después.
        urls_finales = [e.get("url") for e in (clasificado.get("enlaces") or []) if isinstance(e, dict)]
        grupo = _hoster_para_grupo(urls_finales, label, len(botones), i + 1,
                                   hosters_permitidos)
        if grupo is None:
            continue
        clasificado.update(grupo)
        servidores.append(clasificado)
    if not servidores and not incompleto:
        return [], True, "el episodio no devolvió enlaces de descarga"
    return servidores, incompleto, None


def _crear_pestanas(n):
    """Abre n pestañas nuevas (about:blank) en la instancia de Chrome que
    lanzamos, vía CDP del navegador (Target.createTarget, la vía estable
    desde Chrome 111). Devuelve [(webSocketDebuggerUrl, target_id), ...]
    o [] si no se pudo."""
    if n <= 0:
        return []
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json/version" % _PUERTO_CDP,
                timeout=3) as r:
            ver = json.loads(r.read().decode("utf-8", "replace"))
        ws_browser = ver.get("webSocketDebuggerUrl")
        if not ws_browser:
            return []
        ws = _ws.create_connection(ws_browser, timeout=10)
        ids = [1000 + i for i in range(n)]
        for mid in ids:
            ws.send(json.dumps({"id": mid, "method": "Target.createTarget",
                                "params": {"url": "about:blank"}}))
        creadas = set()
        while len(creadas) < n:
            r = json.loads(ws.recv())
            if r.get("id") in ids:
                t = (r.get("result") or {}).get("targetId")
                if t:
                    creadas.add(t)
        ws.close()
    except Exception:
        return []
    # las websockets de cada pestaña nueva, desde /json/list (con un pequeño
    # reintento por si la lista aún no las refleja). Orden del par:
    # (webSocketDebuggerUrl, target_id) — igual que _trabajo.
    salida = []
    for _ in range(5):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % _PUERTO_CDP,
                    timeout=3) as r:
                paginas = [t for t in json.loads(
                    r.read().decode("utf-8", "replace"))
                    if t.get("type") == "page"]
            salida = [(t["webSocketDebuggerUrl"], t["id"]) for t in paginas
                      if t.get("id") in creadas
                      and t.get("webSocketDebuggerUrl")]
            if len(salida) >= n:
                break
        except Exception:
            pass
        time.sleep(0.5)
    return salida


def _extraer_serie_paralela(episodios, n_pestanas, ws_maestro,
                            target_maestro, on_progreso=None, titulo=None,
                            hosters_permitidos=None):
    """Resuelve los episodios de una serie EN PARALELO: n_pestanas pestañas
    nuevas del mismo Chrome (cada una con su conexión CDP) más la pestaña
    maestra (la que ya trae la lista de episodios). Devuelve
    (servidores, incompleto, episodios_fallidos) en el orden de la serie.
    Si no se pueden abrir las pestañas, cae a secuencial con la pestaña maestra.

    on_progreso(servidores, n_resueltos, n_total, titulo, episodios_fallidos)
    se invoca desde
    los hilos trabajadores cada vez que un episodio queda resuelto, con la
    lista acumulada hasta ese momento (para mostrarla EN VIVO en el panel)."""
    import queue as _queue
    import threading as _threading
    pestañas = _crear_pestanas(n_pestanas)
    conexiones = [(ws_maestro, target_maestro)] + pestañas
    if not pestañas:
        _log("[serie] no se pudieron abrir pestañas paralelas: "
             "resolución secuencial (1 conexión)")
    # presupuesto: el tiempo por episodio se reparte entre las conexiones
    n_conex = max(len(conexiones), 1)
    fin_global = time.time() + min(300 + 90 * len(episodios) // n_conex, 3600)
    q = _queue.Queue()
    for i, ep in enumerate(episodios):
        q.put((i, ep))
    resultados = []          # (indice, servidores, incompleto)
    candado = _threading.Lock()

    def _trabajo(ws_url, target_id):
        cdp = None
        try:
            try:
                cdp = _Cdp(ws_url, target_id=target_id)
            except Exception:
                # una pestaña que no conecta no hunde al resto: este
                # trabajador se rinde y los demás siguen
                _log("[serie] un trabajador no pudo conectar su pestaña")
                return
            while True:
                if time.time() >= fin_global:
                    break
                try:
                    i, ep = q.get_nowait()
                except _queue.Empty:
                    break
                eps, inc, motivo = ([], True, "sin intentar")
                # Algunos episodios fallan al cambiar entre el perfil junction
                # y la copia de Chrome. Reintentar evita perder capítulos por
                # un fallo transitorio de navegación/Cloudflare.
                for intento in range(3):
                    try:
                        eps, inc, motivo = _extraer_un_episodio(
                            cdp, ep["url"], ep["label"], fin_global,
                            hosters_permitidos=hosters_permitidos)
                    except Exception as e:
                        eps, inc, motivo = [], True, (
                            "error extrayendo el episodio: %s" % e)
                    if eps and any(x.get("enlaces") for x in eps):
                        break
                    if time.time() >= fin_global:
                        break
                    if intento < 2:
                        time.sleep(2 + intento * 2)
                # no se agrega un servidor vacío: se conserva el fallo en una
                # lista aparte para informarlo y poder reintentarlo.
                with candado:
                    resultados.append((i, eps, inc, motivo))
                    if on_progreso is not None:
                        try:
                            vivos = sorted(resultados)
                            acum = []
                            for _, e_, _inc, _motivo in vivos:
                                acum.extend(e_)
                            fallidos_vivos = [{
                                "indice": j,
                                "label": episodios[j].get("label") or "?",
                                "url": episodios[j].get("url") or "",
                                "error": mot or "sin enlaces de descarga"
                            } for j, _e, _inc, mot in vivos
                              if not any(x.get("enlaces") for x in _e)]
                            try:
                                on_progreso(acum, len(vivos), len(episodios),
                                            titulo, fallidos_vivos)
                            except TypeError:
                                # Compatibilidad con callbacks antiguos de
                                # cuatro argumentos.
                                on_progreso(acum, len(vivos), len(episodios),
                                            titulo)
                        except Exception:
                            pass
        finally:
            if cdp:
                try:
                    cdp.cerrar()
                except Exception:
                    pass

    hilos = [_threading.Thread(target=_trabajo, args=(w, t), daemon=True)
             for (w, t) in conexiones]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    return _consolidar_resultados_serie(resultados, episodios)


def _consolidar_resultados_serie(resultados, episodios):
    """Consolida resultados de trabajadores sin convertir fallos en
    servidores vacíos. Es pura para poder probarla sin Chrome/CDP."""
    servidores = []
    episodios_fallidos = []
    # si el presupuesto cortó antes de recorrerlos todos, va "incompleto"
    incompleto = len(resultados) < len(episodios)
    for item in sorted(resultados):
        # compatibilidad con resultados antiguos de pruebas/consumidores
        i, eps, inc = item[:3]
        motivo = item[3] if len(item) > 3 else None
        validos = [e for e in eps if e.get("enlaces")]
        servidores.extend(validos)
        if not validos:
            episodio = episodios[i] if i < len(episodios) else {}
            episodios_fallidos.append({
                "indice": i, "label": episodio.get("label") or "?",
                "url": episodio.get("url") or "",
                "error": motivo or "sin enlaces de descarga"})
            incompleto = True
        incompleto = incompleto or inc
    return servidores, incompleto, episodios_fallidos


def _js_reto_cloudflare():
    """JS que detecta el reto de Cloudflare ACTIVO (título en ES/EN o
    el texto de verificación en la página). Sirve para no darse por
    vencido mientras Cloudflare verifica (el reto suele resolverse solo
    en unos segundos si el perfil pasa la verificación)."""
    return ("/un momento|just a moment|verificaci[oó]n|verifying|"
            "security check|attention required|access denied/i.test("
            "document.title + ' ' + (document.body && document.body.innerText"
            " || '').slice(0, 3000))")


def _bloqueado_duro(cdp):
    """True si Cloudflare mostró una página de BLOQUEO duro (la IP o el
    navegador no pasan el reto; esperar no ayuda)."""
    try:
        return bool(cdp.eval(
            "/attention required|access denied|bloqueado|blocked/i.test("
            "document.title + ' ' + (document.body && document.body.innerText"
            " || '').slice(0, 3000))"))
    except Exception:
        return False



def extraer(url, on_progreso=None, hosters_permitidos=None, episodios_permitidos=None):
    """Flujo completo: abre la página (juego, episodio o serie) con el perfil
    de Chrome, resuelve el reto y saca los enlaces de cada servidor / episodio.
    Devuelve {"servidores": [...], "titulo"} o {"error": ...}. Para series
    la resolución de episodios corre EN PARALELO (varias pestañas del mismo
    Chrome, ~5-10 min en vez de ~35); el resultado puede traer "incompleto":
    True si se agotó el presupuesto antes de recorrerlos todos (no se guarda
    en caché).

    on_progreso(servidores, n_resueltos, n_total, titulo): se invoca con los
    resultados parciales de las series mientras la extracción sigue (para
    mostrarlos en vivo en el panel)."""
    ws_url, err = _lanzar(url)
    if err:
        return {"error": err}
    cdp = None
    fin_global = time.time() + 300   # tope base: ~5 min (las series lo amplían)
    try:
        cdp = _Cdp(ws_url)

        # ---------- SERIE: lista de episodios, cada uno con sus enlaces
        if _es_pagina_serie(url):
            def _hay_episodios():
                try:
                    return cdp.eval(
                        "document.querySelectorAll('a[href*=\"/series/episode/\"]').length > 0")
                except Exception:
                    return False

            def _esperar_episodios(budget):
                fin = time.time() + budget
                while time.time() < fin and not _hay_episodios():
                    if _bloqueado_duro(cdp):
                        return "bloqueado"
                    time.sleep(3)
                return "ok" if _hay_episodios() else "agotado"

            cond = ("document.querySelectorAll('a[href*=\"/series/episode/\"]').length > 0"
                    " || " + _js_reto_cloudflare())
            # la navegación inicial puede tardar por el reto de Cloudflare:
            # margen amplio y, si no cargó, se reintenta una vez
            if not cdp.navegar(url, condicion=cond, tiempo_max=120):
                if not cdp.navegar(url, condicion=cond, tiempo_max=120):
                    return {"error": "Cloudflare no dejó pasar la página"}
            # espera dedicada para la lista de episodios (no consume el
            # presupuesto de resolución); si el reto se atasca, se reintenta
            # la navegación una vez antes de rendirse
            if _esperar_episodios(240) == "agotado":
                cdp.navegar(url, condicion=cond, tiempo_max=120)
                _esperar_episodios(180)
            titulo = cdp.eval("document.title") or ""
            # lista de episodios tolerante a la estructura del sitio: si la
            # página no trae episodios directos (a /series/episode/), recorre
            # las temporadas (a /series/season/) y junta los de todas
            episodios, _err_ep = _episodios_serie_completa(
                cdp, url, time.time() + 240)
            if not episodios:
                if _bloqueado_duro(cdp):
                    return {"error": ("Cloudflare bloqueó la página de la serie; "
                                     "intentá de nuevo en unos minutos")}
                if re.search(r"un momento|verificando|just a moment|verificaci[oó]n|security check", titulo, re.I):
                    return {"error": ("Cloudflare no respondió a tiempo en la página de la"
                                     " serie; volvé a intentar en un momento")}
                return {"error": "no se encontraron episodios en la página de la serie"}

            if episodios_permitidos:
                permitidos_urls = set(episodios_permitidos)
                episodios = [e for e in episodios if e.get("url") in permitidos_urls]
            servidores, incompleto, episodios_fallidos = _extraer_serie_paralela(
                episodios, _PARALELO_SERIE, ws_url, None,
                on_progreso=on_progreso, titulo=titulo,
                hosters_permitidos=hosters_permitidos)
            # document.title puede quedar vacío durante una navegación con
            # Cloudflare; nunca presentar un título vacío como "Página no
            # encontrada" en la UI.
            if not titulo.strip():
                titulo = "Serie ZonaLeros"
            resultado = {"servidores": servidores, "titulo": titulo[:150]}
            if incompleto or episodios_fallidos:
                resultado["incompleto"] = True
            if episodios_fallidos:
                resultado["episodios_fallidos"] = episodios_fallidos
            return resultado

        # ---------- EPISODIO suelto: una sola página con botones DESCARGAR
        if _es_pagina_episodio(url):
            servidores, incompleto, _motivo = _extraer_un_episodio(
                cdp, url, None, fin_global)
            titulo = cdp.eval("document.title") or ""
            resultado = {"servidores": servidores,
                         "titulo": (titulo.strip() or "Episodio ZonaLeros")[:150]}
            if incompleto:
                resultado["incompleto"] = True
            return resultado

        # ---------- JUEGO: botones de descarga de la página
        cond = ("document.querySelectorAll('a[id=\"download-link\"]').length > 0"
                " || " + _js_reto_cloudflare())
        if not cdp.navegar(url, condicion=cond, tiempo_max=120):
            if not cdp.navegar(url, condicion=cond, tiempo_max=120):
                return {"error": "Cloudflare no dejó pasar la página"}
        # espera extra a que terminen de cargar los botones
        while time.time() < fin_global and not cdp.eval(
                "document.querySelectorAll('a[id=\"download-link\"]').length > 0"):
            if _bloqueado_duro(cdp):
                return {"error": "Cloudflare bloqueó la página (intentá de nuevo en unos minutos)"}
            time.sleep(3)
        titulo = cdp.eval("document.title") or ""
        botones = _extraer_botones(cdp)
        if not botones:
            return {"error": "no se encontraron botones de descarga en la página"}
        servidores = []
        for b in botones:
            servidor = (b.get("t") or "?").strip() or "?"
            clasificado = _resolver_un_boton(cdp, b, fin_global)
            if clasificado is None:
                break   # se acabó el presupuesto: entregamos lo extraído
            clasificado["servidor"] = servidor
            servidores.append(clasificado)
        return {"servidores": servidores, "titulo": titulo[:150]}
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
