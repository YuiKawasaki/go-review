"""攻め合いの問題を作る。

死活（tsumego_seed.py）と違い、攻め合いには「盤を押して急所を当てる」形では
出せない概念がある。

  - セキ: 正解が「どちらも打てない」なので、KataGo の探索は一点に集中しない。
    tsumego_seed の採否基準（集中度 0.45 以上）は定義上これを弾く。
  - 手数の計算: 四目中手は5手、隅の団子は3手……といった知識は、盤上の
    一点では答えられない。

そこでこのモジュールは、盤を見て選択肢や数値で答える問題を作る。
正解は人が決めず、KataGo の ownership（各点をどちらが持つかの評価）で
判定する。「1手に決まる局面だけ」という制約が外れるので、セキが扱える。

配石はテキストの盤面図で書く。攻め合いは死活より石が多く、座標の羅列だと
書いた本人も読めない（このプロジェクトでは実際に、座標で書いた囲いが
閉じていない不良を何度も作っている）。図なら目で見て確認できる。

  記号:
    b / w … 攻め合っている黒・白の石（この2つの生死を問う）
    B / W … まわりを固めている、生きている黒・白の石
    .     … 空点

読み取った結果は check_shape() で数え直し、意図した外ダメ・内ダメに
なっているかを KataGo に渡す前に確かめる。

配石を作るときの注意（実測で確かめた失敗）:

  入れ子（外側の黒 → 白の輪 → 中の黒）にしてはいけない。外側の輪の
  「内側」が、その石にとっての眼のスペースになってしまうため、資料 §3 が
  前提とする「両者に目もない攻め合い」にならない。

  実際に 2 つの図で試し、どちらも KataGo は「黒が白を取る」と判定した。
  こちらの予測（資料の式）はセキだったが、正しかったのは KataGo のほう。

    ・内側が 2×2 の四目 → 黒石を取れても白は眼が 2 つ作れず死に
    ・内側が一列 3 目（直三）→ 同じく死に

  したがって攻め合いの図は、両者が眼を作れない形（幅 1 の通路など）で
  隣り合わせること。外ダメ・内ダメの数だけ合わせても、眼形が残っていれば
  資料の式は当てはまらない。

  なお「盤全体の地合いを互角に近づける」ことも必要（tsumego_seed.py の
  経緯と同じ）。地合いが一方的だと KataGo は局所を真剣に読まず、
  勝率がどの手でも同じになって候補手の比較ができなくなる。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 盤の大きさは図の行数から決める。攻め合いは死活より場所を食う
# （両者が眼を作れない形にするには、幅 1 の線を 2 本並べたうえで、
# それぞれを相手の生きた壁で挟み、さらに盤全体の地合いも釣り合わせる
# 必要がある）。9 路では収まらないので、図の大きさに合わせる。
DEFAULT_SIZE = 9
MAX_SIZE = 19

# 攻め合いの石の呼吸点から空点をたどって、この数を超える範囲に
# つながっていたら「逃げられる」とみなす。攻め合いのダメは普通
# 数個に収まるので、それを大きく超えるなら囲えていない。
ESCAPE_LIMIT = 8

RACING_BLACK = "b"
RACING_WHITE = "w"
WALL_BLACK = "B"
WALL_WHITE = "W"
EMPTY = "."

BLACK = "B"
WHITE = "W"


@dataclass
class Shape:
    """盤面図から読み取った攻め合いの形。"""
    name: str
    diagram: list[str]
    size: int = DEFAULT_SIZE
    black: list[tuple[int, int]] = field(default_factory=list)
    white: list[tuple[int, int]] = field(default_factory=list)
    racing_black: set[tuple[int, int]] = field(default_factory=set)
    racing_white: set[tuple[int, int]] = field(default_factory=set)

    # check_shape() が数え直した結果
    black_outside: int = 0
    white_outside: int = 0
    shared: int = 0
    faults: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.faults


def _neighbors(c: tuple[int, int], size: int) -> list[tuple[int, int]]:
    return [
        (c[0] + dc, c[1] + dr)
        for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1))
        if 0 <= c[0] + dc < size and 0 <= c[1] + dr < size
    ]


def parse_diagram(name: str, diagram: list[str]) -> Shape:
    """盤面図を配石へ読み替える。

    図は 9 行 × 9 文字。空白は読みやすさのために入れてよい（無視する）。
    """
    rows = [line.replace(" ", "") for line in diagram if line.strip()]
    size = len(rows)
    shape = Shape(name=name, diagram=list(rows), size=size)
    if not (2 <= size <= MAX_SIZE):
        shape.faults.append(f"図が {size} 行です（2〜{MAX_SIZE} 行にしてください）")
        return shape

    for r, line in enumerate(rows):
        if len(line) != size:
            shape.faults.append(
                f"{r + 1} 行目が {len(line)} 文字です（{size} 行の図なので {size} 文字必要）")
            return shape
        for c, ch in enumerate(line):
            point = (c, r)
            if ch == RACING_BLACK:
                shape.black.append(point)
                shape.racing_black.add(point)
            elif ch == RACING_WHITE:
                shape.white.append(point)
                shape.racing_white.add(point)
            elif ch == WALL_BLACK:
                shape.black.append(point)
            elif ch == WALL_WHITE:
                shape.white.append(point)
            elif ch != EMPTY:
                shape.faults.append(f"知らない記号です: {ch!r}（{r + 1}行{c + 1}列）")
    return shape


def _group(board: dict, start: tuple[int, int], size: int) -> tuple[set, set]:
    """start の石とつながる一団と、その呼吸点を返す。"""
    color = board.get(start)
    stones: set[tuple[int, int]] = set()
    libs: set[tuple[int, int]] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur in stones:
            continue
        stones.add(cur)
        for n in _neighbors(cur, size):
            v = board.get(n)
            if v is None:
                libs.add(n)
            elif v == color and n not in stones:
                stack.append(n)
    return stones, libs


def check_shape(shape: Shape) -> Shape:
    """外ダメ・内ダメを数え直し、攻め合いとして成立しない形を弾く。

    KataGo に渡す前にここで落とす。1 局面の検証に数分かかるので、
    図の書き間違いをエンジンに見つけてもらうのは高くつく。
    """
    if shape.faults:
        return shape

    board: dict[tuple[int, int], str] = {}
    for p in shape.black:
        board[p] = BLACK
    for p in shape.white:
        board[p] = WHITE

    if not shape.racing_black or not shape.racing_white:
        shape.faults.append("攻め合う石（b と w）が両方必要です")
        return shape

    b_stones, b_libs = _group(board, next(iter(shape.racing_black)), shape.size)
    w_stones, w_libs = _group(board, next(iter(shape.racing_white)), shape.size)

    if b_stones != shape.racing_black:
        shape.faults.append("b の石が 1 つの連になっていません（または壁とつながっています）")
    if w_stones != shape.racing_white:
        shape.faults.append("w の石が 1 つの連になっていません（または壁とつながっています）")
    if shape.faults:
        return shape

    shared = b_libs & w_libs
    shape.shared = len(shared)
    shape.black_outside = len(b_libs - shared)
    shape.white_outside = len(w_libs - shared)

    if not (b_stones and w_stones):
        shape.faults.append("攻め合う石がありません")
    # 隣り合っていない＝攻め合いではない
    adjacent = any(n in w_stones for s in b_stones for n in _neighbors(s, shape.size))
    if not adjacent and not shared:
        shape.faults.append("b と w が接してもダメも共有しておらず、攻め合いになっていません")

    # まわりの壁が取られる形だと、攻め合いの前に壁の生死が問題になる
    checked: set[tuple[int, int]] = set()
    for p, color in board.items():
        if p in checked or p in b_stones or p in w_stones:
            continue
        stones, libs = _group(board, p, shape.size)
        checked |= stones
        if len(libs) < 2:
            shape.faults.append(
                f"まわりの{'黒' if color == BLACK else '白'}が取られる形です"
                f"（呼吸点 {len(libs)}・{sorted(stones)[:3]}…）"
            )
            break

    # 攻め合いの石が逃げ出せないか。
    #
    # 呼吸点の数だけ見ても足りない。呼吸点が 1 つでも、その先が盤の
    # 広い空き地へつながっていれば、その石は走って逃げられるので
    # 攻め合いにならない（実際にこれで 2 つの図を作り損なった）。
    # 呼吸点から空点をたどって、閉じた範囲に収まっているかを見る。
    for label, libs in (("黒", b_libs), ("白", w_libs)):
        region: set[tuple[int, int]] = set()
        stack = list(libs)
        while stack:
            cur = stack.pop()
            if cur in region or board.get(cur) is not None:
                continue
            region.add(cur)
            stack.extend(_neighbors(cur, shape.size))
        if len(region) > ESCAPE_LIMIT:
            shape.faults.append(
                f"{label}の呼吸点が広い空き地（{len(region)}点）につながっています。"
                "囲えておらず、逃げ出せるので攻め合いになりません"
            )
            break
    return shape


def predict(shape: Shape, to_play: str = BLACK) -> dict:
    """資料の手順どおりに手数を数え、勝敗を予測する。

    眼なしどうしの攻め合い（内ダメ 2 口以上）の規則:

      外ダメの少ない方の手数 = 外ダメ + (内ダメ - 1)
      外ダメの多い方の手数   = 外ダメ

      少ない方 > 多い方 → セキ
      少ない方 < 多い方 → 少ない方が取られる
      少ない方 = 多い方 → 少ない方が先手ならセキ、後手なら取られる

    内ダメが 0〜1 口なら中央の 1 手は無視してよく、外ダメの多い方が勝つ。
    同数なら先手が勝つ。

    重要なのは「少ない方が多い方を上回っても、勝ちではなくセキ止まり」
    という点。内ダメが 2 口以上あると、詰めにいった側が先に取られるため、
    外ダメの少ない側が得られる最良の結果はセキになる。

    ここで出すのは予測であって正解ではない。正解は KataGo の ownership で
    判定し、予測と食い違った問題は採用しない（人が答えを決めない、という
    本アプリの原則を保つため）。
    """
    b_out, w_out, inside = shape.black_outside, shape.white_outside, shape.shared

    if inside <= 1:
        if b_out > w_out:
            return {"outcome": "black", "black_count": b_out, "white_count": w_out,
                    "reason": "内ダメが少なく、外ダメの多い黒が勝つ"}
        if w_out > b_out:
            return {"outcome": "white", "black_count": b_out, "white_count": w_out,
                    "reason": "内ダメが少なく、外ダメの多い白が勝つ"}
        winner = "black" if to_play == BLACK else "white"
        return {"outcome": winner, "black_count": b_out, "white_count": w_out,
                "reason": "外ダメが同数なので先手が勝つ"}

    # 内ダメ 2 口以上。外ダメが同数のときは、どちらも「少ない方」として扱う。
    if w_out < b_out:
        fewer, more = "white", "black"
        fewer_count, more_count = w_out + (inside - 1), b_out
    else:
        fewer, more = "black", "white"
        fewer_count, more_count = b_out + (inside - 1), w_out

    counts = {fewer: fewer_count, more: more_count}
    base = {
        "black_count": counts["black"],
        "white_count": counts["white"],
    }

    if fewer_count > more_count:
        return {**base, "outcome": "seki",
                "reason": f"外ダメの少ない{'黒' if fewer == 'black' else '白'}の手数が上回るのでセキ"}
    if fewer_count < more_count:
        return {**base, "outcome": more,
                "reason": f"外ダメの少ない{'黒' if fewer == 'black' else '白'}が手数で足りず取られる"}

    fewer_moves_first = (to_play == BLACK) == (fewer == "black")
    if fewer_moves_first:
        return {**base, "outcome": "seki",
                "reason": "手数が同じで、外ダメの少ない側が先手なのでセキ"}
    return {**base, "outcome": more,
            "reason": "手数が同じで、外ダメの少ない側が後手なので取られる"}


def to_sgf(shape: Shape) -> str:
    """配石だけの SGF。PWA 側の parseSgf が AB/AW を読める形にする。"""
    def cell(c: tuple[int, int]) -> str:
        return f"[{chr(97 + c[0])}{chr(97 + c[1])}]"

    parts = [f"(;GM[1]FF[4]SZ[{shape.size}]KM[7.0]"]
    if shape.black:
        parts.append("AB" + "".join(cell(c) for c in sorted(shape.black)))
    if shape.white:
        parts.append("AW" + "".join(cell(c) for c in sorted(shape.white)))
    parts.append("PL[B])")
    return "".join(parts)


def render(shape: Shape) -> str:
    """読み取った結果を図に描き直す。書いた図と一致するか目で確かめる用。"""
    board = {}
    for p in shape.black:
        board[p] = "●" if p in shape.racing_black else "◍"
    for p in shape.white:
        board[p] = "○" if p in shape.racing_white else "◌"
    cols = "ABCDEFGHJKLMNOPQRST"[: shape.size]
    lines = ["   " + " ".join(cols)]
    for r in range(shape.size):
        cells = " ".join(board.get((c, r), "・") for c in range(shape.size))
        lines.append(f"{shape.size - r:2d} {cells}")
    return "\n".join(lines)


# --------------------------------------------------------------- KataGo 判定

# ownership は -1（白のもの）〜 +1（黒のもの）。攻め合いの決着は、石の
# 所有権がどちらに振れるかで読める。セキは「どちらのものでもない」ので
# 0 付近にとどまる。tagging.py の生死判定と同じしきい値を使う。
OWNED = 0.30
SEKI_BAND = 0.30

OUTCOME_LABELS = {
    "black": "黒が勝つ（白が取られる）",
    "white": "白が勝つ（黒が取られる）",
    "seki": "セキ（どちらも取れない）",
}


def _avg_ownership(
    ownership: list[float], stones: set[tuple[int, int]], size: int
) -> float:
    if not stones:
        return 0.0
    total = sum(
        ownership[s[1] * size + s[0]]
        for s in stones
        if 0 <= s[1] * size + s[0] < len(ownership)
    )
    return total / len(stones)


def judge_with_engine(
    engine,
    shape: Shape,
    to_play: str = BLACK,
    visits: int = 1200,
) -> Optional[dict]:
    """KataGo の ownership で攻め合いの決着を判定する。

    攻め合っている黒石・白石それぞれの所有率を見る。

      黒石が黒のもの・白石が白のもの  → セキ（どちらも取れていない）
      黒石まで白のものになっている    → 黒が取られる
      白石まで黒のものになっている    → 白が取られる

    tsumego_seed の「探索が一点に集中したか」という基準は使わない。
    セキは正解が「どちらも打てない」なので、その基準では必ず落ちる。
    ここで見たいのは着手ではなく結果なので、ownership のほうが素直。

    判定できない（どちらの石も中間の値）ときは None を返し、問題にしない。
    """
    from .sgf import Game

    game = Game(size=shape.size, komi=7.0, rules="Chinese")
    game.setup_black = list(shape.black)
    game.setup_white = list(shape.white)
    # PL は SGF 側で持てないので、手番は解析の呼び出しで表現する。
    analysis = engine.analyze(game, [0], max_visits=visits)[0]
    if not analysis.ownership:
        return None

    b_own = _avg_ownership(analysis.ownership, shape.racing_black, shape.size)
    w_own = _avg_ownership(analysis.ownership, shape.racing_white, shape.size)

    black_lives = b_own > OWNED
    white_lives = w_own < -OWNED
    black_dead = b_own < -OWNED
    white_dead = w_own > OWNED

    if black_dead and not white_dead:
        outcome = "white"
    elif white_dead and not black_dead:
        outcome = "black"
    elif abs(b_own) < SEKI_BAND and abs(w_own) < SEKI_BAND:
        outcome = "seki"
    elif black_lives and white_lives:
        # 双方が自分のものとして評価されている＝取り合いが起きていない。
        # 攻め合いとして成立していないので採用しない。
        return None
    else:
        return None

    return {
        "outcome": outcome,
        "black_ownership": round(b_own, 3),
        "white_ownership": round(w_own, 3),
        "winrate_black": round(analysis.winrate_black, 1),
        "score_lead_black": round(analysis.score_lead_black, 2),
    }
