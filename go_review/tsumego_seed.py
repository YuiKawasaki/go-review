"""アプリ内蔵の詰碁を用意する（FR-14 の詰碁出題の元データ）。

方針は本アプリの大原則と同じで、「正解手は AI エンジンだけが決める」。
ここでは人間が「この手が正解」と決め打ちせず、候補となる配石だけを並べ、
KataGo に解かせて

  - 最善手と次善手の差が十分に大きい（＝迷う余地がない局面）

ものだけを採用し、KataGo が出した手をそのまま正解として登録する。
差が小さい局面は「詰碁として成立していない」とみなして捨てる。

外部の詰碁データセットを丸ごと取り込まないのは、ライセンスが不明瞭なものが
多く、また正解の検証ができないため。手元で検証できる範囲だけを持つ。
"""
from __future__ import annotations

from typing import Callable, Optional

from .config import Settings
from .db import Database
from .katago import get_engine
from .learning import record_tsumego_problem
from .sgf import Game, coord_to_gtp

SIZE = 9

# 最善手と次善手がこれだけ離れていれば「一手しかない局面」とみなす。
# 片方でも満たせば採用（勝率で出る差と、地合いで出る差の両方を見る）。
MIN_WINRATE_GAP = 12.0
MIN_SCORE_GAP = 2.5

# 候補局面。(id, テーマ, 難易度の目安, 黒の配石, 白の配石)
# 座標は (列, 行) の 0 始まり。手番は黒に固定している。
# ここに書いてあるのは「出題したい形」であって「正解」ではない。
CANDIDATES: list[tuple[str, str, int, list[tuple[int, int]], list[tuple[int, int]]]] = [
    ("atari-1", "アタリ見落とし", 1,
     [(0, 1), (2, 1), (1, 0)], [(1, 1)]),
    ("atari-2", "アタリ見落とし", 1,
     [(6, 7), (8, 7), (7, 8)], [(7, 7)]),
    ("ladder-1", "シチョウ", 2,
     [(5, 4), (4, 5)], [(4, 4)]),
    ("ladder-2", "シチョウ", 2,
     [(3, 4), (4, 3)], [(4, 4)]),
    ("double-atari-1", "両アタリ", 2,
     [(2, 3), (3, 2), (6, 3), (5, 2)], [(3, 3), (5, 3)]),
    ("double-atari-2", "両アタリ", 2,
     [(2, 5), (3, 6), (6, 5), (5, 6)], [(3, 5), (5, 5)]),
    ("net-1", "ゲタ", 3,
     [(1, 3), (3, 1)], [(2, 2)]),
    ("net-2", "ゲタ", 3,
     [(6, 1), (8, 3)], [(7, 2)]),
    ("safe-extend-1", "弱い石の放置", 2,
     [(2, 1), (0, 1), (1, 3)], [(1, 1), (1, 2)]),
    ("safe-extend-2", "弱い石の放置", 2,
     [(6, 7), (8, 7), (7, 5)], [(7, 7), (7, 6)]),
    ("snapback-1", "ウッテガエシ", 4,
     [(1, 2), (3, 2), (2, 3), (2, 0)], [(2, 1), (1, 1), (3, 1)]),
    ("capture-race-1", "攻め合い負け", 3,
     [(1, 4), (4, 1), (0, 3), (3, 0)], [(2, 2), (2, 1), (1, 2)]),
]

# テーマごとの、初心者向けの言い換え。解説に使う。
THEME_HINTS: dict[str, list[str]] = {
    "アタリ見落とし": ["あと1手で取られてしまう石がないか、数えてみましょう。"],
    "シチョウ": ["逃げる相手を、盤の端まで追いかけられるか読んでみましょう。"],
    "両アタリ": ["1手で2か所を同時に攻められる場所を探してみましょう。"],
    "ゲタ": ["直接アタリにせず、逃げ道を先回りしてふさぐ手を探しましょう。"],
    "弱い石の放置": ["自分の石で、まだ目も味方もない場所がないか見てみましょう。"],
    "ウッテガエシ": ["わざと取らせてから取り返せる形がないか見てみましょう。"],
    "攻め合い負け": ["お互いの石の呼吸点（空いている隣）を数えて比べてみましょう。"],
}


