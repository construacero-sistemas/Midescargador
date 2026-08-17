# -*- coding: utf-8 -*-
"""Descarga de torrents.

Dos piezas:

1. Resolver de zetrrent.com (el hoster de archivos .torrent que usan los
   enlaces TORRENT de zona-leros). Tras un contador de 10 s la página llama
   a POST /<id>/download/create con el token CSRF y devuelve la URL directa
   del .torrent. Ese flujo es reproducible headless (cookies + token), sin
   navegador:

      GET  /<id>/file                    -> cookies + csrf + downloadId
      POST /<id>/download/create         -> {"download_link": "<.torrent>"}

2. TrabajoTorrent: descarga contenido BitTorrent con aria2c (bin/aria2c.exe,
   soporta .torrent local, URL .torrent y magnet). El progreso se lee de las
   líneas de resumen de aria2c y se expone con la misma forma que el motor
   segmentado, para que el panel lo pinte igual.

Los callbacks on_completada / on_error los asigna el servidor (igual que
motor.on_completada y mega.on_completada).
"""
import os
import re
import sys
import json
import time
import uuid
import ssl
import threading
import subprocess
import tempfile
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _base_dir():
    """Dónde viven los binarios: en modo empaquetado (PyInstaller) se
    extraen a sys._MEIPASS."""
    if getattr(sys, "_MEIPASS", None):
        return sys._MEIPASS
    return BASE_DIR


RUTA_ARIA2 = os.path.join(_base_dir(), "bin", "aria2c.exe")
_TEMP_TORRENT = os.path.join(tempfile.gettempdir(), "MiDescargador", "torrents")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_CTX = ssl.create_default_context()

# el servidor asigna estos callbacks (mismo patrón que motor/mega)
on_completada = None
on_error = None

_ZETRENT = ("zetrrent.com", "www.zetrrent.com")
_MADIASHARE = ("madiashare.com", "www.madiashare.com")


def disponible():
    """True si aria2c está en bin/ (motor de torrents operativo)."""
    return os.path.exists(RUTA_ARIA2)


def es_torrent(url):
    """Reconoce magnet:, URLs .torrent, zetrrent.com y madiashare.com.
    madiashare es el hoster de .torrent que usan las páginas de pivigames:
    la URL visible (downloads?d=<id>) es una página HTML que al pulsar su
    botón navega a /Link/downloads/<id>, que sí entrega el .torrent."""
    u = (url or "").strip()
    if u.lower().startswith("magnet:"):
        return True
    if ".torrent" in u.lower():
        return True
    host = (urllib.parse.urlparse(u).hostname or "").lower()
    return host in _ZETRENT or host in _MADIASHARE


