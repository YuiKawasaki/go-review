// オフライン動作のためのキャッシュ。
// 学習ログは端末側を正とせず、オンライン復帰時にクラウド（解析機）へ送る。

// キャッシュ名を変えると、activate で古いキャッシュが捨てられる。
// アプリの取得方法を変えたときは必ず上げること。
const CACHE = 'go-review-v5';
const SHELL = [
  './',
  './index.html',
  './css/app.css',
  './js/app.js',
  './js/board.js',
  './js/glossary.js',
  './js/goban.js',
  './js/sequence.js',
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
  // アプリ本体もネットワーク優先。キャッシュは圏外のときの控えに徹する。
  //
  // 以前は stale-while-revalidate（キャッシュを返しつつ裏で更新）にしていたが、
  // これだと更新が必ず1回遅れて届く。開いて見て閉じる使い方だと、いつまでも
  // 古い画面のままになる。実際、詰碁機能を追加したあとも端末には旧画面が
  // 表示され続けた。表示が古いと解析結果を見誤るので、鮮度を優先する。
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(event.request)),
  );
});
