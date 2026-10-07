"""9路の序盤（天元・三々・高目）の問題を作る。

1 手目ごとに 3 問。黒の 1 手目に対する白 2 手目（受け方）、AI が最善とする
白の応手のあとの黒 3 手目（続け方）、さらにその後の白 4 手目。正解は
tsumego_seed と同じく KataGo が決める。

序盤は盤の対称で同じ価値の点が複数あるので、最善手を対称に写した点も
正解に含める。最善とほぼ同じ評価の手（ACCEPT_GAP 以内）は「許容」にする。

学習者が過去の実戦で同じ局面に打った手が正解でなければ、その手を打った
あとの進行も手順として持たせる。押したときに「その手だとどうなるか」を
盤で見せるため。

実戦の対局に由来しない問題なので、game_id には OPENING_GAME_ID を入れる。
"""
from __future__ import annotations

from typing import Callable, Optional

from .analysis import _position_key
from .db import Database, dumps
from .explain import _region_name
from .goban import board_at
from .refutations import KIND_BEST, KIND_CANDIDATE, MIN_CANDIDATE_PV, save_refutation
from .sgf import Game, coord_to_gtp, gtp_to_coord, parse_game
from .variations import BRANCH_BEST, BRANCH_PUNISH, pv_comments

OPENING_GAME_ID = "OPENING"
SIZE = 9
KOMI = 7

# (呼び名, ID 用の名前, 黒 1 手目)
OPENINGS = [
    ("天元", "tengen", "E5"),
    ("三々", "sansan", "C3"),
    ("高目", "takamoku", "D5"),
]
STEPS = 3
VISITS = 2000
PUNISH_VISITS = 800
ACCEPT_GAP = 1.5      # 最善との勝率差（ポイント）がこれ以内なら許容
CANDIDATES = 8
PV_MOVES = 10

COLOR_JP = {"B": "黒", "W": "白"}

Logger = Callable[[str], None]


def _transforms():
    out = []
    for swap in (False, True):
        for fx in (False, True):
            for fy in (False, True):
                def f(c, swap=swap, fx=fx, fy=fy):
                    x, y = c
                    if swap:
                        x, y = y, x
                    if fx:
                        x = SIZE - 1 - x
                    if fy:
                        y = SIZE - 1 - y
                    return (x, y)
                out.append(f)
    return out


TRANSFORMS = _transforms()


def _sgf(seq: list[tuple[str, str]]) -> str:
    body = "".join(
        f";{color}[{chr(97 + c[0])}{chr(97 + c[1])}]"
        for color, gtp in seq
        for c in [gtp_to_coord(gtp, SIZE)]
    )
    return f"(;GM[1]FF[4]SZ[{SIZE}]KM[{KOMI}]RU[Chinese]{body})"


def _stones(board) -> list[tuple[str, tuple[int, int]]]:
    return sorted(
        (board.get((x, y)), (x, y))
        for y in range(SIZE) for x in range(SIZE) if board.get((x, y))
    )


def _symmetric_equivalents(game: Game, turn: int, gtp: str) -> list[str]:
    """局面を自分自身に写す対称で、gtp を写した先（gtp 自身を除く）。"""
    stones = _stones(board_at(game, turn))
    coord = gtp_to_coord(gtp, SIZE)
    out = []
    for f in TRANSFORMS:
        if sorted((c, f(p)) for c, p in stones) == stones:
            g = coord_to_gtp(f(coord), SIZE)
            if g != gtp and g not in out:
                out.append(g)
    return out


def _past_moves(db: Database, game: Game, turn: int, color: str) -> dict[str, int]:
    """過去の実戦で同じ局面（対称も含む）に自分が打った手と回数。"""
    target = _stones(board_at(game, turn))
    counts: dict[str, int] = {}
    rows = db.query(
        "SELECT sgf FROM games WHERE board_size = ? AND my_color = ?", (SIZE, color)
    )
    for row in rows:
        past = parse_game(row["sgf"])
        if len(past.moves) <= turn or past.moves[turn].color != color:
            continue
        played = past.moves[turn].coord
        if not played:
            continue
        stones = _stones(board_at(past, turn))
        if len(stones) != len(target):
            continue
        for f in TRANSFORMS:
            if sorted((c, f(p)) for c, p in stones) == target:
                g = coord_to_gtp(f(played), SIZE)
                counts[g] = counts.get(g, 0) + 1
                break
    return counts


def _intro(name: str, seq: list[tuple[str, str]]) -> str:
    if len(seq) == 1:
        return f"黒の{name}（{seq[0][1]}）に対して"
    moves = "・".join(f"{COLOR_JP[c]}{g}" for c, g in seq)
    return f"{moves}のあと"