def _url_torrent_directa(url):
    """Convierte una URL de madiashare a la ruta que entrega el .torrent.
    https://madiashare.com/downloads?d=<id> -> https://madiashare.com/Link/downloads/<id>
    (si ya es /Link/... se deja igual)."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in _MADIASHARE:
        return url
    if "/Link/" in url:
        return url
    m = re.search(r"[?&]d=([A-Za-z0-9_-]+)", url)
    if not m:
        return url
    return "https://madiashare.com/Link/downloads/" + m.group(1)


def _pedir(op, url, metodo="GET", cabeceras=None, datos=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if cabeceras:
        h.update(cabeceras)
    req = urllib.request.Request(url, headers=h, method=metodo, data=datos)
    return op.open(req, timeout=40)


def _extraer_zetrrent(url):
    """Resuelve una página de archivo de zetrrent a la URL directa del .torrent.

    Descarga el .torrent a un archivo temporal (con la sesión de cookies,
    porque el enlace firmado depende de ella) y devuelve
    {"torrent_url", "local", "nombre"}.
    """
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Accept", "*/*")]
    try:
        html = _pedir(op, url).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"zetrrent rechazó la página (HTTP {e.code})") from e
    except Exception as e:
        raise RuntimeError(f"no se pudo abrir zetrrent: {e}") from e

    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    if not m:
        raise RuntimeError("zetrrent no mostró el token de descarga (¿cambió el sitio?)")
    csrf = m.group(1)
    m = re.search(r'downloadId\s*=\s*["\']?([A-Za-z0-9_-]+)', html)
    if not m:
        raise RuntimeError("zetrrent no mostró el id del archivo (¿cambió el sitio?)")
    fid = m.group(1)

    try:
        r = _pedir(
            op, "https://www.zetrrent.com/" + fid + "/download/create",
            metodo="POST", datos=b"",
            cabeceras={"X-CSRF-TOKEN": csrf,
                       "X-Requested-With": "XMLHttpRequest",
                       "Referer": url,
                       "Content-Type": "application/x-www-form-urlencoded"})
        d = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"zetrrent rechazó la descarga (HTTP {e.code})") from e
    except Exception as e:
        raise RuntimeError(f"zetrrent no generó el enlace: {e}") from e
    link = (d or {}).get("download_link")
    if not link:
        raise RuntimeError("zetrrent: " + str((d or {}).get("error") or "sin enlace"))

    # baja el .torrent con la sesión (el enlace firmado depende de la cookie)
    nombre = (os.path.basename(urllib.parse.urlparse(link).path)
              or "descarga.torrent")
    os.makedirs(_TEMP_TORRENT, exist_ok=True)
    local = os.path.join(_TEMP_TORRENT, uuid.uuid4().hex[:8] + ".torrent")
    try:
        datos_torrent = _pedir(op, link).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"zetrrent no entregó el .torrent (HTTP {e.code})") from e
    except Exception as e:
        raise RuntimeError(f"zetrrent no entregó el .torrent: {e}") from e
    if not datos_torrent.startswith(b"d") or b"8:announce" not in datos_torrent[:200]:
        raise RuntimeError("el archivo bajado de zetrrent no parece un .torrent válido")
    with open(local, "wb") as f:
        f.write(datos_torrent)
    return {"torrent_url": link, "local": local, "nombre": nombre}


def resolver(url):
    """Convierte un enlace de torrent a algo que aria2c pueda bajar.

    - zetrrent.com  -> .torrent local (descargado con sesión)
    - .torrent URL / magnet -> tal cual
    Devuelve {"torrent_url", "local"?, "nombre"} o None si no es torrent.
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in _ZETRENT:
        return _extraer_zetrrent(url)
    if es_torrent(url):
        nombre = ""
        if url.lower().startswith("magnet:"):
            nombre = "torrent"
        else:
            # madiashare: la URL visible es una página; la directa entrega el .torrent
            if host in _MADIASHARE:
                url = _url_torrent_directa(url)
            nombre = (os.path.basename(urllib.parse.urlparse(url).path)
                      or "descarga.torrent")
        return {"torrent_url": url, "local": None, "nombre": nombre}
    return None


_UNIDADES = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3,
             "TiB": 1024 ** 4}


def _a_bytes(valor, unidad):
    try:
        return int(float(valor) * _UNIDADES.get(unidad, 1))
    except (TypeError, ValueError):
        return 0


_RE_SUMARIO = re.compile(
    r"\[#\S+\s+([\d.]+)([A-Za-z]+)/([\d.]+)([A-Za-z]+)\((\d+)%\)"
    r"\s+CN:\d+(?:\s+SD:\d+)?\s+DL:([\d.]+)([A-Za-z]+)")

_RE_COMPLETA = re.compile(r"Download complete:")


