// Vinex Stock service worker: makes the app installable and opens the shell quickly.
// Data (/api) is never cached, so stock numbers are always live.
const CACHE = 'vinex-shell-v2';
const SHELL = ['/', '/static/manifest.webmanifest', '/static/icons/icon-192.png',
  '/static/vendor/fonts/ibm-plex-sans-latin-400-normal.woff2', '/static/vendor/fonts/ibm-plex-sans-latin-500-normal.woff2',
  '/static/vendor/fonts/ibm-plex-sans-latin-600-normal.woff2'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  if (e.request.mode === 'navigate') {   // newest page when online, cached page when offline
    e.respondWith(fetch(e.request).then(r => { const copy = r.clone(); caches.open(CACHE).then(c => c.put('/', copy)); return r; })
      .catch(() => caches.match('/')));
    return;
  }
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request).then(r => {
      if (r.ok && url.pathname.includes('/vendor/')) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
      return r;
    })));
  }
});
