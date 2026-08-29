"use strict";

const assert = require("assert");
const {
  estadoUpdateDisponible,
  debeMostrarAviso,
  estadoAlMostrar,
} = require("./update_logic");

const estado = estadoUpdateDisponible({
  version: "2.5.0",
  releaseNotes: "Mejoras",
});
assert.deepStrictEqual(estado, {
  estado: "disponible",
  version: "2.5.0",
  releaseNotes: "Mejoras",
});
assert.strictEqual(debeMostrarAviso(estado), true);
assert.strictEqual(debeMostrarAviso({ estado: "al-dia" }), false);
assert.deepStrictEqual(
  estadoAlMostrar({ version: "2.5.0", releaseNotes: "Mejoras" }, null),
  estado
);
assert.strictEqual(
  estadoAlMostrar({ version: "2.5.0" }, "2.5.0"),
  null
);
console.log("ok  updater: update-available muestra aviso automáticamente");