class TrabajoTorrent:
    """Descarga un torrent con aria2c, con la misma cara que el motor
    segmentado (progreso, pausa, cancelar, reintentar)."""

    def __init__(self, url, carpeta):
        self.id = uuid.uuid4().hex[:8]
        self.url = url
        self.carpeta = carpeta
        self.nombre = "torrent"
        self.total = None
        self.descargado = 0
        self.velocidad = 0.0
        self.estado = "esperando"
        self.error = None
        self._proc = None
        self._hilo = None
        self._cancelar = threading.Event()
        self._pausado = False
        self._reanudando = False

    def iniciar(self):
        self._hilo = threading.Thread(target=self._run, daemon=True)
        self._hilo.start()
        return self

    def _run(self):
        os.makedirs(self.carpeta, exist_ok=True)
        # 1) resolver el objetivo (zetrrent -> .torrent local)
        try:
            res = resolver(self.url)
        except Exception as e:
            self.estado = "error"
            self.error = str(e)
            self._notificar_error()
            return
        if not res:
            self.estado = "error"
            self.error = "no parece un enlace de torrent"
            self._notificar_error()
            return
        self.nombre = res.get("nombre") or self.nombre
        objetivo = res.get("local") or res.get("torrent_url")
        if not objetivo:
            self.estado = "error"
            self.error = "no se pudo resolver el torrent"
            self._notificar_error()
            return

        # 2) aria2c: descarga P2P con progreso por líneas
        if not disponible():
            self.estado = "error"
            self.error = ("falta aria2c en bin/ (descárgalo o reinstala) "
                          "para bajar torrents")
            self._notificar_error()
            return
        cmd = [
            RUTA_ARIA2,
            "--dir=" + self.carpeta,
            "--seed-time=0",
            "--summary-interval=1",
            "--console-log-level=warn",
            "--file-allocation=none",
            "--check-integrity=true",
            "--bt-stop-timeout=0",
            "--bt-save-metadata=true",
            "--enable-dht=true",
            "--enable-peer-exchange=true",
            "--bt-tracker=udp://tracker.opentrackr.org:1337/announce,"
            "udp://open.demonii.com:1337/announce,"
            "udp://tracker.torrent.eu.org:451/announce,"
            "udp://exodus.desync.com:6969/announce,"
            "udp://open.stealth.si:80/announce,"
            "udp://tracker.pirateparty.gr:6969/announce",
            objetivo,
        ]
        # al reanudar, aria2c continúa con sus archivos .aria2 de control
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            self.estado = "error"
            self.error = "no se pudo lanzar aria2c: %s" % e
            self._notificar_error()
            return

        self.estado = "descargando"
        while True:
            linea = self._proc.stdout.readline()
            if not linea:
                break
            linea = linea.strip()
            if self._cancelar.is_set():
                break
            m = _RE_SUMARIO.search(linea)
            if m:
                self.descargado = _a_bytes(m.group(1), m.group(2))
                self.total = _a_bytes(m.group(3), m.group(4))
                self.velocidad = _a_bytes(m.group(6), m.group(7))
            elif _RE_COMPLETA.search(linea):
                pass  # el resumen ya dejó total = descargado
        codigo = self._proc.wait()
        if self._cancelar.is_set():
            self.estado = "cancelada"
            return
        if self._pausado:
            self.estado = "pausada"
            return
        if codigo == 0:
            self.estado = "completa"
            if self.total is None or self.descargado == 0:
                # sin resumen: intenta el tamaño real del archivo
                try:
                    self._fijar_tamano_final()
                except Exception:
                    pass
            self.descargado = self.total or self.descargado
            try:
                self._detectar_archivo_final()
            except Exception:
                pass
            cb = on_completada
            if cb:
                try:
                    cb(self)
                except Exception:
                    pass
        else:
            self.estado = "error"
            self.error = "aria2c falló (código %s)" % codigo
            self._notificar_error()

    def _detectar_archivo_final(self):
        """Los torrents de juegos suelen traer .rar/.zip multiparte. Deja en
        _archivo_final el comprimido PRINCIPAL del conjunto (part1, el .rar,
        etc.) para que la descompresión automática del servidor lo recoja."""
        import descomprimir
        primarios = []
        for raiz, _dirs, archs in os.walk(self.carpeta):
            for a in archs:
                if a.endswith((".aria2", ".torrent")):
                    continue
                if (descomprimir.es_comprimido(a)
                        and not descomprimir.es_parte_secundaria(a)):
                    try:
                        primarios.append((os.path.getsize(os.path.join(raiz, a)),
                                          os.path.join(raiz, a)))
                    except OSError:
                        pass
        if primarios:
            primarios.sort(key=lambda x: -x[0])
            self._archivo_final = primarios[0][1]

    def _fijar_tamano_final(self):
        """Tras completar, si el progreso no llegó, usa el tamaño real de la
        carpeta para que la barra cierre en 100%."""
        total = 0
        for raiz, _dirs, archs in os.walk(self.carpeta):
            for a in archs:
                if a.endswith((".aria2", ".torrent")):
                    continue
                try:
                    total += os.path.getsize(os.path.join(raiz, a))
                except OSError:
                    pass
        self.total = total or None
        if self.total:
            self.descargado = self.total

    def _notificar_error(self):
        cb = on_error
        if cb:
            try:
                cb(self)
            except Exception:
                pass

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
        self._cancelar.clear()
        self._pausado = False
        self._reanudando = False
        self.error = None
        self.descargado = 0
        self.total = None
        self.velocidad = 0.0
        self.estado = "esperando"
        self.iniciar()

    def _terminar(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def progreso(self):
        return {
            "id": self.id,
            "url": self.url,
            "nombre": self.nombre,
            "estado": self.estado,
            "total": self.total,
            "descargado": self.descargado,
            "velocidad": self.velocidad,
            "eta": None,
            "error": self.error,
            "tipo": "torrent",
            "categoria": "Otros",
            "calidad": None,
            "calidad_real": None,
        }
