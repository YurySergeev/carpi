// Offline copy of the config page: network first (so updates arrive), cache when there's no signal.
const CACHE = "carpi-config-v3";   // bump with the ?v= in index.html
const FILES = ["./", "index.html", "config.css?v=3", "config.js?v=3", "manifest.webmanifest", "icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET" || new URL(e.request.url).origin !== location.origin) return;
  // "no-cache": always ask GitHub Pages whether the file changed (its 10-minute browser cache
  // once paired a new index.html with an old config.js). Offline, fall back to our copy.
  e.respondWith(fetch(new Request(e.request.url, { cache: "no-cache" }))
    .then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy));
      return res;
    })
    .catch(() => caches.match(e.request, { ignoreSearch: true })));
});
