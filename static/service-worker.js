const CACHE_NAME = 'pibells-cache-v1';
const URLS_TO_CACHE = [
  '/',
  '/static/style.css',
  '/static/theme.js',
  '/static/pibells-logo.png',
  '/static/smallroundlogo.png',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(URLS_TO_CACHE))
  );
});

self.addEventListener('fetch', event => {
  event.respondWith(
    caches.match(event.request).then(response => response || fetch(event.request))
  );
});
