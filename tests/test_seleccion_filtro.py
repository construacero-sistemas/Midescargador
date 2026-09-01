# -*- coding: utf-8 -*-
"""Pruebas del filtro de selección "temporada + servidor" del backend.

Cubre dos funciones puras extraídas del flujo:

- zonaleros_copia._hoster_para_grupo: decide, grupo a grupo, si un episodio
  pasa el filtro por servidor elegido, y etiqueta el grupo con su hoster real.
- servidor._filtrar_servidores: post-filtro final que deja solo los grupos
  cuyo hoster coincide exactamente con la selección.

Más una prueba de integración que replica el flujo de _enlaces_lanzar (la
selección manda las URLs de episodios de las temporadas elegidas + la lista
de servidores) y verifica que solo devuelve los enlaces elegidos.

Ejecutar:
    python -m unittest discover -s tests -v
    # o directamente:
    cd tests && python -m unittest test_seleccion_filtro -v
"""

import unittest
import os
import sys
import tempfile
import time

# tests/ está dentro del proyecto: se añade la raíz (padre de tests/)
# para importar los módulos del backend (servidor, zonaleros_copia, ...).
_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RAIZ)

import zonaleros_copia as zonaleros
import servidor
import catalogo as catalogo_mod

# URLs de ejemplo por hoster real (el extractor usa el dominio para saber
# el hoster; _nombre_hoster_url en zonaleros_copia hace el mapeo).
URL_MEGA = "https://mega.nz/file/MEGA00"
URL_MEGAUP = "https://megaup.net/archivo"
URL_MEDIAFIRE = "https://www.mediafire.com/file/abc"
URL_GOFILE = "https://gofile.io/d/xyz"
URL_ROOTZ = "https://rootz.so/otro"
URL_FICHIER = "https://www.1fichier.com/?xxxx"


class TestHosterParaGrupo(unittest.TestCase):
    """Decisión grupo a grupo dentro de _extraer_un_episodio."""

    def test_sin_filtro_etiqueta_con_hoster_y_opcion(self):
        # un solo botón => sin sufijo "· Opción"
        g = zonaleros._hoster_para_grupo([URL_MEGA], "4x1 Piloto", 1, 1)
        self.assertEqual(g["hoster"], "Mega")
        self.assertEqual(g["episodio"], "4x1 Piloto")
        self.assertEqual(g["servidor"], "Mega · 4x1 Piloto")

    def test_sin_filtro_anade_opcion_cuando_hay_varias(self):
        g = zonaleros._hoster_para_grupo([URL_MEGA], "4x1 Piloto", 3, 2)
        self.assertEqual(g["servidor"], "Mega · 4x1 Piloto · Opción 2")

    def test_filtro_mega_queda_y_megaup_no(self):
        filtrar = ["Mega"]
        self.assertIsNotNone(zonaleros._hoster_para_grupo([URL_MEGA], "e1", 1, 1, filtrar))
        # la coincidencia debe ser EXACTA: "Mega" NO debe arrastrar "MegaUp"
        self.assertIsNone(zonaleros._hoster_para_grupo([URL_MEGAUP], "e1", 1, 1, filtrar))

    def test_filtro_megaup_queda_y_mega_no(self):
        filtrar = ["MegaUp"]
        self.assertIsNotNone(zonaleros._hoster_para_grupo([URL_MEGAUP], "e1", 1, 1, filtrar))
        self.assertIsNone(zonaleros._hoster_para_grupo([URL_MEGA], "e1", 1, 1, filtrar))

    def test_grupo_con_varios_hosters_queda_si_alguno_coincide(self):
        g = zonaleros._hoster_para_grupo(
            [URL_MEGA, URL_MEDIAFIRE], "e1", 1, 1, ["MediaFire"])
        self.assertIsNotNone(g)
        # el nombre visible usa un hoster del grupo, sin importar la etiqueta
        self.assertIn(g["hoster"], {"Mega", "MediaFire"})

    def test_filtro_varios_acepta_cualquiera(self):
        self.assertIsNotNone(zonaleros._hoster_para_grupo(
            [URL_MEDIAFIRE], "e1", 1, 1, ["MediaFire", "Rootz"]))
        self.assertIsNone(zonaleros._hoster_para_grupo(
            [URL_GOFILE], "e1", 1, 1, ["MediaFire", "Rootz"]))

    def test_servidor_por_confirmar(self):
        # sin URLs resueltas: no se descarta aquí (el hoster es desconocido)
        g = zonaleros._hoster_para_grupo([], "e2", 1, 1, ["Mega"])
        self.assertEqual(g["hoster"], "Servidor por confirmar")
        # y si el usuario elige "Servidor por confirmar", también pasa
        self.assertIsNotNone(zonaleros._hoster_para_grupo(
            [], "e2", 1, 1, ["Servidor por confirmar"]))
        # pero un grupo con hoster conocido no se disfraza de "por confirmar"
        self.assertIsNone(zonaleros._hoster_para_grupo(
            [URL_MEGA], "e2", 1, 1, ["Servidor por confirmar"]))

    def test_case_insensitive(self):
        self.assertIsNotNone(zonaleros._hoster_para_grupo(
            [URL_MEGA], "e1", 1, 1, ["mega"]))


