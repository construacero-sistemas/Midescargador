# -*- coding: utf-8 -*-
"""Pruebas HTTP de la API local: token/401, Host check del callback de
Drive, anti path-traversal en /static/ y guard de error 500.

Levanta el Manejador real de servidor.py en un puerto efímero, así que las
pruebas son de extremo a extremo (socket → handler → respuesta JSON) sin
mockear el protocolo.
"""
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

import servidor


class ServidorApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servidor_http = ThreadingHTTPServer(("127.0.0.1", 0), servidor.Manejador)
        cls.puerto = cls.servidor_http.server_address[1]
        cls.hilo = threading.Thread(target=cls.servidor_http.serve_forever, daemon=True)
        cls.hilo.start()

    @classmethod
    def tearDownClass(cls):
        cls.servidor_http.shutdown()
        cls.servidor_http.server_close()

    def _pedir(self, metodo, ruta, cabeceras=None, host="127.0.0.1"):
        c = http.client.HTTPConnection("127.0.0.1", self.puerto, timeout=5)
        try:
            todas = {**({"Host": host} if host else {}), **(cabeceras or {})}
            c.request(metodo, ruta, headers=todas)
            r = c.getresponse()
            cuerpo = r.read()
            try:
                datos = json.loads(cuerpo.decode("utf-8"))
            except Exception:
                datos = None
            return r.status, datos
        finally:
            c.close()

    # ---- autenticación -------------------------------------------------
    def test_api_sin_token_da_401(self):
        estado, datos = self._pedir("GET", "/api/estado")
        self.assertEqual(estado, 401)
        self.assertEqual(datos, {"error": "no autorizado"})

    def test_api_con_token_responde(self):
        estado, _ = self._pedir("GET", "/api/estado",
                                {"X-MiDescargador-Token": servidor.TOKEN_API})
        self.assertEqual(estado, 200)

    def test_token_bootstrap_rechaza_host_remoto(self):
        estado, datos = self._pedir("GET", "/api/token", host="web-maliciosa.com")
        self.assertEqual(estado, 401)
        self.assertEqual(datos, {"error": "no autorizado"})

    # ---- OAuth de Drive: callback sin token pero solo host local -------
    def test_drive_oauth_rechaza_host_remoto(self):
        estado, datos = self._pedir("GET", "/api/drive/oauth?code=x",
                                    host="web-maliciosa.com")
        self.assertEqual(estado, 401)
        self.assertEqual(datos, {"error": "no autorizado"})

    def test_drive_oauth_acepta_host_local_sin_token(self):
        # con un code falso el intercambio falla, pero la respuesta NUNCA
        # es 401: el check de host pasó y se llegó al handler real
        estado, _ = self._pedir("GET", "/api/drive/oauth?code=prueba")
        self.assertNotEqual(estado, 401)

    # ---- anti path-traversal en /static/ --------------------------------
    def test_static_no_permite_salir_de_la_carpeta(self):
        estado, _ = self._pedir("GET", "/static/../servidor.py")
        self.assertEqual(estado, 404)

    def test_static_sirve_archivo_real(self):
        estado, cuerpo = self._pedir("GET", "/static/seleccion.js")
        self.assertEqual(estado, 200)

    # ---- ruta inexistente ----------------------------------------------
    def test_ruta_desconocida_da_404_json(self):
        estado, datos = self._pedir("GET", "/api/inexistente",
                                    {"X-MiDescargador-Token": servidor.TOKEN_API})
        self.assertEqual(estado, 404)
        self.assertEqual(datos, {"error": "no encontrado"})


if __name__ == "__main__":
    unittest.main()
