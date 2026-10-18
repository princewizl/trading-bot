// Service worker: shows web-push notifications and focuses the app on tap.
// Served at /sw.js (root scope) by web_app.py.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; }
  catch (_) { d = { title: 'Trading Bot', body: event.data ? event.data.text() : '' }; }

  event.waitUntil(self.registration.showNotification(d.title || 'Trading Bot', {
    body: d.body || '',
    tag: d.tag || undefined,
    renotify: Boolean(d.tag),
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/badge-72.png',
    data: { url: d.url || '/' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of wins) {
      if ('focus' in w) { await w.focus(); return; }
    }
    await self.clients.openWindow(url);
  })());
});
