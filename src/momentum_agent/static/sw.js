const CACHE_NAME = "momentum-v1";

const PRECACHE_ASSETS = [
  "/",
  "/login.html",
  "/stats.html",
  "/app.css",
  "/manifest.json",
  "/icon.svg",
];

const PRECACHE_JS = [
  "/js/api.js",
  "/js/app.js",
  "/js/tasks.js",
  "/js/chat.js",
  "/js/advice.js",
  "/js/config.js",
  "/js/heartbeat.js",
  "/js/focus.js",
  "/js/stats.js",
  "/js/notifications.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll([...PRECACHE_ASSETS, ...PRECACHE_JS]))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((name) => name !== CACHE_NAME)
            .map((name) => caches.delete(name))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  if (request.method !== "GET" || !url.pathname.startsWith("/")) {
    return;
  }

  // API / SSE / stream: always network
  if (
    url.pathname.startsWith("/api/") ||
    url.pathname === "/api/chat/stream"
  ) {
    return;
  }

  // HTML navigation: network first, fallback to cache
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          return response;
        })
        .catch(() =>
          caches.match(request).then((cached) => cached || caches.match("/"))
        )
    );
    return;
  }

  // Static assets: cache first, fallback to network
  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        if (!response || response.status !== 200 || response.type !== "basic") {
          return response;
        }
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        return response;
      });
    })
  );
});
