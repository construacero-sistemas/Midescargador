# -*- coding: utf-8 -*-
"""
MiDescargador - Motor de descarga Mega.nz.
Mega cifra los archivos de extremo a extremo: la URL trae la clave en el
fragmento (#...). Este módulo:
  1. Llama a la API pública de Mega (g.api.mega.co.nz) para obtener el enlace
     del archivo, su tamaño y el nombre (cifrado en 'at').
  2. Deriva clave/iv/mac del fragmento (protocolo documentado del cliente web).
  3. Descarga el archivo cifrado y lo DESCIFRA en streaming (AES-CTR con el
     contador continuo, igual que el cliente oficial), con progreso,
     pausa/reanudar/cancelar y organización por tipo.

Interfaz igual al motor segmentado (motor.py): iniciar(), pausar(), reanudar(),
cancelar(), reintentar(), progreso(), y callbacks on_error / on_completada.
"""

import base64
import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
import urllib.error

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    Cipher = algorithms = modes = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_CTX = ssl.create_default_context()
TIMEOUT = 60
CHUNK = 1024 * 1024          # 1 MB por lectura de red

# callbacks globales (los asigna servidor.py, igual que en motor.py)
on_error = None
on_completada = None

SOPORTADOS = ("mega.nz", "mega.co.nz", "mega.io")


# ------------------------------------------------------------ utilidades

