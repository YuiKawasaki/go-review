import { enqueue, jsonResponse } from './_kv.js';

export async function onRequestPost({ request, env }) {
  let payload;
  try {
    payload = await request.json();
  } catch {
    return jsonResponse({ error: 'invalid JSON' }, 400);
  }
  if (!payload.date) {
    return jsonResponse({ error: 'date is required' }, 400);
  }
  await enqueue(env, 'note', payload);
  return jsonResponse({ ok: true, queued: true });
}
