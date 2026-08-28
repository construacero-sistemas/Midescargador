/*
 * Prueba de frontend: panel de selección de temporadas y servidores.
 *
 * Importa directamente la lógica real de static/seleccion.js (las mismas
 * funciones que index.html carga vía /static/seleccion.js) y la ejecuta sobre
 * un DOM de jsdom con fetch espiado, inyectando los stubs en crearSeleccion.
 *
 * Verifica:
 *   1) El panel lista solo los servidores detectados de la serie.
 *   2) Sin detección no pinta lista fija.
 *   3) Al desmarcar una temporada se desmarcan sus episodios (y viceversa).
 *   4) resolverSeleccionAnalisis() solo envía a /api/enlaces los episodios y
 *      servidores que quedaron marcados.
 *
 * Ejecutar:
 *   cd test_frontend && npm install && node seleccion.test.js
 */

"use strict";
const assert = require("assert");
const { JSDOM } = require("jsdom");
const { crearSeleccion } = require("../static/seleccion.js");

// ---------------------------------------------------------------------------
// Ambiente jsdom con stubs, alimentando crearSeleccion (como hace index.html)
// ---------------------------------------------------------------------------
function crearAmbiente() {
  const dom = new JSDOM(
    `<div id="panel-analisis-enlaces"></div>
     <div id="panel-enlaces"></div>`,
    { runScripts: "outside-only", url: "http://127.0.0.1:17890/" }
  );
  const { window } = dom;
  const doc = window.document;
  const fetchCalls = [];

  // Helpers idénticos a los del index.html
  const escapeHtml = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  const mostrado = { valores: [] };

  const s = crearSeleccion({
    $: (sel) => doc.querySelector(sel),
    document: doc,
    escapeHtml,
    t: (key) => key, // texto irrelevante para lo que se verifica aquí
    alert: (msg) => { window.__alertado = msg; },
    verEnlaces: () => { window.__verEnlaces = true; },
    mostrarEnlaces: (d) => { mostrado.valores.push(d); },
    // fetch espiado: el POST inicial a /api/enlaces responde listo al instante
    // para que verEnlacesSeleccionados retorne sin bucle de sondeo.
    fetch: (url, opts) => {
      opts = opts || {};
      let body = null;
      try { body = opts.body ? JSON.parse(opts.body) : null; } catch (_) {}
      fetchCalls.push({ url, method: opts.method || "GET", body });
      return Promise.resolve({
        ok: true,
        json: async () => ({
          servidores: [{ servidor: "Mega · 1x1", hoster: "Mega" }],
        }),
      });
    },
  });

  return {
    window,
    doc,
    fetchCalls,
    mostrado,
    renderAnalisisEnlaces: s.renderAnalisisEnlaces,
    resolverSeleccionAnalisis: s.resolverSeleccionAnalisis,
    verEnlacesSeleccionados: s.verEnlacesSeleccionados,
  };
}

// Serie de ejemplo: dos temporadas, dos episodios cada una.
const SERIE = {
  tipo: "serie",
  titulo: "Serie de prueba",
  total_episodios: 4,
  servidores_posibles: ["Mega", "MegaUp", "MediaFire", "Servidor por confirmar"],
  temporadas: [
    {
      id: "1", nombre: "Temporada 1",
      episodios: [
        { label: "1x1", url: "https://zona-leros.com/series/episode/uno" },
        { label: "1x2", url: "https://zona-leros.com/series/episode/dos" },
      ],
    },
    {
      id: "2", nombre: "Temporada 2",
      episodios: [
        { label: "2x1", url: "https://zona-leros.com/series/episode/tres" },
        { label: "2x2", url: "https://zona-leros.com/series/episode/cuatro" },
      ],
    },
  ],
};

// ---------------------------------------------------------------------------
// Pruebas
// ---------------------------------------------------------------------------
function probarSoloServidoresDetectados() {
  const { renderAnalisisEnlaces, doc } = crearAmbiente();
  // SERIE.servidores_posibles son LOS DETECTADOS de esa serie
  renderAnalisisEnlaces(SERIE);

  const valores = [...doc.querySelectorAll(".analisis-servidor")].map((x) => x.value);
  assert.deepStrictEqual(valores, [...SERIE.servidores_posibles],
    "el panel solo pinta las casillas de los servidores detectados de la serie");

  // Y no aparece el aviso de "sin detectar"
  const cuerpo = doc.querySelector("#panel-analisis-enlaces").innerHTML;
  assert.ok(!/Sin servidores detectados/.test(cuerpo),
    "con detección no se muestra el aviso de falta de detección");

  console.log("ok  panel lista solo los servidores detectados de la serie");
}

function probarSinServidoresDetectadosNoListaNinguno() {
  const { renderAnalisisEnlaces, resolverSeleccionAnalisis, doc, fetchCalls } = crearAmbiente();
  const sinDetectar = JSON.parse(JSON.stringify(SERIE));
  delete sinDetectar.servidores_posibles; // el backend manda vacío/faltante
  renderAnalisisEnlaces(sinDetectar);

  assert.strictEqual(doc.querySelectorAll(".analisis-servidor").length, 0,
    "sin detección no se pinta NI UNA casilla de servidor (nada de lista fija)");

  // Sin filtro de servidor disponible, resolver envía todos los episodios y
  // servidores vacíos (equivale a 'todos')
  resolverSeleccionAnalisis();
  const post = fetchCalls.find((c) => c.url === "/api/enlaces");
  assert.ok(post, "debe POSTear a /api/enlaces");
  assert.deepStrictEqual(post.body.seleccion.servidores, [],
    "sin casillas la selección no filtra servidor");

  console.log("ok  sin servidores detectados el panel no muestra lista fija");
}

