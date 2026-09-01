# -*- coding: utf-8 -*-
"""Smoke E2E de extremo a extremo, 100% local y sin internet.

1. Levanta un servidor de archivos HTTP mínimo (con soporte de Range) en un
   puerto efímero.
2. Arranca el Manejador real de servidor.py en otro puerto efímero.
3. POST /api/descargar con la URL del archivo local.
4. Consulta /api/estado hasta que la tarea quede "completada" (o error).
5. Verifica que el archivo bajado es byte a byte idéntico al original.

Uso:  python scripts/e2e_smoke.py
Sale con código 0 si todo el pipeline funciona; != 0 si algo falla.
"""
import hashlib
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

import servidor   # noqa: E402  (importar NO arranca el servidor: eso lo hace __main__)


# ---- servidor de archivos mínimo con soporte Range -------------------------
class ArchivosHandler(BaseHTTPRequestHandler):
    CONTENIDO = None   # se asigna en main()

    def log_message(self, *a):
        pass

    def _enviar(self, inicio, fin):
        cuerpo = self.CONTENIDO[inicio:fin + 1]
        self.send_response(206 if (inicio, fin) != (0, len(self.CONTENIDO) - 1) else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.CONTENIDO)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        tam = len(self.CONTENIDO)
        rango = self.headers.get("Range")
        if rango:
            m = re.match(r"bytes=(\d+)-(\d*)$", rango.strip())
            if not m:
                self.send_response(416)
                self.end_headers()
                return
            inicio = int(m.group(1))
            fin = int(m.group(2)) if m.group(2) else tam - 1
            self._enviar(inicio, min(fin, tam - 1))
        else:
            self._enviar(0, tam - 1)


def main():
    # archivo de prueba con contenido pseudoaleatorio determinista (~2 MB,
    # suficiente para partirse en varios segmentos)
    tam = 2 * 1024 * 1024
    ArchivosHandler.CONTENIDO = bytes((i * 7 + 13) % 251 for i in range(tam))
    sha_original = hashlib.sha256(ArchivosHandler.CONTENIDO).hexdigest()

    archivos = ThreadingHTTPServer(("127.0.0.1", 0), ArchivosHandler)
    puerto_archivos = archivos.server_address[1]
    threading.Thread(target=archivos.serve_forever, daemon=True).start()

    api = ThreadingHTTPServer(("127.0.0.1", 0), servidor.Manejador)
    puerto_api = api.server_address[1]
    threading.Thread(target=api.serve_forever, daemon=True).start()

    url = "http://127.0.0.1:%d/archivo-prueba.bin" % puerto_archivos
    destino = tempfile.mkdtemp(prefix="mdm_e2e_")

    def pedir(metodo, ruta, datos=None):
        c = http.client.HTTPConnection("127.0.0.1", puerto_api, timeout=10)
        try:
            cuerpo = json.dumps(datos).encode() if datos is not None else None
            c.request(metodo, ruta, body=cuerpo, headers={
                "Content-Type": "application/json",
                "X-MiDescargador-Token": servidor.TOKEN_API})
            r = c.getresponse()
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
        finally:
            c.close()

    print("== POST /api/descargar ==")
    estado, resp = pedir("POST", "/api/descargar", {"url": url, "carpeta": destino})
    if estado != 200 or not resp.get("id"):
        print("FALLO al encolar: %s %s" % (estado, resp))
        return 1
    tid = resp["id"]
    print("   tarea %s encolada" % tid)

    print("== esperando completada (máx 60 s) ==")
    final = None
    fin_espera = time.time() + 60
    while time.time() < fin_espera:
        _, lista = pedir("GET", "/api/estado")
        tarea = next((t for t in lista if t.get("id") == tid), None)
        if tarea is None:
            print("FALLO: la tarea desapareció del estado")
            return 1
        est = tarea.get("estado")
        if est in ("completa", "completada"):
            final = tarea
            break
        if est == "error":
            print("FALLO: la tarea terminó en error: %s" % tarea.get("error"))
            return 1
        time.sleep(0.5)
    if not final:
        print("FALLO: la descarga no terminó a tiempo")
        return 1
    print("   completada: %s" % final.get("nombre"))

    print("== verificando contenido byte a byte ==")
    # la carpeta destino puede tener subcarpetas (organizar por tipo): buscar
    # recursivo y excluir fragmentos .part
    bajados = []
    for raiz, _dirs, archivos_bajados in os.walk(destino):
        for f in archivos_bajados:
            if not f.endswith(".part") and not f.startswith("."):
                bajados.append(os.path.join(raiz, f))
    if not bajados:
        print("FALLO: no hay archivo en %s" % destino)
        return 1
    with open(bajados[0], "rb") as f:
        sha_bajado = hashlib.sha256(f.read()).hexdigest()
    if sha_bajado != sha_original:
        print("FALLO: el sha256 no coincide (%s != %s)" % (sha_bajado, sha_original))
        return 1
    print("   sha256 OK (%d bytes, %s)" % (tam, os.path.basename(bajados[0])))

    archivos.shutdown()
    api.shutdown()
    print("== SMOKE E2E OK ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
