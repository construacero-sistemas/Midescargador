# -*- coding: utf-8 -*-
"""
MiDescargador - Subida a Google Drive.
OAuth2 (alcance drive.file: la app solo ve los archivos/carpetas que ella
misma crea) + subida resumible por chunks, con reintentos por chunk y
progreso. Sin dependencias externas (solo la biblioteca estándar).

Uso (desde servidor.py):
    import drive
    drive.inicializar(_dir_datos())
    drive.guardar_credenciales(client_id, client_secret)
    url = drive.url_autorizacion()            # abrir en el navegador
    drive.intercambiar_codigo(code)           # callback de Google
    drive.subir_archivo(ruta, on_progreso=cb) # devuelve {id, url}
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/drive/v3/files"
UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3/files"
SCOPES = "https://www.googleapis.com/auth/drive.file"
CHUNK = 8 * 1024 * 1024          # 8 MB por chunk (máximo recomendado por Google)
REINTENTOS_CHUNK = 3             # intentos por chunk antes de fallar
TIMEOUT = 120                    # segundos por request de chunk

# carpeta de datos persistente (la setea servidor.py); en pruebas se puede
# apuntar a un directorio temporal
DIR_DATOS = None

# token de acceso vivo: se pide al subir y se cachea hasta que expire
_ACCESS = {"token": None, "expira": 0}


# ---------------------------------------------------------------- rutas

def inicializar(dir_datos):
    """Define dónde se guardan credenciales y refresh token."""
    global DIR_DATOS
    DIR_DATOS = dir_datos


def _ruta_creds():
    return os.path.join(DIR_DATOS or "", "drive_creds.json")


def _ruta_token():
    return os.path.join(DIR_DATOS or "", "drive_token.json")


# ------------------------------------------------------------ credenciales

def guardar_credenciales(client_id, client_secret):
    """Persiste el Client ID/Secret de Google Cloud (OAuth 'Desktop app')."""
    if not client_id or not client_secret:
        raise ValueError("Faltan el Client ID o el Client Secret de Google")
    os.makedirs(DIR_DATOS or ".", exist_ok=True)
    with open(_ruta_creds(), "w", encoding="utf-8") as f:
        json.dump({"client_id": str(client_id).strip(),
                   "client_secret": str(client_secret).strip()}, f)


def credenciales():
    """(client_id, client_secret) o None si no se configuraron."""
    try:
        with open(_ruta_creds(), encoding="utf-8") as f:
            c = json.load(f)
        return c.get("client_id"), c.get("client_secret")
    except Exception:
        return None, None


# ------------------------------------------------------- OAuth2 (flujo)

def url_autorizacion():
    """URL del consentimiento de Google (redirect a la API local)."""
    cid, _ = credenciales()
    if not cid:
        raise RuntimeError("Configurá primero el Client ID de Google en "
                           "Configuración → Google Drive")
    q = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": "http://127.0.0.1:17890/api/drive/oauth",
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",   # fuerza el refresh_token aunque ya se autorizó
    })
    return AUTH_URL + "?" + q


def _pedir_token(params):
    """POST a oauth2.googleapis.com/token y devuelve el JSON de respuesta."""
    datos = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(TOKEN_URL, data=datos, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8")), r.status
    except urllib.error.HTTPError as e:
        try:
            cuerpo = json.loads(e.read().decode("utf-8"))
            err = cuerpo.get("error_description") or cuerpo.get("error") \
                or "HTTP %d" % e.code
        except Exception:
            err = "HTTP %d" % e.code
        raise RuntimeError("Google rechazó el token: " + str(err)) from e


def intercambiar_codigo(code):
    """Cambia el código de autorización por refresh_token y lo guarda.
    Devuelve la cuenta conectada (email)."""
    cid, secret = credenciales()
    if not cid or not secret:
        raise RuntimeError("Configurá primero el Client ID/Secret de Google")
    r, _ = _pedir_token({
        "code": code,
        "client_id": cid,
        "client_secret": secret,
        "redirect_uri": "http://127.0.0.1:17890/api/drive/oauth",
        "grant_type": "authorization_code",
    })
    if not r.get("refresh_token"):
        raise RuntimeError("Google no devolvió refresh_token (revisá que el "
                           "tipo de credencial sea 'Desktop app')")
    cuenta = _quien_soy(r.get("access_token"))
    os.makedirs(DIR_DATOS or ".", exist_ok=True)
    with open(_ruta_token(), "w", encoding="utf-8") as f:
        json.dump({"refresh_token": r["refresh_token"], "cuenta": cuenta}, f)
    _ACCESS["token"] = r.get("access_token")
    _ACCESS["expira"] = time.time() + int(r.get("expires_in") or 3600) - 60
    return cuenta


def desconectar():
    """Borra el refresh token (la app deja de poder subir)."""
    try:
        os.remove(_ruta_token())
    except OSError:
        pass
    _ACCESS["token"] = None
    _ACCESS["expira"] = 0


def _quien_soy(access_token):
    """Email de la cuenta conectada (endpoint about)."""
    try:
        req = urllib.request.Request(
            API_BASE + "/about?fields=user",
            headers={"Authorization": "Bearer " + (access_token or "")})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8"))
        return (d.get("user") or {}).get("emailAddress") or "cuenta de Google"
    except Exception:
        return "cuenta de Google"


# ------------------------------------------------------- access token

def _access_token():
    """Access token vigente (renueva con el refresh token si expiró)."""
    if _ACCESS["token"] and _ACCESS["expira"] > time.time():
        return _ACCESS["token"]
    try:
        with open(_ruta_token(), encoding="utf-8") as f:
            t = json.load(f)
    except Exception:
        raise RuntimeError("No hay sesión de Google Drive. Conectala en "
                           "Configuración → Google Drive.")
    refresh = t.get("refresh_token")
    if not refresh:
        raise RuntimeError("No hay sesión de Google Drive (token inválido). "
                           "Conectala de nuevo.")
    cid, secret = credenciales()
    if not cid or not secret:
        raise RuntimeError("Faltan el Client ID/Secret de Google")
    r, _ = _pedir_token({
        "refresh_token": refresh,
        "client_id": cid,
        "client_secret": secret,
        "grant_type": "refresh_token",
    })
    _ACCESS["token"] = r.get("access_token")
    _ACCESS["expira"] = time.time() + int(r.get("expires_in") or 3600) - 60
    return _ACCESS["token"]


def estado():
    """{conectado, cuenta, error} sin tocar la red si no hay token."""
    try:
        with open(_ruta_token(), encoding="utf-8") as f:
            t = json.load(f)
        return {"conectado": True,
                "cuenta": t.get("cuenta") or "cuenta de Google"}
    except Exception:
        return {"conectado": False, "cuenta": None}


# ------------------------------------------------------- subida resumible

def _api(token, nombre_buscar):
    """GET a la API de Drive para buscar la carpeta por nombre. Devuelve
    el primer archivo de tipo carpeta cuyo nombre coincide (o None).
    El 404/400 con 'File not found' se traduce para dar pista clara."""
    q = urllib.parse.quote(
        "name='%s' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        % nombre_buscar.replace("'", "\\'"))
    url = API_BASE + "?q=%s&fields=files(id,name,mimeType)" % q
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            datos = json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError("Google rechazó la subida (%d): %s"
                           % (e.code, _cuerpo_error(e))) from e
    archivos = datos.get("files") or []
    return archivos[0]["id"] if archivos else None


def _crear_carpeta(token, nombre):
    """Crea una carpeta en Drive y devuelve su ID."""
    body = json.dumps({
        "name": nombre,
        "mimeType": "application/vnd.google-apps.folder",
    }).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + "?fields=id,name,mimeType", data=body, method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json; charset=UTF-8",
        })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8")).get("id")
    except urllib.error.HTTPError as e:
        raise RuntimeError("Google rechazó la subida (%d): %s"
                           % (e.code, _cuerpo_error(e))) from e


def _id_carpeta_upload(token, carpeta="MiDescargador"):
    """Devuelve el ID real (en Drive) de la carpeta donde subir. Si la
    carpeta indicada no existe, la crea. Evita el 404 'File not found'
    que ocurre cuando se pasa el nombre (texto) como si fuera un ID."""
    if not carpeta:
        return None  # raíz
    pid = _api(token, carpeta)
    if pid is None:
        pid = _crear_carpeta(token, carpeta)
    return pid


def _iniciar_sesion_upload(token, nombre, tamano, carpeta="MiDescargador"):
    """POST inicial del flujo resumible; devuelve la URI de subida (Location).
    Carpeta se interpreta como nombre (por defecto 'MiDescargador'); se
    resuelve a su ID real, creándola si hace falta."""
    parent = _id_carpeta_upload(token, carpeta)
    body = json.dumps({
        "name": nombre,
        "parents": [parent] if parent else [],
    }).encode("utf-8")
    req = urllib.request.Request(
        UPLOAD_BASE + "?uploadType=resumable&fields=id,name,webViewLink,size",
        data=body, method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "application/octet-stream",
            "X-Upload-Content-Length": str(tamano),
        })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.headers.get("Location") or r.geturl()
    except urllib.error.HTTPError as e:
        raise RuntimeError("Google rechazó la subida (%d): %s"
                           % (e.code, _cuerpo_error(e))) from e


def _cuerpo_error(e):
    try:
        d = json.loads(e.read().decode("utf-8"))
        return (d.get("error") or {}).get("message") or e.reason or ""
    except Exception:
        return e.reason or ""


def _subir_chunk(uri, token, chunk, inicio, tamano, es_ultimo):
    """PUT de un chunk con Content-Range. Devuelve (estado_http, body, location)."""
    fin = inicio + len(chunk) - 1
    rango = "bytes %d-%d/%d" % (inicio, fin, tamano)
    req = urllib.request.Request(
        uri, data=chunk, method="PUT",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Range": rango,
            "Content-Length": str(len(chunk)),
        })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            cuerpo = r.read()
            return r.status, cuerpo, r.headers.get("Location")
    except urllib.error.HTTPError as e:
        cuerpo = e.read()
        # 308 = el servidor espera más chunks; puede venir sin Location
        return e.code, cuerpo, e.headers.get("Location")


def subir_archivo(ruta, nombre=None, carpeta="MiDescargador",
                  on_progreso=None):
    """Sube el archivo a Google Drive (subida resumible, 8 MB por chunk).
    Devuelve {id, url}. on_progreso(pct) se llama con 0..100."""
    if not os.path.isfile(ruta):
        raise RuntimeError("El archivo no existe: %s" % ruta)
    tamano = os.path.getsize(ruta)
    if tamano <= 0:
        raise RuntimeError("El archivo está vacío: %s" % os.path.basename(ruta))
    token = _access_token()
    nombre = nombre or os.path.basename(ruta)
    uri = _iniciar_sesion_upload(token, nombre, tamano, carpeta)

    enviados = 0
    with open(ruta, "rb") as f:
        while enviados < tamano:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            inicio = enviados
            es_ultimo = (inicio + len(chunk)) >= tamano
            for intento in range(1, REINTENTOS_CHUNK + 1):
                status, cuerpo, loc = _subir_chunk(
                    uri, token, chunk, inicio, tamano, es_ultimo)
                if status in (200, 201):
                    try:
                        d = json.loads(cuerpo.decode("utf-8"))
                    except Exception:
                        d = {}
                    url = d.get("webViewLink") or (
                        "https://drive.google.com/file/d/%s/view"
                        % d.get("id", ""))
                    if on_progreso:
                        on_progreso(100)
                    return {"id": d.get("id", ""), "url": url}
                if status == 308:
                    enviados += len(chunk)
                    if on_progreso:
                        on_progreso(int(enviados * 100.0 / tamano))
                    break  # chunk OK, siguiente
                if intento < REINTENTOS_CHUNK:
                    time.sleep(1.5 * intento)  # backoff
                    uri = loc or uri
                else:
                    raise RuntimeError(
                        "Falló la subida a Google Drive en el byte %d "
                        "(HTTP %d)" % (inicio, status))
            else:
                raise RuntimeError("No se pudo avanzar la subida a Drive")
    raise RuntimeError("La subida terminó sin confirmar (tamaño inesperado)")
