"""学習記録の蓄積と突合分析（FR-11 / FR-13）。

詰碁は外部アプリを使う前提で、結果の自動取得はできない。したがって
手入力の摩擦をいかに小さくするかが成否を決める。通常入力は
「解いた数・間違えた数・テーマ」だけ（10 秒以内）とする。
"""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .config import Settings
from .db import Database, dumps, loads
from .srs import accuracy, next_due


def _today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------- 詰碁


def record_tsumego_session(
    db: Database,
    solved: int,
    wrong: int,
    themes: list[str],
    source: str = "",
    on_date: Optional[str] = None,
) -> int:
    """通常入力: セッション終了時にタップだけで記録する。"""
    on_date = on_date or _today_str()
    cur = db.execute(
        "INSERT INTO tsumego_sessions (date, solved, wrong, themes, source) VALUES (?,?,?,?,?)",
        (on_date, int(solved), int(wrong), dumps(themes or []), source),
    )
    db.commit()
    refresh_daily_log(db, on_date)
    return int(cur.lastrowid)


def record_tsumego_problem(
    db: Database,
    theme_tag: str,
    source: str = "",
    image_path: str = "",
    answer_note: str = "",
    tsumego_id: Optional[str] = None,
) -> str:
    """詳細入力: 間違えた 1 問をスクリーンショット付きで登録する。

    登録した問題は生成問題と同じ間隔で復習対象になる（正誤は自己申告）。
    """
    tsumego_id = tsumego_id or f"T-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT OR REPLACE INTO tsumego (id, source, theme_tag, image_path, answer_note, "
        "streak, next_due_at, graduated) VALUES (?,?,?,?,?,0,?,0)",
        (
            tsumego_id, source, theme_tag, image_path, answer_note,
            (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat(),
        ),
    )
    db.commit()
    return tsumego_id


def record_tsumego_answer(
    db: Database,
    tsumego_id: str,
    is_correct: bool,
    settings: Settings,
    seconds: float = 0.0,
    hint_used: bool = False,
) -> dict:
    """詰碁の復習結果。正解を見てから解けた場合は正解として扱わない運用。"""
    row = db.query_one("SELECT streak FROM tsumego WHERE id = ?", (tsumego_id,))
    if not row:
        raise KeyError(f"詰碁が見つかりません: {tsumego_id}")

    streak = (row["streak"] or 0) + 1 if (is_correct and not hint_used) else 0
    graduated = 1 if streak >= settings.tsumego_graduate_streak else 0
    due = None if graduated else next_due(max(streak, 1), settings)
    solved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    db.execute(
        "INSERT INTO tsumego_logs (tsumego_id, solved_at, is_correct, seconds, hint_used, "
        "streak, next_due_at) VALUES (?,?,?,?,?,?,?)",
        (tsumego_id, solved_at, int(is_correct), seconds, int(hint_used), streak, due),
    )
    db.execute(
        "UPDATE tsumego SET streak = ?, next_due_at = ?, graduated = ? WHERE id = ?",
        (streak, due, graduated, tsumego_id),
    )
    db.commit()
    refresh_daily_log(db, _today_str())
    return {"tsumego_id": tsumego_id, "streak": streak, "graduated": bool(graduated), "next_due_at": due}


