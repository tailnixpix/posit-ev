// Posit+EV service worker — push alerts only (no offline caching, so picks are never stale).
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (_) {}
  event.waitUntil(self.registration.showNotification(d.title || 'New +EV pick', {
    body: d.body || 'Open Posit+EV to see it.',
    icon: '/static/icon-192.png',
    badge: '/static/favicon-32.png',
    tag: 'new-pick',
    renotify: true,
    data: { url: d.url || '/dashboard' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/dashboard';
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    for (const c of list) { if (c.url.includes(url) && 'focus' in c) return c.focus(); }
    return self.clients.openWindow(url);
  }));
});
