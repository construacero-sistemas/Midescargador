// Lista de exclusiones de captura automática (estilo defexclist.txt de IDM).
// Se aplica SOLO al takeover automático de enlaces de archivo: si una URL
// coincide, el clic navega normal (no se intercepta). El menú contextual y
// la tecla Insert (fuerza) SIEMPRE descargan, aunque la URL esté excluida.
// Este archivo se comparte entre content.js (clic) y background.js (menú).

const MDM_EXCLUSIONES = [
  // recursos de página: imágenes, estilos, scripts, fuentes, favicons
  /\.(css|js|mjs|json|map|svg|ico|png|jpe?g|gif|webp|avif|bmp|woff2?|ttf|otf|eot)(\?|#|$)/i,
  // ads / medición / tracking
  /(doubleclick\.net|googlesyndication|googletagmanager|google-analytics|adservice\.google|adnxs\.|rubiconproject|adform\.net|criteo\.|outbrain\.|taboola\.|scorecardresearch|quantserve|zedo\.|chartbeat\.|hotjar\.|mixpanel\.|segment\.io)/i,
  /(\/ads?\/|adframe|adserver)/i,
  // sonidos embebidos de apps web y notificaciones
  /(whatsapp\.com\/res\/|gstatic\.com\/[^/]*sounds|\.sndcdn\.com|notification\.mp3|message\.mp3|pop\.mp3|interval\.mp3)/i,
  // updates / infraestructura
  /(windowsupdate\.com|msedge\.b\.tlu\.dl\.delivery\.microsoft\.com|autoupdate|\.msi\?)/i,
  // mail
  /(mail\.google\.com|outlook\.live\.com|mail\.yahoo\.com)/i,
  // mapas y captchas
  /(maps\.googleapis\.com|gstatic\.com\/maps|recaptcha)/i,
  // el propio instalador del gestor (para no descargarse a sí mismo)
  /midescargador.*\.exe|MiDescargador.*\.exe/i,
];

function mdmUrlExcluida(url) {
  if (!url) return true;
  const u = url.split("#")[0].trim();
  try {
    const p = new URL(u);
    if (p.protocol !== "http:" && p.protocol !== "https:") return true;
  } catch (_e) {
    return true; // no es una URL http(s) válida: no se captura
  }
  return MDM_EXCLUSIONES.some((re) => re.test(u));
}
