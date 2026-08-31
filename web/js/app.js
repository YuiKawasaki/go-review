// 画面遷移と各ビュー。
//
// 学習設計上の要点:
//  - 回答前に AI 評価値・正解手を一切表示しない（FR-09）
//  - 棋譜画面は「自己診断 → AI答え合わせ」の順序を UI で強制する（US-07）

import { BoardView } from './board.js';
import { Board, buildStates, coordToGtp, gtpToCoord, opposite, parseSgf } from './goban.js';
import { renderExplanation } from './glossary.js';
import { createSequencePlayer } from './sequence.js';
import * as store from './store.js';

const app = document.getElementById('app');
const statusBar = document.getElementById('status-bar');

const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value !== null && value !== undefined) {
      node.setAttribute(key, value);
    }
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
};

// replaceChildren / append は null を文字列 "null" にしてしまうので、
// 条件付きの子要素を渡すときは必ずこれを通す。
const fill = (node, ...children) => {
  node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined));
  return node;
};

const fmt = (value, digits = 0, suffix = '') =>
  (value === null || value === undefined || Number.isNaN(value)) ? '—' : `${Number(value).toFixed(digits)}${suffix}`;

function setStatus(text, tone = '') {
  statusBar.textContent = text || '';
  statusBar.className = tone ? `status ${tone}` : 'status';
}

function nav(hash) { window.location.hash = hash; }

// ---------------------------------------------------------------- ホーム

async function viewHome() {
  app.replaceChildren(el('p', { class: 'loading' }, '読み込み中…'));
  let index;
  try {
    index = await store.loadIndex();
  } catch {
    app.replaceChildren(el('div', { class: 'card' }, [
      el('h2', {}, 'データがまだありません'),
      el('p', {}, '解析機でバッチを実行し、data/ を配信してください。'),
    ]));
    return;
  }

  const due = await store.loadDue().catch(() => ({ problems: [], tsumego: [] }));
  const dueTsumego = (due.tsumego || []).filter((t) => t.interactive);
  const pending = index.unanalyzed || 0;
  const queued = await store.queueSize();

  const cards = [
    el('div', { class: 'card' }, [
      el('div', { class: 'row-between' }, [
        el('h2', {}, '今日の復習'),
        el('span', { class: 'badge' }, `${(due.problems || []).length} 問`),
      ]),
      el('p', { class: 'muted' }, '過去の自分の悪手局面から出題します。'),
      el('button', {
        class: 'primary',
        disabled: (due.problems || []).length ? null : 'disabled',
        onclick: () => nav('#/quiz'),
      }, '演習を始める'),
    ]),
    el('div', { class: 'card' }, [
      el('div', { class: 'row-between' }, [
        el('h2', {}, '今日の詰碁'),
        el('span', { class: 'badge' }, `${dueTsumego.length} 問`),
      ]),
      el('p', { class: 'muted' }, dueTsumego.length
        ? '盤の上で解けます。間違えた問題はまた出ます。'
        : '出題できる詰碁がありません。'),
      el('button', {
        class: 'primary',
        disabled: dueTsumego.length ? null : 'disabled',
        onclick: () => nav('#/tsumego'),
      }, '詰碁を解く'),
      el('button', { class: 'link', onclick: () => nav('#/tsumego-log') }, '別アプリで解いた分を記録する'),
    ]),
  ];

  if (pending > 0) {
    cards.unshift(el('div', { class: 'card warn' }, [
      el('h2', {}, `未解析 ${pending} 局`),
      el('p', { class: 'muted' }, '解析機を起動すると、溜まった棋譜がまとめて処理されます。'),
    ]));
  }
  if (queued > 0) {
    cards.push(el('div', { class: 'card' }, [
      el('h2', {}, `送信待ち ${queued} 件`),
      el('p', { class: 'muted' }, 'オンラインに戻ると自動で送信します。'),
    ]));
  }

  const list = el('div', { class: 'list' }, (index.games || []).map((g) => el('button', {
    class: 'list-item',
    onclick: () => nav(`#/game/${g.game_id}`),
  }, [
    el('div', { class: 'list-main' }, [
      el('strong', {}, `${(g.played_at || '').slice(0, 10)} ${g.my_color === 'B' ? '黒' : '白'}`),
      el('span', { class: 'muted' }, ` vs ${g.opponent || '?'}`),
    ]),
    el('div', { class: 'list-sub' }, [
      el('span', { class: g.result === '勝' ? 'win' : 'lose' }, g.result || '—'),
      el('span', { class: 'muted' }, ` ${g.move_count || 0}手`),
      ...(g.main_tags || []).slice(0, 2).map((t) => el('span', { class: 'tag' }, t)),
    ]),
  ])));

  app.replaceChildren(
    el('section', { class: 'cards' }, cards),
    el('h2', { class: 'section-title' }, '対局一覧'),
    list,
    // いつ時点のデータを見ているかを出す。古い画面を掴んでいても
    // 表示だけでは気づけないため（実際それで解析結果が届いていないと誤解した）。
    el('p', { class: 'muted small' }, `データ更新: ${_stamp(index.generated_at)}`),
  );
}

