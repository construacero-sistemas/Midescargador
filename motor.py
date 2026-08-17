# -*- coding: utf-8 -*-
"""
MiDescargador - Motor de descargas segmentadas.
Descarga un archivo en N conexiones paralelas usando cabeceras HTTP "Range",
con reanudación por segmento, pausa/reanudar/cancelar, y sin dependencias
externas (solo la biblioteca estándar de Python).

Uso:
    python motor.py --selftest          # prueba interna (servidor local con Range)
    python motor.py URL [--segmentos N] [--carpeta DIR]
"""

import os
import re
import sys
import ssl
import time
import threading
import urllib.request
import urllib.error
import urllib.parse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CHUNK = 256 * 1024          # 256 KB por lectura
MAX_INTENTOS = 6            # reintentos por segmento con backoff
TIMEOUT = 30

_CTX = ssl.create_default_context()

# callbacks globales
on_error = None        # on_error(descarga) cuando entra en error
on_completada = None  # on_completada(descarga) cuando termina bien


# ---------------------------------------------------------------- utilidades

def _sanitizar(nombre):
    nombre = re.sub(r'[\\/:*?"<>|\r\n]', "_", nombre).strip().strip(".")
    return nombre or "descarga"


def _nombre_desde_url(url):
    p = urllib.parse.urlparse(url)
    nombre = os.path.basename(urllib.parse.unquote(p.path))
    return _sanitizar(nombre) or "descarga"


def _pedir(url, rango=None, metodo="GET", datos=None, cookies=None):
    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Referer": url,
        "Connection": "keep-alive",
    }
    if datos is not None:
        # algunos CDN (p. ej. MediaFire con archivos "sospechosos") solo
        # sirven el archivo con un POST que lleva una clave (pass=...).
        # Esos POST aceptan Range igual que un GET normal.
        metodo = "POST"
        h["Content-Type"] = "application/x-www-form-urlencoded"
    if cookies:
        # hosters que exigen la sesión del navegador (rapidshare.co, etc.)
        h["Cookie"] = cookies
    req = urllib.request.Request(url, headers=h, method=metodo, data=datos)
    if rango:
        req.add_header("Range", rango)
    return urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX)


def _leer_cr(contenido):
    """Extrae 'bytes 0-0/12345' de una cabecera Content-Range."""
    m = re.search(r"bytes (\d+)-(\d+)/(\d+)", contenido or "")
    if m:
        return int(m.group(3))
    return None


# ---------------------------------------------------------------- limpieza
# Depuración INTELIGENTE de fragmentos temporales:
#  - se borran al instante las partes de descargas canceladas/borradas/erróneas
#  - al arrancar se borran las partes huérfanas de más de 24 h (de sesiones
#    anteriores que quedaron a medias)
#  - se conservan las de descargas activas o pausadas (para poder reanudar)

import tempfile as _tempfile

_PARTES_DIR = os.path.join(_tempfile.gettempdir(), "MiDescargador", ".partes")
_ACTIVAS = set()          # nombres de descargas con partes en uso
_ACTIVAS_LOCK = threading.Lock()
_MAX_HUERFANAS_H = 24     # edad máxima de partes huérfanas (horas)


def _registrar_parts(nombre):
    """Marca una descarga como dueña de sus partes (para no depurarlas)."""
    if not nombre:
        return
    with _ACTIVAS_LOCK:
        _ACTIVAS.add(nombre)


def _liberar_parts(nombre):
    """Una descarga terminó/canceló: sus partes ya no se necesitan."""
    if not nombre:
        return
    with _ACTIVAS_LOCK:
        _ACTIVAS.discard(nombre)
    borrar_parts_de(nombre)


def borrar_parts_de(nombre):
    """Borra los fragmentos .partN de una descarga concreta."""
    if not nombre:
        return
    try:
        if not os.path.isdir(_PARTES_DIR):
            return
        base = nombre
        for f in os.listdir(_PARTES_DIR):
            if f.startswith(base + ".part") or f.startswith(base + "-"):
                try:
                    os.remove(os.path.join(_PARTES_DIR, f))
                except OSError:
                    pass
    except Exception:
        pass


