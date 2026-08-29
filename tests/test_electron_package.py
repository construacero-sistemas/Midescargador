import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ElectronPackageTests(unittest.TestCase):
    def test_main_requires_are_listed_in_build_files(self):
        package = json.loads((ROOT / "electron" / "package.json").read_text(encoding="utf-8"))
        files = set(package["build"]["files"])
        required = {"main.js", "preload.js", "update_logic.js"}
        self.assertTrue(required <= files, sorted(required - files))

    def test_local_modules_exist(self):
        for name in ("main.js", "preload.js", "update_logic.js"):
            self.assertTrue((ROOT / "electron" / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
