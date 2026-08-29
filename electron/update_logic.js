"use strict";

function estadoUpdateDisponible(info) {
  const version = info && info.version ? String(info.version) : "";
  return {
    estado: "disponible",
    version,
    releaseNotes: info && info.releaseNotes ? info.releaseNotes : null,
  };
}

function debeMostrarAviso(data) {
  return !!data && (data.estado === "disponible" || data.estado === "lista");
}

function estadoAlMostrar(info, ultimoAviso) {
  if (!info || !info.version || info.version === ultimoAviso) return null;
  return estadoUpdateDisponible(info);
}

module.exports = {
  estadoUpdateDisponible,
  debeMostrarAviso,
  estadoAlMostrar,
};
