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
        self.cat._enumerar_marca_manual = True
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
        self._agregar([("https://x/juegos/a", "juegos")])
        with self.cat._lock:
            it = self.cat._progreso["items"]["https://x/juegos/a"]
            it["estado"] = "error"
            it["reintentos"] = catalogo._MAX_REINTENTOS
        self.assertEqual(self.cat._pendientes_ordenados(), [])


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
