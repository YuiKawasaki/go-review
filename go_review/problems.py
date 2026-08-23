"""問題生成（FR-06）。

悪手が発生した「直前の局面」＋ 正解手 ＋ 解説 を 1 問にまとめる。
唯一解は強制せず、最善手との勝率差が閾値以内の手は正解として扱う。
"""
from __future__ import annotations

from typing import Optional

from .badmoves import problem_candidates
from .config import Settings
from .db import Database, dumps, loads
from .goban import board_at, weakest_group
from .explain import ClaudeClient, MoveContext, generate_explanation, suggest_tags
from .katago import TurnAnalysis
from .sgf import Game, coord_to_gtp, position_sgf
from .variations import opponent_missed_punishment

HINT_LEVELS = 3


def generate_problems(
    db: Database,
    game: Game,
    game_id: str,
    my_color: str,
    bad: list[dict],
    analyses: dict[int, TurnAnalysis],
    settings: Settings,
    client: Optional[ClaudeClient] = None,
) -> list[str]:
    """1 局から最大 max_problems_per_game 問を作り、問題 ID のリストを返す。"""
    created: list[str] = []
    for item in problem_candidates(bad, settings):
        problem_id = _create_problem(
            db, game, game_id, my_color, item, analyses, settings, client
        )
        if problem_id:
            created.append(problem_id)
    return created


def _create_problem(
    db: Database,
    game: Game,
    game_id: str,
    my_color: str,
    item: dict,
    analyses: dict[int, TurnAnalysis],
    settings: Settings,
    client: Optional[ClaudeClient],
) -> Optional[str]:
    move_no = item["move_no"]
    before = analyses.get(move_no - 1)
    if not before or not before.best():
        return None

    board = board_at(game, move_no - 1)
    position_key = f"{board.position_key()}:{my_color}"

    # 盤面が同一の問題はまとめる（重複排除）
    existing = db.query_one(
        "SELECT id FROM problems WHERE position_key = ?", (position_key,)
    )
    if existing:
        return None

    correct_moves = _correct_moves(before, my_color, settings)
    if not correct_moves:
        return None

    tags = loads(
        db.scalar(
            "SELECT tags FROM bad_moves WHERE game_id = ? AND move_no = ?",
            (game_id, move_no),
        ),
        [],
    ) or []

    best_var = db.query_one(
        "SELECT pv_moves FROM variations WHERE game_id = ? AND move_no = ? AND branch_type = 'best'",
        (game_id, move_no),
    )
    punish_var = db.query_one(
        "SELECT pv_moves FROM variations WHERE game_id = ? AND move_no = ? AND branch_type = 'punish'",
        (game_id, move_no),
    )

    context = MoveContext(
        move_no=move_no,
        my_color=my_color,
        actual_move=item.get("coord") or "",
        actual_winrate_drop=abs(item["delta"]),
        winrate_before=item["winrate_before"],
        winrate_after=item["winrate_after"],
        best_move=correct_moves[0]["coord"],
        best_winrate=before.best().winrate_for(my_color),
        score_before=before.score_for(my_color),
        score_after=analyses[move_no].score_for(my_color) if move_no in analyses else None,
        tags=list(tags),
        best_pv=loads(best_var["pv_moves"], []) if best_var else [],
        punish_pv=loads(punish_var["pv_moves"], []) if punish_var else [],
        opponent_missed=opponent_missed_punishment(game, move_no, analyses, my_color),
        total_moves=game.move_count,
    )

    # Claude はタグの補完と解説文の作成のみ。正解手には関与させない。
    extra_tags = suggest_tags(client, context, list(tags))
    if extra_tags:
        tags = list(tags) + extra_tags
        context.tags = list(tags)
        db.execute(
            "UPDATE bad_moves SET tags = ? WHERE game_id = ? AND move_no = ?",
            (dumps(tags), game_id, move_no),
        )

    explanation = generate_explanation(client, context)
    hints = build_hints(game, move_no, my_color, tags, correct_moves)
    difficulty = compute_difficulty(item["delta"], before, my_color, settings)

    problem_id = db.next_problem_id()
    db.execute(
        "INSERT INTO problems (id, game_id, move_no, position_sgf, player_to_move, "
        "actual_move, actual_delta, correct_moves, tags, hints, explanation, difficulty, "
        "position_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            problem_id,
            game_id,
            move_no,
            position_sgf(game, move_no - 1),
            my_color,
            item.get("coord"),
            round(item["delta"], 2),
            dumps(correct_moves),
            dumps(tags),
            dumps(hints),
            explanation,
            difficulty,
            position_key,
        ),
    )
    db.execute(
        "INSERT OR IGNORE INTO problem_state (problem_id, streak, next_due_at, graduated) "
        "VALUES (?, 0, NULL, 0)",
        (problem_id,),
    )
    db.commit()
    return problem_id


