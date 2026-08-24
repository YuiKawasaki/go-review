import { enqueue, jsonResponse } from './_kv.js';

// 詰碁1問ごとの回答（アプリ内蔵の詰碁を解いた場合）。
// 集計だけの /api/tsumego（別アプリで解いた分の手入力）とは別経路。
export async function onRequestPost({ request, env }) {
  let payload;
  try {
    payload = await request.json();
  } catch {
    return jsonResponse({ error: 'invalid JSON' }, 400);
  }
  if (!payload.tsumego_id) {
    return jsonResponse({ error: 'tsumego_id is required' }, 400);
  }
  await enqueue(env, 'tsumego_answer', payload);
  return jsonResponse({ ok: true, queued: true });
}
