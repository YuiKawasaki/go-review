// PWA から送られてくる回答・記録を Cloudflare KV に一時保管する共通処理。
// 夜間バッチが読み出して Surface 側の本物の DB に取り込み、読み終えたら消す
// （ここは受信箱であって、正のデータではない）。

export async function enqueue(env, kind, payload) {
  const key = `${kind}:${Date.now()}:${crypto.randomUUID()}`;
  await env.ANSWERS_KV.put(key, JSON.stringify(payload));
  return key;
}

export function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
}
