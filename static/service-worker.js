const CACHE_NAME = "pibells-shell-v5";
const ASSETS = [
  "/static/style.css",
  "/static/app.js",
  "/static/schedule.js",
  "/static/admin.js",
  "/static/buttons.js",
  "/static/login.js",
  "/static/setup.js",
  "/static/logo.svg",
  "/static/pibells-logo.png",
  "/static/favicons/favicon.svg",
  "/static/favicons/favicon-32x32.png",
  "/static/favicons/apple-touch-icon.png",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-1024.png",
  "/static/css/all.min.css",
  "/static/webfonts/fa-solid-900.woff2",
  "/static/webfonts/fa-regular-400.woff2"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith("/static/")) return;

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
        }
        return response;
      })
      .catch(() => caches.match(request))
  );
});
