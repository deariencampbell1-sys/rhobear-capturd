/* RHOBEAR Captur'd — service worker.
   HTML navigations are NETWORK-FIRST (a new deploy is served immediately and a
   user is never trapped on a stale cached build); static shell assets are
   cache-first; /api, /auth, /billing are never cached. Bump VERSION to force
   every client to drop its old caches on the next activation. */
const VERSION = 'capturd-v5-netfirst';
const SHELL = [
  '/',
  '/m',
  '/manifest.webmanifest',
  '/assets/icons/icon-192.png',
  '/assets/icons/icon-512.png',
  '/assets/capturd-bear.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const { request } = e;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // dynamic / auth / billing surfaces are never cached — always hit the network
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/') || url.pathname.startsWith('/billing/')) {
    return;
  }

  // HTML navigations: network-first. Latest build wins when online; the cached
  // page (then cached '/') is only the offline fallback. Cache-first here is
  // the bug — it hands a user a stale / for every visit until the SW swaps in.
  if (request.mode === 'navigate') {
    e.respondWith(
      fetch(request).then((res) => {
        if (res && res.status === 200) {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(request, copy));
        }
        return res;
      }).catch(() => caches.match(request).then((hit) => hit || caches.match('/')))
    );
    return;
  }

  // other same-origin static assets (icons, manifest, etc.): cache-first, then
  // network, caching a copy for next time.
  e.respondWith(
    caches.match(request).then((hit) => hit || fetch(request).then((res) => {
      if (res && res.status === 200 && res.type === 'basic') {
        const copy = res.clone();
        caches.open(VERSION).then((c) => c.put(request, copy));
      }
      return res;
    }))
  );
});