def due_tsumego(db: Database, on_date: Optional[str] = None) -> list[dict]:
    on_date = on_date or _today_str()
    rows = db.query(
        "SELECT * FROM tsumego WHERE graduated = 0 AND (next_due_at IS NULL OR next_due_at <= ?) "
        "ORDER BY next_due_at",
        (on_date,),
    )
    return [
        {
            "tsumego_id": r["id"],
            "theme_tag": r["theme_tag"],
            "source": r["source"],
            "image_path": r["image_path"],
            "streak": r["streak"] or 0,
            "next_due_at": r["next_due_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------- 日次ログ


def refresh_daily_log(db: Database, on_date: Optional[str] = None) -> dict:
    """その日の対局数・詰碁数・正答率・最頻タグを集計して保存する。"""
    on_date = on_date or _today_str()

    games = db.scalar(
        "SELECT COUNT(*) FROM games WHERE substr(COALESCE(played_at,''),1,10) = ?", (on_date,)
    ) or 0
    solved = db.scalar(
        "SELECT COALESCE(SUM(solved),0) FROM tsumego_sessions WHERE date = ?", (on_date,)
    ) or 0
    wrong = db.scalar(
        "SELECT COALESCE(SUM(wrong),0) FROM tsumego_sessions WHERE date = ?", (on_date,)
    ) or 0

    reviews = db.query(
        "SELECT is_correct, think_seconds FROM reviews WHERE substr(reviewed_at,1,10) = ?",
        (on_date,),
    )
    if reviews:
        rate = round(sum(1 for r in reviews if r["is_correct"]) / len(reviews) * 100.0, 1)
        minutes = round(sum((r["think_seconds"] or 0) for r in reviews) / 60.0, 1)
    else:
        rate, minutes = None, 0.0

    top_tag = _top_tag_for_date(db, on_date)
    existing_note = db.scalar("SELECT note FROM daily_logs WHERE date = ?", (on_date,)) or ""

    db.execute(
        "INSERT INTO daily_logs (date, games, tsumego_count, tsumego_wrong, problem_accuracy, "
        "study_minutes, top_tag, note) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET games = excluded.games, "
        "tsumego_count = excluded.tsumego_count, tsumego_wrong = excluded.tsumego_wrong, "
        "problem_accuracy = excluded.problem_accuracy, study_minutes = excluded.study_minutes, "
        "top_tag = excluded.top_tag",
        (on_date, games, solved, wrong, rate, minutes, top_tag, existing_note),
    )
    db.commit()
    return {
        "date": on_date,
        "games": games,
        "tsumego_count": solved,
        "tsumego_wrong": wrong,
        "problem_accuracy": rate,
        "study_minutes": minutes,
        "top_tag": top_tag,
    }


def set_note(db: Database, on_date: str, note: str) -> None:
    """「気づき」欄。Notion 側からの入力も取り込むため双方向対象。"""
    db.execute(
        "INSERT INTO daily_logs (date, note) VALUES (?,?) "
        "ON CONFLICT(date) DO UPDATE SET note = excluded.note",
        (on_date, note),
    )
    db.commit()


def _top_tag_for_date(db: Database, on_date: str) -> Optional[str]:
    rows = db.query(
        "SELECT b.tags FROM bad_moves b JOIN games g ON g.id = b.game_id "
        "WHERE substr(COALESCE(g.played_at,''),1,10) = ?",
        (on_date,),
    )
    counter: Counter[str] = Counter()
    for row in rows:
        counter.update(loads(row["tags"], []) or [])
    return counter.most_common(1)[0][0] if counter else None


# ---------------------------------------------------------------- ダッシュボード


def tag_counts(db: Database, recent_games: Optional[int] = None) -> dict[str, int]:
    """タグ別の発生件数。recent_games を指定すると直近 N 局に限定する。"""
    if recent_games:
        game_ids = [
            r["id"]
            for r in db.query(
                "SELECT id FROM games WHERE status = '解析済' ORDER BY played_at DESC, id DESC LIMIT ?",
                (recent_games,),
            )
        ]
        if not game_ids:
            return {}
        placeholders = ",".join("?" for _ in game_ids)
        rows = db.query(
            f"SELECT tags FROM bad_moves WHERE game_id IN ({placeholders})", game_ids
        )
    else:
        rows = db.query("SELECT tags FROM bad_moves")

    counter: Counter[str] = Counter()
    for row in rows:
        counter.update(loads(row["tags"], []) or [])
    return dict(counter.most_common())


def tsumego_accuracy_by_theme(db: Database) -> dict[str, float]:
    """テーマ別の詰碁正答率（%）。"""
    rows = db.query(
        "SELECT t.theme_tag AS theme, l.is_correct AS ok FROM tsumego_logs l "
        "JOIN tsumego t ON t.id = l.tsumego_id"
    )
    totals: Counter[str] = Counter()
    correct: Counter[str] = Counter()
    for row in rows:
        theme = row["theme"] or "未分類"
        totals[theme] += 1
        if row["ok"]:
            correct[theme] += 1
    return {
        theme: round(correct[theme] / total * 100.0, 1)
        for theme, total in totals.items()
        if total
    }


def cross_analysis(db: Database, recent_games: int = 20) -> list[dict]:
    """突合分析（FR-11 の主目的）。

    実戦の悪手タグ別発生数 × 同一タグの詰碁正答率 を並べ、
      - 実戦では出るが詰碁では解けている → 知識はあるが実戦で使えていない
      - 詰碁でも解けない → 知識自体が不足
    を切り分けて提示する。
    """
    counts = tag_counts(db, recent_games)
    tsumego = tsumego_accuracy_by_theme(db)
    out: list[dict] = []
    for tag in sorted(set(counts) | set(tsumego), key=lambda t: -counts.get(t, 0)):
        game_count = counts.get(tag, 0)
        rate = tsumego.get(tag)
        if rate is None:
            diagnosis = "詰碁の記録なし"
            action = "このテーマの詰碁を記録してみる"
        elif game_count >= 2 and rate >= 70.0:
            diagnosis = "知識はあるが実戦で使えていない"
            action = "対局中のチェックリスト運用"
        elif game_count >= 2 and rate < 70.0:
            diagnosis = "知識自体が不足"
            action = "このテーマの詰碁を反復"
        else:
            diagnosis = "実戦での発生は少ない"
            action = "様子見"
        out.append(
            {
                "tag": tag,
                "game_count": game_count,
                "tsumego_accuracy": rate,
                "diagnosis": diagnosis,
                "action": action,
            }
        )
    return out


def dashboard(db: Database, settings: Settings) -> dict:
    """ダッシュボード用の集計一式（FR-13）。"""
    from .srs import stats as srs_stats

    games = db.query(
        "SELECT id, played_at, result, margin, opponent_rating, losing_move_no, move_count "
        "FROM games WHERE status = '解析済' ORDER BY played_at, id"
    )
    win_series = [
        {
            "game_id": g["id"],
            "date": (g["played_at"] or "")[:10],
            "result": g["result"],
            "margin": g["margin"],
            "opponent_rating": g["opponent_rating"],
        }
        for g in games
    ]

    phases = Counter()
    for g in games:
        if not g["losing_move_no"] or not g["move_count"]:
            continue
        ratio = g["losing_move_no"] / max(g["move_count"], 1)
        phases["序盤" if ratio < 0.34 else ("中盤" if ratio < 0.67 else "終盤")] += 1

    bands: dict[str, dict[str, int]] = {}
    for g in games:
        rating = g["opponent_rating"]
        if rating is None:
            continue
        band = f"{int(rating) // 100 * 100}〜"
        entry = bands.setdefault(band, {"games": 0, "wins": 0})
        entry["games"] += 1
        if g["result"] == "勝":
            entry["wins"] += 1

    daily = db.query("SELECT * FROM daily_logs ORDER BY date DESC LIMIT 60")
    return {
        "tag_counts_recent": tag_counts(db, 20),
        "tag_counts_all": tag_counts(db),
        "games": win_series,
        "losing_phase": dict(phases),
        "rating_bands": bands,
        "problems": srs_stats(db, settings),
        "tsumego": {
            "themes": tsumego_accuracy_by_theme(db),
            "graduated": db.scalar("SELECT COUNT(*) FROM tsumego WHERE graduated = 1") or 0,
            "total": db.scalar("SELECT COUNT(*) FROM tsumego") or 0,
            "first_accuracy": _tsumego_first_accuracy(db),
        },
        "cross": cross_analysis(db),
        "daily": [
            {
                "date": d["date"],
                "games": d["games"],
                "tsumego_count": d["tsumego_count"],
                "tsumego_wrong": d["tsumego_wrong"],
                "problem_accuracy": d["problem_accuracy"],
                "study_minutes": d["study_minutes"],
                "top_tag": d["top_tag"],
                "note": d["note"],
            }
            for d in daily
        ],
        "streak_days": _study_streak(db),
    }


def _tsumego_first_accuracy(db: Database) -> Optional[float]:
    rows = db.query(
        "SELECT is_correct FROM tsumego_logs WHERE id IN "
        "(SELECT MIN(id) FROM tsumego_logs GROUP BY tsumego_id)"
    )
    if not rows:
        return None
    return round(sum(1 for r in rows if r["is_correct"]) / len(rows) * 100.0, 1)


def _study_streak(db: Database) -> int:
    """連続学習日数。記録が無い日で途切れる。"""
    rows = db.query(
        "SELECT date FROM daily_logs WHERE games > 0 OR tsumego_count > 0 "
        "OR problem_accuracy IS NOT NULL ORDER BY date DESC"
    )
    if not rows:
        return 0
    streak = 0
    cursor = datetime.now(timezone.utc).date()
    logged = {r["date"] for r in rows}
    while cursor.isoformat() in logged:
        streak += 1
        cursor -= timedelta(days=1)
    return streak