class TestFiltrarServidores(unittest.TestCase):
    """Post-filtro final en servidor._enlaces_lanzar."""

    def _grupos(self):
        return [
            {"servidor": "Mega · e1", "hoster": "Mega"},
            {"servidor": "MegaUp · e1", "hoster": "MegaUp"},
            {"servidor": "MediaFire · e1", "hoster": "MediaFire"},
            {"servidor": "Servidor por confirmar · e2", "hoster": "Servidor por confirmar"},
        ]

    def test_sin_seleccion_devuelve_todo(self):
        grupos = self._grupos()
        self.assertEqual(servidor._filtrar_servidores(grupos, None), grupos)
        self.assertEqual(servidor._filtrar_servidores(grupos, []), grupos)

    def test_filtra_por_hoster_exacto(self):
        quedan = servidor._filtrar_servidores(self._grupos(), ["Mega"])
        self.assertEqual([g["hoster"] for g in quedan], ["Mega"])

    def test_mega_no_arrastra_megaup(self):
        quedan = servidor._filtrar_servidores(self._grupos(), ["Mega"])
        self.assertNotIn("MegaUp", {g["hoster"] for g in quedan})

    def test_varios_hosters(self):
        quedan = servidor._filtrar_servidores(
            self._grupos(), ["Mega", "MediaFire"])
        self.assertEqual({g["hoster"] for g in quedan}, {"Mega", "MediaFire"})

    def test_por_confirmar(self):
        quedan = servidor._filtrar_servidores(
            self._grupos(), ["Servidor por confirmar"])
        self.assertEqual([g["hoster"] for g in quedan], ["Servidor por confirmar"])

    def test_resultado_sin_hoster_se_descarta_al_filtrar(self):
        grupos = self._grupos() + [{"servidor": "Mega · e9"}]  # sin campo hoster
        quedan = servidor._filtrar_servidores(grupos, ["Mega"])
        self.assertEqual([g["hoster"] for g in quedan], ["Mega"])

    def test_case_insensitive(self):
        quedan = servidor._filtrar_servidores(self._grupos(), ["mega"])
        self.assertEqual([g["hoster"] for g in quedan], ["Mega"])

    def test_cache_tambien_respeta_servidor_elegido(self):
        url = "https://karanpc.com/programa-de-prueba/"
        anterior = dict(servidor._ENLACES_CACHE)
        try:
            servidor._ENLACES_CACHE.clear()
            servidor._ENLACES_CACHE[url] = (time.time(), {
                "servidores": self._grupos(), "titulo": "Programa"})
            r = servidor._enlaces_lanzar(url, servidores_seleccionados=["MediaFire"])
            self.assertEqual([x["hoster"] for x in r["servidores"]], ["MediaFire"])
        finally:
            servidor._ENLACES_CACHE.clear()
            servidor._ENLACES_CACHE.update(anterior)


