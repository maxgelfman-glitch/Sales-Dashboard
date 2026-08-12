/* PowerWash CRM — service worker (offline app shell).
 *
 * Strategy:
 *   - App shell (this app's own files) → cache-first, so the app opens and
 *     runs with no network at all. Data still lives in IndexedDB (see db.js);
 *     the shell is just the code.
 *   - Everything else (Supabase API calls) → network-only. We never cache API
 *     responses; offline reads/writes are handled by the IndexedDB mirror and
 *     the sync engine, not by the service worker.
 *
 * Bump CACHE when you ship new app files so clients pick them up.
 */
var CACHE = 'pwcrm-shell-v1';
var SHELL = [
  './',
  './index.html',
  './config.js',
  './sync.js',
  './db.js',
  './vendor/supabase.js',
  './manifest.webmanifest',
  './icons/icon-192.png',
  './icons/icon-512.png'
];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) {
    // addAll fails the whole install if any file 404s; add resiliently instead.
    return Promise.all(SHELL.map(function (u) {
      return c.add(u).catch(function () { /* ignore a missing optional asset */ });
    }));
  }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.map(function (k) { if (k !== CACHE) return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return; // never intercept writes
  var url = new URL(req.url);

  // Only serve OUR OWN origin+scope from cache. Anything cross-origin
  // (the Supabase API) goes straight to the network.
  if (url.origin !== self.location.origin) return;

  // Navigations (index.html, possibly with a ?supabase_url=… query): serve the
  // cached shell first so the app opens instantly and works fully offline.
  // ignoreSearch so the query string never causes a cache miss.
  if (req.mode === 'navigate') {
    e.respondWith(
      caches.match('./index.html', { ignoreSearch: true }).then(function (hit) {
        return hit || fetch(req).catch(function () { return caches.match('./index.html', { ignoreSearch: true }); });
      })
    );
    return;
  }

  e.respondWith(
    caches.match(req, { ignoreSearch: true }).then(function (hit) {
      if (hit) {
        // refresh in the background (stale-while-revalidate for the shell)
        fetch(req).then(function (res) {
          if (res && res.ok) caches.open(CACHE).then(function (c) { c.put(req, res.clone()); });
        }).catch(function () {});
        return hit;
      }
      return fetch(req).then(function (res) {
        if (res && res.ok && (req.destination === 'script' || req.destination === 'style' ||
            req.destination === 'document' || req.destination === 'image' || url.pathname.indexOf('/app/') !== -1)) {
          var copy = res.clone();
          caches.open(CACHE).then(function (c) { c.put(req, copy); });
        }
        return res;
      }).catch(function () {
        if (req.mode === 'navigate') return caches.match('./index.html', { ignoreSearch: true });
        throw new Error('offline');
      });
    })
  );
});
