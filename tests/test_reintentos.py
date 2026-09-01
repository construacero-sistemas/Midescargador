# -*- coding: utf-8 -*-
"""Pruebas del scheduler de reintentos automáticos (estilo IDM) y de la
persistencia de la cola (cola.json).

Trabajan contra el GESTOR real de servidor.py pero con trabajos falsos y
cola.json redirigido a un temporal: nunca toca la cola del usuario.
"""
import tempfile
import os
import time
import unittest
from unittest import mock

import servidor


class TrabajoFalso:
    """Suficiente para el scheduler: estado, error y reintentar()."""

    def __init__(self, tid, error="timeout de conexión", estado="error",
                 reintentos=0, proximo=0):
        self.id = tid
        self.url = "https://ejemplo/archivo.zip"
        self.estado = estado
        self.error = error
        self._reintentos_auto = reintentos
        self._proximo_reintento = proximo
        self.reintentado = 0

    def reintentar(self):
        self.reintentado += 1
        self.estado = "descargando"

    def progreso(self):
        return {"tipo": "directo", "nombre": "archivo.zip", "total": 0,
                "estado": self.estado, "error": self.error}


class EsperaReintentoTests(unittest.TestCase):
    def test_backoff_exponencial(self):
        esperas = [servidor._espera_reintento(n) for n in range(5)]
        self.assertEqual(esperas, [30, 60, 120, 240, 480])

    def test_tope_de_espera(self):
        self.assertEqual(servidor._espera_reintento(10),
                         servidor.REINTENTOS_MAX_ESPERA)


class EsReintentableTests(unittest.TestCase):
    def test_errores_permanentes_no_se_reintentan(self):
        for err in ("HTTP 404 - no encontrado", "410 Gone",
                    "requested format is not available",
                    "sesión vencida", "link no longer valid"):
            self.assertFalse(servidor._es_reintentable(err), err)

    def test_errores_transitorios_se_reintentan(self):
        for err in ("timeout de conexión", "connection reset by peer",
                    "servidor ocupado (503)"):
            self.assertTrue(servidor._es_reintentable(err), err)


class TickReintentosTests(unittest.TestCase):
    def setUp(self):
        self._cola_original = servidor._RUTA_COLA
        self.tmp = tempfile.mkdtemp()
        servidor._RUTA_COLA = os.path.join(self.tmp, "cola.json")
        self.trabajo = TrabajoFalso("t1")
        with servidor.GESTOR._lock:
            servidor.GESTOR.trabajos = {"t1": self.trabajo}

    def tearDown(self):
        with servidor.GESTOR._lock:
            servidor.GESTOR.trabajos = {}
        servidor._RUTA_COLA = self._cola_original

    def test_primer_fallo_programa_backoff_sin_reintentar(self):
        reintentados = servidor._tick_reintentos()
        self.assertEqual(reintentados, [])
        self.assertEqual(self.trabajo.reintentado, 0)
        self.assertGreater(self.trabajo._proximo_reintento, time.time() - 1)

    def test_backoff_cumplido_reintenta(self):
        self.trabajo._proximo_reintento = time.time() - 1   # backoff vencido
        reintentados = servidor._tick_reintentos()
        self.assertEqual(reintentados, ["t1"])
        self.assertEqual(self.trabajo.reintentado, 1)
        self.assertEqual(self.trabajo._reintentos_auto, 1)

    def test_error_permanente_nunca_se_reintenta(self):
        self.trabajo.error = "404 not found"
        self.trabajo._proximo_reintento = time.time() - 1
        self.assertEqual(servidor._tick_reintentos(), [])
        self.assertEqual(self.trabajo.reintentado, 0)

    def test_tope_de_reintentos(self):
        self.trabajo._reintentos_auto = servidor.REINTENTOS_MAX
        self.trabajo._proximo_reintento = time.time() - 1
        self.assertEqual(servidor._tick_reintentos(), [])
        self.assertEqual(self.trabajo.reintentado, 0)

    def test_cola_se_persiste(self):
        servidor._tick_reintentos()
        self.assertTrue(os.path.exists(servidor._RUTA_COLA))
        with open(servidor._RUTA_COLA, encoding="utf-8") as f:
            contenido = f.read()
        self.assertIn("t1", contenido)
        self.assertIn("reintentos_auto", contenido)


if __name__ == "__main__":
    unittest.main()
