// Caches the app shell only, so the app opens without a network after the first visit.
// Anything under /api/ (reviews go there and come back) is never touched, never cached.
const CACHE = 'bedahreview-shell-v1';
const SHELL = ['./', './manifest.webmanifest', './icons/icon-192.png', './icons/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  const inShell = url.pathname === '/' || SHELL.some((p) => new URL(p, location.href).pathname === url.pathname);
  if (!inShell) return;
  // Network first so an updated page is picked up; the cached copy is the offline fallback.
  // ignoreSearch: /?text=... (the Share menu) is the same page as /.
  e.respondWith(
    fetch(e.request).then((res) => {
      if (res.ok && url.search === '') { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
      return res;
    }).catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
