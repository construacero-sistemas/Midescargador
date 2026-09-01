/*
 * Lint de sintaxis del proyecto, sin dependencias externas:
 *   - node --check en cada .js propio (electron/, extension/, scripts/,
 *     test_frontend/)
 *   - python -m py_compile en cada .py de la raíz (módulos del backend)
 * Falla con código != 0 si algún archivo no parsea. Lo corre `npm test`
 * (suite "lint") y el CI. Evita que un error de sintaxis llegue a empaquetarse
 * (así se coló el 2.5.0) o a un release.
 */
"use strict";
const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const RAIZ = path.resolve(__dirname, "..");

function archivosJs(dir) {
  const salida = [];
  const ignorar = new Set(["node_modules", "dist", "__pycache__"]);
  for (const nombre of fs.readdirSync(dir)) {
    const ruta = path.join(dir, nombre);
    if (ignorar.has(nombre)) continue;
    const st = fs.statSync(ruta);
    if (st.isDirectory()) salida.push(...archivosJs(ruta));
    else if (nombre.endsWith(".js")) salida.push(ruta);
  }
  return salida;
}

let fallos = 0;
const revisados = [];

for (const dir of ["electron", "extension", "scripts", "test_frontend"]) {
  const abs = path.join(RAIZ, dir);
  if (!fs.existsSync(abs)) continue;
  for (const ruta of archivosJs(abs)) {
    revisados.push(path.relative(RAIZ, ruta));
    try {
      execFileSync(process.execPath, ["--check", ruta], { stdio: "pipe" });
    } catch (e) {
      fallos++;
      console.error(`[JS] ${path.relative(RAIZ, ruta)}:\n${e.stderr}`);
    }
  }
}

const pys = fs.readdirSync(RAIZ).filter((n) => n.endsWith(".py"));
if (pys.length) {
  revisados.push(...pys.map((p) => p));
  try {
    execFileSync("python", ["-m", "py_compile", ...pys], {
      cwd: RAIZ,
      stdio: "pipe",
    });
  } catch (e) {
    fallos++;
    console.error(`[PY] ${e.stderr || e.message}`);
  }
}

console.log(`lint: ${revisados.length} archivos revisados (js --check + py_compile)`);
if (fallos) {
  console.error(`\n[ERROR] ${fallos} archivo(s) con errores de sintaxis.`);
  process.exit(1);
}
