"""復習スケジューリング（FR-10）。

正解するたびに間隔を広げ（4日後 → 10日後）、3回連続正解で卒業。
不正解なら連続正解をリセットして翌日にやり直す。

以前は 5 回連続（1・3・7・14日）で卒業だったが、1 局から 2〜3 問ずつ
増えるのに対して卒業が遅すぎ、未消化が増え続けて続けられなくなった。
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .badmoves import CRITICAL
from .config import Settings
from .db import Database, loads

VERDICT_CORRECT = "正解"
VERDICT_ACCEPTABLE = "許容"
VERDICT_WRONG = "不正解"


def _today() -> date:
    return datetime.now(timezone.utc).date()


def next_due(streak: int, settings: Settings, today: Optional[date] = None) -> Optional[str]:
    """連続正解数から次回出題日を求める。卒業なら None。"""
    today = today or _today()
    if streak >= settings.graduate_streak:
        return None
    if streak <= 0:
        days = 1
    else:
        intervals = settings.review_intervals
        days = intervals[min(streak - 1, len(intervals) - 1)]
    return (today + timedelta(days=days)).isoformat()


def judge(answer_coord: str, correct_moves: list[dict]) -> str:
    """回答手を正解／許容／不正解に判定する。許容手も正解として扱う。"""
    answer = (answer_coord or "").strip().upper()
    for move in correct_moves or []:
        if (move.get("coord") or "").upper() != answer:
            continue
        return VERDICT_CORRECT if move.get("label") == "最善" else VERDICT_ACCEPTABLE
    return VERDICT_WRONG


def record_answer(
    db: Database,
    problem_id: str,
    answer_coord: str,
    think_seconds: float,
    settings: Settings,
    hint_used: bool = False,
    reviewed_at: Optional[str] = None,
) -> dict:
    """回答を記録し、次回出題日を更新する。"""
    row = db.query_one("SELECT correct_moves FROM problems WHERE id = ?", (problem_id,))
    if not row:
        raise KeyError(f"問題が見つかりません: {problem_id}")

    correct_moves = loads(row["correct_moves"], []) or []
    verdict = judge(answer_coord, correct_moves)
    is_correct = verdict in (VERDICT_CORRECT, VERDICT_ACCEPTABLE)

    state = db.query_one(
        "SELECT streak, graduated FROM problem_state WHERE problem_id = ?", (problem_id,)
    )
    streak = state["streak"] if state else 0

    # ヒントを使って当てた場合は連続正解を伸ばさない（自力正解のみ加算）
    if is_correct and not hint_used:
        streak += 1
    elif is_correct and hint_used:
        streak = max(streak, 1)
    else:
        streak = 0

    due = next_due(streak, settings)
    graduated = 1 if (streak >= settings.graduate_streak) else 0
    reviewed_at = reviewed_at or datetime.now(timezone.utc).isoformat(timespec="seconds")

    db.execute(
        "INSERT INTO reviews (problem_id, reviewed_at, answer_coord, is_correct, verdict, "
        "think_seconds, hint_used, streak, next_due_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            problem_id, reviewed_at, answer_coord, int(is_correct), verdict,
            think_seconds, int(hint_used), streak, due,
        ),
    )
    db.execute(
        "INSERT INTO problem_state (problem_id, streak, next_due_at, graduated, last_result) "
        "VALUES (?,?,?,?,?) ON CONFLICT(problem_id) DO UPDATE SET "
        "streak = excluded.streak, next_due_at = excluded.next_due_at, "
        "graduated = excluded.graduated, last_result = excluded.last_result",
        (problem_id, streak, due, graduated, verdict),
    )
    db.commit()

    # 学習記録（daily_logs）をこの回答の日付で即時更新する。writeback の
    # push_daily_log 任せだと「バッチが動いた日」しか更新されず、バッチが
    # 不定期に動くこのアプリでは、実際に解いた日と記録の日付がずれ続けて
    # 「毎日やっているのに連続日数が 0」になっていた。record_tsumego_answer
    # と同じ理由・同じ直し方。srs → learning の逆方向 import は循環になる
    # ため、ここでだけ遅延 import する。
    from .learning import refresh_daily_log

    refresh_daily_log(db, reviewed_at[:10])

    return {
        "problem_id": problem_id,
        "verdict": verdict,
        "is_correct": is_correct,
        "streak": streak,
        "next_due_at": due,
        "graduated": bool(graduated),
    }


def due_problems(
    db: Database,
    settings: Settings,
    today: Optional[date] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """本日出題する問題を選ぶ。先頭が今日の分、そのあとが追加練習の分。

    今日の分（最大 limit 問）:
      1. 期日が来た復習を、期日の古い順に
      2. 空きがあれば、まだ一度も解いていない問題を最大 daily_new_limit 問
         （敗着候補を優先し、残りはランダム）
    を選んだうえで、出題順をランダムにする。

    新しい問題に上限を設けるのは、1 局から 2〜3 問ずつ増えるのに対して
    以前は未出題を毎日すべて対象にしていたため、復習が新問に押し出され、
    未消化が増え続けて続けられなくなったから。

    追加練習の分は、卒業していない残りから最大 daily_extra_limit 問
    （間違えたことのある問題を先に、ランダム順）。全部を並べると
    終わりが見えず負担になるので上限を設ける。

    乱数は日付で固定する。同じ日に何度書き出しても選ばれる問題が
    入れ替わらないようにするため。
    """
    today = today or _today()
    limit = limit or settings.daily_review_limit
    rng = random.Random(today.isoformat())
    rows = db.query(
        """
        SELECT p.id, p.game_id, p.move_no, p.difficulty, s.streak, s.next_due_at,
               s.graduated, s.last_result, b.severity
        FROM problems p
        LEFT JOIN problem_state s ON s.problem_id = p.id
        LEFT JOIN bad_moves b ON b.game_id = p.game_id AND b.move_no = p.move_no
        WHERE COALESCE(s.graduated, 0) = 0
        ORDER BY p.id
        """
    )

    def severity_rank(row) -> int:
        return 0 if row["severity"] == CRITICAL else 1

    reviews = sorted(
        (r for r in rows if r["next_due_at"] and r["next_due_at"] <= today.isoformat()),
        key=lambda r: (r["next_due_at"], severity_rank(r)),
    )
    fresh = [r for r in rows if r["next_due_at"] is None]
    fresh.sort(key=lambda r: (severity_rank(r), rng.random()))

    primary = reviews[:limit]
    room = min(limit - len(primary), settings.daily_new_limit)
    primary += fresh[:max(room, 0)]
    rng.shuffle(primary)

    primary_ids = {r["id"] for r in primary}
    rest = [r for r in rows if r["id"] not in primary_ids]
    wrong = [r for r in rest if r["last_result"] == VERDICT_WRONG]
    others = [r for r in rest if r["last_result"] != VERDICT_WRONG]
    rng.shuffle(wrong)
    rng.shuffle(others)
    extra = (wrong + others)[:settings.daily_extra_limit]

    def payload(r: object, is_extra: bool) -> dict:
        return {
            "problem_id": r["id"],
            "game_id": r["game_id"],
            "move_no": r["move_no"],
            "difficulty": r["difficulty"],
            "streak": r["streak"] or 0,
            "next_due_at": r["next_due_at"],
            "severity": r["severity"],
            "first_time": r["next_due_at"] is None,
            "extra": is_extra,
        }

    return [payload(r, False) for r in primary] + [payload(r, True) for r in extra]


def accuracy(db: Database, first_attempt_only: bool = False) -> Optional[float]:
    """問題の正答率（%）。first_attempt_only なら初見のみ。"""
    if first_attempt_only:
        rows = db.query(
            "SELECT is_correct FROM reviews r WHERE r.id IN "
            "(SELECT MIN(id) FROM reviews GROUP BY problem_id)"
        )
    else:
        rows = db.query("SELECT is_correct FROM reviews")
    if not rows:
        return None
    correct = sum(1 for r in rows if r["is_correct"])
    return round(correct / len(rows) * 100.0, 1)


def stats(db: Database, settings: Settings) -> dict:
    total = db.scalar("SELECT COUNT(*) FROM problems") or 0
    graduated = db.scalar("SELECT COUNT(*) FROM problem_state WHERE graduated = 1") or 0
    due = sum(1 for p in due_problems(db, settings) if not p["extra"])
    return {
        "total_problems": total,
        "graduated": graduated,
        "due_today": due,
        "accuracy_all": accuracy(db),
        "accuracy_first": accuracy(db, first_attempt_only=True),
    }
