// 手順を盤上で 1 手ずつ送って見せる部品。
//
// 「その手が悪い」と字で言われても分からないので、打つとどうなるかを
// 盤で見せる、というのがこのアプリの中心にある考え方。棋譜画面には
// 前からその仕組みがあったが、練習画面と詰碁画面には無かった。
// 同じものを 3 か所で書かずに済むよう、ここに切り出してある。
//
// 呼び出し側との約束:
//   - sequences[].pv の 1 手目は、必ず「問題の局面での着手」から始まる
//   - firstColor は その 1 手目を打つ側の色
// この 2 つが守られていれば、正解の手順も咎められる手順も同じ扱いでよい。

import { Board, gtpToCoord, opposite } from './goban.js';

const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
};

const CAVEAT = '双方が最善で打った場合の一本道です。実際の相手が同じように打つとは限りません。';

/**
 * @param {object}   opts
 * @param {BoardView} opts.view        盤の描画先（呼び出し側と共有する）
 * @param {number}   opts.size         盤の大きさ
 * @param {object}   opts.startState   問題の局面 {grid, numbers?}
 * @param {string}   opts.firstColor   最初の 1 手を打つ側 'B' | 'W'
 * @param {Array}    opts.sequences    [{key, label, pv, comments, note, tone}]
 * @returns {{element: HTMLElement, show: (key:string)=>void}}
 */
export function createSequencePlayer({ view, size, startState, firstColor, sequences }) {
  const usable = (sequences || []).filter((s) => s && (s.pv || []).length);
  const panel = el('div', { class: 'sequence' });

  if (!usable.length) {
    panel.appendChild(el('p', { class: 'muted small' }, '盤で見せられる手順が用意できていません。'));
    return { element: panel, show: () => {} };
  }

  let current = usable[0];
  let step = 0;               // 0 = 問題の局面（まだ 1 手も進めていない）
  // 答え合わせの直後は、呼び出し側が正解点に印を付けた盤を出している。
  // 学習者が手順を送り始めるまでは、その盤を横取りしない。
  let touched = false;

  const tabs = el('div', { class: 'row seq-tabs' });
  const controls = el('div', { class: 'row seq-controls' });
  const commentBox = el('p', { class: 'pv-comment' });
  const noteBox = el('p', { class: 'muted small' });

  panel.append(tabs, controls, commentBox, noteBox,
    el('p', { class: 'muted small' }, CAVEAT));

  function stateAt(n) {
    const board = new Board(size);
    board.grid = startState.grid.slice();
    let color = firstColor;
    for (let i = 0; i < n; i += 1) {
      const coord = gtpToCoord(current.pv[i], size);
      if (coord) board.play(color, coord);
      color = opposite(color);
    }
    return { grid: board.grid };
  }

  // 盤上に残っている石にだけ番号を振る。取られた石に番号を残すと、
  // どれが今ある石なのか分からなくなる。
  function ghostsAt(n) {
    const grid = stateAt(n).grid;
    const out = [];
    let color = firstColor;
    for (let i = 0; i < n; i += 1) {
      const coord = gtpToCoord(current.pv[i], size);
      if (coord && grid[coord[1] * size + coord[0]]) {
        out.push({ coord, color, label: String(i + 1) });
      }
      color = opposite(color);
    }
    return out;
  }

  function draw() {
    if (touched) {
      view.interactive = false;
      view.setState(stateAt(step), { ghosts: ghostsAt(step) });
    }

    // 手順が 1 つだけのときは切り替えるものが無いので、ラベルを見出しとして出す。
    // タブの行を空のままにすると、今どの手順を見ているのか分からなくなる。
    tabs.replaceChildren(...(usable.length > 1
      ? usable.map((seq) => el('button', {
        class: seq === current ? 'chip on' : 'chip',
        onclick: () => { current = seq; step = 0; touched = true; draw(); },
      }, seq.label))
      : [el('span', { class: 'seq-single' }, current.label)]));

    const total = current.pv.length;
    controls.replaceChildren(
      el('button', { disabled: step <= 0 ? 'disabled' : null, onclick: () => go(-1) }, '◀'),
      el('span', { class: 'seq-count' }, step === 0 ? `問題の局面` : `${step} / ${total} 手`),
      el('button', { disabled: step >= total ? 'disabled' : null, onclick: () => go(1) }, '▶'),
      el('button', { class: 'link', onclick: () => { step = 0; touched = true; draw(); } }, '最初から'),
      step < total
        ? el('button', { class: 'link', onclick: () => { step = total; touched = true; draw(); } }, '最後まで')
        : null,
    );

    const comment = step > 0 ? (current.comments || [])[step - 1] || '' : '';
    if (step === 0) {
      commentBox.textContent = '▶ を押すと、この手順が 1 手ずつ進みます。';
    } else {
      const [actor, text] = splitComment(comment);
      const who = actor ? `（${actor}）` : '';
      commentBox.textContent = `${current.pv[step - 1]}${who} ${text}`;
    }
    noteBox.textContent = current.note || '';
  }

  function go(delta) {
    step = Math.max(0, Math.min(current.pv.length, step + delta));
    touched = true;
    draw();
  }

  function show(key) {
    const found = usable.find((s) => s.key === key);
    if (!found) return;
    current = found;
    step = 0;
    touched = true;
    draw();
  }

  draw();
  return { element: panel, show };
}

// pv_comments の 1 要素を「打ち手」と「内容」に分ける。
// 先頭要素に付く前置きは、盤の下に別途出しているのでここでは落とす
// （go_review/explain.py の _split_comment と同じ処理）。
function splitComment(comment) {
  let text = (comment || '').trim();
  for (const head of ['相手が最善で応じれば、という前提の進行です。',
    '相手が最善で咎めてきた場合の進行です。']) {
    if (text.startsWith(head)) text = text.slice(head.length).trim();
  }
  for (const actor of ['自分', '相手']) {
    if (text.startsWith(`${actor}:`)) {
      return [actor, text.slice(actor.length + 1).trim() || 'この地点に打ちます。'];
    }
  }
  return ['', text || 'この地点に打ちます。'];
}
