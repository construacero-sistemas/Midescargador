# -*- coding: utf-8 -*-
"""Pruebas de hosters.py sin red: enrutado de resolver(), errores claros,
parseo puro y el guard de Chrome del extractor fuckingfast.
"""
import unittest
from unittest import mock

import hosters
import zonaleros_copia


class ResolverDispatchTests(unittest.TestCase):
    def _con_mock(self, nombre_extractor, url):
        with mock.patch.object(hosters, nombre_extractor,
                               return_value={"url": "directa", "nombre": "a.zip"}) as ex:
            resultado = hosters.resolver(url)
        ex.assert_called_once_with(url)
        self.assertEqual(resultado, {"url": "directa", "nombre": "a.zip"})

    def test_enruta_mediafire(self):
        self._con_mock("_extraer_mediafire", "https://www.mediafire.com/file/abc/a.zip/file")

    def test_enruta_gofile(self):
        self._con_mock("_extraer_gofile", "https://gofile.io/d/AbC123")

    def test_enruta_megaup(self):
        self._con_mock("_extraer_megaup", "https://megaup.net/2aBc/a.zip")

    def test_enruta_fireload(self):
        self._con_mock("_extraer_fireload", "https://fireload.com/abc123")

    def test_dominio_no_soportado_da_none(self):
        self.assertIsNone(hosters.resolver("https://ejemplo-desconocido.org/x"))
        self.assertIsNone(hosters.resolver("no es una url"))

    def test_subdominio_www_enruta_igual(self):
        for host in ("mediafire.com", "www.mediafire.com"):
            with mock.patch.object(hosters, "_extraer_mediafire") as ex:
                hosters.resolver("https://%s/file/abc/a.zip" % host)
                ex.assert_called_once()


class ErroresClarosTests(unittest.TestCase):
    def test_gofile_sin_codigo_da_error_claro(self):
        with self.assertRaises(RuntimeError) as ctx:
            hosters._extraer_gofile("https://gofile.io/sin-codigo")
        self.assertIn("/d/", str(ctx.exception))


class GuardChromeTests(unittest.TestCase):
    def test_fuckingfast_con_chrome_abierto_da_mensaje_claro(self):
        # además verifica que el extractor usa el módulo único
        # zonaleros_copia (el viejo zonaleros.py fue eliminado)
        with mock.patch.object(zonaleros_copia, "_chrome_corriendo",
                               return_value=True):
            with self.assertRaises(RuntimeError) as ctx:
                hosters._extraer_fuckingfast("https://fuckingfast.net/x")
            self.assertIn("cierra Chrome", str(ctx.exception))


class ParseoPuroTests(unittest.TestCase):
    def test_mensaje_drive_por_archivo_grande(self):
        self.assertIn("muy grande", hosters._mensaje_drive(
            "<html>Sorry, this file is too large to scan</html>"))

    def test_mensaje_drive_por_sesion(self):
        self.assertIn("iniciar sesi", hosters._mensaje_drive(
            "<html>accounts.google.com/ServiceLogin sign in</html>").lower())

    def test_mensaje_drive_generico(self):
        self.assertIn("verificaci", hosters._mensaje_drive("<html>banana</html>").lower())


class NombreDeUrlTests(unittest.TestCase):
    def test_nombre_simple(self):
        self.assertEqual(
            zonaleros_copia._nombre_de_url("https://x.com/carpetas/Archivo.part1.rar"),
            "Archivo.part1.rar")

    def test_mediafire_usa_penultimo_segmento(self):
        self.assertEqual(
            zonaleros_copia._nombre_de_url(
                "https://www.mediafire.com/file/abc123/Juego.part2.rar/file"),
            "Juego.part2.rar")

    def test_sin_nombre_da_vacio(self):
        self.assertEqual(zonaleros_copia._nombre_de_url("https://x.com/"), "")


class AnadirPesosTests(unittest.TestCase):
    def test_rellena_el_peso_de_cada_enlace(self):
        servidores = [{
            "servidor": "MEGA",
            "enlaces": [
                {"url": "https://www.mediafire.com/file/1/a.zip/file",
                 "nombre": "a.zip", "parte": 1, "total": 2},
                {"url": "https://www.mediafire.com/file/2/b.zip/file",
                 "nombre": "b.zip", "parte": 2, "total": 2},
            ]}]
        with mock.patch.object(
                hosters, "resolver",
                side_effect=[{"url": "d", "nombre": "a", "tamano": 500},
                             {"url": "d", "nombre": "b", "tamano": 3000}]) as m:
            salida = zonaleros_copia._anadir_pesos(servidores)
        self.assertIs(salida, servidores)
        self.assertEqual(servidores[0]["enlaces"][0]["tamano"], 500)
        self.assertEqual(servidores[0]["enlaces"][1]["tamano"], 3000)
        self.assertEqual(m.call_count, 2)

    def test_ignora_sin_tamano_y_sin_resolver(self):
        servidores = [{"servidor": "X",
                        "enlaces": [{"url": "https://x.com/f", "nombre": "f"}]}]
        with mock.patch.object(hosters, "resolver",
                               side_effect=Exception("boom")):
            zonaleros_copia._anadir_pesos(servidores)
        # no lanza y deja el enlace sin tamano
        self.assertNotIn("tamano", servidores[0]["enlaces"][0])

    def test_sin_servidores_no_hace_nada(self):
        self.assertEqual(zonaleros_copia._anadir_pesos([]), [])


if __name__ == "__main__":
    unittest.main()