def _correct_moves(
    before: TurnAnalysis,
    my_color: str,
    settings: Settings,
) -> list[dict]:
    """最善手と、勝率差が許容範囲内の手を正解として並べる。"""
    best = before.best()
    if not best:
        return []
    best_wr = best.winrate_for(my_color)
    out: list[dict] = []
    for info in before.moves:
        delta = info.winrate_for(my_color) - best_wr
        if delta < -settings.acceptable_delta:
            continue
        out.append(
            {
                "coord": info.gtp,
                "winrate_delta": round(delta, 1),
                "label": "最善" if info.order == 0 else "許容",
            }
        )
        if len(out) >= 5:
            break
    return out


def build_hints(
    game: Game,
    move_no: int,
    my_color: str,
    tags: list[str],
    correct_moves: list[dict],
) -> list[str]:
    """3 段階のヒント。正解手そのものは明示しない（FR-06）。"""
    board = board_at(game, move_no - 1)
    hints: list[str] = []

    # 段階1: 着眼点
    weak = weakest_group(board, my_color)
    if weak and len(weak[1]) <= 2:
        hints.append("自分の一番弱い石はどれか、呼吸点を数えてください。")
    elif tags:
        hints.append(f"この局面のテーマは「{tags[0]}」です。どこが争点か探してください。")
    else:
        hints.append("盤面全体を見て、いま一番価値の大きい場所を考えてください。")

    # 段階2: 場所の絞り込み
    if correct_moves:
        hints.append(f"注目すべきは盤面の{_region(correct_moves[0]['coord'], game.size)}です。")
    else:
        hints.append("石が接触しているところを重点的に読んでください。")

    # 段階3: 判断の指針
    if tags:
        hints.append(f"「{'、'.join(tags[:2])}」を避ける手を選んでください。")
    else:
        hints.append("相手に先に主導権を渡さない手を選んでください。")

    return hints[:HINT_LEVELS]


def _region(gtp: str, size: int) -> str:
    """GTP 座標を「左上」「中央」などの語に落とす。"""
    from .sgf import gtp_to_coord

    try:
        coord = gtp_to_coord(gtp, size)
    except Exception:
        coord = None
    if coord is None:
        return "中央付近"
    col, row = coord
    third = size / 3.0
    vertical = "上" if row < third else ("下" if row >= 2 * third else "中段")
    horizontal = "左" if col < third else ("右" if col >= 2 * third else "中央")
    if vertical == "中段" and horizontal == "中央":
        return "中央"
    if vertical == "中段":
        return f"{horizontal}辺"
    if horizontal == "中央":
        return f"{vertical}辺"
    return f"{vertical}{horizontal}"


def compute_difficulty(
    delta: float,
    before: TurnAnalysis,
    my_color: str,
    settings: Settings,
) -> int:
    """勝率低下幅と、正解手の分かりやすさ（候補手の集中度）から算出する。

    1（易しい）〜5（難しい）。候補が割れているほど難しいとみなす。
    """
    drop = abs(delta)
    base = 1
    if drop >= settings.critical_threshold:
        base = 3
    elif drop >= settings.bad_threshold:
        base = 2

    concentration = 0
    if len(before.moves) >= 2:
        best_wr = before.moves[0].winrate_for(my_color)
        second_wr = before.moves[1].winrate_for(my_color)
        gap = best_wr - second_wr
        if gap < 2.0:
            concentration = 2      # 候補が拮抗＝どれが正解か分かりにくい
        elif gap < 5.0:
            concentration = 1
    return max(1, min(5, base + concentration))


def problem_payload(db: Database, problem_id: str) -> Optional[dict]:
    """PWA 配信用に 1 問を JSON 化する（FR-06 のデータ構造）。"""
    row = db.query_one("SELECT * FROM problems WHERE id = ?", (problem_id,))
    if not row:
        return None
    state = db.query_one(
        "SELECT * FROM problem_state WHERE problem_id = ?", (problem_id,)
    )
    return {
        "problem_id": row["id"],
        "source_game_id": row["game_id"],
        "move_number": row["move_no"],
        "board_position": row["position_sgf"],
        "player_to_move": row["player_to_move"],
        "actual_move": row["actual_move"],
        "actual_winrate_drop": abs(row["actual_delta"] or 0.0),
        "correct_moves": loads(row["correct_moves"], []),
        "tags": loads(row["tags"], []),
        "hints": loads(row["hints"], []),
        "explanation": row["explanation"],
        "difficulty": row["difficulty"],
        "streak": state["streak"] if state else 0,
        "next_due_at": state["next_due_at"] if state else None,
        "graduated": bool(state["graduated"]) if state else False,
    }
