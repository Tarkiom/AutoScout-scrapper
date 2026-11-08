// Service worker : la page s'ouvre même hors connexion, les données restent toujours fraîches quand le réseau est là.
const CACHE = 'radar-v1';
const SHELL = ['./', './index.html', './manifest.webmanifest', './zip-be.json', './icons/icon-192.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;   // API GitHub, photos, CDN : réseau direct
  // Réseau d'abord (données et page toujours à jour), cache en secours hors connexion
  e.respondWith(
    fetch(e.request).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(e.request.url.split('?')[0], copy)); }
      return res;
    }).catch(() => caches.match(e.request.url.split('?')[0]).then(r => r || caches.match('./')))
  );
});