def depurar():
    """Depuración en caliente: borra partes de descargas que ya no existen
    (canceladas/borradas/erróneas) y las huérfanas de más de 24 h.
    """
    try:
        if not os.path.isdir(_PARTES_DIR):
            return
        activas = set()
        with _ACTIVAS_LOCK:
            activas = set(_ACTIVAS)
        ahora = time.time()
        for f in os.listdir(_PARTES_DIR):
            if not (f.endswith(".part") or ".part" in f):
                continue
            # pertenece a alguna descarga activa?
            es_activa = any(f.startswith(n + ".part") for n in activas)
            if es_activa:
                continue
            ruta = os.path.join(_PARTES_DIR, f)
            try:
                edad = ahora - os.path.getmtime(ruta)
            except OSError:
                continue
            # huérfana reciente (menos de 24 h) puede ser de una descarga
            # que está arrancando; se conserva. Las viejas se borran.
            if edad > _MAX_HUERFANAS_H * 3600:
                try:
                    os.remove(ruta)
                except OSError:
                    pass
    except Exception:
        pass


def limpiar_restos():
    """Limpieza de arranque: carpetas .partes de la ubicación antigua,
    registro de partes activas vacío (el servidor se reinició) y huérfanas
    de más de 24 h del temp.
    """
    # 1) .partes en carpetas de descargas (ubicación antigua)
    base = os.path.join(os.path.expanduser("~"), "Downloads", "MiDescargador")
    for carpeta in (base, os.path.expanduser("~/Desktop"),
                    os.path.expanduser("~/Documents")):
        try:
            p = os.path.join(carpeta, ".partes")
            if os.path.isdir(p):
                import shutil
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
    # 2) huérfanas viejas del temp (no borramos todo: si hay una descarga
    #    pausada que sobrevivió al reinicio, la reanudación la puede retomar)
    with _ACTIVAS_LOCK:
        _ACTIVAS.clear()
    depurar()


# ---------------------------------------------------------------- Descarga

