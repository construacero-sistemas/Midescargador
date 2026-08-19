# -*- coding: utf-8 -*-
"""Descompresión automática tras la descarga.

Usa el WinRAR/UnRAR/7-Zip instalado en el sistema (soportan .rar, .zip, .7z,
.tar, .gz, ...) y contraseñas. Si no hay herramienta externa, extrae .zip
planos con la librería estándar de Python.
"""
import os
import re
import shutil
import subprocess
import zipfile

_EXT_COMPRIMIDOS = (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2",
                    ".xz", ".tgz", ".tbz2", ".txz")

_RUTA_7Z = None
_RUTA_UNRAR = None
_RUTA_WINRAR = None


def _existe(ruta):
    return bool(ruta) and os.path.exists(ruta)


def _ruta_7z():
    global _RUTA_7Z
    if _RUTA_7Z is not None:
        return _RUTA_7Z if _existe(_RUTA_7Z) else None
    for p in (
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\7-Zip\7z.exe"),
    ):
        if _existe(p):
            _RUTA_7Z = p
            return p
    _RUTA_7Z = shutil.which("7z") or ""
    return _RUTA_7Z or None


def _ruta_unrar():
    global _RUTA_UNRAR
    if _RUTA_UNRAR is not None:
        return _RUTA_UNRAR if _existe(_RUTA_UNRAR) else None
    for p in (
        r"C:\Program Files\WinRAR\UnRAR.exe",
        r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
    ):
        if _existe(p):
            _RUTA_UNRAR = p
            return p
    _RUTA_UNRAR = shutil.which("unrar") or ""
    return _RUTA_UNRAR or None


def _ruta_winrar():
    global _RUTA_WINRAR
    if _RUTA_WINRAR is not None:
        return _RUTA_WINRAR if _existe(_RUTA_WINRAR) else None
    for p in (
        r"C:\Program Files\WinRAR\WinRAR.exe",
        r"C:\Program Files (x86)\WinRAR\WinRAR.exe",
    ):
        if _existe(p):
            _RUTA_WINRAR = p
            return p
    _RUTA_WINRAR = shutil.which("winrar") or ""
    return _RUTA_WINRAR or None


def herramienta_disponible():
    """True si hay alguna herramienta capaz de extraer archivos protegidos."""
    return bool(_ruta_winrar() or _ruta_unrar() or _ruta_7z())


def es_comprimido(nombre):
    """True si el archivo parece un comprimido (por extensión)."""
    n = (nombre or "").lower()
    return n.endswith(_EXT_COMPRIMIDOS)


def es_parte_secundaria(nombre):
    """Partes de CONTINUACIÓN de un conjunto multiparte: no se extraen solas.

    - game.part2.rar, game.part3.rar ...  (solo part1 extrae el conjunto)
    - game.r00, game.r01 ...             (el .rar principal extrae el conjunto)
    - game.001, game.002 ... (excepto .000/.001 inicial)
    El número de parte es el ÚLTIMO antes de la extensión, porque el nombre
    base puede contener "part": game.part1.part2.rar -> parte 2 (secundaria).
    """
    n = (nombre or "").lower()
    base = os.path.splitext(n)[0]
    m = re.search(r"\.part(\d+)$", base)
    if m:
        return int(m.group(1)) > 1
    m = re.search(r"\.r(\d{2,3})$", n)
    if m and m.group(1) not in ("00", "000"):
        return True
    m = re.search(r"\.(\d{3})$", n)
    if m and m.group(1) not in ("000", "001"):
        return True
    return False


def _zip_tiene_escape(ruta_zip):
    """True si el .zip contiene entradas que escapan de la carpeta destino
    (../, rutas absolutas o drives de Windows). Se rechaza el archivo ENTERO
    antes de delegar en cualquier herramienta: ni Python ni WinRAR/7-Zip
    deben extraerlo (sus protecciones varían según la versión)."""
    try:
        with zipfile.ZipFile(ruta_zip) as z:
            for info in z.infolist():
                nombre = (info.filename or "").replace("\\", "/")
                if not nombre:
                    continue
                if nombre.startswith("/") or re.match(r"^[A-Za-z]:", nombre):
                    return True
                if ".." in nombre.split("/"):
                    return True
    except Exception:
        return False
    return False


def es_multiparte(nombre):
    """True si el archivo forma parte de un conjunto multiparte."""
    n = (nombre or "").lower()
    base = os.path.splitext(n)[0]
    return bool(re.search(r"\.part\d+$", base)) or bool(
        re.search(r"\.r\d{2,3}$", n))


