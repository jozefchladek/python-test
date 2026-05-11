const CACHE_NAME = 'footgolfisti-v2';
const STATIC_ASSETS = [
    '/static/style.css',
    '/static/manifest.json',
];

// IndexedDB for queuing offline score submissions
function openDB() {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open('footgolfisti-offline', 1);
        req.onupgradeneeded = () => {
            req.result.createObjectStore('pending-scores', { keyPath: 'id', autoIncrement: true });
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
}

async function queueScore(url, formData) {
    const db = await openDB();
    const tx = db.transaction('pending-scores', 'readwrite');
    const data = {};
    for (const [key, value] of formData.entries()) {
        data[key] = value;
    }
    tx.objectStore('pending-scores').add({ url, data, timestamp: Date.now() });
    return new Promise((resolve, reject) => {
        tx.oncomplete = resolve;
        tx.onerror = reject;
    });
}

async function replayPendingScores() {
    const db = await openDB();
    const tx = db.transaction('pending-scores', 'readonly');
    const store = tx.objectStore('pending-scores');
    const all = await new Promise((resolve, reject) => {
        const req = store.getAll();
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });

    for (const entry of all) {
        try {
            const formBody = new URLSearchParams(entry.data).toString();
            const response = await fetch(entry.url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: formBody,
                redirect: 'follow',
            });
            if (response.ok || response.redirected) {
                const delTx = db.transaction('pending-scores', 'readwrite');
                delTx.objectStore('pending-scores').delete(entry.id);
            }
        } catch (e) {
            // Still offline, stop trying
            break;
        }
    }
}

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
    );
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);

    // Intercept score submissions when offline
    if (event.request.method === 'POST' && url.pathname.match(/\/sessions\/\d+\/score/)) {
        event.respondWith(
            fetch(event.request.clone())
                .catch(async () => {
                    const formData = await event.request.formData();
                    await queueScore(url.href, formData);
                    // Notify the client that the score was saved offline
                    const clients = await self.clients.matchAll();
                    clients.forEach(client => client.postMessage({ type: 'score-saved-offline' }));
                    // Return a redirect-like response back to the session page
                    return Response.redirect(url.pathname.replace('/score', ''), 303);
                })
        );
        return;
    }

    // Network-first strategy for GET requests
    if (event.request.method === 'GET') {
        event.respondWith(
            fetch(event.request)
                .then(response => {
                    if (response.status === 200) {
                        const clone = response.clone();
                        caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
                    }
                    return response;
                })
                .catch(() => caches.match(event.request))
        );
        return;
    }

    // All other requests: just fetch normally
    event.respondWith(fetch(event.request));
});

// Replay pending scores when coming back online
self.addEventListener('message', event => {
    if (event.data === 'replay-pending-scores') {
        replayPendingScores();
    }
});