function probarDesmarcarTemporada() {
  const { renderAnalisisEnlaces, window, doc } = crearAmbiente();
  renderAnalisisEnlaces(SERIE);

  const temporadas = [...doc.querySelectorAll(".analisis-temporada")];
  const episodios = [...doc.querySelectorAll(".analisis-episodio")];

  assert.strictEqual(temporadas.length, 2, "debe pintar 2 casillas de temporada");
  assert.strictEqual(episodios.length, 4, "debe pintar 4 episodios");
  assert.ok(episodios.every((e) => e.checked), "todo arranca marcado");

  // Desmarcamos la temporada 2
  const temp2 = doc.querySelector('.analisis-temporada[data-id="2"]');
  assert.ok(temp2, "existe la temporada 2");
  temp2.checked = false;
  temp2.dispatchEvent(new window.Event("change", { bubbles: true }));

  // Sus episodios quedan desmarcados
  const epsTemp2 = [...doc.querySelectorAll('.analisis-episodio[data-temp="2"]')];
  assert.ok(epsTemp2.length === 2 && epsTemp2.every((e) => !e.checked),
    "desmarcar la temporada desmarca sus episodios");

  // La temporada 1 queda intacta
  const epsTemp1 = [...doc.querySelectorAll('.analisis-episodio[data-temp="1"]')];
  assert.ok(epsTemp1.length === 2 && epsTemp1.every((e) => e.checked),
    "las otras temporadas no se tocan");

  // Y al volver a marcarla, se marcan todos de nuevo
  temp2.checked = true;
  temp2.dispatchEvent(new window.Event("change", { bubbles: true }));
  assert.ok(epsTemp2.every((e) => e.checked), "marcar la temporada vuelve a marcar sus episodios");

  console.log("ok  desmarcar temporada desmarca sus episodios (y viceversa)");
}

function probarResolverSoloEnviaSeleccion() {
  const { renderAnalisisEnlaces, resolverSeleccionAnalisis, window, doc, fetchCalls } = crearAmbiente();
  renderAnalisisEnlaces(SERIE);

  // Elijo SOLO la temporada 1, y de ella SOLO el episodio 1x2.
  // 1) desmarcar la temporada 2 completa
  const temp2 = doc.querySelector('.analisis-temporada[data-id="2"]');
  temp2.checked = false;
  temp2.dispatchEvent(new window.Event("change", { bubbles: true }));

  // 2) desmarcar a mano el episodio 1x1 dentro de la temporada 1
  const epsTemp1 = [...doc.querySelectorAll('.analisis-episodio[data-temp="1"]')];
  epsTemp1[0].checked = false;
  // quedar marcado: 1x2

  // 3) elegir SOLO el servidor Mega
  const mega = doc.querySelector('.analisis-servidor[value="Mega"]');
  mega.checked = true;
  mega.dispatchEvent(new window.Event("change", { bubbles: true }));
  // los otros servidores siguen desmarcados

  resolverSeleccionAnalisis();

  // Se disparó UNA petición de extracción
  const post = fetchCalls.find((c) => c.url === "/api/enlaces");
  assert.ok(post, "debe POSTear a /api/enlaces");
  assert.strictEqual(post.method, "POST");

  // El payload lleva exactamente el episodio y servidor elegidos.
  // La temporada 2 y el episodio 1x1 NO van.
  assert.strictEqual(post.body.url, SERIE.temporadas[0].episodios[1].url,
    "la url raíz es el primer episodio elegido (1x2)");

  assert.deepStrictEqual(post.body.seleccion.urls, [
    SERIE.temporadas[0].episodios[1].url,
  ], "seleccion.urls contiene solo los episodios marcados (la temporada 2 y 1x1 quedan fuera)");

  assert.deepStrictEqual(post.body.seleccion.servidores, ["Mega"],
    "seleccion.servidores contiene solo el servidor elegido");

  console.log("ok  resolver solo envía los episodios y servidores elegidos");
}

function probarResolverSinServidorEnviaListaVacia() {
  const { renderAnalisisEnlaces, resolverSeleccionAnalisis, fetchCalls } = crearAmbiente();
  renderAnalisisEnlaces(SERIE);

  // Sin marcar ningún servidor => "todos los servidores" (array vacío)
  resolverSeleccionAnalisis();

  const post = fetchCalls.find((c) => c.url === "/api/enlaces");
  assert.ok(post, "debe POSTear a /api/enlaces");
  assert.deepStrictEqual(post.body.seleccion.servidores, [],
    "sin servidor marcado se envía lista vacía (equivale a todos)");
  assert.deepStrictEqual(post.body.seleccion.urls, SERIE.temporadas
    .flatMap((t) => t.episodios).map((e) => e.url),
    "sin desmarcar temporadas se envían todos los episodios");

  console.log("ok  sin servidor marcado se envía lista vacía (todos)");
}

let fallos = 0;
const pruebas = [
  probarSoloServidoresDetectados,
  probarSinServidoresDetectadosNoListaNinguno,
  probarDesmarcarTemporada,
  probarResolverSoloEnviaSeleccion,
  probarResolverSinServidorEnviaListaVacia,
];
for (const fn of pruebas) {
  try { fn(); }
  catch (e) {
    fallos++;
    console.error("FAIL", fn.name, "\n  ", e && e.message);
  }
}

if (fallos > 0) {
  console.error(`\n${fallos} prueba(s) fallaron.`);
  process.exit(1);
}
console.log("\nTodas las pruebas de frontend pasaron.");