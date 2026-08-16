# -*- coding: utf-8 -*-
"""Verificación del flujo REAL de producción: llama zonaleros.extraer() contra
la página de Kaiserpunk y comprueba que ROOTZ devuelve enlaces."""
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zonaleros

URL = "https://www.zona-leros.com/juegos-pc/kaiserpunk-pc-e"


def main():
    t0 = time.time()
    r = zonaleros.extraer(URL)
    print("duración: %.0fs" % (time.time() - t0))
    if "error" in r:
        print("ERROR:", r["error"])
        return
    print("TITULO:", r.get("titulo"))
    for s in r.get("servidores", []):
        print("\n### SERVIDOR:", s.get("servidor"))
        if s.get("error"):
            print("   error:", s["error"])
        print("   enlaces:", len(s.get("enlaces", [])))
        for e in s.get("enlaces", []):
            print("   - parte %s de %s | %s | %s" %
                  (e.get("parte") or "-", e.get("total") or "-",
                   e.get("nombre") or "(sin nombre)", e.get("url", "")[:70]))
        if s.get("es_multipartes"):
            print("   -> MULTIPARTES:", s.get("total_partes"),
                  "| base:", s.get("nombre_base"))


if __name__ == "__main__":
    main()
