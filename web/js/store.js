// データ取得と IndexedDB キャッシュ、オフライン学習ログのキュー。
//
// 電波のない移動中でも、キャッシュ済みの棋譜再生・変化図再生・問題演習が
// 動作すること（非機能要件 オフライン）。
// 学習ログはオフライン中 IndexedDB に保持し、復帰時に自動送信する。

const CONFIG = Object.assign(
  { dataBase: './data', apiBase: '', slug: '' },
  window.GOREVIEW_CONFIG || {},
);

const DB_NAME = 'go-review';
const DB_VERSION = 1;
const STORE_CACHE = 'cache';
const STORE_QUEUE = 'queue';

let dbPromise = null;

function openDb() {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE_CACHE)) db.createObjectStore(STORE_CACHE);
      if (!db.objectStoreNames.contains(STORE_QUEUE)) {
        db.createObjectStore(STORE_QUEUE, { keyPath: 'id', autoIncrement: true });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbPromise;
}

function tx(store, mode, fn) {
  return openDb().then((db) => new Promise((resolve, reject) => {
    const transaction = db.transaction(store, mode);
    const req = fn(transaction.objectStore(store));
    transaction.oncomplete = () => resolve(req && req.result);
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error);
  }));
}

const idbGet = (key) => tx(STORE_CACHE, 'readonly', (s) => s.get(key));
const idbPut = (key, value) => tx(STORE_CACHE, 'readwrite', (s) => s.put(value, key));

// ---------------------------------------------------------------- 取得

function dataUrl(path) {
  const base = CONFIG.slug ? `${CONFIG.dataBase}/${CONFIG.slug}` : CONFIG.dataBase;
  return `${base}/${path}`;
}

export async function loadJson(path, { force = false } = {}) {
  const key = `data:${path}`;
  if (!force && !navigator.onLine) {
    const cached = await idbGet(key);
    if (cached) return cached;
  }
  try {
    const res = await fetch(dataUrl(path), { cache: 'no-cache' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    await idbPut(key, payload);
    return payload;
  } catch (err) {
    const cached = await idbGet(key);
    if (cached) return cached;
    throw err;
  }
}

export const loadIndex = () => loadJson('index.json');
export const loadGame = (gameId) => loadJson(`games/${gameId}.json`);
export const loadProblems = () => loadJson('problems.json');
export const loadDue = () => loadJson('due.json');
export const loadDashboard = () => loadJson('dashboard.json');

// よく使うデータを先読みしてオフラインに備える
export async function prefetch(limit = 5) {
  try {
    const index = await loadIndex();
    await Promise.allSettled([loadProblems(), loadDue(), loadDashboard()]);
    const games = (index.games || []).slice(0, limit);
    await Promise.allSettled(games.map((g) => loadGame(g.game_id)));
    return games.length;
  } catch {
    return 0;
  }
}

// ---------------------------------------------------------------- 送信キュー

export async function enqueue(kind, payload) {
  await tx(STORE_QUEUE, 'readwrite', (s) => s.add({
    kind, payload, created_at: new Date().toISOString(),
  }));
  flushQueue();
}

export async function queueSize() {
  const all = await tx(STORE_QUEUE, 'readonly', (s) => s.getAll());
  return (all || []).length;
}

function apiUrl(path) {
  if (CONFIG.apiBase) return `${CONFIG.apiBase.replace(/\/$/, '')}${path}`;
  return path;   // 同一オリジン（解析機のローカルサーバから配信している場合）
}

export async function flushQueue() {
  if (!navigator.onLine) return 0;
  const items = (await tx(STORE_QUEUE, 'readonly', (s) => s.getAll())) || [];
  let sent = 0;
  for (const item of items) {
    const endpoint = {
      answer: '/api/answer',
      tsumego: '/api/tsumego',
      'tsumego-answer': '/api/tsumego-answer',
      note: '/api/note',
    }[item.kind];
    if (!endpoint) continue;
    try {
      const res = await fetch(apiUrl(endpoint), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(item.payload),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await tx(STORE_QUEUE, 'readwrite', (s) => s.delete(item.id));
      sent += 1;
    } catch {
      break;   // 送れないうちは残す。端末側を正としない。
    }
  }
  return sent;
}

window.addEventListener('online', () => { flushQueue(); });

// ---------------------------------------------------------------- 解析機の在否

let engineState = null;

export async function probeEngine() {
  if (engineState) return engineState;
  try {
    const res = await fetch(apiUrl('/api/health'), { cache: 'no-store' });
    if (!res.ok) throw new Error('unavailable');
    engineState = await res.json();
  } catch {
    engineState = { ok: false, katago: false };
  }
  return engineState;
}

// 検討モード: 並べた手をその場で評価する（自宅Wi-Fi・解析機起動中のみ）
export async function analyzePosition(sgf, moves, visits) {
  const res = await fetch(apiUrl('/api/analyze'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sgf, moves, visits }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${res.status}`);
  }
  return res.json();
}

// ---------------------------------------------------------------- ローカル状態

const LOCAL_KEY = 'go-review:local';

export function localState() {
  try {
    return JSON.parse(localStorage.getItem(LOCAL_KEY) || '{}');
  } catch {
    return {};
  }
}

export function saveLocal(patch) {
  const next = Object.assign(localState(), patch);
  localStorage.setItem(LOCAL_KEY, JSON.stringify(next));
  return next;
}

// 自己診断（US-07）。AI 評価を見る前の敗着候補をここに残す。
export function saveSelfDiagnosis(gameId, moveNo, note) {
  const state = localState();
  const diagnoses = state.diagnoses || {};
  diagnoses[gameId] = { move_no: moveNo, note: note || '', at: new Date().toISOString() };
  saveLocal({ diagnoses });
  return diagnoses[gameId];
}

export function getSelfDiagnosis(gameId) {
  return (localState().diagnoses || {})[gameId] || null;
}

// 回答結果はサーバへ送りつつ、端末側にも直近の結果を残す
export async function submitAnswer(problemId, coord, seconds, hintUsed, reason) {
  const payload = {
    problem_id: problemId,
    coord,
    seconds,
    hint_used: Boolean(hintUsed),
    reason: reason || '',
  };
  await enqueue('answer', payload);
  const state = localState();
  const answers = state.answers || {};
  answers[problemId] = { coord, at: new Date().toISOString() };
  saveLocal({ answers });
  return payload;
}

// アプリ内蔵の詰碁を1問解いたときの記録（正誤は盤上の着手で判定済みのものを送る）
export async function submitTsumegoAnswer(tsumegoId, isCorrect, seconds, hintUsed) {
  const payload = {
    tsumego_id: tsumegoId,
    is_correct: Boolean(isCorrect),
    seconds,
    hint_used: Boolean(hintUsed),
  };
  await enqueue('tsumego-answer', payload);
  return payload;
}

export async function submitTsumego(solved, wrong, themes, source) {
  return enqueue('tsumego', {
    solved, wrong, themes, source,
    date: new Date().toISOString().slice(0, 10),
  });
}

export async function submitNote(date, note) {
  return enqueue('note', { date, note });
}