class TestHostersDetectadosEnCatalogo(unittest.TestCase):
    """persistencia de 'servidores_posibles' por serie en el catálogo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cat = catalogo_mod.Catalogo(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sin_registros_devuelve_none(self):
        self.assertIsNone(self.cat.hosters_de("https://zona-leros.com/series/x"))

    def test_registra_hosters_y_persiste(self):
        url = "https://zona-leros.com/series/serie-a"
        self.cat.registrar_hosters(url, ["Mega", "MediaFire"])
        self.assertEqual(self.cat.hosters_de(url), ["MediaFire", "Mega"])
        # recargar desde disco (nueva instancia) conserva lo guardado
        cat2 = catalogo_mod.Catalogo(self._tmp.name)
        self.assertEqual(cat2.hosters_de(url), ["MediaFire", "Mega"])

    def test_se_acumula_y_no_borra(self):
        url = "https://zona-leros.com/series/serie-b"
        self.cat.registrar_hosters(url, ["Mega"])
        self.cat.registrar_hosters(url, ["MegaUp"])
        self.assertIn("Mega", self.cat.hosters_de(url))
        self.assertIn("MegaUp", self.cat.hosters_de(url))
        # url distintas no comparten
        self.assertIsNone(self.cat.hosters_de("https://zona-leros.com/series/otra"))

    def test_ignora_vacios(self):
        url = "https://zona-leros.com/series/serie-c"
        self.cat.registrar_hosters(url, [])
        self.cat.registrar_hosters(url, [None, "", "Mega"])
        self.assertIsNone(self.cat.hosters_de(
            "https://zona-leros.com/series/otra"))
        self.assertEqual(self.cat.hosters_de(url), ["Mega"])

    def test_nombres_se_ordenan(self):
        url = "https://zona-leros.com/series/serie-d"
        self.cat.registrar_hosters(url, ["Zeta", "Alpha", "Mega"])
        self.assertEqual(self.cat.hosters_de(url), ["Alpha", "Mega", "Zeta"])


class TestSeleccionTemporadaMasServidor(unittest.TestCase):
    """Integración: replica _enlaces_lanzar con selección.

    'temporada' === qué URLs de episodios mandamos (el panel envía SOLO los
    episodios de las temporadas marcadas) y 'servidor' === filtro por hoster.
    """

    # Cada temporada resuelve sus episodios a ciertos hosters.
    TEMPORADA_1 = [  # solo Mega y MediaFire
        {"ep": "1x1", "urls": [URL_MEGA]},
        {"ep": "1x2", "urls": [URL_MEDIAFIRE]},
    ]
    TEMPORADA_2 = [  # Mega + MegaUp (para detectar el solapamiento "Mega")
        {"ep": "2x1", "urls": [URL_MEGA]},
        {"ep": "2x2", "urls": [URL_MEGAUP]},
    ]

    def _resolver_temporada(self, episodios, servidores):
        """Simula la extracción: por cada episodio (URL) resuelve grupos y
        aplica el filtro de hoster; devuelve la lista plana de grupos."""
        resultados = []
        for ep in episodios:
            g = zonaleros._hoster_para_grupo(ep["urls"], ep["ep"], 1, 1, servidores)
            if g is not None:
                resultados.append(g)
        return resultados

    def test_solo_temporada_1_megafire(self):
        # el usuario marcó SOLO temporada 1 y SOLO el servidor Mega
        urls_elegidas = [ep["ep"] for ep in self.TEMPORADA_1]
        # procesamos solo los episodios de la temporada elegida
        parciales = self._resolver_temporada(self.TEMPORADA_1, ["Mega"])
        final = servidor._filtrar_servidores(parciales, ["Mega"])

        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]["hoster"], "Mega")
        self.assertEqual(final[0]["episodio"], "1x1")
        # nada de la temporada 2
        self.assertNotIn("2x", {g["episodio"] for g in final})
        # y el episodio 1x2 (MediaFire) se descartó por el filtro de servidor
        self.assertNotIn("1x2", {g["episodio"] for g in final})

    def test_dos_temporadas_varios_servidores(self):
        parciales = self._resolver_temporada(
            self.TEMPORADA_1 + self.TEMPORADA_2, ["Mega", "MediaFire"])
        final = servidor._filtrar_servidores(parciales, ["Mega", "MediaFire"])

        episodios = {g["episodio"] for g in final}
        self.assertEqual(episodios, {"1x1", "1x2", "2x1"})  # todo menos MegaUp 2x2
        self.assertNotIn("2x2", episodios)

    def test_marcar_mega_excluye_megaup_en_toda_la_seleccion(self):
        parciales = self._resolver_temporada(
            self.TEMPORADA_1 + self.TEMPORADA_2, ["Mega"])
        final = servidor._filtrar_servidores(parciales, ["Mega"])

        self.assertTrue(all(g["hoster"] == "Mega" for g in final))
        self.assertNotIn("MegaUp", {g["hoster"] for g in final})

    def test_parcial_en_vivo_tambien_filtrado(self):
        # el reporte: marcando SOLO MediaFire, el panel en vivo mostraba
        # todos los servidores (Mega, drive, ...). El parcial debe filtrarse
        # igual que el resultado final, no solo al terminar la extracción.
        episodios = [
            {"ep": "1x1", "urls": [URL_MEDIAFIRE, URL_MEGA]},
            {"ep": "1x2", "urls": [URL_MEGAUP, URL_MEGA]},
            {"ep": "1x3", "urls": [URL_MEDIAFIRE]},
        ]
        # mientras extrae (parcial): la app llama _filtrar_servidores sobre
        # lo acumulado en cada _progreso, exactamente como el resultado final
        parcial = servidor._filtrar_servidores(
            self._resolver_temporada(episodios, ["MediaFire"]), ["MediaFire"])
        self.assertTrue(parcial, "el parcial no debe quedar vacío")
        self.assertEqual({g["hoster"] for g in parcial}, {"MediaFire"})
        # ningún grupo de Mega/MegaUp en el parcial
        self.assertNotIn("Mega", {g["hoster"] for g in parcial})
        self.assertNotIn("MegaUp", {g["hoster"] for g in parcial})


class _CdpFake:
    """Fake de la conexión CDP para _episodios_serie_completa.
    Devuelve lo que esperan _extraer_episodios y _temporadas: los ANCLAS
    crudos {t, h} (el JS del navegador devuelve eso y las funciones los
    convierten a {label, url})."""

    def __init__(self, anclas_por_url):
        # url -> lista de anclas crudas {t, h}; "__season" devuelve las
        # temporadas de la página inicial; "<actual>" son los episodios
        # directos de la página inicial
        self._mapa = anclas_por_url or {}
        self._actual = "<directos>"

    def navegar(self, url, condicion=None, tiempo_max=None):
        self._actual = url
        return True

    def eval(self, js):
        if "series/season/" in js and "episode" not in js:
            return self._mapa.get("__season", [])
        if "series/episode/" in js:
            return self._mapa.get(self._actual) or []
        return None

    def cerrar(self):
        pass


class TestEpisodiosSerieCompleta(unittest.TestCase):
    """La página `/series/ataque-a-los-titanes` lista TEMPORADAS en vez de
    episodios directos: _episodios_serie_completa debe recorrerlas y juntar
    todos los episodios (antes buscaba solo episodios directos y fallaba)."""

    def _ancla(self, p, n):
        return {"t": "%dx%d" % (p, n),
                "h": "https://zona-leros.com/series/episode/titulo-%d-%d" % (p, n)}

    def test_pagina_con_episodios_directos(self):
        anclas = [self._ancla(1, i) for i in (1, 2, 3)]
        cdp = _CdpFake({"<directos>": anclas})
        r, err = zonaleros._episodios_serie_completa(
            cdp, "https://zona-leros.com/series/x", time.time() + 240)
        self.assertIsNone(err)
        self.assertEqual(len(r), 3)
        self.assertEqual({e["label"] for e in r}, {"1x1", "1x2", "1x3"})

    def test_pagina_que_lista_temporadas_recorre_todas(self):
        # la página inicial NO tiene episodios, solo enlaces a temporadas
        temporadas = [
            {"h": "https://zona-leros.com/series/season/x-1", "t": "Temp 1"},
            {"h": "https://zona-leros.com/series/season/x-2", "t": "Temp 2"},
        ]
        cdp = _CdpFake({
            "<directos>": [],
            "__season": temporadas,
            "https://zona-leros.com/series/season/x-1": [
                self._ancla(1, i) for i in (1, 2)],
            "https://zona-leros.com/series/season/x-2": [
                self._ancla(2, i) for i in (1, 2)],
        })
        r, err = zonaleros._episodios_serie_completa(
            cdp, "https://zona-leros.com/series/x", time.time() + 240)
        self.assertIsNone(err)
        self.assertEqual(len(r), 4)
        labels = {e["label"] for e in r}
        self.assertEqual(labels, {"1x1", "1x2", "2x1", "2x2"})

    def test_sin_episodios_ni_temporadas_devuelve_vacio(self):
        cdp = _CdpFake({"<directos>": []})
        r, err = zonaleros._episodios_serie_completa(
            cdp, "https://zona-leros.com/series/x", time.time() + 240)
        self.assertIsNone(err)
        self.assertEqual(r, [])

    def test_temporadas_sin_episodios_evitan_duplicados(self):
        temporadas = [
            {"h": "https://zona-leros.com/series/season/x-1", "t": "T1"},
            {"h": "https://zona-leros.com/series/season/x-2", "t": "T2"},
        ]
        ancla = self._ancla(1, 1)
        cdp = _CdpFake({
            "<directos>": [],
            "__season": temporadas,
            "https://zona-leros.com/series/season/x-1": [ancla],
            "https://zona-leros.com/series/season/x-2": [ancla],  # duplicado
        })
        r, err = zonaleros._episodios_serie_completa(
            cdp, "https://zona-leros.com/series/x", time.time() + 240)
        self.assertEqual(len(r), 1)


class TestTituloValido(unittest.TestCase):
    """El título del resultado nunca debe ser un texto genérico de error
    ("Página no encontrada", retos de Cloudflare…): se reemplaza por un
    fallback presentable por tipo de página."""

    def test_quita_sufijo_del_sitio(self):
        self.assertEqual(zonaleros._titulo_valido("Ataque a los Titanes | ZonaLeRoS"),
                         "Ataque a los Titanes")

    def test_titulos_de_error_usan_fallback(self):
        for malo in ("Página no encontrada", "Page Not Found", "404 Not Found",
                     "Just a moment...", "Attention Required", "Error 403",
                     "   ", ""):
            self.assertEqual(zonaleros._titulo_valido(malo, "Serie ZonaLeros"),
                             "Serie ZonaLeros", malo)

    def test_titulo_valido_pasa(self):
        self.assertEqual(zonaleros._titulo_valido("Ver Ataque a los Titanes 2x1"),
                         "Ver Ataque a los Titanes 2x1")

    def test_recorta_a_150(self):
        self.assertEqual(len(zonaleros._titulo_valido("x" * 300)), 150)


class TestConsolidarResultadosSerie(unittest.TestCase):
    """Los episodios que no entregan enlaces no deben aparecer como tarjetas
    de servidor vacías; deben quedar reportados como fallidos."""

    def test_separa_episodio_sin_enlaces(self):
        episodios = [
            {"label": "S02E01", "url": "https://example/1"},
            {"label": "S02E02", "url": "https://example/2"},
        ]
        resultados = [
            (0, [{"servidor": "MediaFire", "enlaces": [{"url": "u1"}]}], False, None),
            (1, [], True, "sin enlaces de descarga"),
        ]
        servidores, incompleto, fallidos = zonaleros._consolidar_resultados_serie(
            resultados, episodios)
        self.assertEqual(len(servidores), 1)
        self.assertTrue(incompleto)
        self.assertEqual([x["label"] for x in fallidos], ["S02E02"])
        self.assertNotIn("enlaces", fallidos[0])

    def test_resultado_reintentado_valido_no_falla(self):
        episodios = [{"label": "S02E03", "url": "https://example/3"}]
        resultados = [
            (0, [{"servidor": "MediaFire", "enlaces": [{"url": "u3"}]}], False, None),
        ]
        servidores, incompleto, fallidos = zonaleros._consolidar_resultados_serie(
            resultados, episodios)
        self.assertEqual(len(servidores), 1)
        self.assertFalse(incompleto)
        self.assertEqual(fallidos, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)