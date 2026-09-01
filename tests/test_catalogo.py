# -*- coding: utf-8 -*-
"""Pruebas del catálogo ZonaLeros (catalogo.py), sin Chrome ni red:

- enumeración por categorías con marca persistente (la regresión que dejaba
  series en 0 para siempre al reanudar una corrida cortada),
- orden de la cola por prioridad (episodios → series → películas → juegos),
- alta de ítems sin duplicados.
"""
import os
import tempfile
import unittest

import catalogo


class CatalogoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mdm_cat_")
        self.cat = catalogo.Catalogo(self.tmp)
        self._ruta_log_original = catalogo._RUTA_LOG

    def tearDown(self):
        catalogo._RUTA_LOG = self._ruta_log_original


class CategoriasAEnumerarTests(CatalogoBase):
    def test_fresh_enumerar_todas(self):
        # el orden es por prioridad: series primero, juegos al final
        self.assertEqual(self.cat._categorias_a_enumerar(),
                         ["series", "peliculas", "juegos"])

    def test_reanudar_salta_categorias_completas(self):
        # regresión: una corrida cortada antes de "series" deja juegos y
        # películas marcadas; reanudar solo debe enumerar series
        with self.cat._lock:
            self.cat._progreso["enumerado"] = {"juegos": True, "peliculas": True}
        self.assertEqual(self.cat._categorias_a_enumerar(), ["series"])

    def test_revisar_re_enumerar_todas(self):
        with self.cat._lock:
            self.cat._progreso["enumerado"] = {c: True for c in catalogo.CATEGORIAS}
        self.assertEqual(self.cat._categorias_a_enumerar(revisar=True),
                         list(catalogo.CATEGORIAS))

    def test_iniciar_con_revisar_limpia_marcas(self):
        with self.cat._lock:
            self.cat._progreso["enumerado"] = {"juegos": True}
        self.cat.iniciar(revisar=True)
        self.cat.pausar()   # no dejar el hilo trabajando
        with self.cat._lock:
            self.assertEqual(self.cat._progreso.get("enumerado"), {})

    def test_marca_se_persiste_en_progreso_json(self):
        with self.cat._lock:
            self.cat._progreso.setdefault("enumerado", {})["series"] = True
            self.cat._guardar()
        # una instancia nueva (reinicio de la app) conserva la marca; el
        # orden de las faltantes es por prioridad (películas antes que juegos)
        cat2 = catalogo.Catalogo(self.tmp)
        self.assertEqual(cat2._categorias_a_enumerar(), ["peliculas", "juegos"])


