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

# 採否の基準。
#
# ここに至るまで2回失敗しているので、経緯を残しておく。
#
# 1回目: ほぼ空の盤に数子だけ置き、勝率差か地合い差で判定しようとした。
#   最善手も次善手も黒の勝率 99.9% で並んで差が出ず、KataGo は「白1子を取る」
#   より「中央に打つ」を選んだ。これは囲碁として正しい判断で、空の盤では
#   1子より中央の勢力が大きい。つまり詰碁として成立していなかった。
#
# 2回目: 隅の白の一団の生死が懸かる形（直三・曲がり三目・中手）に作り替え、
#   地合い差だけで判定した。それでも通らず、診断すると原因が判明した。
#   黒が +15 目リードで勝率 99.87% の局面では、KataGo は急所を 6 visits しか
#   読まない（最善手は 526 visits）。KataGo の評価は勝率が支配的なので、
#   勝負が決した局面では目数を真剣に最大化しない。つまり「地合い差で測る」
#   という前提そのものが、勝敗の決した局面では成り立たなかった。
#
# 3回目: 局面を互角に近づけた。白にも見合う地を与え、隅の死活がそのまま
#   勝敗を決める形にしたところ、狙いどおりになった。地合いが +15.4 目から
#   +2.1 目に縮まり、急所への探索が 6 visits から 1195 visits（全1500中）に
#   跳ね上がって、KataGo が急所を最善手に選んだ。
#
# 4回目（現在）: 3回目で局面は正しくなったが、今度は判定条件の方が
#   間違っていた。「勝率が偏った局面は使わない」「次善手が浅いなら比較
#   できない」という2条件が、どちらも良問を弾いてしまった。
#   良い詰碁ほど「正解を打てば確実に勝つ」ので勝率は偏るし、「正解以外に
#   打つ手がない」ので次善手は読まれない。どちらも詰碁として成立している
#   証拠であって、除外する理由ではなかった。
#
#   正しい物差しは「KataGo が1つの手に探索を集中させたか」。迷いがあれば
#   探索は複数の手に分散するので、集中していること自体が一意性の証拠になる。
MIN_WINRATE_GAP = 15.0
MIN_SCORE_GAP = 4.0

# 最善手にこれだけの割合の探索が集まっていれば、KataGo は迷っていない。
# 実測値: 成立した局面は 0.54〜0.80、成立しなかった局面は 0.35 以下。
MIN_BEST_SHARE = 0.45


