"""復習スケジューリング（FR-10）。

初見で不正解 → 翌日 → 3日後 → 7日後 → 14日後、5回連続正解で卒業。
一度でも不正解なら間隔をリセットして翌日に戻す。
"""
from __future__ import annotations

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
    intervals = settings.review_intervals
    idx = min(max(streak - 1, 0), len(intervals) - 1)
    days = intervals[idx] if streak > 0 else intervals[0]
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
    """本日出題する問題を選ぶ。

    期日超過分は優先度順（敗着候補 > 悪手）に、次いで期日の古い順。
    初見（未出題）の問題も対象に含める。
    """
    today = today or _today()
    limit = limit or settings.daily_review_limit
    rows = db.query(
        """
        SELECT p.id, p.game_id, p.move_no, p.difficulty, s.streak, s.next_due_at,
               s.graduated, b.severity
        FROM problems p
        LEFT JOIN problem_state s ON s.problem_id = p.id
        LEFT JOIN bad_moves b ON b.game_id = p.game_id AND b.move_no = p.move_no
        WHERE COALESCE(s.graduated, 0) = 0
          AND (s.next_due_at IS NULL OR s.next_due_at <= ?)
        """,
        (today.isoformat(),),
    )

    def sort_key(row) -> tuple:
        severity_rank = 0 if row["severity"] == CRITICAL else 1
        due = row["next_due_at"] or ""      # 未出題を先に
        return (severity_rank, due, -(row["difficulty"] or 0))

    ordered = sorted(rows, key=sort_key)
    return [
        {
            "problem_id": r["id"],
            "game_id": r["game_id"],
            "move_no": r["move_no"],
            "difficulty": r["difficulty"],
            "streak": r["streak"] or 0,
            "next_due_at": r["next_due_at"],
            "severity": r["severity"],
            "first_time": r["next_due_at"] is None,
        }
        for r in ordered[:limit]
    ]


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
    due = len(due_problems(db, settings, limit=10_000))
    return {
        "total_problems": total,
        "graduated": graduated,
        "due_today": due,
        "accuracy_all": accuracy(db),
        "accuracy_first": accuracy(db, first_attempt_only=True),
    }