def _b64d(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def _aes_ecb_enc(bloque, clave):
    c = Cipher(algorithms.AES(clave), modes.ECB())
    e = c.encryptor()
    return e.update(bloque) + e.finalize()


def _aes_ecb_dec(bloque, clave):
    c = Cipher(algorithms.AES(clave), modes.ECB())
    d = c.decryptor()
    return d.update(bloque) + d.finalize()


def _a32_to_bytes(a32):
    """Lista de ints de 32 bits -> bytes big-endian."""
    out = b""
    for n in a32:
        out += (n & 0xFFFFFFFF).to_bytes(4, "big")
    return out


def _bytes_to_a32(b):
    """Bytes -> lista de ints de 32 bits (big-endian)."""
    return [int.from_bytes(b[i:i + 4], "big")
            for i in range(0, len(b) - len(b) % 4, 4)]


def _nombre_desde_at(at_b64, clave_k):
    """El atributo 'at' trae el nombre del archivo (JSON cifrado con AES-CBC,
    IV en ceros, y prefijo 'MEGA'; la clave es la del archivo k)."""
    try:
        datos = _b64d(at_b64)
        c = Cipher(algorithms.AES(_a32_to_bytes(clave_k)), modes.CBC(b"\x00" * 16))
        d = c.decryptor()
        plano = (d.update(datos) + d.finalize()).rstrip(b"\x00")
        texto = plano.decode("utf-8", "replace")
        if texto.startswith("MEGA"):
            texto = texto[4:]
        attrs = json.loads(texto)
        return attrs.get("n") or None
    except Exception:
        return None


def _chunk_sizes(total):
    """Tamaños de los chunks según el esquema de Mega (primera parte pequeña,
    luego 128 KB * 2^n). Devuelve lista de (inicio, fin) inclusive."""
    CHUNKS_PEQUE = 8
    P = 0x20000 - 0x10          # 131056: tamaño de los chunks pequeños
    pos = 0
    i = 1
    lista = []
    if total < P * CHUNKS_PEQUE + 0x100000:
        paso = P if total > P else total
        while pos < total:
            lista.append((pos, min(pos + paso, total) - 1))
            pos += paso
        return lista
    # parte A: 8 chunks de 131056
    for _ in range(CHUNKS_PEQUE):
        lista.append((pos, pos + P - 1))
        pos += P
    # parte B: chunks crecientes (128 KiB * 2^i)
    while pos < total:
        tam = 0x20000 * i
        lista.append((pos, min(pos + tam, total) - 1))
        pos += tam
        i *= 2
    return lista


# ------------------------------------------------------------ resolución

def _api(payload):
    """POST a la API de Mega. payload es una lista de comandos."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://g.api.mega.co.nz/cs?id=1",
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    r = urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX)
    return json.loads(r.read().decode("utf-8", "replace"))


def resolver(url):
    """Convierte un enlace mega.nz en un dict con la información del archivo:
    {url: enlace de descarga cifrada, nombre, tamano, clave, iv, mac}.
    Devuelve None si la URL no es de mega."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in SOPORTADOS):
        return None
    m = re.search(r"/file/([A-Za-z0-9_-]{8})(?:#([A-Za-z0-9_-]+))?", url)
    if not m:
        raise RuntimeError("enlace mega.nz no válido (falta /file/ID#clave)")
    fid = m.group(1)
    frag = m.group(2)
    if not frag:
        raise RuntimeError("el enlace mega.nz no trae la clave (#...)")

    # clave del fragmento: 8 ints de 32 bits. k = los 4 primeros XOR con los
    # dos siguientes; iv = los 2 del medio (con ceros al final); mac = últimos 2
    try:
        clave_cifrada = _bytes_to_a32(_b64d(frag))
        if len(clave_cifrada) < 8:
            raise ValueError("fragmento demasiado corto")
        k = (clave_cifrada[0] ^ clave_cifrada[4],
             clave_cifrada[1] ^ clave_cifrada[5],
             clave_cifrada[2] ^ clave_cifrada[6],
             clave_cifrada[3] ^ clave_cifrada[7])
        iv = (clave_cifrada[4], clave_cifrada[5], 0, 0)
        meta_mac = (clave_cifrada[6], clave_cifrada[7])
    except Exception as e:
        raise RuntimeError(f"clave mega.nz no válida: {e}") from e

    # pedir el enlace de descarga a la API
    try:
        resp = _api([{"a": "g", "g": 1, "p": fid}])
    except Exception as e:
        raise RuntimeError(f"Mega rechazó la consulta: {e}") from e
    if not resp or not isinstance(resp[0], dict) or "g" not in resp[0]:
        raise RuntimeError("Mega: archivo no disponible o enlace caducado "
                           "(¿eliminado? Respuesta: %r)" % (resp,))
    d = resp[0]
    nombre = _nombre_desde_at(d.get("at") or "", k)
    if not nombre:
        nombre = "mega_" + fid
    return {
        "url": d["g"],
        "nombre": nombre,
        "tamano": d.get("s"),
        "clave": k,          # 4 ints de 32 bits
        "iv": iv,            # (iv0, iv1, 0, 0)
        "mac": meta_mac,
    }


# ------------------------------------------------------------ Descarga

LIMITE_GLOBAL_BPS = 0  # límite global en bytes/s (0 = sin límite); lo setea el servidor


class Descarga:
    """Descarga un archivo Mega descifrándolo en streaming."""

    def __init__(self, info, carpeta, segmentos=8, limite_bps=0):
        self.info = info or {}
        self.url = self.info.get("url") or ""
        self.carpeta = os.path.abspath(carpeta)
        self.nombre = self.info.get("nombre") or "descarga"
        self.total = self.info.get("tamano")
        self.descargado = 0
        self.velocidad = 0.0
        self.estado = "esperando"
        self.error = None
        self.id = None
        self.pagina = None
        self.segmentos_max = max(1, min(int(segmentos), 32))
        self.limite_bps = max(0, int(limite_bps or 0))
        self._throttle_lock = threading.Lock()
        self._t0 = None
        self._acum = 0.0

        self._lock = threading.Lock()
        self._pausa = threading.Event()
        self._cancelar = threading.Event()
        self._muestras = []
        self._hilo = None
        self._reanudando = False

    # -------------------------------------------------------- públicas

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
        self._reanudando = True
        self.iniciar()

    def cancelar(self):
        self._cancelar.set()
        self._pausa.clear()
        with self._lock:
            self.estado = "cancelada"

    def reintentar(self):
        self._cancelar.clear()
        self._pausa.clear()
        with self._lock:
            self.error = None
            self.descargado = 0
            self.estado = "esperando"
        self._reanudando = False
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
        if vel and total and estado == "descargando":
            eta = int((total - desc) / vel)
        return {
            "id": self.id,
            "url": self.pagina or self.url,
            "nombre": nombre,
            "estado": estado,
            "total": total,
            "descargado": desc,
            "velocidad": vel,
            "eta": eta,
            "error": error,
            "tipo": "mega",
            "carpeta": self.carpeta,
        }

    # -------------------------------------------------------- internas

    def _limpiar_part(self):
        try:
            part = os.path.join(self.carpeta, self.nombre + ".part")
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass

    def _marcar_error(self, mensaje):
        with self._lock:
            self.estado = "error"
            self.error = mensaje
        self._limpiar_part()
        cb = on_error
        if cb:
            try:
                cb(self)
            except Exception:
                pass

    def _marcar_completada(self):
        with self._lock:
            self.estado = "completa"
        self._limpiar_part()
        cb = on_completada
        if cb:
            try:
                cb(self)
            except Exception:
                pass

    def _limitar(self, n):
        """Igual que el throttle del motor principal: limita la velocidad
        TOTAL de esta descarga a limite_bps o al límite global."""
        lim = self.limite_bps if self.limite_bps else LIMITE_GLOBAL_BPS
        if not lim or lim <= 0:
            return
        with self._throttle_lock:
            ahora = time.time()
            if self._t0 is None:
                self._t0 = ahora
                self._acum = 0.0
            self._acum += n
            restante = (self._acum / lim) - (ahora - self._t0)
            if restante <= 0:
                return
            fin = time.time() + restante
            while True:
                if self._cancelar.is_set() or self._pausa.is_set():
                    return
                falta = fin - time.time()
                if falta <= 0:
                    return
                time.sleep(min(falta, 0.2))

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

    def _principal(self):
        if Cipher is None:
            self._marcar_error("Mega necesita la librería 'cryptography'")
            return
        if not self.info.get("clave"):
            self._marcar_error("Mega: falta la clave de descifrado")
            return
        if self._cancelar.is_set():
            return
        try:
            self._descargar_y_descifrar()
        except Exception as e:
            self._marcar_error(str(e))
        finally:
            self._pausa.clear()

    def _descargar_y_descifrar(self):
        os.makedirs(self.carpeta, exist_ok=True)
        destino = os.path.join(self.carpeta, self.nombre)
        part = destino + ".part"

        # Al reanudar tras pausa no se puede continuar a mitad de chunk
        # (el keystream es continuo): limpiamos y volvemos a empezar.
        if self._reanudando and os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass

        req = urllib.request.Request(self.url, headers={"User-Agent": UA,
                                                       "Referer": "https://mega.nz/"})
        # Mega limita el ancho de banda por nodo (HTTP 509): reintenta con
        # espera progresiva hasta que el nodo libere cuota
        r = None
        for intento in range(5):
            if self._cancelar.is_set():
                return
            try:
                r = urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX)
                break
            except urllib.error.HTTPError as e:
                if e.code == 509 and intento < 4:
                    time.sleep(15 * (intento + 1))
                    continue
                raise RuntimeError(
                    f"Mega: el enlace de descarga falló (HTTP {e.code})")
        if r is None:
            raise RuntimeError("Mega: el enlace de descarga no respondió")

        # AES-CTR continuo, igual que el cliente oficial:
        # counter = ((iv0 << 32) + iv1) << 64  (los 64 bits bajos van en 0)
        k = self.info["clave"]
        iv = self.info["iv"]
        ctr_inicial = ((iv[0] << 32) + iv[1]) << 64
        c = Cipher(algorithms.AES(_a32_to_bytes(k)),
                   modes.CTR(ctr_inicial.to_bytes(16, "big")))
        enc = c.encryptor()

        with self._lock:
            self.estado = "descargando"
        self._sumar(0)
        with open(destino, "wb") as f, open(part, "wb") as pf:
            while True:
                if self._cancelar.is_set():
                    with self._lock:
                        self.estado = "cancelada"
                    return
                if self._pausa.is_set():
                    self._pausa.wait(0.25)
                    continue
                cif = r.read(CHUNK)
                if not cif:
                    break
                plano = enc.update(cif)
                f.write(plano)
                pf.write(cif)
                self._sumar(len(cif))
                self._limitar(len(cif))
        if self._cancelar.is_set():
            with self._lock:
                self.estado = "cancelada"
            return
        # verificación de integridad: el archivo debe tener el tamaño esperado
        tam_final = os.path.getsize(destino)
        if self.total and tam_final != self.total:
            raise RuntimeError(
                f"Mega: tamaño del archivo descifrado incorrecto "
                f"({tam_final} != {self.total})")
        try:
            os.remove(part)
        except OSError:
            pass
        self._marcar_completada()
