import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ELECTRON = ROOT / "electron"

# require("./algo") y require("../electron/algo") en el código del proceso
# principal: cada uno debe terminar DENTRO del asar, si no la app crashea al
# arrancar con "Cannot find module './x'" (así rompió el 2.5.0).
_PATRON_REQUIRE = re.compile(r"require\(\s*[\"'](\.[^\"']+)[\"']\s*\)")


def _requires_relativos(nombre_archivo):
    """Rutas relativas que main.js/preload.js piden con require()."""
    codigo = (ELECTRON / nombre_archivo).read_text(encoding="utf-8")
    return sorted(set(_PATRON_REQUIRE.findall(codigo)))


class ElectronPackageTests(unittest.TestCase):
    def test_build_files_incluye_modulos_requeridos(self):
        package = json.loads((ELECTRON / "package.json").read_text(encoding="utf-8"))
        files = set(package["build"]["files"])
        requeridos = []
        for nombre in ("main.js", "preload.js"):
            for ruta in _requires_relativos(nombre):
                base = Path(ruta).name
                resuelto = base if Path(base).suffix else base + ".js"
                requeridos.append(resuelto)
                self.assertIn(resuelto, files,
                              "main.js/preload.js requieren '%s' y no está en "
                              "build.files (la app crashearía al arrancar)" % resuelto)
        # el propio main y preload siempre empaquetados
        self.assertIn("main.js", files)
        self.assertIn("preload.js", files)

    def test_modulos_requeridos_existen_en_disco(self):
        for nombre in ("main.js", "preload.js"):
            for ruta in _requires_relativos(nombre):
                destino = (ELECTRON / ruta)
                candidato = destino if destino.suffix else destino.with_suffix(".js")
                self.assertTrue(candidato.is_file(),
                                "require('%s') no existe en electron/" % ruta)

    def test_update_logic_empaquetado(self):
        # regresión del 2.5.0: update_logic.js fuera del asar
        files = set(json.loads(
            (ELECTRON / "package.json").read_text(encoding="utf-8"))["build"]["files"])
        self.assertIn("update_logic.js", files)


if __name__ == "__main__":
    unittest.main()
