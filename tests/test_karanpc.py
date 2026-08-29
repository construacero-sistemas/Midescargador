# -*- coding: utf-8 -*-
"""Pruebas del extractor de posts individuales de KaranPC."""
import unittest

import karanpc


class _CdpFake:
    def __init__(self, resultados):
        self.resultados = resultados
        self.actual = "post"
        self.navegaciones = []

    def navegar(self, url, condicion=None, tiempo_max=None):
        self.navegaciones.append(url)
        self.actual = "glo" if "glodls.online" in url else "post"
        return True

    def eval(self, js):
        if "document.title" in js:
            return "Programa de prueba | KaranPC" if self.actual == "post" else "GloTorrents"
        if "document.body" in js or "querySelectorAll" in js:
            return self.resultados.get(self.actual, [])
        return None

    def cerrar(self):
        pass


class TestKaranPc(unittest.TestCase):
    def test_detecta_post_individual(self):
        self.assertTrue(karanpc._es_karanpc("https://karanpc.com/winrar-latest/"))
        self.assertTrue(karanpc._es_karanpc("https://www.karanpc.com/alguna-app/"))
        self.assertFalse(karanpc._es_karanpc("https://example.com/alguna-app/"))

    def test_reconoce_indice_posts(self):
        self.assertTrue(karanpc._es_indice("https://karanpc.com/posts/"))
        self.assertTrue(karanpc._es_indice("https://karanpc.com/posts/page/2/"))
        self.assertFalse(karanpc._es_indice("https://karanpc.com/winrar-latest/"))

    def test_clasifica_hosters_reales(self):
        casos = {
            "https://download.mediafire.com/file/test/app.zip": "MediaFire",
            "https://mega.nz/file/abc#key": "Mega",
            "https://gofile.io/d/abc": "GoFile",
            "https://glodls.online/": "GloTorrents",
            "https://unknown.example/download/app.zip": "unknown.example",
        }
        for url, esperado in casos.items():
            with self.subTest(url=url):
                self.assertEqual(karanpc._nombre_hoster(url), esperado)

    def test_extrae_enlaces_de_dom_y_omite_recursos(self):
        cdp = _CdpFake({"post": [
            {"url": "https://download.mediafire.com/file/a/app.zip", "texto": "Download"},
            {"url": "https://mega.nz/file/b#key", "texto": "Mirror"},
            {"url": "https://glodls.online/", "texto": "GloTorrents"},
            {"url": "https://karanpc.com/wp-content/uploads/logo.png", "texto": ""},
            {"url": "https://facebook.com/karanpc", "texto": "Facebook"},
        ]})
        enlaces = karanpc._extraer_urls_dom(cdp, "karanpc.com")
        urls = {x["url"] for x in enlaces}
        self.assertEqual(urls, {
            "https://download.mediafire.com/file/a/app.zip",
            "https://mega.nz/file/b#key",
            "https://glodls.online/",
        })

    def test_resuelve_post_glo_y_agrupa_resultado_final(self):
        cdp = _CdpFake({
            "post": [{"url": "https://glodls.online/app-123", "texto": "Download"}],
            "glo": [
                {"url": "https://download.mediafire.com/file/app.zip", "texto": "Download"},
                {"url": "https://mega.nz/file/key#abc", "texto": "Mirror"},
            ],
        })
        candidatos = karanpc._extraer_urls_dom(cdp, "karanpc.com")
        finales = []
        for candidato in candidatos:
            if karanpc._es_intermedio(candidato["url"]):
                finales.extend(karanpc._navegar_intermedio(cdp, candidato["url"], 9999999999))
            else:
                finales.append(candidato)
        servidores = karanpc._clasificar(finales)
        self.assertEqual({s["hoster"] for s in servidores}, {"Mega", "MediaFire"})
        self.assertNotIn("GloTorrents", {s["hoster"] for s in servidores})
        self.assertIn("https://glodls.online/app-123", cdp.navegaciones)

    def test_clasifica_resultados_por_hoster(self):
        servidores = karanpc._clasificar([
            {"url": "https://mega.nz/file/a#k", "texto": "Mirror"},
            {"url": "https://download.mediafire.com/file/b/app.zip", "texto": "Download"},
        ])
        self.assertEqual({s["hoster"] for s in servidores}, {"Mega", "MediaFire"})
        self.assertTrue(all(s["enlaces"] for s in servidores))


if __name__ == "__main__":
    unittest.main(verbosity=2)
