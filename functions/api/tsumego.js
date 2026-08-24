import { enqueue, jsonResponse } from './_kv.js';

export async function onRequestPost({ request, env }) {
  let payload;
  try {
    payload = await request.json();
  } catch {
    return jsonResponse({ error: 'invalid JSON' }, 400);
  }
  await enqueue(env, 'tsumego', payload);
  return jsonResponse({ ok: true, queued: true });
}
