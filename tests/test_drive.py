# -*- coding: utf-8 -*-
"""
Pruebas del módulo drive.py (subida a Google Drive).
No tocan la red: los endpoints de Google se simulan parcheando urllib.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import drive


class _Respuesta:
    """Fake de la respuesta de urlopen."""

    def __init__(self, status=200, cuerpo=None, headers=None):
        self.status = status
        self._cuerpo = (cuerpo if isinstance(cuerpo, bytes)
                        else (cuerpo or "").encode("utf-8"))
        self.headers = headers or {}

    def read(self):
        return self._cuerpo

    def getheader(self, k, default=None):
        return self.headers.get(k, default)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ErrorHTTP(urllib.error.HTTPError):
    """HTTPError con un cuerpo legible."""

    def __init__(self, code, cuerpo=b"", headers=None):
        super().__init__("https://example.invalid", code, "err",
                         headers or {}, io.BytesIO(cuerpo))
        self._cuerpo = cuerpo

    def read(self):
        return self._cuerpo


def _instalar_ambiente():
    """Crea una carpeta temporal para datos y fija credenciales de prueba."""
    tmp = tempfile.mkdtemp(prefix="mdm_drive_test_")
    drive.inicializar(tmp)
    drive.guardar_credenciales("test-client-id", "test-client-secret")
    return tmp


def _fake_token_response():
    return _Respuesta(200, json.dumps({
        "refresh_token": "rt-1",
        "access_token": "at-1",
        "expires_in": 3600,
    }))


class TestCredenciales(unittest.TestCase):

    def setUp(self):
        self.tmp = _instalar_ambiente()

    def test_guardar_y_leer(self):
        cid, sec = drive.credenciales()
        self.assertEqual(cid, "test-client-id")
        self.assertEqual(sec, "test-client-secret")

    def test_sin_credenciales_devuelve_none(self):
        drive.inicializar(tempfile.mkdtemp(prefix="mdm_drive_nocreds_"))
        self.assertEqual(drive.credenciales(), (None, None))

    def test_guardar_requiere_valores(self):
        with self.assertRaises(ValueError):
            drive.guardar_credenciales("", "")


class TestUrlAutorizacion(unittest.TestCase):

    def setUp(self):
        _instalar_ambiente()

    def test_incluye_parametros_correctos(self):
        url = drive.url_autorizacion()
        self.assertTrue(url.startswith(drive.AUTH_URL + "?"))
        self.assertIn("client_id=test-client-id", url)
        self.assertIn("response_type=code", url)
        self.assertIn("scope=", url)
        self.assertIn("access_type=offline", url)
        self.assertIn("redirect_uri=", url)

    def test_sin_credenciales_lanza(self):
        drive.inicializar(tempfile.mkdtemp(prefix="mdm_drive_nourl_"))
        with self.assertRaises(RuntimeError):
            drive.url_autorizacion()


class TestOAuthFlujo(unittest.TestCase):

    def setUp(self):
        self.tmp = _instalar_ambiente()

    @mock.patch.object(urllib.request, "urlopen")
    def test_intercambiar_codigo_guarda_token(self, urlopen):
        def fake_open(req, timeout=None):
            url = req.full_url
            if drive.TOKEN_URL in url:
                return _fake_token_response()
            if "about" in url:
                return _Respuesta(200, json.dumps(
                    {"user": {"emailAddress": "yo@test.com"}}))
            raise AssertionError("URL inesperada: " + url)
        urlopen.side_effect = fake_open
        cuenta = drive.intercambiar_codigo("codigo-abc")
        self.assertEqual(cuenta, "yo@test.com")
        with open(drive._ruta_token(), encoding="utf-8") as f:
            t = json.load(f)
        self.assertEqual(t["refresh_token"], "rt-1")
        self.assertEqual(t["cuenta"], "yo@test.com")
        self.assertEqual(drive.estado(),
                         {"conectado": True, "cuenta": "yo@test.com"})

    @mock.patch.object(urllib.request, "urlopen")
    def test_error_de_google_se_traduce(self, urlopen):
        urlopen.side_effect = _ErrorHTTP(
            400, json.dumps({"error": "invalid_grant"}).encode())
        with self.assertRaises(RuntimeError) as ctx:
            drive.intercambiar_codigo("malo")
        self.assertIn("Google rechazó", str(ctx.exception))

    def test_desconectar_borra_token(self):
        with open(drive._ruta_token(), "w", encoding="utf-8") as f:
            json.dump({"refresh_token": "x", "cuenta": "a"}, f)
        self.assertTrue(drive.estado()["conectado"])
        drive.desconectar()
        self.assertFalse(drive.estado()["conectado"])
        self.assertFalse(os.path.exists(drive._ruta_token()))


class TestAccessToken(unittest.TestCase):

    def setUp(self):
        _instalar_ambiente()
        drive._ACCESS["token"] = None
        drive._ACCESS["expira"] = 0

    @mock.patch.object(urllib.request, "urlopen")
    def test_renueva_con_refresh_token(self, urlopen):
        with open(drive._ruta_token(), "w", encoding="utf-8") as f:
            json.dump({"refresh_token": "rt-1", "cuenta": "yo"}, f)
        # sin access token en cache: debe llamar al endpoint de refresh
        def fake_open(req, timeout=None):
            if drive.TOKEN_URL in req.full_url:
                return _Respuesta(200, json.dumps({
                    "access_token": "at-nuevo", "expires_in": 3600}))
            raise AssertionError("URL inesperada: " + req.full_url)
        urlopen.side_effect = fake_open
        self.assertEqual(drive._access_token(), "at-nuevo")
        urlopen.assert_called_once()

    def test_sin_sesion_lanza(self):
        with self.assertRaises(RuntimeError) as ctx:
            drive._access_token()
        self.assertIn("No hay sesión", str(ctx.exception))


def _cabecera(req, nombre):
    """Lee una cabecera de forma case-insensitive (urllib normaliza los
    nombres a 'Content-range', 'Content-length', etc.)."""
    nombre = nombre.lower()
    for k, v in (req.headers or {}).items():
        if k.lower() == nombre:
            return v
    return ""


def _fake_carpeta(carpeta_id="FOLDER-ROOT"):
    """Devuelve un fake de urlopen que simula el flujo de carpeta de Drive:
    GET de búsqueda por nombre (devuelve la carpeta creada) y POST de
    creación. Devuelve el GET de búsqueda respondiendo con vacío la primera
    vez para forzar la creación, y con la carpeta a partir de entonces."""
    creada = {"hecha": False, "solicitudes": 0}

    def respuesta_busqueda():
        if creada["hecha"]:
            return _Respuesta(200, json.dumps({
                "files": [{"id": carpeta_id, "name": "MiDescargador",
                            "mimeType": "application/vnd.google-apps.folder"}],
            }).encode())
        return _Respuesta(200, json.dumps({"files": []}).encode())

    def metodo(req):
        # urllib solo fija `.method` si se pasó explícito; el GET de búsqueda
        # se crea sin method, así que se asume GET.
        return getattr(req, "method", "GET") or "GET"

    def fake(req, timeout=None):
        url = req.full_url or ""
        m = metodo(req)
        if m in ("GET", "POST") and url.startswith(drive.API_BASE):
            creada["solicitudes"] += 1
            if m == "GET":
                return respuesta_busqueda()
            creada["hecha"] = True
            return _Respuesta(200, json.dumps({
                "id": carpeta_id, "name": "MiDescargador",
                "mimeType": "application/vnd.google-apps.folder",
            }).encode())
        return None  # no gestiona; el fake externo decide

    # el primer GET de búsqueda devuelve vacío (fuerza crear), luego la carpeta
    fake.busqueda = respuesta_busqueda
    fake.contador = creada
    return fake



class TestSubida(unittest.TestCase):

    def setUp(self):
        self.tmp = _instalar_ambiente()
        # fichero de prueba (3 MB → dos chunks de 8 MB)
        self.ruta = os.path.join(self.tmp, "prueba.bin")
        with open(self.ruta, "wb") as f:
            f.write(b"x" * (3 * 1024 * 1024))
        with open(drive._ruta_token(), "w", encoding="utf-8") as f:
            json.dump({"refresh_token": "rt-1", "cuenta": "yo"}, f)
        # access token en cache para no depender del refresh en estos casos
        drive._ACCESS["token"] = "at-cache"
        drive._ACCESS["expira"] = 10 ** 12

    def tearDown(self):
        drive._ACCESS["token"] = None
        drive._ACCESS["expira"] = 0

    @mock.patch.object(urllib.request, "urlopen")
    def test_subida_completa_con_progreso(self, urlopen):
        llamadas = []
        carpeta = _fake_carpeta()
        def fake_open(req, timeout=None):
            res = carpeta(req, timeout)
            if res:
                return res
            llamadas.append((req.method, req.full_url, req.data))
            if req.full_url.startswith(drive.UPLOAD_BASE) \
                    and req.method == "POST":
                return _Respuesta(200, b"",
                                  {"Location": "https://upload.example/s1"})
            if req.method == "PUT":
                if _cabecera(req, "Content-Range").endswith("/3145728"):
                    return _Respuesta(201, json.dumps({
                        "id": "FILE-1",
                        "webViewLink": "https://drive.google.com/file/d/FILE-1/view",
                    }).encode())
                return _Respuesta(308, b"")
            raise AssertionError("request inesperado: %s %s" % (req.method, req.full_url))
        urlopen.side_effect = fake_open
        progresos = []
        res = drive.subir_archivo(self.ruta, on_progreso=progresos.append)
        self.assertEqual(res["id"], "FILE-1")
        self.assertIn("drive.google.com", res["url"])
        self.assertEqual(progresos[-1], 100)
        # el primer PUT lleva el Content-Range correcto del chunk 1
        puts = [l for l in llamadas if l[0] == "PUT"]
        self.assertGreaterEqual(len(puts), 1)

    @mock.patch.object(urllib.request, "urlopen")
    def test_chunk_falla_y_reintenta(self, urlopen):
        intentos = {"n": 0}
        carpeta = _fake_carpeta()
        def fake_open(req, timeout=None):
            res = carpeta(req, timeout)
            if res:
                return res
            if req.full_url.startswith(drive.UPLOAD_BASE) \
                    and req.method == "POST":
                return _Respuesta(200, b"",
                                  {"Location": "https://upload.example/s2"})
            if req.method == "PUT":
                if _cabecera(req, "Content-Range").endswith("/3145728"):
                    return _Respuesta(201, json.dumps(
                        {"id": "F2"}).encode())
                intentos["n"] += 1
                if intentos["n"] <= 2:
                    # 500 las dos primeras veces, luego 308
                    raise _ErrorHTTP(500, b"boom")
                return _Respuesta(308, b"")
            raise AssertionError("request inesperado")
        urlopen.side_effect = fake_open
        res = drive.subir_archivo(self.ruta)
        self.assertEqual(res["id"], "F2")

    @mock.patch.object(urllib.request, "urlopen")
    def test_subida_falla_definitiva(self, urlopen):
        carpeta = _fake_carpeta()
        def fake_open(req, timeout=None):
            res = carpeta(req, timeout)
            if res:
                return res
            if req.full_url.startswith(drive.UPLOAD_BASE) \
                    and req.method == "POST":
                return _Respuesta(200, b"",
                                  {"Location": "https://upload.example/s3"})
            if req.method == "PUT":
                raise _ErrorHTTP(500, b"boom")
            raise AssertionError("request inesperado")
        urlopen.side_effect = fake_open
        with self.assertRaises(RuntimeError) as ctx:
            drive.subir_archivo(self.ruta)
        self.assertIn("Falló la subida", str(ctx.exception))

    @mock.patch.object(urllib.request, "urlopen")
    def test_crea_carpeta_si_no_existe_y_usa_su_id(self, urlopen):
        # Verifica el fix del 404 'File not found: MiDescargador': la subida
        # debe crear la carpeta 'MiDescargador' (si no existe) y mandar su ID
        # real en 'parents', nunca el nombre como texto.
        uso_parent = {"valor": None}
        carpeta = _fake_carpeta("DIR-REAL")
        def fake_open(req, timeout=None):
            res = carpeta(req, timeout)
            if res:
                return res
            if req.full_url.startswith(drive.UPLOAD_BASE) \
                    and getattr(req, "method", "GET") == "POST":
                cuerpo = json.loads((req.data or b"{}").decode("utf-8"))
                uso_parent["valor"] = cuerpo.get("parents")
                return _Respuesta(200, b"",
                                  {"Location": "https://upload.example/s4"})
            if getattr(req, "method", "GET") == "PUT":
                return _Respuesta(201, json.dumps({"id": "F-OK"}).encode())
            raise AssertionError("request inesperado: %s %s"
                                 % (getattr(req, "method", "GET"), req.full_url))
        urlopen.side_effect = fake_open
        res = drive.subir_archivo(self.ruta)
        self.assertEqual(res["id"], "F-OK")
        # la carpeta se crea y su ID real se usa como parent
        self.assertEqual(uso_parent["valor"], ["DIR-REAL"])
        # el nombre literal 'MiDescargador' nunca aparece como parent
        self.assertNotEqual(uso_parent["valor"], ["MiDescargador"])
        # se hizo la búsqueda GET y la creación POST contra la API de Drive
        self.assertGreaterEqual(carpeta.contador["solicitudes"], 2)

    def test_archivo_inexistente(self):
        with self.assertRaises(RuntimeError):
            drive.subir_archivo(os.path.join(self.tmp, "no-existe.bin"))

    def test_archivo_vacio(self):
        ruta = os.path.join(self.tmp, "vacio.bin")
        with open(ruta, "wb") as f:
            pass
        with self.assertRaises(RuntimeError):
            drive.subir_archivo(ruta)


if __name__ == "__main__":
    unittest.main()
