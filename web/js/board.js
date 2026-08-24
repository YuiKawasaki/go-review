// 盤面の描画とタップ処理。
// 着手はダブルタップ方式（1タップ目で位置確認、2タップ目で確定）。
// 九路盤でも指の誤操作を防ぐため（非機能要件 操作性）。

import { Board, colLetter } from './goban.js';

const MARKER_STYLE = {
  good:     { color: '#1f9d55', shape: 'circle' },
  dubious:  { color: '#d69e2e', shape: 'triangle' },
  bad:      { color: '#e53e3e', shape: 'cross' },
  critical: { color: '#c53030', shape: 'cross' },
  losing:   { color: '#9b1c1c', shape: 'double-cross' },
};

export class BoardView {
  constructor(canvas, options = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.size = options.size || 9;
    this.showNumbers = options.showNumbers ?? false;
    this.onPlay = options.onPlay || null;     // (coord) => void  確定時
    this.pending = null;                      // 1タップ目の候補
    this.state = null;
    this.markers = [];                        // {coord, type, label}
    this.ghosts = [];                         // 変化図の予定手 {coord, color, label}
    this.lastMove = null;
    this.interactive = Boolean(options.onPlay);

    // DOM へ挿入される前に setState されると幅が 0 で描けない。
    // レイアウト確定時に描き直す。
    if (typeof ResizeObserver !== 'undefined') {
      this.observer = new ResizeObserver(() => this.draw());
      this.observer.observe(canvas);
    }

    canvas.addEventListener('click', (e) => this.handleTap(e));
    canvas.addEventListener('touchend', (e) => {
      if (e.cancelable) e.preventDefault();
      const touch = e.changedTouches[0];
      if (touch) this.handleTap(touch);
    }, { passive: false });
  }

  setState(state, { lastMove = null, markers = [], ghosts = [], numbers = null } = {}) {
    this.state = state;
    this.lastMove = lastMove;
    this.markers = markers;
    this.ghosts = ghosts;
    this.numbers = numbers;
    this.pending = null;
    this.draw();
  }

  setShowNumbers(value) {
    this.showNumbers = value;
    this.draw();
  }

  // ---------------------------------------------------------- 座標変換

  metrics() {
    const rect = this.canvas.getBoundingClientRect();
    const px = Math.min(rect.width, rect.height) || 320;
    const pad = px * 0.09;
    const step = (px - pad * 2) / (this.size - 1);
    return { px, pad, step, rect };
  }

  toPixel(col, row) {
    const { pad, step } = this.metrics();
    return [pad + col * step, pad + row * step];
  }

  fromEvent(event) {
    const { pad, step, rect } = this.metrics();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const col = Math.round((x - pad) / step);
    const row = Math.round((y - pad) / step);
    if (col < 0 || col >= this.size || row < 0 || row >= this.size) return null;
    // 交点から離れすぎたタップは無効にする
    const [cx, cy] = this.toPixel(col, row);
    if (Math.hypot(x - cx, y - cy) > step * 0.55) return null;
    return [col, row];
  }

  handleTap(event) {
    if (!this.interactive) return;
    const coord = this.fromEvent(event);
    if (!coord) { this.pending = null; this.draw(); return; }
    if (this.state && this.state.grid[coord[1] * this.size + coord[0]]) {
      this.pending = null;
      this.draw();
      return;
    }
    if (this.pending && this.pending[0] === coord[0] && this.pending[1] === coord[1]) {
      const confirmed = this.pending;
      this.pending = null;
      this.draw();
      this.onPlay(confirmed);
      return;
    }
    this.pending = coord;   // 1 タップ目: 位置の確認だけ
    this.draw();
  }

  // ---------------------------------------------------------- 描画

  draw() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    const px = Math.min(rect.width, rect.height);
    if (px < 2) return;   // まだレイアウトされていない
    if (this.canvas.width !== Math.round(px * dpr)) {
      this.canvas.width = Math.round(px * dpr);
      this.canvas.height = Math.round(px * dpr);
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, px, px);

    const { pad, step } = this.metrics();
    const stone = step * 0.46;

    // 盤
    ctx.fillStyle = getComputedStyle(this.canvas).getPropertyValue('--board-bg').trim() || '#e8c88a';
    ctx.fillRect(0, 0, px, px);

    ctx.strokeStyle = '#4a3a20';
    ctx.lineWidth = Math.max(1, step * 0.03);
    ctx.beginPath();
    for (let i = 0; i < this.size; i += 1) {
      const p = pad + i * step;
      ctx.moveTo(pad, p); ctx.lineTo(pad + (this.size - 1) * step, p);
      ctx.moveTo(p, pad); ctx.lineTo(p, pad + (this.size - 1) * step);
    }
    ctx.stroke();

