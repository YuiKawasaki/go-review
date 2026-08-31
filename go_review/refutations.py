"""「その手を打つとどうなるか」を手順として持たせる。

練習画面で学習者が盤上のどこかを押したとき、正解なら決めの手順を、
不正解ならその手が咎められる手順を、1 手ずつ盤上で見せたい。
そのためには「押されうる手ごとの読み筋」が事前に要る。PWA は静的 JSON を
配るだけでオフラインでも動くため、その場で KataGo に聞くことはできない。

幸い、必要な材料はすでに DB にある。

  - moves.candidates … 各局面の KataGo 候補手（上位数手）と、それぞれの読み筋
  - variations       … 最善手の進行（best）と、実戦の手が咎められる進行（punish）

これらを 1 つの表に集めて、「この手を押したらこの手順」という形に直す。
新しい解析は要らない。

保存する行の約束:

  pv_moves[0] は必ず move 自身。つまり読み筋は必ず「問題の局面」から始まり、
  最初の 1 手が学習者の打った手になる。punish は本来「実戦の手のあと」から
  始まる読み筋なので、先頭に実戦の手を足して揃えている。この約束のおかげで、
  UI 側は kind を見ずに同じ描画処理を使える。
"""
from __future__ import annotations

from typing import Callable, Optional

from .config import Settings
from .db import Database, dumps, loads
from .sgf import parse_game
from .variations import BRANCH_BEST, BRANCH_PUNISH, pv_comments

KIND_BEST = "best"
KIND_CANDIDATE = "candidate"
KIND_ACTUAL = "actual"

# 候補手の手順として残す最低の手数。
#
# KataGo はほとんど読まなかった手にも読み筋を 1 手だけ付けて返す。
# それを保存すると、盤で「自分の手」を置いただけで手順が終わり、
# 肝心の「相手がどう来るか」が出ない。相手の応手が 1 手も無いものは
# 見せないほうがよい（正解手順のほうへ案内する）。
MIN_CANDIDATE_PV = 3


def save_refutation(
    db: Database,
    problem_id: str,
    move: str,
    kind: str,
    pv: list[str],
    comments: list[str],
    winrate: Optional[float],
    score: Optional[float],
    visits: Optional[int],
) -> None:
    db.execute(
        "INSERT INTO refutations (problem_id, move, kind, pv_moves, pv_comments, "
        "winrate, score, visits) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(problem_id, move) DO UPDATE SET "
        "kind = excluded.kind, pv_moves = excluded.pv_moves, "
        "pv_comments = excluded.pv_comments, winrate = excluded.winrate, "
        "score = excluded.score, visits = excluded.visits",
        (
            problem_id, move.upper(), kind, dumps(pv), dumps(comments),
            None if winrate is None else round(winrate, 2),
            None if score is None else round(score, 2),
            visits,
        ),
    )


def load_refutations(db: Database, problem_id: str) -> list[dict]:
    """PWA へ配る形。手の強い順（勝率の高い順）に並べる。"""
    rows = db.query(
        "SELECT * FROM refutations WHERE problem_id = ?", (problem_id,)
    )
    out = [
        {
            "move": row["move"],
            "kind": row["kind"],
            "pv": loads(row["pv_moves"], []) or [],
            "comments": loads(row["pv_comments"], []) or [],
            "winrate": row["winrate"],
            "score": row["score"],
            "visits": row["visits"],
        }
        for row in rows
    ]
    out.sort(key=lambda r: (r["kind"] != KIND_BEST, -(r["winrate"] or 0.0)))
    return out