def _destino(archivo):
    """Carpeta de extracción: junto al archivo, con el nombre base limpio.

    game.part1.rar      -> <carpeta>/game
    game.part1.part1.rar -> <carpeta>/game
    game.rar            -> <carpeta>/game
    fotos.tar.gz        -> <carpeta>/fotos
    """
    nombre = os.path.basename(archivo)
    base = nombre
    for ext in (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
                ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"):
        if base.lower().endswith(ext):
            base = base[:-len(ext)]
            break
    # quita los marcadores de parte del final, tantas veces como haya
    while True:
        nuevo = re.sub(r"\.(part\d+|r\d{2,3}|\d{3})$", "", base)
        if nuevo == base:
            break
        base = nuevo
    base = base.strip() or "extraido"
    return os.path.join(os.path.dirname(archivo), base)


def _clave_contraseña(password):
    """Devuelve el flag de contraseña para la CLI, o [] si no hay."""
    if not password:
        return []
    return ["-p" + password]


def _extraer_unrar(archivo, destino, password):
    cmd = [_ruta_unrar(), "x", "-y", "-o+"] + _clave_contraseña(password) + [
        archivo, destino + os.sep]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=1800, creationflags=getattr(
                           subprocess, "CREATE_NO_WINDOW", 0))
    salida = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0:
        return True, ""
    if r.returncode == 11 or "password" in salida.lower():
        return False, "contraseña incorrecta"
    if "cannot find volume" in salida.lower() or "no files to extract" in salida.lower():
        return False, "faltan partes del archivo (no completaron todas)"
    return False, "error al extraer (código %s)" % r.returncode


def _extraer_winrar(archivo, destino, password):
    cmd = [_ruta_winrar(), "x", "-y", "-ibck", "-o+"] + \
        _clave_contraseña(password) + [archivo, destino + os.sep]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=1800, creationflags=getattr(
                           subprocess, "CREATE_NO_WINDOW", 0))
    salida = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0:
        return True, ""
    if "password" in salida.lower():
        return False, "contraseña incorrecta o archivo protegido"
    return False, "error al extraer (código %s)" % r.returncode


def _extraer_zip_python(archivo, destino, password):
    """Fallback sin herramientas externas: solo .zip SIN contraseña."""
    try:
        with zipfile.ZipFile(archivo) as z:
            if z.testzip() is not None:
                return False, "el .zip está dañado"
            # anti zip-slip: las entradas no pueden escapar de la carpeta
            # destino (../, rutas absolutas o drives de Windows); un .zip
            # malicioso escribiría archivos en cualquier carpeta del usuario
            raiz = os.path.abspath(destino)
            for info in z.infolist():
                nombre = (info.filename or "").replace("\\", "/")
                if not nombre:
                    continue
                if nombre.startswith("/") or re.match(r"^[A-Za-z]:", nombre):
                    return False, ("el .zip contiene rutas absolutas "
                                   "(se cancela la extracción)")
                if ".." in nombre.split("/"):
                    return False, ("el .zip contiene rutas fuera de la "
                                   "carpeta (se cancela la extracción)")
                salida = os.path.abspath(os.path.join(destino, nombre))
                if salida != raiz and not salida.startswith(raiz + os.sep):
                    return False, ("el .zip contiene rutas fuera de la "
                                   "carpeta (se cancela la extracción)")
            os.makedirs(destino, exist_ok=True)
            z.extractall(destino)
        return True, ""
    except RuntimeError:
        return False, "el .zip está protegido con contraseña (instala WinRAR o 7-Zip)"
    except Exception as e:
        return False, "no se pudo extraer el .zip: %s" % e


def descomprimir(archivo, password=None):
    """Extrae `archivo` a una subcarpeta junto a él.

    Devuelve (ok, mensaje). Corre en el hilo que lo llame (puede tardar
    en archivos grandes); el llamador debe ejecutarlo en segundo plano.
    """
    if not os.path.exists(archivo):
        return False, "el archivo ya no existe"
    nombre = os.path.basename(archivo).lower()
    destino = _destino(archivo)
    # anti zip-slip: un .zip malicioso se rechaza aquí, ANTES de delegar en
    # cualquier herramienta de extracción (ni Python ni WinRAR/7-Zip lo tocan)
    if nombre.endswith(".zip") and _zip_tiene_escape(archivo):
        return False, ("el .zip contiene rutas fuera de la carpeta "
                       "(se cancela la extracción)")
    os.makedirs(destino, exist_ok=True)

    # .zip sin herramienta externa: lo hace Python directamente
    if nombre.endswith(".zip") and not (_ruta_winrar() or _ruta_7z()):
        ok, msg = _extraer_zip_python(archivo, destino, password)
        return ok, ("extraído a " + os.path.basename(destino)) if ok else msg

    if nombre.endswith(".rar"):
        unrar = _ruta_unrar()
        if unrar:
            ok, msg = _extraer_unrar(archivo, destino, password)
            return (ok, ("extraído a " + os.path.basename(destino))
                    if ok else msg)
        winrar = _ruta_winrar()
        if winrar:
            ok, msg = _extraer_winrar(archivo, destino, password)
            return (ok, ("extraído a " + os.path.basename(destino))
                    if ok else msg)
        return False, "no hay WinRAR/7-Zip instalado para extraer .rar"

    if nombre.endswith(".zip") and not password:
        ok, msg = _extraer_zip_python(archivo, destino, password)
        if ok:
            return True, "extraído a " + os.path.basename(destino)

    winrar = _ruta_winrar()
    if winrar:
        ok, msg = _extraer_winrar(archivo, destino, password)
        return (ok, ("extraído a " + os.path.basename(destino)) if ok else msg)
    siete = _ruta_7z()
    if siete:
        cmd = [siete, "x", "-y", "-o" + destino] + \
            (["-p" + password] if password else []) + [archivo]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0:
            return True, "extraído a " + os.path.basename(destino)
        if "password" in ((r.stdout or "") + (r.stderr or "")).lower():
            return False, "contraseña incorrecta"
        return False, "error al extraer (código %s)" % r.returncode
    return False, "no hay herramienta para extraer este formato"
