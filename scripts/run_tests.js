/*
 * Runner de pruebas: ejecuta la suite de backend y/o la de frontend, y termina
 * con código != 0 si alguna falla. Lo lanza `npm test` desde la raíz.
 *
 *   - backend : python -m unittest discover -s tests
 *   - frontend: node test_frontend/seleccion.test.js (jsdom)
 *
 * Por defecto corre las DOS suites. Se puede ejecutar solo una con flags:
 *   node scripts/run_tests.js --backend            # solo backend
 *   node scripts/run_tests.js --frontend           # solo frontend
 *   node scripts/run_tests.js --solo backend       # igual que --backend
 *   node scripts/run_tests.js --solo frontend      # igual que --frontend
 *   node scripts/run_tests.js --solo-tipo backend  # alias de --solo
 *
 * Vía npm (los args van después de `--`):
 *   npm test -- --backend
 *   npm test -- --solo frontend
 *
 * Se usa spawn para que el funcionamiento sea idéntico en Windows (cmd),
 * Linux y CI (GitHub Actions), sin depender de la sintaxis de `&&` del shell.
 */
"use strict";
const { spawnSync } = require("child_process");
const path = require("path");

const RAIZ = path.resolve(__dirname, "..");

function ejecutar(nombre, comando, args, cwd) {
  console.log(`\n=== ${nombre} ===`);
  console.log(`$ ${comando} ${args.join(" ")}   (cwd: ${path.relative(RAIZ, cwd) || "."})`);
  const r = spawnSync(comando, args, { cwd, stdio: "inherit", shell: false });
  if (r.status !== 0) {
    console.error(`\n[ERROR] ${nombre} falló (código ${r.status}).`);
    return false;
  }
  return true;
}

function correr(suite) {
  if (suite === "backend") {
    return ejecutar(
      "backend (unittest)",
      "python",
      ["-m", "unittest", "discover", "-s", "tests", "-p", "test*.py", "-v"],
      RAIZ
    );
  }
  return ejecutar(
    "frontend (jsdom)",
    process.execPath,
    ["seleccion.test.js"],
    path.join(RAIZ, "test_frontend")
  );
}

// ---------------------------------------------------------------------------
// Flags: qué suites correr
// ---------------------------------------------------------------------------
const args = process.argv.slice(2);
const VALOR_RE = /^--solo(?:-tipo)?$/;

let backend = true;
let frontend = true;

const flagsUnicos = args.filter((a) => a === "--backend" || a === "--frontend");
if (flagsUnicos.length) {
  backend = flagsUnicos.includes("--backend");
  frontend = flagsUnicos.includes("--frontend");
}

const idxSolo = args.findIndex((a) => VALOR_RE.test(a));
if (idxSolo !== -1) {
  const tipo = args[idxSolo + 1];
  if (tipo === "backend") { backend = true; frontend = false; }
  else if (tipo === "frontend") { backend = false; frontend = true; }
  else {
    console.error(`[ERROR] --solo <tipo> desconocido: ${tipo || "(falta valor)"} (usa 'backend' o 'frontend')`);
    process.exit(2);
  }
}

const wasOk = [];
if (backend) wasOk.push(correr("backend"));
if (frontend) wasOk.push(correr("frontend"));

if (wasOk.length === 0) {
  console.error("\n[ERROR] No se pidió correr ninguna suite (uso: --backend | --frontend | --solo backend|frontend).");
  process.exit(2);
}

const hayFallo = wasOk.some((ok) => !ok);
if (hayFallo) {
  console.error("\nFallaron una o más suites de pruebas.");
  process.exitCode = 1;
} else {
  console.log("\nTodas las suites de pruebas pasaron.");
}