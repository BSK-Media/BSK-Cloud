// Service worker BSK Media Cloud (wersja __VERSION__)
// - pliki statyczne i miniatury: z pamięci podręcznej (szybko, także przy słabym zasięgu),
// - strony: zawsze z sieci (aktualne dane); bez sieci — ekran „Brak połączenia”,
// - pobierania, wysyłki i API nigdy nie są zapisywane w pamięci.
const VERSION = "__VERSION__";
const STATIC = `bsk-static-${VERSION}`;
const THUMBS = "bsk-thumbs-v1";
const MAX_THUMBS = 600;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(STATIC).then((c) => c.addAll([
    "/offline", "/static/icons/icon-192.png", "/static/favicon.svg",
    `/static/style.css?v=${VERSION}`, `/static/app.js?v=${VERSION}`,
  ])).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((k) => k.startsWith("bsk-static-") && k !== STATIC).map((k) => caches.delete(k)),
  )).then(() => self.clients.claim()));
});

async function trimThumbs() {
  const cache = await caches.open(THUMBS);
  const keys = await cache.keys();
  for (const k of keys.slice(0, Math.max(0, keys.length - MAX_THUMBS))) await cache.delete(k);
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(STATIC).then((c) => c.put(req, copy)); }
      return res;
    })));
    return;
  }
  // miniatury: adres zawiera sumę pliku (?v=...), więc wolno je trzymać długo
  if (/^\/files\/\d+\/thumb$/.test(url.pathname) && url.searchParams.has("v")) {
    event.respondWith(caches.open(THUMBS).then(async (cache) => {
      const hit = await cache.match(req);
      if (hit) return hit;
      const res = await fetch(req);
      if (res.ok) { cache.put(req, res.clone()); trimThumbs(); }
      return res;
    }));
    return;
  }
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/offline")));
  }
});