def build_opening_problems(db: Database, engine, log: Logger = lambda _m: None) -> int:
    created = 0
    for name, slug, first in OPENINGS:
        seq: list[tuple[str, str]] = [("B", first)]
        for step in range(1, STEPS + 1):
            turn = len(seq)
            to_move = "W" if turn % 2 == 1 else "B"
            game = parse_game(_sgf(seq))
            log(f"{name} {step}/{STEPS}: {COLOR_JP[to_move]}{turn + 1}手目を解析中")
            ana = engine.analyze(game, [turn], VISITS)[turn]
            best = ana.best()
            if best is None:
                log(f"  候補手が返らなかったので {name} はここで打ち切ります")
                break
            best_wr = best.winrate_for(to_move)

            correct = [{"coord": best.gtp, "label": "最善", "winrate_delta": 0.0}]
            equivalents = _symmetric_equivalents(game, turn, best.gtp)
            for g in equivalents:
                correct.append({"coord": g, "label": "最善", "winrate_delta": 0.0})
            accepted = []
            for m in ana.moves[1:CANDIDATES]:
                if m.gtp in {c["coord"] for c in correct}:
                    continue
                gap = best_wr - m.winrate_for(to_move)
                if gap <= ACCEPT_GAP and m.visits >= best.visits * 0.1:
                    correct.append({"coord": m.gtp, "label": "許容", "winrate_delta": round(-gap, 2)})
                    accepted.append(m.gtp)
            correct_set = {c["coord"] for c in correct}

            problem_id = f"O-{slug}-{step}"
            db.execute("DELETE FROM refutations WHERE problem_id = ?", (problem_id,))

            def store(move: str, kind: str, pv: list[str], wr, score, visits) -> None:
                branch = BRANCH_BEST if kind == KIND_BEST else BRANCH_PUNISH
                comments = pv_comments(game, turn, pv, to_move, branch)
                save_refutation(db, problem_id, move, kind, pv, comments, wr, score, visits)

            store(best.gtp, KIND_BEST, best.pv[:PV_MOVES], best_wr,
                  best.score_for(to_move), best.visits)
            for m in ana.moves[1:CANDIDATES]:
                if len(m.pv) >= MIN_CANDIDATE_PV:
                    store(m.gtp, KIND_CANDIDATE, m.pv[:PV_MOVES], m.winrate_for(to_move),
                          m.score_for(to_move), m.visits)

            listed = {m.gtp for m in ana.moves[:CANDIDATES]}
            habits = _past_moves(db, game, turn, to_move)
            wrong_habits = []
            for g, n in sorted(habits.items(), key=lambda kv: -kv[1]):
                if g in correct_set:
                    continue
                wrong_habits.append((g, n))
                if g in listed:
                    continue
                after_game = parse_game(_sgf(seq + [(to_move, g)]))
                log(f"  実戦で打った {g}（{n}回）の進行を解析中")
                after = engine.analyze(after_game, [turn + 1], PUNISH_VISITS)[turn + 1]
                reply = after.best()
                if reply is None:
                    continue
                store(g, KIND_CANDIDATE, [g] + reply.pv[:PV_MOVES - 1],
                      after.winrate_for(to_move), after.score_for(to_move), after.visits)

            lines = [f"{_intro(name, seq)}、{COLOR_JP[to_move]}の最善は{best.gtp}です。"]
            if equivalents:
                lines.append(f"盤の対称で{'・'.join(equivalents)}も同じ価値です。")
            if accepted:
                lines.append(f"{'・'.join(accepted)}もほぼ同じ評価です。")
            if wrong_habits:
                past = "・".join(f"{g}（{n}回）" for g, n in wrong_habits)
                lines.append(f"あなたが実戦でこの局面に打った{past}は評価が下がります。")
            explanation = "\n".join(lines)

            db.execute("DELETE FROM problems WHERE id = ?", (problem_id,))
            db.execute(
                "INSERT INTO problems (id, game_id, move_no, position_sgf, player_to_move, "
                "actual_move, actual_delta, correct_moves, tags, hints, explanation, "
                "difficulty, position_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    problem_id, OPENING_GAME_ID, turn + 1, _sgf(seq), to_move,
                    None, None, dumps(correct), dumps(["序盤", name]),
                    dumps([f"{_region_name(best.gtp, SIZE)}に注目してください。"]),
                    explanation, 3, _position_key(game, turn),
                ),
            )
            db.commit()
            created += 1
            log(f"  {problem_id}: 最善 {best.gtp} / 正解扱い {len(correct)} 手 / 実戦の手 {len(wrong_habits)} 種")
            seq.append((to_move, best.gtp))
    return created
