/*
 * Activa los hooks del repo: apunta core.hooksPath a .githooks/ para que el
 * pre-commit (que corre `npm test`) se ejecute en cada commit local.
 *
 * Se invoca desde el script `prepare` de package.json (corre tras `npm install`
 * en la raíz), así un clon nuevo queda con los hooks activos sin pasos extra.
 * Es idempotente y no es un error si no hay repo git (se avisa y sigue).
 */
"use strict";
const { execFileSync } = require("child_process");
const path = require("path");

const RAIZ = path.resolve(__dirname, "..");

try {
  execFileSync("git", ["config", "core.hooksPath", ".githooks"], { cwd: RAIZ, stdio: "inherit" });
  console.log("[hooks] core.hooksPath -> .githooks (pre-commit ejecutará 'npm test')");
} catch (e) {
  console.warn("[hooks] No pude configurar core.hooksPath (¿no es un repo git?). " + (e && e.message) || e);
}