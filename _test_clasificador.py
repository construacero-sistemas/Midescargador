# -*- coding: utf-8 -*-
"""Pruebas rápidas del clasificador de multipartes de zonaleros.py."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zonaleros import _clasificar_enlaces, _nombre_de_url, _clave_parte


def entrada(url, texto="", nombre=None):
    return {"url": url, "texto": texto,
            "nombre": nombre if nombre is not None else _nombre_de_url(url)}


def mostrar(titulo, entradas):
    r = _clasificar_enlaces(entradas)
    print("\n###", titulo)
    print("  es_multipartes:", r["es_multipartes"],
          "| total:", r["total_partes"], "| base:", r["nombre_base"])
    for e in r["enlaces"]:
        print("   parte %-2s de %-2s | %-30s | %s" %
              (e["parte"] or "-", e["total"] or "-", e["nombre"] or "(sin nombre)", e["url"][:55]))


# 1) multipartes .partN desordenados (mediafire real)
mostrar("partN desordenado", [
    entrada("https://www.mediafire.com/file/x1/Juego.part3.rar/file", nombre="Juego.part3.rar"),
    entrada("https://www.mediafire.com/file/x2/Juego.part1.rar/file", nombre="Juego.part1.rar"),
    entrada("https://www.mediafire.com/file/x3/Juego.part2.rar/file", nombre="Juego.part2.rar"),
])

# 2) split winrar: .rar + .r00 + .r01
mostrar("winrar .rar+.r00+.r01", [
    entrada("https://host/1", nombre="Juego.r01"),
    entrada("https://host/2", nombre="Juego.rar"),
    entrada("https://host/3", nombre="Juego.r00"),
])

# 3) 7z partido .001 .002 .003
mostrar("7z partido .001..003", [
    entrada("https://host/a", nombre="Juego.7z.002"),
    entrada("https://host/b", nombre="Juego.7z.001"),
    entrada("https://host/c", nombre="Juego.7z.003"),
])

# 4) archivo único
mostrar("archivo único", [entrada("https://mega.nz/file/yao1CQjB#ZZ", texto="")])

# 5) varios archivos sueltos (no multipartes)
mostrar("varios archivos sueltos", [
    entrada("https://host/1", nombre="Crack.rar"),
    entrada("https://host/2", nombre="Juego.rar"),
    entrada("https://host/3", nombre="Parche.v2.rar"),
])

# 6) enlaces rootz /d/ sin nombre, etiquetas "Parte N"
mostrar("rootz /d/ con etiquetas Parte", [
    entrada("https://www.rootz.so/d/1OC8iu", texto="Parte 1"),
    entrada("https://www.rootz.so/d/JZbge", texto="Parte 2"),
    entrada("https://www.rootz.so/d/1gSRSi", texto="Parte 3"),
    entrada("https://www.rootz.so/d/1SDzaS", texto="Parte 4"),
    entrada("https://www.rootz.so/d/TmClU", texto="Parte 5"),
    entrada("https://www.rootz.so/d/BVtxY", texto="Parte 6"),
])

# 7) rootz /d/ sin ninguna pista -> archivos sueltos
mostrar("rootz /d/ sin pistas", [
    entrada("https://www.rootz.so/d/1OC8iu"),
    entrada("https://www.rootz.so/d/JZbge"),
])

# 8) multipartes + extra (crack) mezclado
mostrar("multipartes + crack extra", [
    entrada("https://host/1", nombre="Juego.part1.rar"),
    entrada("https://host/2", nombre="Juego.part2.rar"),
    entrada("https://host/3", nombre="Crack.rar"),
])

# 9) transfer: un solo enlace de un servicio de transferencia
mostrar("transfer un enlace", [entrada("https://transfer.it/t/9ffRvgxlgZqK")])

# 10) etiqueta sin nombre pero con 'parte N de M'
mostrar("etiqueta 'parte 2/6'", [
    entrada("https://host/a", texto="Parte 2 de 6"),
    entrada("https://host/b", texto="Parte 1 de 6"),
])

print("\nOK")