def build_for_game_problems(
    db: Database,
    game_id: str,
    settings: Settings,
    log: Callable[[str], None] = lambda _: None,
) -> int:
    """1 局ぶんの問題について、既存データから手順を組み立てる。

    KataGo は使わない。読み筋への一言（pv_comments）は、語の見直しが
    入っているのでここで作り直す。
    """
    row = db.query_one("SELECT sgf, my_color FROM games WHERE id = ?", (game_id,))
    if not row:
        return 0
    game = parse_game(row["sgf"])
    my_color = row["my_color"]
    limit = settings.pv_max_moves

    written = 0
    problems = db.query(
        "SELECT id, move_no, actual_move, correct_moves FROM problems WHERE game_id = ?",
        (game_id,),
    )
    for problem in problems:
        problem_id = problem["id"]
        move_no = problem["move_no"]
        start_turn = move_no - 1
        seen: set[str] = set()
        # 作り直しなので、まず前回ぶんを消す。上書きだけだと、今回は
        # 採用しない条件になった手（読み筋が短すぎる等）が残ってしまう。
        db.execute("DELETE FROM refutations WHERE problem_id = ?", (problem_id,))

        def store(
            move: str, kind: str, pv: list[str], winrate, score, visits,
            starts_with_move: bool = True,
        ) -> None:
            """1 手ぶんの手順を保存する。

            starts_with_move は、渡された読み筋がその手自身から始まっているか。
            punish の読み筋だけは「実戦の手のあと」から始まるので False にして、
            先頭に実戦の手を足す。座標の一致で判断すると、たまたま同じ座標が
            来たときに 1 手ずれるので、呼び出し側が明示する。
            """
            nonlocal written
            key = (move or "").upper()
            if not key or key in seen or not pv:
                return
            if not starts_with_move:
                pv = [move] + pv
            if kind == KIND_CANDIDATE and len(pv) < MIN_CANDIDATE_PV:
                return
            pv = pv[:limit]
            branch = BRANCH_BEST if kind == KIND_BEST else BRANCH_PUNISH
            comments = pv_comments(game, start_turn, pv, my_color, branch)
            save_refutation(
                db, problem_id, move, kind, pv, comments, winrate, score, visits
            )
            seen.add(key)
            written += 1

        # 1. 正解手の進行
        best_row = db.query_one(
            "SELECT pv_moves, end_winrate, end_score, visits FROM variations "
            "WHERE game_id = ? AND move_no = ? AND branch_type = ?",
            (game_id, move_no, BRANCH_BEST),
        )
        correct = loads(problem["correct_moves"], []) or []
        best_move = correct[0].get("coord") if correct else None
        if best_row and best_move:
            store(
                best_move, KIND_BEST, loads(best_row["pv_moves"], []) or [],
                best_row["end_winrate"], best_row["end_score"], best_row["visits"],
            )

        # 2. KataGo が挙げた候補手それぞれの進行
        move_row = db.query_one(
            "SELECT candidates FROM moves WHERE game_id = ? AND move_no = ?",
            (game_id, move_no),
        )
        for cand in (loads(move_row["candidates"], []) if move_row else []) or []:
            store(
                cand.get("coord") or "", KIND_CANDIDATE, list(cand.get("pv") or []),
                cand.get("winrate"), cand.get("score"), cand.get("visits"),
            )

        # 3. 実戦で打った手が咎められる進行
        #    punish は「実戦の手のあと」から始まるので、先頭に実戦の手を足す。
        punish_row = db.query_one(
            "SELECT pv_moves, end_winrate, end_score, visits FROM variations "
            "WHERE game_id = ? AND move_no = ? AND branch_type = ?",
            (game_id, move_no, BRANCH_PUNISH),
        )
        actual = problem["actual_move"]
        if punish_row and actual:
            store(
                actual, KIND_ACTUAL, loads(punish_row["pv_moves"], []) or [],
                punish_row["end_winrate"], punish_row["end_score"],
                punish_row["visits"], starts_with_move=False,
            )

    if written:
        _refresh_variation_comments(db, game_id, game, my_color)
        db.commit()
        log(f"{game_id}: 手順 {written} 件")
    return written


def _refresh_variation_comments(db: Database, game_id: str, game, my_color: str) -> None:
    """変化図の一言を今の書き方で作り直す。

    棋譜画面は variations の pv_comments をそのまま表示するので、
    ここを直しておかないと画面ごとに言い回しが食い違う。
    """
    for row in db.query(
        "SELECT move_no, branch_type, pv_moves FROM variations WHERE game_id = ?",
        (game_id,),
    ):
        pv = loads(row["pv_moves"], []) or []
        if not pv:
            continue
        start_turn = (
            row["move_no"] - 1 if row["branch_type"] == BRANCH_BEST else row["move_no"]
        )
        comments = pv_comments(game, start_turn, pv, my_color, row["branch_type"])
        db.execute(
            "UPDATE variations SET pv_comments = ? WHERE game_id = ? AND move_no = ? "
            "AND branch_type = ?",
            (dumps(comments), game_id, row["move_no"], row["branch_type"]),
        )


def build_all(
    db: Database,
    settings: Settings,
    log: Callable[[str], None] = lambda _: None,
) -> int:
    """解析済みの全局について手順を組み立てる。"""
    total = 0
    for row in db.query("SELECT DISTINCT game_id FROM problems ORDER BY game_id"):
        total += build_for_game_problems(db, row["game_id"], settings, log)
    log(f"合計 {total} 件の手順を保存しました。")
    return total