function _stamp(iso) {
  if (!iso) return '不明';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '不明';
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ---------------------------------------------------------------- 棋譜リプレイ

async function viewGame(gameId) {
  app.replaceChildren(el('p', { class: 'loading' }, '棋譜を読み込み中…'));
  let game;
  try {
    game = await store.loadGame(gameId);
  } catch {
    app.replaceChildren(el('div', { class: 'card' }, '棋譜を取得できませんでした。'));
    return;
  }

  const parsed = parseSgf(game.sgf);
  const states = buildStates(parsed);
  const size = game.board_size || parsed.size || 9;
  const moves = game.moves || [];
  const badMoves = moves.filter((m) => m.severity);

  let cursor = 0;
  let showNumbers = false;
  let autoplay = null;
  let autoSpeed = 900;
  let variation = null;      // {branch, pv, comments, step}
  let revealed = Boolean(store.getSelfDiagnosis(gameId));

  const canvas = el('canvas', { class: 'board', width: 360, height: 360 });
  const view = new BoardView(canvas, { size, showNumbers });

  const winrateBar = el('div', { class: 'winrate-bar' }, [el('span')]);
  const graphHost = el('div', { class: 'graph' });
  const info = el('div', { class: 'move-info' });
  const commentary = el('div', { class: 'commentary' });
  const variationPanel = el('div', { class: 'variation-panel' });

  // ------------------------------------------------ 自己診断ゲート（US-07）
  const gate = el('div', { class: 'card gate' });

  function renderGate() {
    gate.replaceChildren();
    if (revealed) {
      const saved = store.getSelfDiagnosis(gameId);
      gate.replaceChildren(el('div', { class: 'row-between' }, [
        el('span', { class: 'muted' }, saved
          ? `自己診断: ${saved.move_no ? `${saved.move_no}手目` : '未指定'} ${saved.note || ''}`
          : 'AI評価を表示中'),
        el('button', { class: 'link', onclick: () => { revealed = false; render(); } }, '隠す'),
      ]));
      return;
    }
    const input = el('input', { type: 'number', min: '1', placeholder: '手数', class: 'small' });
    const note = el('input', { type: 'text', placeholder: '理由（任意）' });
    gate.replaceChildren(
      el('h2', {}, 'まず自分で敗着を予想する'),
      el('p', { class: 'muted' }, 'AIの評価を見る前に、崩れたと思う手を記録します。先に答えを見ると判断力が育ちません。'),
      el('div', { class: 'row' }, [input, note]),
      el('div', { class: 'row' }, [
        el('button', {
          class: 'primary',
          onclick: () => {
            store.saveSelfDiagnosis(gameId, Number(input.value) || null, note.value);
            revealed = true;
            render();
          },
        }, '記録してAI評価を見る'),
        el('button', { class: 'link', onclick: () => { revealed = true; render(); } }, 'スキップ'),
      ]),
    );
  }

  // ------------------------------------------------ 描画

  function markersFor(index) {
    // 盤上に残っている自分の好手・悪手にマーカーを重ねる（FR-07）
    if (!revealed) return [];
    const state = states[index];
    const out = [];
    for (const m of moves) {
      if (m.move_no > index || !m.marker) continue;
      const coord = gtpToCoord(m.coord, size);
      if (!coord) continue;
      if (!state.grid[coord[1] * size + coord[0]]) continue;   // 取られた石には付けない
      out.push({ coord, type: m.marker });
    }
    return out;
  }

  function ghostsFor() {
    if (!variation) return [];
    const start = variation.startState;
    let color = variation.firstColor;
    const ghosts = [];
    for (let i = 0; i < variation.step; i += 1) {
      const coord = gtpToCoord(variation.pv[i], size);
      if (coord) ghosts.push({ coord, color, label: String(i + 1) });
      color = opposite(color);
    }
    return ghosts;
  }

  function variationState() {
    if (!variation) return null;
    const board = new Board(size);
    board.grid = states[variation.startIndex].grid.slice();
    let color = variation.firstColor;
    for (let i = 0; i < variation.step; i += 1) {
      const coord = gtpToCoord(variation.pv[i], size);
      board.play(color, coord);
      color = opposite(color);
    }
    return { grid: board.grid };
  }

  function render() {
    const state = variation ? variationState() : states[cursor];
    const move = moves.find((m) => m.move_no === cursor);
    view.setShowNumbers(showNumbers);
    view.setState(state, {
      lastMove: variation ? null : (states[cursor].last || null),
      markers: variation ? [] : markersFor(cursor),
      ghosts: variation ? ghostsFor() : [],
      numbers: variation ? null : states[cursor].numbers,
    });

    // 勝率（常に自分視点）
    const winrate = revealed && move && move.winrate !== null && move.winrate !== undefined
      ? move.winrate : null;
    winrateBar.firstChild.style.width = winrate === null ? '50%' : `${winrate}%`;
    winrateBar.dataset.value = winrate === null ? '' : `${fmt(winrate, 0, '%')}`;

    fill(
      info,
      el('span', {}, `${cursor} / ${game.move_count} 手`),
      el('span', { class: 'muted' }, move ? ` ${move.color === 'B' ? '黒' : '白'} ${move.coord}` : ''),
      revealed && winrate !== null ? el('span', { class: 'winrate' }, ` 自分の勝率 ${fmt(winrate, 0, '%')}`) : null,
    );

    renderGraph();
    renderCommentary(move);
    renderVariation(move);
    renderGate();
    renderBadList();
  }

  function renderGraph() {
    graphHost.replaceChildren();
    if (!revealed) {
      graphHost.appendChild(el('p', { class: 'muted small' }, '自己診断のあとに勝率推移を表示します。'));
      return;
    }
    const points = moves
      .filter((m) => m.winrate !== null && m.winrate !== undefined)
      .map((m) => [m.move_no, m.winrate]);
    if (!points.length) return;

    const width = 320;
    const height = 72;
    const maxMove = game.move_count || 1;
    const path = points
      .map(([n, w], i) => `${i === 0 ? 'M' : 'L'}${(n / maxMove) * width},${height - (w / 100) * height}`)
      .join(' ');

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('class', 'winrate-graph');
    svg.innerHTML =
      `<line x1="0" y1="${height / 2}" x2="${width}" y2="${height / 2}" class="mid"/>` +
      `<path d="${path}" class="line"/>` +
      moves.filter((m) => m.marker).map((m) => {
        const w = m.winrate ?? 50;
        return `<circle cx="${(m.move_no / maxMove) * width}" cy="${height - (w / 100) * height}" r="3" class="mk-${m.marker}"/>`;
      }).join('') +
      `<line x1="${(cursor / maxMove) * width}" y1="0" x2="${(cursor / maxMove) * width}" y2="${height}" class="cursor"/>`;

    svg.addEventListener('click', (event) => {
      const rect = svg.getBoundingClientRect();
      const ratio = (event.clientX - rect.left) / rect.width;
      goTo(Math.round(ratio * maxMove));
    });
    graphHost.appendChild(svg);
  }

  function renderCommentary(move) {
    commentary.replaceChildren();
    if (!revealed) return;
    if (!move || !move.severity) {
      if (move && move.marker === 'good') {
        commentary.appendChild(el('p', { class: 'good' }, 'この手はよく咎めています。'));
      }
      return;
    }
    const problem = (game.problems || []).find((p) => p.move_no === move.move_no);
    fill(
      commentary,
      el('div', { class: 'row' }, [
        el('span', { class: `sev sev-${move.marker}` }, move.severity),
        el('span', { class: 'muted' }, ` 勝率 ${fmt(move.delta, 1, 'pt')}`),
        ...(move.tags || []).map((t) => el('span', { class: 'tag' }, t)),
      ]),
      problem ? el('p', { class: 'explanation' }, problem.explanation || '') : null,
    );
  }

  function renderVariation(move) {
    variationPanel.replaceChildren();
    if (!revealed || !move || !move.severity) return;
    const entry = (game.variations || {})[String(move.move_no)];
    if (!entry) return;

    const buttons = el('div', { class: 'row' }, [
      entry.best ? el('button', {
        onclick: () => startVariation('best', entry.best, move.move_no - 1, move.color),
      }, '正解の進行を見る') : null,
      entry.punish ? el('button', {
        onclick: () => startVariation('punish', entry.punish, move.move_no, opposite(move.color)),
      }, '咎められる進行を見る') : null,
      variation ? el('button', { class: 'link', onclick: () => { variation = null; render(); } }, '実戦に戻る') : null,
    ]);
    variationPanel.appendChild(buttons);

    if (entry.best && entry.punish) {
      variationPanel.appendChild(el('table', { class: 'compare' }, [
        el('tr', {}, [el('th', {}, ''), el('th', {}, '正解の進行'), el('th', {}, '実戦（悪手）')]),
        el('tr', {}, [
          el('td', {}, '10手後の勝率'),
          el('td', {}, fmt(entry.best.end_winrate, 0, '%')),
          el('td', {}, fmt(entry.punish.end_winrate, 0, '%')),
        ]),
        el('tr', {}, [
          el('td', {}, '想定地合い'),
          el('td', {}, fmt(entry.best.end_score, 1, '目')),
          el('td', {}, fmt(entry.punish.end_score, 1, '目')),
        ]),
      ]));
    }

    if (variation) {
      const comment = variation.comments[variation.step - 1] || '';
      variationPanel.append(
        el('div', { class: 'row' }, [
          el('button', { onclick: () => stepVariation(-1) }, '◀'),
          el('span', {}, `${variation.step} / ${variation.pv.length} 手`),
          el('button', { onclick: () => stepVariation(1) }, '▶'),
        ]),
        el('p', { class: 'pv-comment' }, comment),
        el('p', { class: 'muted small' },
          '相手が最善で応じた場合の一本道です。実際の相手が同じに応じるとは限りません。'),
      );
    }
  }

  function startVariation(branch, data, startIndex, firstColor) {
    variation = {
      branch,
      pv: data.pv || [],
      comments: data.comments || [],
      startIndex,
      startState: states[startIndex],
      firstColor,
      step: 1,
    };
    render();
  }

  function stepVariation(delta) {
    if (!variation) return;
    variation.step = Math.max(0, Math.min(variation.pv.length, variation.step + delta));
    if (variation.step === 0) variation = null;
    render();
  }

  function goTo(index) {
    variation = null;
    cursor = Math.max(0, Math.min(states.length - 1, index));
    render();
  }

  function nextBad() {
    const next = badMoves.find((m) => m.move_no > cursor);
    if (next) goTo(next.move_no);
    else setStatus('これ以降に悪手はありません', 'muted');
  }

  function toggleAutoplay() {
    if (autoplay) {
      clearInterval(autoplay);
      autoplay = null;
    } else {
      autoplay = setInterval(() => {
        if (cursor >= states.length - 1) { clearInterval(autoplay); autoplay = null; render(); return; }
        goTo(cursor + 1);
      }, autoSpeed);
    }
    render();
  }

  const controls = el('div', { class: 'controls' }, [
    el('button', { onclick: () => goTo(0), title: '初手へ' }, '⟲'),
    el('button', { onclick: () => goTo(cursor - 10) }, '≪10'),
    el('button', { onclick: () => goTo(cursor - 1) }, '‹'),
    el('button', { onclick: () => goTo(cursor + 1) }, '›'),
    el('button', { onclick: () => goTo(cursor + 10) }, '10≫'),
    el('button', { onclick: () => goTo(states.length - 1), title: '終局へ' }, '⟳'),
  ]);

  const tools = el('div', { class: 'controls' }, [
    el('button', { onclick: () => toggleAutoplay() }, '自動再生'),
    el('button', {
      onclick: () => { autoSpeed = autoSpeed === 900 ? 400 : 900; if (autoplay) { toggleAutoplay(); toggleAutoplay(); } setStatus(`再生速度: ${autoSpeed === 900 ? '標準' : '速い'}`); },
    }, '速度'),
    el('button', { onclick: () => { showNumbers = !showNumbers; render(); } }, '手数表示'),
    el('button', { onclick: () => nextBad() }, '次の悪手へ'),
  ]);

  const badList = el('div', { class: 'list compact' });
  function renderBadList() {
    badList.replaceChildren();
    if (!revealed) return;
    if (!badMoves.length) {
      badList.appendChild(el('p', { class: 'muted small' }, '閾値を超える悪手はありませんでした。'));
      return;
    }
    for (const m of badMoves) {
      badList.appendChild(el('button', {
        class: 'list-item',
        onclick: () => goTo(m.move_no),
      }, [
        el('span', { class: `sev sev-${m.marker}` }, m.severity),
        el('span', {}, ` ${m.move_no}手目 ${m.coord}`),
        el('span', { class: 'muted' }, ` ${fmt(m.delta, 1, 'pt')}`),
      ]));
    }
  }

  const header = el('div', { class: 'game-header' }, [
    el('strong', {}, `${game.players.black} vs ${game.players.white}`),
    el('span', { class: 'muted' }, ` ${game.result || ''} ${game.end_type || ''}`),
  ]);

  app.replaceChildren(
    header,
    gate,
    canvas,
    winrateBar,
    info,
    graphHost,
    controls,
    tools,
    commentary,
    variationPanel,
    el('h2', { class: 'section-title' }, '悪手一覧'),
    badList,
    el('div', { class: 'row' }, [
      el('button', { class: 'link', onclick: () => nav('#/home') }, '← 一覧へ戻る'),
    ]),
  );
  render();

  window.addEventListener('hashchange', () => { if (autoplay) clearInterval(autoplay); }, { once: true });
}

// ---------------------------------------------------------------- 問題演習

// 答え合わせのあとに見せる手順を組み立てる。
//
// refutations は「その手を打つとどうなるか」の一覧で、押された手が
// その中にあれば咎められる手順を、無ければ正解手順だけを見せる。
// 候補に無い手は、AI が読む価値も無いと判断した手なので、そう伝える。
function buildSequences(problem, playedGtp) {
  const refs = problem.refutations || [];
  const key = (playedGtp || '').toUpperCase();
  const best = refs.find((r) => r.kind === 'best');
  const played = refs.find((r) => (r.move || '').toUpperCase() === key);
  const out = [];

  if (played && played !== best) {
    out.push({
      key: 'played',
      label: 'あなたの手のあと',
      pv: played.pv,
      comments: played.comments,
      note: played.kind === 'actual'
        ? 'これは実戦で実際に打った手です。'
        : (played.winrate === null || played.winrate === undefined
          ? ''
          : `この手のあと、あなたの勝ちやすさは ${Math.round(played.winrate)}% です。`),
    });
  }
  if (best) {
    out.push({
      key: 'best',
      label: played === best ? 'この手で決まります' : '正解の手順',
      pv: best.pv,
      comments: best.comments,
      note: best.winrate === null || best.winrate === undefined
        ? ''
        : `この手順なら、あなたの勝ちやすさは ${Math.round(best.winrate)}% を保てます。`,
    });
  }
  return { sequences: out, played, best };
}

// 手順の節をまるごと組み立てる。見せられる手順が 1 つも無いときは
// 見出しごと出さない（「用意できていません」とだけ書かれた節が残ると
// 壊れているように見える）。
function sequenceSection(problem, playedGtp, view, size, state) {
  const { sequences, played } = buildSequences(problem, playedGtp);
  if (!sequences.length) return [];
  const player = createSequencePlayer({
    view,
    size,
    startState: state,
    firstColor: problem.player_to_move,
    sequences,
  });
  return [
    el('h3', { class: 'seq-title' }, '盤で手順を確かめる'),
    unlistedNote(problem, playedGtp, played),
    player.element,
  ];
}

// 手順を用意できなかった手のときだけ、その旨を一行で伝える。
// 「候補に無い手」と「ほとんど読まれず手順が作れなかった手」を
// 区別しても学習者には意味が無いので、まとめて同じ言い方にする。
function unlistedNote(problem, playedGtp, played) {
  if (played || !(problem.refutations || []).length) return null;
  return el('p', { class: 'muted small' },
    `${playedGtp} は、AI がほとんど読んでいない手です（有力ではないと見ています）。正解の手順と見比べてみてください。`);
}

async function viewQuiz() {
  app.replaceChildren(el('p', { class: 'loading' }, '問題を読み込み中…'));
  const [due, all] = await Promise.all([
    store.loadDue().catch(() => ({ problems: [] })),
    store.loadProblems().catch(() => ({ problems: [] })),
  ]);
  const byId = new Map((all.problems || []).map((p) => [p.problem_id, p]));
  const queue = (due.problems || []).map((d) => byId.get(d.problem_id)).filter(Boolean);

  if (!queue.length) {
    app.replaceChildren(el('div', { class: 'card' }, [
      el('h2', {}, '今日の出題はありません'),
      el('p', { class: 'muted' }, '解析機で新しい棋譜を処理すると問題が増えます。'),
      el('button', { onclick: () => nav('#/home') }, 'ホームへ'),
    ]));
    return;
  }

  let position = 0;
  renderProblem();

  function renderProblem() {
    const problem = queue[position];
    if (!problem) {
      app.replaceChildren(el('div', { class: 'card' }, [
        el('h2', {}, 'お疲れさまでした'),
        el('p', {}, `${queue.length} 問を終えました。`),
        el('button', { class: 'primary', onclick: () => nav('#/home') }, 'ホームへ'),
      ]));
      return;
    }

    const parsed = parseSgf(problem.board_position || '');
    const states = buildStates(parsed);
    const size = parsed.size || 9;
    const state = states[states.length - 1];
    const startedAt = performance.now();
    let hintLevel = 0;
    let answered = null;

    const canvas = el('canvas', { class: 'board' });
    const hintBox = el('div', { class: 'hints' });
    const resultBox = el('div', { class: 'result' });
    const reasonInput = el('input', {
      type: 'text',
      placeholder: 'なぜその手を選んだか（任意・スキップ可）',
    });

    const view = new BoardView(canvas, {
      size,
      onPlay: (coord) => submit(coord),
    });

    function submit(coord) {
      if (answered) return;
      const seconds = (performance.now() - startedAt) / 1000;
      const gtp = coordToGtp(coord, size);
      const correct = (problem.correct_moves || []).find(
        (m) => (m.coord || '').toUpperCase() === gtp.toUpperCase(),
      );
      const verdict = correct ? (correct.label === '最善' ? '正解' : '許容') : '不正解';
      answered = { gtp, verdict, seconds };

      store.submitAnswer(problem.problem_id, gtp, seconds, hintLevel > 0, reasonInput.value);
      showResult(coord, verdict, gtp);
    }

    function showResult(coord, verdict, gtp) {
      const isActual = (problem.actual_move || '').toUpperCase() === gtp.toUpperCase();
      const ghosts = (problem.correct_moves || []).map((m, i) => ({
        coord: gtpToCoord(m.coord, size),
        color: problem.player_to_move,
        label: m.label === '最善' ? '正' : String(i + 1),
      }));
      view.interactive = false;
      view.setState(state, {
        lastMove: coord,
        ghosts,
        numbers: state.numbers,
      });

      fill(
        resultBox,
        el('div', { class: `verdict verdict-${verdict === '不正解' ? 'wrong' : 'ok'}` }, verdict),
        isActual ? el('p', { class: 'muted' }, 'これが実戦で打った手です。') : null,
        ...sequenceSection(problem, gtp, view, size, state),
        el('h3', { class: 'seq-title' }, '解説'),
        renderExplanation(problem.explanation || ''),
        el('div', { class: 'row' }, (problem.tags || []).map((t) => el('span', { class: 'tag' }, t))),
        el('div', { class: 'row' }, [
          el('button', {
            class: 'primary',
            onclick: () => { position += 1; renderProblem(); },
          }, '理解した'),
          el('button', {
            onclick: () => { queue.push(problem); position += 1; renderProblem(); },
          }, 'もう一度'),
          el('button', {
            class: 'link',
            onclick: () => nav(`#/game/${problem.source_game_id}`),
          }, '実戦の進行を見る'),
        ]),
      );
    }

    function showHint() {
      const hints = problem.hints || [];
      if (hintLevel >= hints.length) return;
      hintBox.appendChild(el('p', { class: 'hint' }, hints[hintLevel]));
      hintLevel += 1;
    }

    app.replaceChildren(
      el('div', { class: 'quiz-header' }, [
        el('span', {}, `${position + 1} / ${queue.length}`),
        el('span', { class: 'muted' }, ` 難易度 ${problem.difficulty || '-'}`),
        el('span', { class: 'muted' }, ` ${problem.player_to_move === 'B' ? '黒番' : '白番'}`),
      ]),
      el('p', { class: 'prompt' }, 'このとき、どう打つべきだったか。'),
      canvas,
      el('p', { class: 'muted small' }, '交点を2回タップで確定します。'),
      reasonInput,
      el('div', { class: 'row' }, [
        el('button', { onclick: () => showHint() }, 'ヒント'),
        el('button', { class: 'link', onclick: () => { position += 1; renderProblem(); } }, 'とばす'),
      ]),
      hintBox,
      resultBox,
    );
    // 盤面は DOM へ挿入したあとに描く
    view.setState(state, { lastMove: state.last || null, numbers: state.numbers });
  }
}

// ---------------------------------------------------------------- 詰碁演習

// アプリ内蔵の詰碁を盤上で解く。正解手は KataGo が事前に検証したものだけを
// 持っているので、ここでは照合するだけで「正解らしさ」を独自に判断しない。
async function viewTsumegoQuiz() {
  app.replaceChildren(el('p', { class: 'loading' }, '読み込み中…'));
  const due = await store.loadDue().catch(() => ({ tsumego: [] }));
  const queue = (due.tsumego || []).filter((t) => t.interactive);

  if (!queue.length) {
    app.replaceChildren(el('div', { class: 'card' }, [
      el('h2', {}, '今日の詰碁はありません'),
      el('p', { class: 'muted' }, '出題した問題をすべて解き終えています。'),
      el('button', { onclick: () => nav('#/home') }, 'ホームへ'),
      el('button', { class: 'link', onclick: () => nav('#/tsumego-log') }, '別アプリで解いた分を記録する'),
    ]));
    return;
  }

  let position = 0;
  renderTsumego();

  function renderTsumego() {
    const problem = queue[position];
    if (!problem) {
      app.replaceChildren(el('div', { class: 'card' }, [
        el('h2', {}, 'お疲れさまでした'),
        el('p', {}, `詰碁 ${queue.length} 問を終えました。`),
        el('button', { class: 'primary', onclick: () => nav('#/home') }, 'ホームへ'),
      ]));
      return;
    }

    const parsed = parseSgf(problem.position_sgf || '');
    const states = buildStates(parsed);
    const size = parsed.size || problem.size || 9;
    const state = states[states.length - 1];
    const startedAt = performance.now();
    let hintLevel = 0;
    let answered = false;

    const canvas = el('canvas', { class: 'board' });
    const hintBox = el('div', { class: 'hints' });
    const resultBox = el('div', { class: 'result' });

    const view = new BoardView(canvas, { size, onPlay: (coord) => submit(coord) });

    function submit(coord) {
      if (answered) return;
      answered = true;
      const seconds = (performance.now() - startedAt) / 1000;
      const gtp = coordToGtp(coord, size);
      const hit = (problem.correct_moves || []).find(
        (m) => (m.coord || '').toUpperCase() === gtp.toUpperCase(),
      );
      const isCorrect = Boolean(hit);

      store.submitTsumegoAnswer(problem.tsumego_id, isCorrect, seconds, hintLevel > 0);
      showResult(coord, isCorrect, hit);
    }

    function showResult(coord, isCorrect, hit) {
      const ghosts = (problem.correct_moves || []).map((m, i) => ({
        coord: gtpToCoord(m.coord, size),
        color: problem.player_to_move,
        label: i === 0 ? '正' : String(i + 1),
      }));
      view.interactive = false;
      view.setState(state, { lastMove: coord, ghosts, numbers: state.numbers });

      const playedGtp = coordToGtp(coord, size);

      fill(
        resultBox,
        el('div', { class: `verdict verdict-${isCorrect ? 'ok' : 'wrong'}` }, isCorrect ? '正解' : '不正解'),
        ...sequenceSection(problem, playedGtp, view, size, state),
        el('h3', { class: 'seq-title' }, '解説'),
        renderExplanation(hit && hit.note ? hit.note : (problem.answer_note || '')),
        problem.theme_tag ? el('div', { class: 'row' }, [el('span', { class: 'tag' }, problem.theme_tag)]) : null,
        el('p', { class: 'muted small' }, isCorrect
          ? '次に出るのは少し先になります。'
          : '間違えた問題は明日もう一度出ます。'),
        el('div', { class: 'row' }, [
          el('button', {
            class: 'primary',
            onclick: () => { position += 1; renderTsumego(); },
          }, '理解した'),
          el('button', {
            onclick: () => { position += 1; renderTsumego(); },
          }, '次へ'),
        ]),
      );
    }

    function showHint() {
      const hints = problem.hints || [];
      if (hintLevel >= hints.length) return;
      hintBox.appendChild(el('p', { class: 'hint' }, hints[hintLevel]));
      hintLevel += 1;
    }

    app.replaceChildren(
      el('div', { class: 'quiz-header' }, [
        el('span', {}, `${position + 1} / ${queue.length}`),
        el('span', { class: 'muted' }, ` 難易度 ${problem.difficulty || '-'}`),
        el('span', { class: 'muted' }, ` ${problem.player_to_move === 'W' ? '白番' : '黒番'}`),
        problem.first_time ? null : el('span', { class: 'badge' }, '復習'),
      ]),
      el('p', { class: 'prompt' }, problem.theme_tag
        ? `${problem.theme_tag}の問題です。最善の一手はどこか。`
        : 'この局面での最善の一手はどこか。'),
      canvas,
      el('p', { class: 'muted small' }, '交点を2回タップで確定します。'),
      el('div', { class: 'row' }, [
        el('button', {
          disabled: (problem.hints || []).length ? null : 'disabled',
          onclick: () => showHint(),
        }, 'ヒント'),
        el('button', { class: 'link', onclick: () => { position += 1; renderTsumego(); } }, 'とばす'),
      ]),
      hintBox,
      resultBox,
    );
    view.setState(state, { numbers: state.numbers });
  }
}

// ---------------------------------------------------------------- 詰碁記録

const TSUMEGO_THEMES = [
  'アタリ見落とし', '切断された', 'シチョウ', 'ゲタ', 'ウッテガエシ',
  'オイオトシ', '両アタリ', '中手', '欠け眼', '攻め合い負け', '眼形不足', 'ヨセ損',
];

async function viewTsumego() {
  const selected = new Set();
  let solved = 0;
  let wrong = 0;

  const counter = (label, get, set) => {
    const value = el('span', { class: 'counter-value' }, String(get()));
    return el('div', { class: 'counter' }, [
      el('span', { class: 'counter-label' }, label),
      el('button', { onclick: () => { set(Math.max(0, get() - 1)); value.textContent = String(get()); } }, '−'),
      value,
      el('button', { onclick: () => { set(get() + 1); value.textContent = String(get()); } }, '＋'),
      el('button', { class: 'link', onclick: () => { set(get() + 5); value.textContent = String(get()); } }, '+5'),
    ]);
  };

  const themeRow = el('div', { class: 'chips' }, TSUMEGO_THEMES.map((theme) => {
    const chip = el('button', { class: 'chip', onclick: () => {
      if (selected.has(theme)) { selected.delete(theme); chip.classList.remove('on'); }
      else { selected.add(theme); chip.classList.add('on'); }
    } }, theme);
    return chip;
  }));

  app.replaceChildren(
    el('div', { class: 'card' }, [
      el('h2', {}, '別アプリで解いた詰碁の記録'),
      el('p', { class: 'muted' }, '解いた問題ではなく、間違えた問題だけを覚えておけば足ります。'),
      counter('解いた数', () => solved, (v) => { solved = v; }),
      counter('間違えた数', () => wrong, (v) => { wrong = v; }),
      el('p', { class: 'label' }, 'テーマ（タップで選択・複数可）'),
      themeRow,
      el('button', {
        class: 'primary',
        onclick: async () => {
          await store.submitTsumego(solved, wrong, [...selected], '');
          setStatus('記録しました（オフライン時は復帰後に送信します）', 'ok');
          nav('#/home');
        },
      }, '記録する'),
      el('button', { class: 'link', onclick: () => nav('#/home') }, 'やめる'),
    ]),
  );
}

// ---------------------------------------------------------------- ダッシュボード

async function viewDashboard() {
  app.replaceChildren(el('p', { class: 'loading' }, '集計中…'));
  let data;
  try {
    data = await store.loadDashboard();
  } catch {
    app.replaceChildren(el('div', { class: 'card' }, 'ダッシュボードのデータがありません。'));
    return;
  }

  const tagRows = Object.entries(data.tag_counts_recent || {}).slice(0, 10);
  const maxTag = Math.max(1, ...tagRows.map(([, v]) => v));

  const cross = el('table', { class: 'compare' }, [
    el('tr', {}, [
      el('th', {}, 'タグ'), el('th', {}, '実戦'), el('th', {}, '詰碁正答率'), el('th', {}, '見立て'),
    ]),
    ...(data.cross || []).slice(0, 12).map((row) => el('tr', {}, [
      el('td', {}, row.tag),
      el('td', {}, String(row.game_count)),
      el('td', {}, row.tsumego_accuracy === null || row.tsumego_accuracy === undefined
        ? '—' : `${row.tsumego_accuracy}%`),
      el('td', { class: 'small' }, row.diagnosis),
    ])),
  ]);

  const problems = data.problems || {};
  app.replaceChildren(
    el('section', { class: 'cards' }, [
      el('div', { class: 'card' }, [
        el('h2', {}, '問題'),
        el('p', {}, `初見正答率 ${fmt(problems.accuracy_first, 1, '%')} / 卒業 ${problems.graduated || 0} 問`),
        el('p', { class: 'muted' }, `未消化 ${problems.due_today || 0} 問 / 全 ${problems.total_problems || 0} 問`),
      ]),
      el('div', { class: 'card' }, [
        el('h2', {}, '学習の継続'),
        el('p', {}, `連続 ${data.streak_days || 0} 日`),
      ]),
    ]),
    el('h2', { class: 'section-title' }, 'タグ別の発生件数（直近20局）'),
    el('div', { class: 'bars' }, tagRows.map(([tag, count]) => el('div', { class: 'bar-row' }, [
      el('span', { class: 'bar-label' }, tag),
      el('span', { class: 'bar', style: `width:${(count / maxTag) * 100}%` }),
      el('span', { class: 'bar-value' }, String(count)),
    ]))),
    el('h2', { class: 'section-title' }, '敗着の発生時期'),
    el('div', { class: 'row' }, Object.entries(data.losing_phase || {}).map(
      ([phase, count]) => el('span', { class: 'pill' }, `${phase} ${count}`),
    )),
    el('h2', { class: 'section-title' }, 'タグ別突合（実戦 × 詰碁）'),
    cross,
  );
}

// ---------------------------------------------------------------- ルータ

const routes = [
  [/^#\/home$/, viewHome],
  [/^#\/game\/(.+)$/, viewGame],
  [/^#\/quiz$/, viewQuiz],
  [/^#\/tsumego$/, viewTsumegoQuiz],
  [/^#\/tsumego-log$/, viewTsumego],
  [/^#\/dashboard$/, viewDashboard],
];

async function route() {
  const hash = window.location.hash || '#/home';
  setStatus('');
  for (const [pattern, handler] of routes) {
    const match = hash.match(pattern);
    if (match) {
      document.querySelectorAll('nav a').forEach((a) => {
        a.classList.toggle('active', hash.startsWith(a.getAttribute('href')));
      });
      try {
        await handler(match[1]);
      } catch (err) {
        app.replaceChildren(el('div', { class: 'card' }, `表示に失敗しました: ${err.message}`));
      }
      window.scrollTo(0, 0);
      return;
    }
  }
  nav('#/home');
}

window.addEventListener('hashchange', route);
window.addEventListener('load', () => {
  route();
  store.flushQueue();
  store.prefetch();
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('./sw.js').catch(() => {});
    // 新しい Service Worker に切り替わったら読み込み直す。
    // これがないと、更新した直後の1回だけ古い画面が表示されてしまう。
    let reloading = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (reloading) return;
      reloading = true;
      window.location.reload();
    });
  }
});
window.addEventListener('offline', () => setStatus('オフライン: キャッシュ済みのデータで動作します', 'warn'));
window.addEventListener('online', () => setStatus('オンラインに復帰しました', 'ok'));

// 最新を取れず前回の内容で代用したときの知らせ。原因は断定せず、
// 「最新ではない」ことと、やり直す手段だけを示す。
// URL に時刻を付けるのは、Service Worker のキャッシュを避けて必ず
// ネットワークへ出すため。ログインが切れている場合、これをしないと
// ログイン画面まで辿り着けない。
window.addEventListener('goreview:stale', () => {
  statusBar.replaceChildren(
    el('span', {}, '最新のデータを取得できませんでした。表示は前回の内容です。'),
    el('button', {
      class: 'link',
      onclick: () => { window.location.href = `${location.pathname}?r=${Date.now()}`; },
    }, '取得し直す'),
  );
  statusBar.className = 'status warn';
});