class Descarga:
    """
    Una descarga. Estados: esperando, descargando, pausada, uniendo,
    completa, cancelada, error.
    """

    def __init__(self, url, carpeta, segmentos=8, nombre=None, total=None,
                 post=None, cookies=None, unico=None):
        self.url = url
        self.carpeta = os.path.abspath(carpeta)
        self.segmentos_max = max(1, min(int(segmentos), 32))
        self.nombre = _sanitizar(nombre) if nombre else None
        self.id = None
        # cuerpo form-urlencoded para descargas que exigen POST
        # (hosters como MediaFire con archivos marcados como sospechosos)
        self.post = post
        # cookies de sesión (hosters como rapidshare.co: el enlace de
        # descarga exige la cookie que se obtuvo al resolverlo)
        self.cookies = cookies
        # unico=True: forzado por el resolver (p. ej. rapidshare.co, cuyo
        # enlace de descarga es de UN SOLO USO: el probe lo consumiría y
        # la descarga fallaría con 401). Salta el probe y descarga en una
        # sola conexión.
        self._forzar_unico = bool(unico)

        self.total = total         # tamaño total en bytes (None = desconocido)
        self.descargado = 0
        self.velocidad = 0.0       # bytes por segundo
        self.estado = "esperando"
        self.error = None
        self.segmentos = []        # [{i, inicio, fin, hecho}]
        # los fragmentos van a la carpeta TEMPORAL del sistema (no a la de
        # descargas): se limpian solos con el tiempo y no ensucian la carpeta
        import tempfile
        base_tmp = os.path.join(tempfile.gettempdir(), "MiDescargador")
        os.makedirs(base_tmp, exist_ok=True)
        self.parts_dir = os.path.join(base_tmp, ".partes")

        self._lock = threading.Lock()
        self._pausa = threading.Event()
        self._cancelar = threading.Event()
        self._unico = False        # servidor ignora rangos -> descarga entera
        self._muestras = []        # [(tiempo, bytes_acumulados)]
        self._hilo = None

    # ------------------------------------------------------------ públicas

    def iniciar(self):
        self._hilo = threading.Thread(target=self._principal, daemon=True)
        self._hilo.start()
        return self

    def pausar(self):
        with self._lock:
            if self.estado in ("descargando", "esperando"):
                self.estado = "pausada"
        self._pausa.set()

    def reanudar(self):
        if not self._pausa.is_set():
            return
        self._pausa.clear()
        with self._lock:
            if self.estado == "pausada":
                self.estado = "descargando"

    def cancelar(self):
        self._cancelar.set()
        self._pausa.clear()
        with self._lock:
            self.estado = "cancelada"
        _liberar_parts(self.nombre)

    def reintentar(self):
        """Vuelve a lanzar la descarga desde cero (tras cancelar/error)."""
        self._cancelar.clear()
        self._pausa.clear()
        with self._lock:
            self.error = None
            self.descargado = 0
            self.estado = "esperando"
        _registrar_parts(self.nombre)
        self.iniciar()

    def progreso(self):
        with self._lock:
            total = self.total
            desc = self.descargado
            vel = self.velocidad
            estado = self.estado
            error = self.error
            nombre = self.nombre
        eta = None
        if vel and total and estado in ("descargando",):
            restante = (total - desc) / vel
            eta = int(restante)
        return {
            "id": self.id,
            "url": self.url,
            "nombre": nombre,
            "estado": estado,
            "total": total,
            "descargado": desc,
            "velocidad": vel,
            "eta": eta,
            "error": error,
            "tipo": "directa",
            "carpeta": self.carpeta,
        }

    # ------------------------------------------------------------ internas

    def _marcar_error(self, mensaje):
        with self._lock:
            self.estado = "error"
            self.error = mensaje
        _liberar_parts(self.nombre)   # sus fragmentos ya no sirven
        cb = on_error
        if cb:
            try:
                cb(self)
            except Exception:
                pass

    def _marcar_completada(self):
        with self._lock:
            self.estado = "completa"
        _liberar_parts(self.nombre)
        cb = on_completada
        if cb:
            try:
                cb(self)
            except Exception:
                pass

    def _sumar(self, n):
        with self._lock:
            self.descargado += n
        ahora = time.time()
        with self._lock:
            self._muestras.append((ahora, self.descargado))
            while self._muestras and ahora - self._muestras[0][0] > 2.0:
                self._muestras.pop(0)
            if len(self._muestras) >= 2:
                t0, b0 = self._muestras[0]
                dt = ahora - t0
                if dt > 0:
                    self.velocidad = (self.descargado - b0) / dt

    def _ruta_part(self, i):
        return os.path.join(self.parts_dir, f"{self.nombre}.part{i}")

    def _probar(self):
        """Descubre tamaño y si el servidor soporta rangos."""
        if self._forzar_unico:
            # enlace de un solo uso (rapidshare.co): cualquier petición
            # previa lo consume y la descarga real fallaría con 401
            self._unico = True
            return
        datos = self.post.encode("utf-8") if self.post else None
        # 1) HEAD (se salta si ya conocemos el tamaño o si la descarga
        #    exige POST, que no admite HEAD)
        if self.total is None and datos is None:
            try:
                with _pedir(self.url, metodo="HEAD", cookies=self.cookies) as r:
                    cl = r.headers.get("Content-Length")
                    if cl and cl.isdigit():
                        self.total = int(cl)
                    self._nombre_desde_cabeceras(r)
            except Exception:
                pass
        # 2) confirmar rangos con una petición mínima (siempre: así también
        #    se descubre el tamaño en servidores que no responden HEAD)
        try:
            with _pedir(self.url, rango="bytes=0-0", datos=datos,
                        cookies=self.cookies) as r:
                if r.status == 206:
                    total = _leer_cr(r.headers.get("Content-Range"))
                    if total is not None:
                        self.total = total
                else:
                    self.total = None   # responde 200 -> no respeta rangos
        except Exception:
            self.total = None
        if self.total is None:
            self._unico = True

    def _nombre_desde_cabeceras(self, r):
        if self.nombre:
            return
        cd = r.headers.get("Content-Disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
        if m:
            self.nombre = _sanitizar(urllib.parse.unquote(m.group(1)))
        else:
            self.nombre = _nombre_desde_url(self.url)

    def _nombre_desde_query(self):
        """Algunos servidores (Cloudflare R2 firmado) ponen el nombre real en
        el parámetro response-content-disposition de la query, mientras que el
        path trae un identificador (ej. 1786499570230-PRSNSCMNS18720 ZL.rar).
        Preferimos ese nombre limpio para el archivo guardado.
        """
        if self.nombre:
            return
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.url).query)
        cd = (q.get("response-content-disposition") or [""])[0]
        m = re.search(r"filename\*?=(?:UTF-8''|\"?)([^\";]+)", cd, re.I)
        if m:
            self.nombre = _sanitizar(urllib.parse.unquote(m.group(1)))

    def _principal(self):
        try:
            self._probar()
            self._nombre_desde_query()
            if not self.nombre:
                self.nombre = _nombre_desde_url(self.url)
            if self._cancelar.is_set():
                return
            _registrar_parts(self.nombre)
            if self._unico:
                self._descarga_unica()
            else:
                self._descarga_segmentada()
        except Exception as e:
            self._marcar_error(str(e))
        finally:
            self._pausa.clear()

    def _descarga_unica(self):
        """Servidor sin soporte de rangos: una sola conexión, con reanudación."""
        os.makedirs(self.carpeta, exist_ok=True)
        destino = os.path.join(self.carpeta, self.nombre)
        inicio = os.path.getsize(destino) if os.path.exists(destino) else 0
        self._sumar(0)
        with self._lock:
            self.estado = "descargando"
        intento = 0
        datos = self.post.encode("utf-8") if self.post else None
        while True:
            if self._cancelar.is_set():
                with self._lock:
                    self.estado = "cancelada"
                return
            if self._pausa.is_set():
                self._pausa.wait(0.25)
                continue
            try:
                rango = f"bytes={inicio}-" if inicio else None
                with _pedir(self.url, rango=rango, datos=datos,
                            cookies=self.cookies) as r:
                    if r.status == 206:
                        total = _leer_cr(r.headers.get("Content-Range"))
                        if total is not None:
                            self.total = total
                    elif self.total is None:
                        cl = r.headers.get("Content-Length")
                        if cl and cl.isdigit():
                            self.total = int(cl)
                    modo = "ab" if (inicio and r.status == 206) else "wb"
                    with open(destino, modo) as f:
                        while True:
                            if self._cancelar.is_set():
                                with self._lock:
                                    self.estado = "cancelada"
                                return
                            if self._pausa.is_set():
                                self._pausa.wait(0.25)
                                continue
                            bloque = r.read(CHUNK)
                            if not bloque:
                                break
                            f.write(bloque)
                            inicio += len(bloque)
                            self._sumar(len(bloque))
                self._marcar_completada()
                return
            except (urllib.error.URLError, urllib.error.HTTPError,
                    ConnectionError, TimeoutError, OSError) as e:
                intento += 1
                if intento > MAX_INTENTOS or self._cancelar.is_set():
                    self._marcar_error(f"sin rangos: {e}")
                    return
                time.sleep(min(2 ** intento, 15))

    def _descarga_segmentada(self):
        n = self.segmentos_max
        if self.total and self.total < 1024 * 1024:
            n = 1
        tam = (self.total // n) if n else 0
        self.segmentos = []
        for i in range(n):
            ini = i * tam
            fin = self.total - 1 if i == n - 1 else (i + 1) * tam - 1
            if ini > fin:
                continue
            part = self._ruta_part(i)
            hecho = os.path.getsize(part) if os.path.exists(part) else 0
            self.segmentos.append({"i": i, "inicio": ini, "fin": fin, "hecho": hecho})
        os.makedirs(self.parts_dir, exist_ok=True)
        self._sumar(0)
        with self._lock:
            self.estado = "descargando"

        hilos = [threading.Thread(target=self._segmento, args=(s,), daemon=True)
                 for s in self.segmentos]
        for t in hilos:
            t.start()
        for t in hilos:
            t.join()

        if self._cancelar.is_set():
            with self._lock:
                self.estado = "cancelada"
            return
        if self._unico:
            return  # otro hilo se encargó de la descarga entera
        fallidos = [s for s in self.segmentos
                    if s["hecho"] < s["fin"] - s["inicio"] + 1]
        if fallidos:
            self._marcar_error(f"{len(fallidos)} segmento(s) no completados")
            return
        self._unir()

    def _segmento(self, seg):
        i, ini, fin = seg["i"], seg["inicio"], seg["fin"]
        part = self._ruta_part(i)
        total_seg = fin - ini + 1
        intento = 0
        while True:
            if self._cancelar.is_set() or self._unico:
                return
            if self._pausa.is_set():
                self._pausa.wait(0.25)
                continue
            if seg["hecho"] >= total_seg:
                return
            inicio_actual = ini + seg["hecho"]
            try:
                datos = self.post.encode("utf-8") if self.post else None
                with _pedir(self.url, rango=f"bytes={inicio_actual}-{fin}",
                            datos=datos, cookies=self.cookies) as r:
                    if r.status == 206:
                        os.makedirs(os.path.dirname(part), exist_ok=True)
                        with open(part, "ab") as f:
                            while True:
                                if self._cancelar.is_set() or self._unico:
                                    return
                                if self._pausa.is_set():
                                    self._pausa.wait(0.25)
                                    continue
                                bloque = r.read(CHUNK)
                                if not bloque:
                                    break
                                f.write(bloque)
                                seg["hecho"] += len(bloque)
                                self._sumar(len(bloque))
                    elif r.status == 200:
                        # el servidor ignoró el Range: toda la descarga en uno
                        with self._lock:
                            self._unico = True
                        self._descarga_entera_desde(r, part, i)
                        return
                    else:
                        raise urllib.error.HTTPError(
                            self.url, r.status, "status inesperado",
                            r.headers, None)
            except (urllib.error.HTTPError, urllib.error.URLError,
                    ConnectionError, TimeoutError, OSError) as e:
                if isinstance(e, urllib.error.HTTPError) and e.code == 416:
                    # rango insatisfecho: el archivo cambió o ya estamos completos
                    if seg["hecho"] >= total_seg:
                        return
                    seg["hecho"] = 0
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                intento += 1
                if intento > MAX_INTENTOS or self._cancelar.is_set():
                    return
                time.sleep(min(2 ** intento, 15))

    def _descarga_entera_desde(self, r, part, i):
        """Servidor respondió 200 a un Range: escribir el cuerpo completo."""
        os.makedirs(os.path.dirname(part), exist_ok=True)
        try:
            with open(part, "wb") as f:
                while True:
                    if self._cancelar.is_set():
                        return
                    bloque = r.read(CHUNK)
                    if not bloque:
                        break
                    f.write(bloque)
                    self._sumar(len(bloque))
            self.segmentos = [{"i": i, "inicio": 0, "fin": 0, "hecho": 1}]
            with self._lock:
                self.total = os.path.getsize(part)
                self.estado = "uniendo"
            self._unir(part_unico=part)
        except Exception:
            pass

    def _unir(self, part_unico=None):
        with self._lock:
            self.estado = "uniendo"
        destino = os.path.join(self.carpeta, self.nombre)
        os.makedirs(self.carpeta, exist_ok=True)
        if part_unico:
            if os.path.exists(destino):
                os.remove(destino)
            os.replace(part_unico, destino)
        else:
            with open(destino, "wb") as f:
                for seg in sorted(self.segmentos, key=lambda s: s["i"]):
                    part = self._ruta_part(seg["i"])
                    with open(part, "rb") as p:
                        while True:
                            bloque = p.read(CHUNK)
                            if not bloque:
                                break
                            f.write(bloque)
        # limpiar partes (quedan en la carpeta temporal)
        for seg in self.segmentos:
            part = self._ruta_part(seg["i"])
            try:
                os.remove(part)
            except OSError:
                pass
        try:
            os.rmdir(self.parts_dir)
        except OSError:
            pass
        with self._lock:
            if self.total:
                self.descargado = self.total
        self._marcar_completada()


# ------------------------------------------------------------------ auto-test

def _selftest():
    """Descarga un archivo generado localmente en 4 conexiones y verifica SHA."""
    import hashlib
    import http.server
    import tempfile
    import random

    tmp = tempfile.mkdtemp(prefix="midesc-")
    origen = os.path.join(tmp, "prueba.bin")
    tam = 3 * 1024 * 1024 + 12345   # tamaño no redondo a propósito
    with open(origen, "wb") as f:
        f.write(bytes(random.getrandbits(8) for _ in range(tam)))

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            rango = self.headers.get("Range")
            try:
                size = os.path.getsize(origen)
                if rango:
                    m = re.match(r"bytes=(\d+)-(\d*)", rango)
                    inicio = int(m.group(1))
                    fin = int(m.group(2)) if m.group(2) else size - 1
                    fin = min(fin, size - 1)
                    cuerpo = b""
                    with open(origen, "rb") as f:
                        f.seek(inicio)
                        cuerpo = f.read(fin - inicio + 1)
                    self.send_response(206)
                    self.send_header("Content-Range",
                                     f"bytes {inicio}-{fin}/{size}")
                else:
                    with open(origen, "rb") as f:
                        cuerpo = f.read()
                    self.send_response(200)
                self.send_header("Content-Length", str(len(cuerpo)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(cuerpo)
            except Exception:
                self.send_error(500)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    puerto = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    carpeta = os.path.join(tmp, "out")
    url = f"http://127.0.0.1:{puerto}/prueba.bin"
    d = Descarga(url, carpeta, segmentos=4)
    d.iniciar()
    while d.estado in ("esperando", "descargando", "uniendo"):
        time.sleep(0.05)
    srv.shutdown()

    sha1 = hashlib.sha1()
    sha2 = hashlib.sha1()
    with open(origen, "rb") as f:
        sha1.update(f.read())
    with open(os.path.join(carpeta, "prueba.bin"), "rb") as f:
        sha2.update(f.read())
    ok = (d.estado == "completa" and sha1.hexdigest() == sha2.hexdigest()
          and d.descargado == tam)
    print(f"[selftest] estado={d.estado} descargado={d.descargado}/{tam} "
          f"sha_ok={sha1.hexdigest() == sha2.hexdigest()}")
    print("[selftest] " + ("OK ✅" if ok else "FALLO ❌"))
    return ok


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    url = sys.argv[1]
    seg = 8
    carpeta = os.path.join(os.path.expanduser("~"), "Downloads", "MiDescargador")
    if "--segmentos" in sys.argv:
        seg = int(sys.argv[sys.argv.index("--segmentos") + 1])
    if "--carpeta" in sys.argv:
        carpeta = sys.argv[sys.argv.index("--carpeta") + 1]
    d = Descarga(url, carpeta, segmentos=seg)
    d.iniciar()
    while d.estado not in ("completa", "error", "cancelada"):
        p = d.progreso()
        vel = p["velocidad"] / 1024
        print(f"\r{p['estado']:>12} {p['descargado']/1048576:.1f} MB "
              f"{vel:6.1f} KB/s      ", end="", flush=True)
        time.sleep(0.3)
    print("\n" + str(d.progreso()))
