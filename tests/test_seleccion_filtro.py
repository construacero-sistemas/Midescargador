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


if __name__ == "__main__":
    unittest.main(verbosity=2)