def _mirror(stones: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """左上の形を右下へ点対称に写す。手で書き写すと間違えるので計算で作る。"""
    return [(SIZE - 1 - c, SIZE - 1 - r) for c, r in stones]


def _row(r: int) -> list[tuple[int, int]]:
    """1行を石で埋める。盤を上下に分ける壁に使う。"""
    return [(c, r) for c in range(SIZE)]


# 候補局面。(id, テーマ, 難易度の目安, 黒の配石, 白の配石)
# 座標は (列, 行) の 0 始まり。手番は黒に固定している。
# ここに書いてあるのは「出題したい形」であって「正解」ではない。
# 正解は KataGo に解かせて決める。
#
# 盤の上半分を黒、下半分を白が持つ形にして地を互角に近づけ、隅の白が
# 生きるか死ぬかで勝敗が入れ替わるようにしてある。こうしないと KataGo が
# 隅を真剣に読まない（上の経緯を参照）。
_L_AND_D: list[tuple[str, str, int, list[tuple[int, int]], list[tuple[int, int]]]] = [
    # 隅の白を黒が取りにいく形。白は囲まれていて、目の形だけが生死を決める。
    ("life-death-1", "死活（三目の急所）", 2,
     [(0, 2), (1, 2), (2, 2), (3, 2), (4, 0), (4, 1), *_row(4)],
     [(0, 1), (1, 1), (2, 1), (3, 1), (3, 0), *_row(5)]),
    ("life-death-2", "死活（曲がり三目）", 3,
     [(3, 0), (3, 1), (2, 2), (1, 3), (0, 3), *_row(4)],
     [(2, 0), (2, 1), (1, 1), (1, 2), (0, 2), *_row(5)]),
    ("life-death-3", "死活（中手）", 4,
     [(4, 0), (4, 1), (3, 2), (2, 3), (1, 3), (0, 3), *_row(4)],
     [(3, 0), (3, 1), (2, 1), (2, 2), (1, 2), (0, 2), *_row(5)]),
]

CANDIDATES: list[tuple[str, str, int, list[tuple[int, int]], list[tuple[int, int]]]] = [
    *_L_AND_D,
    # 同じ形を右下の隅にも置く。出題が隅の片側に偏らないように。
    *[
        (f"{pid}-mirror", theme, difficulty, _mirror(black), _mirror(white))
        for pid, theme, difficulty, black, white in _L_AND_D
    ],
    # 実測で成立した2問。2子を一度に取れるので地合いに 5.5 目の差が出た。
    ("double-atari-1", "両アタリ", 2,
     [(2, 3), (3, 2), (6, 3), (5, 2)], [(3, 3), (5, 3)]),
    ("double-atari-2", "両アタリ", 2,
     [(2, 5), (3, 6), (6, 5), (5, 6)], [(3, 5), (5, 5)]),
]

# テーマごとの、初心者向けの言い換え。解説に使う。
THEME_HINTS: dict[str, list[str]] = {
    "死活（三目の急所）": [
        "白の石を囲んでいる内側の空き地に注目しましょう。",
        "白が目を2つ作れないようにするには、どこに置けばよいでしょうか。",
    ],
    "死活（曲がり三目）": [
        "白が囲っている空き地は、曲がった形をしています。",
        "その空き地のまん中にあたる点はどこか、考えてみましょう。",
    ],
    "死活（中手）": [
        "白の内側の空き地が広く見えても、形によっては目が2つ作れません。",
        "白に目を2つ作らせない、内側の急所を探してみましょう。",
    ],
    "両アタリ": ["1手で2か所を同時に攻められる場所を探してみましょう。"],
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


def describe_enclosure(
    black: list[tuple[int, int]], white: list[tuple[int, int]]
) -> dict:
    """白が本当に囲まれているか、内側の空き地はいくつかを数える。

    死活の問題として成立するには、白の一団が外に逃げ道を持たず、
    残る呼吸点が内側の空き地だけになっている必要がある。配石を手で
    書くと必ず取りこぼすので、KataGo に渡す前にここで確かめる。
    """
    board = {c: "B" for c in black}
    board.update({c: "W" for c in white})

    def neighbors(c: tuple[int, int]) -> list[tuple[int, int]]:
        return [
            (c[0] + dc, c[1] + dr)
            for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1))
            if 0 <= c[0] + dc < SIZE and 0 <= c[1] + dr < SIZE
        ]

    groups: list[tuple[set, set]] = []
    seen: set = set()
    for stone in white:
        if stone in seen:
            continue
        stack, stones, liberties = [stone], set(), set()
        while stack:
            cur = stack.pop()
            if cur in stones:
                continue
            stones.add(cur)
            for n in neighbors(cur):
                if board.get(n) is None:
                    liberties.add(n)
                elif board.get(n) == "W" and n not in stones:
                    stack.append(n)
        seen |= stones
        groups.append((stones, liberties))

    return {
        "white_groups": len(groups),
        "sizes": sorted(len(s) for s, _ in groups),
        "liberties": sorted(len(l) for _, l in groups),
        "detail": groups,
    }


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
            gap_score = best.score_lead_black - second.score_lead_black
            gap_winrate = best.winrate_black - second.winrate_black
            share = best.visits / max(visits, 1)
            focused = share >= MIN_BEST_SHARE
            wide_gap = gap_winrate >= MIN_WINRATE_GAP or gap_score >= MIN_SCORE_GAP
            decisive = focused and wide_gap
            if not focused:
                reason = "（探索が分散しており、一手に決まる局面ではありません）"
            elif not wide_gap:
                reason = "（最善手と次善手の差が小さすぎます）"
            else:
                reason = ""
            log(
                f"{'OK' if decisive else 'NG'} {prob_id}: 局面 黒{analysis.winrate_black:.1f}%"
                f"／{analysis.score_lead_black:+.1f}目 ｜ 最善 {best.gtp}"
                f"（{best.visits}visits・集中度{share:.0%}）/ 次善 {second.gtp}"
                f"（{second.visits}visits）勝率差 {gap_winrate:.1f}pt"
                f" / 地合差 {gap_score:.2f}目{reason}"
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
                        f"{best.gtp} が正解です。ここに打つかどうかで地合いが"
                        f"約 {abs(gap_score):.0f} 目変わります。"
                        # 差が現在のリードを上回るときだけ「勝敗が変わる」と言える。
                        # 大差の局面で同じことを書くと嘘になる。
                        + (
                            "この一手で勝ち負けがひっくり返ります。"
                            if abs(gap_score) > abs(analysis.score_lead_black)
                            else ""
                        )
                        + "AI もこの一手に読みを集中させていて、ほかに代わりの手はありません。"
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