    // 座標ラベル（列 A〜J[Iは飛ばす] / 行 9〜1。GTP表記と一致させる）
    ctx.fillStyle = '#4a3a20';
    ctx.font = `${Math.round(step * 0.32)}px system-ui, sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (let col = 0; col < this.size; col += 1) {
      const [x] = this.toPixel(col, 0);
      ctx.fillText(colLetter(col), x, pad * 0.42);
    }
    for (let row = 0; row < this.size; row += 1) {
      const [, y] = this.toPixel(0, row);
      ctx.fillText(String(this.size - row), pad * 0.42, y);
    }

    // 星
    const stars = this.size === 9 ? [[2, 2], [6, 2], [4, 4], [2, 6], [6, 6]] : [];
    ctx.fillStyle = '#4a3a20';
    for (const [c, r] of stars) {
      const [x, y] = this.toPixel(c, r);
      ctx.beginPath();
      ctx.arc(x, y, step * 0.08, 0, Math.PI * 2);
      ctx.fill();
    }

    if (!this.state) return;

    // 石
    for (let row = 0; row < this.size; row += 1) {
      for (let col = 0; col < this.size; col += 1) {
        const v = this.state.grid[row * this.size + col];
        if (!v) continue;
        this.drawStone(col, row, v, stone, 1);
      }
    }

    // 変化図の予定手（半透明）
    for (const g of this.ghosts) {
      if (!g.coord) continue;
      this.drawStone(g.coord[0], g.coord[1], g.color, stone, 0.45);
      if (g.label) this.drawLabel(g.coord, g.label, g.color === 'B' ? '#fff' : '#111', stone);
    }

    // 着手番号
    if (this.showNumbers && this.numbers) {
      for (const [index, number] of this.numbers.entries()) {
        const col = index % this.size;
        const row = Math.floor(index / this.size);
        const v = this.state.grid[index];
        if (!v) continue;
        this.drawLabel([col, row], String(number), v === 'B' ? '#fff' : '#111', stone);
      }
    }

    // 直前の手
    if (this.lastMove) {
      const [x, y] = this.toPixel(this.lastMove[0], this.lastMove[1]);
      ctx.strokeStyle = '#e53e3e';
      ctx.lineWidth = Math.max(1.5, step * 0.06);
      ctx.beginPath();
      ctx.arc(x, y, stone * 0.45, 0, Math.PI * 2);
      ctx.stroke();
    }

    // 好手・悪手マーカー
    for (const marker of this.markers) {
      if (!marker.coord) continue;
      this.drawMarker(marker.coord, marker.type, stone);
    }

    // ダブルタップの 1 タップ目
    if (this.pending) {
      const [x, y] = this.toPixel(this.pending[0], this.pending[1]);
      ctx.strokeStyle = '#2b6cb0';
      ctx.lineWidth = Math.max(2, step * 0.07);
      ctx.setLineDash([step * 0.15, step * 0.12]);
      ctx.beginPath();
      ctx.arc(x, y, stone, 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  drawStone(col, row, color, radius, alpha) {
    const ctx = this.ctx;
    const [x, y] = this.toPixel(col, row);
    ctx.globalAlpha = alpha;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fillStyle = color === 'B' ? '#161616' : '#fdfdfd';
    ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = color === 'B' ? '#000' : '#888';
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  drawLabel(coord, text, color, radius) {
    const ctx = this.ctx;
    const [x, y] = this.toPixel(coord[0], coord[1]);
    ctx.fillStyle = color;
    ctx.font = `bold ${Math.round(radius * 0.95)}px system-ui, sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, x, y);
  }

  drawMarker(coord, type, radius) {
    const style = MARKER_STYLE[type];
    if (!style) return;
    const ctx = this.ctx;
    const [x, y] = this.toPixel(coord[0], coord[1]);
    ctx.strokeStyle = style.color;
    ctx.fillStyle = style.color;
    ctx.lineWidth = Math.max(2, radius * 0.18);
    const r = radius * 0.62;

    if (style.shape === 'circle') {
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.stroke();
    } else if (style.shape === 'triangle') {
      ctx.beginPath();
      ctx.moveTo(x, y - r);
      ctx.lineTo(x + r * 0.9, y + r * 0.7);
      ctx.lineTo(x - r * 0.9, y + r * 0.7);
      ctx.closePath();
      ctx.stroke();
    } else {
      const draw = (scale) => {
        ctx.beginPath();
        ctx.moveTo(x - r * scale, y - r * scale);
        ctx.lineTo(x + r * scale, y + r * scale);
        ctx.moveTo(x + r * scale, y - r * scale);
        ctx.lineTo(x - r * scale, y + r * scale);
        ctx.stroke();
      };
      draw(1);
      if (style.shape === 'double-cross') draw(0.55);
    }
  }
}

// 盤面配列から Board を作る（変化図の再生に使う）
export function boardFromGrid(grid, size) {
  const board = new Board(size);
  board.grid = grid.slice();
  return board;
}