def _game_with(black: list[tuple[int, int]], white: list[tuple[int, int]]) -> Game:
    game = Game(size=SIZE, komi=7.0, rules="Chinese")
    game.setup_black = list(black)
    game.setup_white = list(white)
    return game


def _position_sgf(black: list[tuple[int, int]], white: list[tuple[int, int]]) -> str:
    """配石だけの SGF。PWA 側の parseSgf が AB/AW を読める形にする。"""
    def cell(c: tuple[int, int]) -> str:
        return f"[{chr(97 + c[0])}{chr(97 + c[1])}]"

    parts = [f"(;GM[1]FF[4]SZ[{SIZE}]KM[7.0]"]
    if black:
        parts.append("AB" + "".join(cell(c) for c in black))
    if white:
        parts.append("AW" + "".join(cell(c) for c in white))
    parts.append("PL[B])")
    return "".join(parts)


def verify_candidates(
    settings: Settings,
    log: Callable[[str], None],
    visits: int = 1200,
    only: Optional[list[str]] = None,
) -> list[dict]:
    """候補局面を KataGo に解かせ、明確なものだけを返す。"""
    engine = get_engine(settings, allow_stub=False)
    verified: list[dict] = []
    try:
        for prob_id, theme, difficulty, black, white in CANDIDATES:
            if only and prob_id not in only:
                continue
            game = _game_with(black, white)
            try:
                analysis = engine.analyze(game, [0], max_visits=visits)[0]
            except Exception as exc:
                log(f"NG {prob_id}: 解析できませんでした（{exc}）")
                continue

            best = analysis.best()
            if not best or len(analysis.moves) < 2:
                log(f"NG {prob_id}: 候補手が足りません")
                continue

            second = analysis.moves[1]
            gap_winrate = best.winrate_black - second.winrate_black
            gap_score = best.score_lead_black - second.score_lead_black
            decisive = gap_winrate >= MIN_WINRATE_GAP or gap_score >= MIN_SCORE_GAP
            log(
                f"{'OK' if decisive else 'NG'} {prob_id}: 最善 {best.gtp}"
                f"（{best.winrate_black:.1f}%）/ 次善 {second.gtp}"
                f"（{second.winrate_black:.1f}%）差 {gap_winrate:.1f}pt / 地合 {gap_score:.2f}目"
            )
            if not decisive:
                continue

            verified.append({
                "id": f"T-{prob_id}",
                "theme": theme,
                "difficulty": difficulty,
                "position_sgf": _position_sgf(black, white),
                "player_to_move": "B",
                "correct_moves": [{
                    "coord": best.gtp,
                    "label": "最善",
                    "note": (
                        f"{best.gtp} が最善です。この一手で"
                        f"{'形勢が大きく変わります' if gap_winrate >= MIN_WINRATE_GAP else f'地合いが約 {gap_score:.1f} 目変わります'}。"
                        f"次に良い {second.gtp} と比べても差がはっきりしています。"
                    ),
                }],
                "hints": THEME_HINTS.get(theme, []),
                "gap_winrate": round(gap_winrate, 2),
                "gap_score": round(gap_score, 2),
            })
    finally:
        engine.close()

    log(f"採用: {len(verified)} / {len(CANDIDATES) if not only else len(only)} 件")
    return verified


def import_verified(db: Database, verified: list[dict]) -> int:
    """検証済みの詰碁を DB へ登録する（同じ id は上書き）。"""
    for item in verified:
        record_tsumego_problem(
            db,
            source="内蔵（KataGo検証済み）",
            theme_tag=item["theme"],
            answer_note=item["correct_moves"][0]["note"],
            tsumego_id=item["id"],
            size=SIZE,
            position_sgf=item["position_sgf"],
            player_to_move=item["player_to_move"],
            correct_moves=item["correct_moves"],
            difficulty=item["difficulty"],
            hints=item["hints"],
        )
    return len(verified)
