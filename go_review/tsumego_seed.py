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
from .db import Database, loads
from .katago import get_engine
from .learning import record_tsumego_problem
from .variations import BRANCH_BEST, BRANCH_PUNISH, pv_comments
from .refutations import KIND_BEST, KIND_CANDIDATE, save_refutation
from .sgf import Game, coord_to_gtp, parse_game

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

# 詰碁 1 問につき手順を残す候補手の数と、その長さ。
CANDIDATE_COUNT = 8
PV_MOVES = 10


def _mirror(stones: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """点対称（180度回転）に写す。手で書き写すと間違えるので計算で作る。"""
    return [(SIZE - 1 - c, SIZE - 1 - r) for c, r in stones]


def _flip_h(stones: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """左右反転。"""
    return [(SIZE - 1 - c, r) for c, r in stones]


def _flip_v(stones: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """上下反転。"""
    return [(c, SIZE - 1 - r) for c, r in stones]


# 盤ごと写すぶんには、どの向きに写しても碁として同じ局面になる。
# 壁も含めて全部を同時に写すのが条件（片方だけ写すと構造が壊れる）。
SYMMETRIES = {
    "": lambda s: list(s),
    "-mirror": _mirror,
    "-fliph": _flip_h,
    "-flipv": _flip_v,
}


def _row(r: int) -> list[tuple[int, int]]:
    """1行を石で埋める。盤を上下に分ける壁に使う。"""
    return [(c, r) for c in range(SIZE)]


def _neighbors(c: tuple[int, int]) -> list[tuple[int, int]]:
    return [
        (c[0] + dc, c[1] + dr)
        for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1))
        if 0 <= c[0] + dc < SIZE and 0 <= c[1] + dr < SIZE
    ]


# 死活問題の骨組み。
#
# 盤の上半分を黒、下半分を白が持つ形にして地を互角に近づけ、上側の白が
# 生きるか死ぬかで勝敗が入れ替わるようにする。こうしないと KataGo が
# 隅を真剣に読まない（このファイル冒頭の経緯を参照）。
LD_BLACK_WALL = 4
LD_WHITE_WALL = 5


def _build_ld(
    white_group: list[tuple[int, int]], inside: list[tuple[int, int]]
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """白の一団と、その内側の空き地から、黒の囲いを計算して配石を作る。

    黒の石を手で並べると必ず取りこぼす（囲い切れていない形を KataGo に
    渡してしまう）ので、白の石に接する点のうち内側でないものを、
    そのまま黒にする。こうすれば白の呼吸点は内側の空き地だけになる。
    """
    white_set = set(white_group)
    inside_set = set(inside)
    black = sorted(
        {n for w in white_group for n in _neighbors(w)} - white_set - inside_set
    )
    return black + _row(LD_BLACK_WALL), list(white_group) + _row(LD_WHITE_WALL)


def check_ld_shape(
    white_group: list[tuple[int, int]], inside: list[tuple[int, int]]
) -> list[str]:
    """出題として成立しない形を、KataGo に渡す前にこちらで弾く。

    見るのは 3 点。白が 1 つの一団になっているか、白の呼吸点が内側の
    空き地とちょうど一致しているか、囲っている黒が取られる形になって
    いないか。KataGo は 1 局面に数十秒かかるので、明らかな不良は
    ここで落としておく。
    """
    problems: list[str] = []
    white_set = set(white_group)
    inside_set = set(inside)

    if white_set & inside_set:
        problems.append("白の石と内側の空き地が重なっています")
    if any(r >= LD_BLACK_WALL for _, r in white_group + inside):
        problems.append("形が壁（4行目）にかかっています")

    # 白が 1 つの一団か
    seen: set[tuple[int, int]] = set()
    stack = [white_group[0]]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(n for n in _neighbors(cur) if n in white_set and n not in seen)
    if seen != white_set:
        problems.append("白の石がつながっていません")

    black, white = _build_ld(white_group, inside)
    board = {c: "B" for c in black}
    board.update({c: "W" for c in white})

    # 内側の空き地が外へ漏れていないか。
    # 黒は「白に接する点」から作るので、白に接していない空き点が内側と
    # つながっていると、そこが逃げ道になって死活問題にならない。
    region: set[tuple[int, int]] = set()
    stack = list(inside_set)
    while stack:
        cur = stack.pop()
        if cur in region or board.get(cur) is not None:
            continue
        region.add(cur)
        stack.extend(_neighbors(cur))
    if region != inside_set:
        leak = sorted(region - inside_set)
        problems.append(f"内側の空き地が外へつながっています（{leak[:5]}…）")

    # 囲っている黒が薄すぎないか
    checked: set[tuple[int, int]] = set()
    for stone in black:
        if stone in checked:
            continue
        group, libs, stack = set(), set(), [stone]
        while stack:
            cur = stack.pop()
            if cur in group:
                continue
            group.add(cur)
            for n in _neighbors(cur):
                if board.get(n) is None:
                    libs.add(n)
                elif board.get(n) == "B" and n not in group:
                    stack.append(n)
        checked |= group
        if len(libs) < 2:
            problems.append(f"黒の囲いが取られる形です（{sorted(group)[:3]}…）")
            break
    return problems


# 出題したい眼形。(id, テーマ, 難易度, 白の一団, 内側の空き地)
# 「どこが急所か」はここには書かない。正解は KataGo に解かせて決める。
_LD_SHAPES: list[tuple[str, str, int, list[tuple[int, int]], list[tuple[int, int]]]] = [
    ("straight-three", "死活（三目の急所）", 2,
     [(0, 1), (1, 1), (2, 1), (3, 1), (3, 0)],
     [(0, 0), (1, 0), (2, 0)]),
    ("bent-three", "死活（曲がり三目）", 2,
     [(2, 0), (2, 1), (1, 1), (1, 2), (0, 2)],
     [(0, 0), (1, 0), (0, 1)]),
    ("t-four", "死活（中手）", 3,
     [(0, 1), (0, 2), (1, 2), (2, 2), (2, 1), (3, 1), (3, 0)],
     [(0, 0), (1, 0), (2, 0), (1, 1)]),
    ("bulky-five", "死活（中手）", 3,
     [(3, 0), (3, 1), (2, 1), (2, 2), (1, 2), (0, 2)],
     [(0, 0), (1, 0), (2, 0), (0, 1), (1, 1)]),
    ("straight-five", "死活（五目中手）", 3,
     [(0, 1), (1, 1), (2, 1), (3, 1), (4, 1), (4, 0)],
     [(0, 0), (1, 0), (2, 0), (3, 0)]),
    ("edge-five", "死活（五目中手）", 4,
     [(0, 0), (0, 1), (1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 1), (6, 0)],
     [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)]),
    ("corner-l", "死活（隅の急所）", 3,
     [(0, 2), (1, 2), (2, 2), (2, 1), (3, 1), (3, 0)],
     [(0, 0), (1, 0), (2, 0), (0, 1), (1, 1)]),
    ("edge-six", "死活（六目の急所）", 4,
     [(0, 1), (1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 1), (6, 0)],
     [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (5, 0)]),
]

# 石を取る手筋。盤をほとんど空にしたまま出す形。
# 死活の枠と違って地の釣り合いを作れないので、2 子以上まとめて取れる形
# だけを置いている（1 子では目数の差が出ず、KataGo が真剣に読まない）。
_TACTICS: list[tuple[str, str, int, list[tuple[int, int]], list[tuple[int, int]]]] = [
    ("double-atari-1", "両アタリ", 2,
     [(2, 3), (3, 2), (6, 3), (5, 2)], [(3, 3), (5, 3)]),
    ("double-atari-2", "両アタリ", 2,
     [(2, 5), (3, 6), (6, 5), (5, 6)], [(3, 5), (5, 5)]),
    ("double-atari-3", "両アタリ", 3,
     [(1, 4), (2, 3), (2, 5), (5, 4), (6, 3), (6, 5)],
     [(2, 4), (3, 4), (5, 3), (5, 5)]),
    ("double-atari-4", "両アタリ", 3,
     [(4, 1), (3, 2), (5, 2), (4, 6), (3, 5), (5, 5)],
     [(4, 2), (4, 3), (3, 6), (5, 6)]),
]


def _candidates() -> list[tuple[str, str, int, list, list]]:
    """出題候補を組み立てる。同じ配置になったものは 1 つにまとめる。"""
    out: list[tuple[str, str, int, list, list]] = []
    seen: set[tuple] = set()

    def add(pid: str, theme: str, difficulty: int, black: list, white: list) -> None:
        key = (tuple(sorted(black)), tuple(sorted(white)))
        if key in seen:
            return
        seen.add(key)
        out.append((pid, theme, difficulty, black, white))

    for pid, theme, difficulty, group, inside in _LD_SHAPES:
        faults = check_ld_shape(group, inside)
        if faults:
            # 形が壊れている候補を KataGo に渡しても時間の無駄なので落とす。
            # ここに出るのは出題データの書き間違いなので、黙って消さない。
            raise ValueError(f"{pid}: {' / '.join(faults)}")
        black, white = _build_ld(group, inside)
        for suffix, fn in SYMMETRIES.items():
            add(f"{pid}{suffix}", theme, difficulty, fn(black), fn(white))

    for pid, theme, difficulty, black, white in _TACTICS:
        for suffix, fn in SYMMETRIES.items():
            add(f"{pid}{suffix}", theme, difficulty, fn(black), fn(white))
    return out


CANDIDATES: list[tuple[str, str, int, list[tuple[int, int]], list[tuple[int, int]]]] = (
    _candidates()
)


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
    "死活（五目中手）": [
        "白の内側の空き地は5つあります。このままでは目が2つできます。",
        "白が2つに割って目を作れないよう、空き地の中心にあたる点を探しましょう。",
    ],
    "死活（六目の急所）": [
        "白の内側は広く見えますが、一列に並んだ空き地は意外に弱い形です。",
        "端から2つ目の点に注目してみましょう。",
    ],
    "死活（隅の急所）": [
        "隅は辺よりも目が作りにくい場所です。",
        "白が目を2つに分けられないよう、内側の折れ曲がった点を探しましょう。",
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
                # 学習者が押しそうな手ごとの読み筋。KataGo が 1 回の解析で
                # すでに返しているものなので、追加の解析時間はかからない。
                "refutations": [
                    {
                        "move": m.gtp,
                        "kind": KIND_BEST if i == 0 else KIND_CANDIDATE,
                        "pv": m.pv[:PV_MOVES],
                        "winrate": round(m.winrate_black, 2),
                        "score": round(m.score_lead_black, 2),
                        "visits": m.visits,
                    }
                    for i, m in enumerate(analysis.moves[:CANDIDATE_COUNT])
                ],
            })
    finally:
        engine.close()

    log(f"採用: {len(verified)} / {len(CANDIDATES) if not only else len(only)} 件")
    return verified