class PendientesOrdenadosTests(CatalogoBase):
    def _agregar(self, urls_cat):
        for u, cat in urls_cat:
            with self.cat._lock:
                self.cat._progreso["items"][u] = {
                    "cat": cat, "estado": "pendiente",
                    "carpeta": os.path.join(self.tmp, cat, "x"),
                    "reintentos": 0,
                }

    def test_episodios_series_antes_que_juegos(self):
        self._agregar([
            ("https://x/juegos/a", "juegos"),
            ("https://x/peliculas/b", "peliculas"),
            ("https://x/series/c", "series"),
            ("https://x/series/episode/d", "series_ep"),
            ("https://x/juegos/e", "juegos"),
        ])
        orden = self.cat._pendientes_ordenados()
        self.assertEqual([u.rsplit("/", 1)[-1] for u in orden],
                         ["d", "c", "b", "a", "e"])

    def test_errores_con_reintentos_vuelven_a_la_cola(self):
        self._agregar([("https://x/juegos/a", "juegos")])
        with self.cat._lock:
            it = self.cat._progreso["items"]["https://x/juegos/a"]
            it["estado"] = "error"
            it["reintentos"] = 1
        self.assertEqual(self.cat._pendientes_ordenados(), ["https://x/juegos/a"])

    def test_descartados_no_vuelven(self):
        self._agregar([("https://x/juegos/a", "juegos"),
                       ("https://x/juegos/b", "juegos")])
        with self.cat._lock:
            # permanente agotado (2 reintentos)
            it = self.cat._progreso["items"]["https://x/juegos/a"]
            it["estado"] = "error"
            it["reintentos"] = catalogo._MAX_REINTENTOS
            it["error"] = "sin botones de descarga (página cargada: Juego X)"
            # transitorio agotado (5 reintentos)
            it2 = self.cat._progreso["items"]["https://x/juegos/b"]
            it2["estado"] = "error"
            it2["reintentos"] = catalogo._MAX_REINTENTOS_TRANSITORIOS
            it2["error"] = "cloudflare no dejó cargar la página (espera agotada)"
        self.assertEqual(self.cat._pendientes_ordenados(), []
                         )

    def test_transitorio_insiste_mas_que_permanente(self):
        self._agregar([("https://x/juegos/a", "juegos"),
                       ("https://x/juegos/b", "juegos")])
        with self.cat._lock:
            a = self.cat._progreso["items"]["https://x/juegos/a"]
            b = self.cat._progreso["items"]["https://x/juegos/b"]
            a["estado"] = b["estado"] = "error"
            a["reintentos"] = b["reintentos"] = catalogo._MAX_REINTENTOS
            a["error"] = "sin botones de descarga (página cargada: X)"
            b["error"] = "error interno: socket is already closed."
        pendientes = self.cat._pendientes_ordenados()
        # el permanente ya se descartó (2/2); el transitorio sigue (2/5)
        self.assertEqual(pendientes, ["https://x/juegos/b"])


class ErrorTransitorioTests(CatalogoBase):
    def test_entorno_es_transitorio(self):
        for err in ("cloudflare no dejó cargar la página (espera agotada)",
                    "no se pudo abrir la pagina",
                    "error interno: socket is already closed.",
                    "error interno: [WinError 10053] Se ha anulado una conexión",
                    ""):
            self.assertTrue(catalogo._es_error_transitorio(err), err)

    def test_pagina_cargada_sin_botones_es_permanente(self):
        for err in ("sin botones de descarga",
                    "sin botones de descarga (página cargada: Juego X)",
                    "sin episodios"):
            self.assertFalse(catalogo._es_error_transitorio(err), err)

    def test_revisar_rescata_descartados(self):
        with self.cat._lock:
            self.cat._progreso["items"]["https://x/juegos/a"] = {
                "cat": "juegos", "estado": "descartado", "reintentos": 2,
                "error": "sin botones de descarga",
                "carpeta": os.path.join(self.tmp, "juegos", "a"),
            }
            self.cat._progreso["items"]["https://x/juegos/b"] = {
                "cat": "juegos", "estado": "hecho", "reintentos": 0,
                "carpeta": os.path.join(self.tmp, "juegos", "b"),
            }
        self.cat.iniciar(revisar=True)
        self.cat.pausar()   # no dejar el hilo trabajando
        with self.cat._lock:
            a = self.cat._progreso["items"]["https://x/juegos/a"]
            b = self.cat._progreso["items"]["https://x/juegos/b"]
        self.assertEqual(a["estado"], "pendiente")
        self.assertEqual(a["reintentos"], 0)
        self.assertNotIn("error", a)
        self.assertEqual(b["estado"], "hecho")   # los hechos no se tocan


class AgregarItemsTests(CatalogoBase):
    def test_agrega_sin_duplicados(self):
        n1 = self.cat._agregar_items("series", ["https://zona-leros.com/series/ataque-a-los-titanes"])
        n2 = self.cat._agregar_items("series", ["https://zona-leros.com/series/ataque-a-los-titanes"])
        self.assertEqual((n1, n2), (1, 0))
        with self.cat._lock:
            it = self.cat._progreso["items"]["https://zona-leros.com/series/ataque-a-los-titanes"]
        self.assertEqual(it["cat"], "series")
        self.assertEqual(it["estado"], "pendiente")


if __name__ == "__main__":
    unittest.main()
