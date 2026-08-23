// オフライン動作のためのキャッシュ。
// 学習ログは端末側を正とせず、オンライン復帰時にクラウド（解析機）へ送る。

const CACHE = 'go-review-v3';
const SHELL = [
  './',
  './index.html',
  './css/app.css',
  './js/app.js',
  './js/board.js',
  './js/goban.js',
  './js/store.js',
  './manifest.webmanifest',
  './icon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => Promise.all(SHELL.map((u) => cache.add(new Request(u, { cache: 'reload' }))))).then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;            // 送信系はキャッシュしない
  if (url.pathname.startsWith('/api/')) return;          // 解析機への問い合わせも素通し

  // アプリシェルはキャッシュ優先、データはネットワーク優先
  const isData = url.pathname.includes('/data/');
  if (isData) {
    event.respondWith(
      fetch(event.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
          return res;
        })
        .catch(() => caches.match(event.request)),
    );
    return;
  }
  // アプリシェルは stale-while-revalidate。
  // キャッシュ優先だけにすると、更新したコードが端末に永久に届かない。
  event.respondWith(
    caches.match(event.request).then((hit) => {
      const network = fetch(event.request)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((cache) => cache.put(event.request, copy));
          }
          return res;
        })
        .catch(() => hit);
      return hit || network;
    }),
  );
});