def fill_missing_refutations(
    settings: Settings,
    db: Database,
    log: Callable[[str], None],
    visits: int = 1200,
) -> int:
    """手順を持っていない詰碁に、あとから手順を付ける。

    候補の作り方を変えると、以前の出題形が今の候補一覧から外れることがある。
    その問題は正解と解説は持っているのに盤で手順を見せられない状態になるので、
    ここで局面だけ解かせて手順を作る。

    正解手は既存の記録をそのまま使う。ここで KataGo が別の手を推しても
    上書きはしない（出題済みの答えを黙って変えない）。食い違ったときは
    問題として成立しなくなっている可能性があるので、記録だけ残す。
    """
    rows = [
        r for r in db.query(
            "SELECT id, position_sgf, player_to_move, correct_moves FROM tsumego "
            "WHERE position_sgf IS NOT NULL AND position_sgf != '' ORDER BY id"
        )
        if not db.scalar(
            "SELECT COUNT(*) FROM refutations WHERE problem_id = ?", (r["id"],)
        )
    ]
    if not rows:
        log("手順が欠けている詰碁はありません。")
        return 0

    log(f"手順を作る詰碁: {len(rows)} 問（1 局面 {visits} visits）")
    engine = get_engine(settings, allow_stub=False)
    done = 0
    try:
        for row in rows:
            game = parse_game(row["position_sgf"])
            try:
                analysis = engine.analyze(game, [0], max_visits=visits)[0]
            except Exception as exc:
                log(f"NG {row['id']}: 解析できませんでした（{exc}）")
                continue

            correct = loads(row["correct_moves"], []) or []
            answer = (correct[0].get("coord") if correct else "") or ""
            my_color = row["player_to_move"] or "B"
            best = analysis.best()
            if best and answer and best.gtp.upper() != answer.upper():
                log(
                    f"注意 {row['id']}: 記録の正解 {answer} と KataGo の最善 "
                    f"{best.gtp} が食い違います。記録側を正解のまま残します。"
                )

            db.execute("DELETE FROM refutations WHERE problem_id = ?", (row["id"],))
            saved = 0
            for move in analysis.moves[:CANDIDATE_COUNT]:
                pv = move.pv[:PV_MOVES]
                if not pv:
                    continue
                is_answer = move.gtp.upper() == answer.upper()
                kind = KIND_BEST if is_answer else KIND_CANDIDATE
                branch = BRANCH_BEST if is_answer else BRANCH_PUNISH
                save_refutation(
                    db, row["id"], move.gtp, kind, pv,
                    pv_comments(game, 0, pv, my_color, branch),
                    round(move.winrate_black, 2),
                    round(move.score_lead_black, 2),
                    move.visits,
                )
                saved += 1
            db.commit()
            done += 1
            log(f"OK {row['id']}: 手順 {saved} 通り（{done}/{len(rows)}）")
    finally:
        engine.close()
    return done


