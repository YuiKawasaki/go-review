// 自由に石を置ける碁盤。黒白交互に着手を記録し、SGFとして書き出せる。
// 演習・詰碁と違って正誤判定は無い。並べて確認するための機能。

import { Board, coordToGtp, coordToSgf, opposite } from './goban.js';
import { BoardView } from './board.js';

const SIZES = [9, 13, 19];

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

// replaceChildren は非 Node の引数を文字列化するため、条件付きの子要素は
// 必ずこれを通す（sequence.js と同じ理由）。
const fill = (node, ...children) => {
  node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined));
  return node;
};

export function viewFreePlay(app) {
  let size = 9;
  let board = new Board(size);
  let moves = [];        // {color, coord|null}
  let numbers = new Map();

  const canvas = el('canvas', { class: 'board' });
  const view = new BoardView(canvas, { size, onPlay: (coord) => placeStone(coord) });

  const sizeRow = el('div', { class: 'chips' });
  const statusLine = el('p', { class: 'muted' });
  const controls = el('div', { class: 'controls' });
  const moveList = el('p', { class: 'muted small' });

  function toMove() {
    return moves.length % 2 === 0 ? 'B' : 'W';
  }

  // 一手戻す・盤の大きさ変更のたびに、記録した着手を最初から打ち直して
  // 盤面を作り直す。取った石を個別に元へ戻す処理を持たずに済む。
  function replay() {
    board = new Board(size);
    numbers = new Map();
    moves.forEach((mv, i) => {
      if (!mv.coord) return;
      const captured = board.play(mv.color, mv.coord);
      if (captured) {
        for (const s of captured) numbers.delete(board.idx(s));
        numbers.set(board.idx(mv.coord), i + 1);
      }
    });
  }

  function reset(newSize) {
    size = newSize;
    view.size = size;
    board = new Board(size);
    moves = [];
    numbers = new Map();
    renderSizeRow();
    render();
  }

  function placeStone(coord) {
    if (board.get(coord)) return;
    const color = toMove();
    const captured = board.play(color, coord);
    if (captured === null) {
      statusLine.textContent = '自殺手になるため、そこには打てません。';
      return;
    }
    if (captured.length) for (const s of captured) numbers.delete(board.idx(s));
    numbers.set(board.idx(coord), moves.length + 1);
    moves.push({ color, coord });
    render();
  }

  function pass() {
    moves.push({ color: toMove(), coord: null });
    render();
  }

  function undo() {
    if (!moves.length) return;
    moves.pop();
    replay();
    render();
  }

  function exportSgf() {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const dt = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
    let sgf = `(;GM[1]FF[4]CA[UTF-8]AP[go-review]SZ[${size}]DT[${dt}]`;
    for (const mv of moves) sgf += `;${mv.color}[${mv.coord ? coordToSgf(mv.coord) : ''}]`;
    sgf += ')';

    const blob = new Blob([sgf], { type: 'application/x-go-sgf' });
    const url = URL.createObjectURL(blob);
    const a = el('a', {
      href: url,
      download: `kifu_${dt.replace(/-/g, '')}_${pad(now.getHours())}${pad(now.getMinutes())}.sgf`,
    });
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  function renderSizeRow() {
    fill(sizeRow, ...SIZES.map((s) => el('button', {
      class: s === size ? 'chip on' : 'chip',
      onclick: () => reset(s),
    }, `${s}路盤`)));
  }

  function renderControls() {
    fill(
      controls,
      el('button', { disabled: moves.length ? null : 'disabled', onclick: undo }, '一手戻す'),
      el('button', { onclick: pass }, 'パス'),
      el('button', { onclick: () => reset(size) }, '新規対局'),
      el('button', {
        class: 'primary',
        disabled: moves.length ? null : 'disabled',
        onclick: exportSgf,
      }, 'SGFを書き出す'),
    );
  }

  function renderMoveList() {
    moveList.textContent = moves.length
      ? moves.map((mv, i) =>
        `${i + 1}手目 ${mv.color === 'B' ? '黒' : '白'} ${mv.coord ? coordToGtp(mv.coord, size) : 'パス'}`).join('　')
      : '';
  }

  function render() {
    view.setState({ grid: board.grid }, {
      lastMove: moves.length ? moves[moves.length - 1].coord : null,
      numbers,
    });
    statusLine.textContent =
      `${moves.length + 1}手目: ${toMove() === 'B' ? '黒' : '白'}番`
      + `　取り石 黒 ${board.captures.B} / 白 ${board.captures.W}`;
    renderControls();
    renderMoveList();
  }

  renderSizeRow();
  app.replaceChildren(
    el('h2', { class: 'section-title' }, '自由対局'),
    el('p', { class: 'muted small' }, '交点を2回タップで確定します（誤操作防止のため）。'),
    sizeRow,
    canvas,
    statusLine,
    controls,
    el('p', { class: 'muted small' }, 'エクスポートせずに新規対局すると、この記録は消えます。'),
    el('h2', { class: 'section-title' }, '棋譜'),
    moveList,
  );
  render();
}
