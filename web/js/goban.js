// 盤面ルールと SGF の最小パーサ（Python 側 goban.py / sgf.py と同じ規則）。

const GTP_COLS = 'ABCDEFGHJKLMNOPQRST';

export function sgfToCoord(s, size) {
  if (!s || s.length !== 2) return null;
  const col = s.charCodeAt(0) - 97;
  const row = s.charCodeAt(1) - 97;
  if (col < 0 || col >= size || row < 0 || row >= size) return null;
  return [col, row];
}

export function colLetter(col) {
  return GTP_COLS[col];
}

export function coordToGtp(c, size) {
  if (!c) return 'pass';
  return GTP_COLS[c[0]] + (size - c[1]);
}

export function gtpToCoord(s, size) {
  if (!s) return null;
  const t = String(s).trim().toUpperCase();
  if (t === 'PASS' || t === 'RESIGN' || t === '') return null;
  const col = GTP_COLS.indexOf(t[0]);
  const row = size - parseInt(t.slice(1), 10);
  if (col < 0 || Number.isNaN(row) || row < 0 || row >= size) return null;
  return [col, row];
}

export function opposite(color) {
  return color === 'B' ? 'W' : 'B';
}

// ---------------------------------------------------------------- SGF

export function parseSgf(text) {
  const props = {};
  const moves = [];
  let i = 0;
  let depth = 0;
  let branchSkipped = false;

  while (i < text.length) {
    const ch = text[i];
    if (ch === '(') { depth += 1; if (depth > 1) branchSkipped = true; i += 1; continue; }
    if (ch === ')') { depth -= 1; i += 1; continue; }
    if (ch === ';') { i += 1; continue; }
    if (/[A-Za-z]/.test(ch)) {
      let start = i;
      while (i < text.length && /[A-Za-z]/.test(text[i])) i += 1;
      const ident = text.slice(start, i).toUpperCase();
      const values = [];
      while (true) {
        while (i < text.length && /\s/.test(text[i])) i += 1;
        if (text[i] !== '[') break;
        i += 1;
        let out = '';
        while (i < text.length && text[i] !== ']') {
          if (text[i] === '\\') { i += 1; if (i < text.length) { out += text[i]; i += 1; } continue; }
          out += text[i];
          i += 1;
        }
        i += 1;
        values.push(out);
      }
      // 分岐に入ったあとの着手は本線ではないので取らない
      if ((ident === 'B' || ident === 'W') && !branchSkipped) {
        moves.push({ color: ident, raw: values[0] ?? '' });
      } else if (!(ident === 'B' || ident === 'W')) {
        if (!props[ident]) props[ident] = [];
        props[ident].push(...values);
      }
      continue;
    }
    i += 1;
  }

  const size = parseInt((props.SZ && props.SZ[0]) || '9', 10) || 9;
  return {
    size,
    komi: parseFloat((props.KM && props.KM[0]) || '0') || 0,
    result: (props.RE && props.RE[0]) || '',
    pb: (props.PB && props.PB[0]) || '',
    pw: (props.PW && props.PW[0]) || '',
    setupBlack: (props.AB || []).map((v) => sgfToCoord(v, size)).filter(Boolean),
    setupWhite: (props.AW || []).map((v) => sgfToCoord(v, size)).filter(Boolean),
    moves: moves.map((m) => ({ color: m.color, coord: sgfToCoord(m.raw, size) })),
  };
}

// ---------------------------------------------------------------- 盤面

export class Board {
  constructor(size = 9) {
    this.size = size;
    this.grid = new Array(size * size).fill(null);
    this.captures = { B: 0, W: 0 };
  }

  clone() {
    const b = new Board(this.size);
    b.grid = this.grid.slice();
    b.captures = { ...this.captures };
    return b;
  }

  idx(c) { return c[1] * this.size + c[0]; }
  get(c) { return this.grid[this.idx(c)]; }
  set(c, v) { this.grid[this.idx(c)] = v; }
  onBoard(c) { return c[0] >= 0 && c[0] < this.size && c[1] >= 0 && c[1] < this.size; }

  neighbors(c) {
    const out = [];
    for (const [dc, dr] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const n = [c[0] + dc, c[1] + dr];
      if (this.onBoard(n)) out.push(n);
    }
    return out;
  }

  group(c) {
    const color = this.get(c);
    if (!color) return { stones: [], liberties: [] };
    const seen = new Set();
    const stones = [];
    const liberties = new Set();
    const stack = [c];
    while (stack.length) {
      const cur = stack.pop();
      const key = this.idx(cur);
      if (seen.has(key)) continue;
      seen.add(key);
      stones.push(cur);
      for (const n of this.neighbors(cur)) {
        const v = this.get(n);
        if (!v) liberties.add(this.idx(n));
        else if (v === color && !seen.has(this.idx(n))) stack.push(n);
      }
    }
    return { stones, liberties: [...liberties] };
  }

  libertyCount(c) { return this.group(c).liberties.length; }

  // 打てたら取り上げた石の配列、打てなければ null
  play(color, coord) {
    if (!coord) return [];
    if (!this.onBoard(coord) || this.get(coord)) return null;
    this.set(coord, color);
    const enemy = opposite(color);
    const captured = [];
    for (const n of this.neighbors(coord)) {
      if (this.get(n) !== enemy) continue;
      const g = this.group(n);
      if (g.liberties.length === 0) {
        for (const s of g.stones) this.set(s, null);
        captured.push(...g.stones);
      }
    }
    if (captured.length === 0 && this.group(coord).liberties.length === 0) {
      this.set(coord, null);
      return null;   // 自殺手
    }
    if (captured.length) this.captures[color] += captured.length;
    return captured;
  }
}

// 指定手数までの盤面と、各手の情報（取り上げも反映）
export function buildStates(parsed) {
  const board = new Board(parsed.size);
  for (const c of parsed.setupBlack) board.set(c, 'B');
  for (const c of parsed.setupWhite) board.set(c, 'W');

  const states = [{ grid: board.grid.slice(), last: null, moveNo: 0, captures: { B: 0, W: 0 } }];
  const numbers = new Map();   // 盤上の位置 -> 手数

  parsed.moves.forEach((mv, index) => {
    const captured = board.play(mv.color, mv.coord);
    if (captured) {
      for (const s of captured) numbers.delete(board.idx(s));
      if (mv.coord) numbers.set(board.idx(mv.coord), index + 1);
    }
    states.push({
      grid: board.grid.slice(),
      last: mv.coord || null,
      moveNo: index + 1,
      color: mv.color,
      isPass: !mv.coord,
      captures: { ...board.captures },
      numbers: new Map(numbers),
    });
  });
  return states;
}