def import_verified(db: Database, verified: list[dict]) -> int:
    """検証済みの詰碁を DB へ登録する。

    照合は id ではなく配石で行う。候補の作り方を変えると同じ形に別の id が
    付くので、id で見ると同じ問題が二重に出題されてしまう。さらに
    record_tsumego_problem は streak を 0 で入れ直すため、id が変わると
    それまでの復習の積み上げも消える。すでに同じ配石の問題があれば、
    その id と復習の進み具合をそのまま引き継ぐ。
    """
    for item in verified:
        existing = db.query_one(
            "SELECT id, streak, next_due_at, graduated FROM tsumego WHERE position_sgf = ?",
            (item["position_sgf"],),
        )
        if existing:
            item = {**item, "id": existing["id"]}
        _import_refutations(db, item)
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
        if existing:
            db.execute(
                "UPDATE tsumego SET streak = ?, next_due_at = ?, graduated = ? "
                "WHERE id = ?",
                (existing["streak"], existing["next_due_at"], existing["graduated"],
                 item["id"]),
            )
    db.commit()
    return len(verified)


def _import_refutations(db: Database, item: dict) -> None:
    """詰碁の「この手を押したらこう進む」を保存する。

    盤面が狭く候補も絞れるので、上位数手あれば学習者が押しそうな点は
    ほぼ覆える。読み筋への一言は盤面から機械的に作る（LLM は使わない）。
    """
    game = parse_game(item["position_sgf"])
    my_color = item["player_to_move"]
    # 作り直しなので前回ぶんを消す。採用条件から外れた手が残らないように。
    db.execute("DELETE FROM refutations WHERE problem_id = ?", (item["id"],))
    for entry in item.get("refutations", []):
        pv = entry["pv"]
        if not pv:
            continue
        branch = BRANCH_BEST if entry["kind"] == KIND_BEST else BRANCH_PUNISH
        save_refutation(
            db,
            item["id"],
            entry["move"],
            entry["kind"],
            pv,
            pv_comments(game, 0, pv, my_color, branch),
            entry["winrate"],
            entry["score"],
            entry["visits"],
        )
    db.commit()
