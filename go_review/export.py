"""PWA 向け静的 JSON の書き出し。

スマホ側は閲覧・演習に徹する設計のため、必要なデータはすべてここで
組み立てて配信する。配信先は URL 推測困難なパス（PUBLISH_SLUG）に置ける。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .badmoves import good_move_markers, marker_for
from .config import Settings
from .db import Database, loads
from .learning import dashboard, due_tsumego
from .problems import problem_payload
from .srs import due_problems


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def target_dir(settings: Settings) -> Path:
    base = settings.publish_dir or (Path(__file__).resolve().parent.parent / "web" / "data")
    if settings.publish_slug:
        base = base / settings.publish_slug
    return base


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def export_all(db: Database, settings: Settings) -> Path:
    """索引・棋譜・問題・ダッシュボードを一括で書き出す。"""
    out = target_dir(settings)
    games = db.query(
        "SELECT * FROM games WHERE status = '解析済' ORDER BY played_at DESC, id DESC"
    )

    index_games = []
    for row in games:
        write_json(out / "games" / f"{row['id']}.json", game_payload(db, row["id"]))
        index_games.append(
            {
                "game_id": row["id"],
                "played_at": row["played_at"],
                "my_color": row["my_color"],
                "opponent": row["opponent_name"],
                "opponent_rating": row["opponent_rating"],
                "result": row["result"],
                "margin": row["margin"],
                "move_count": row["move_count"],
                "end_type": row["end_type"],
                "losing_move_no": row["losing_move_no"],
                "main_tags": loads(row["main_tags"], []) or [],
            }
        )

    write_json(
        out / "index.json",
        {
            "generated_at": _now(),
            "unanalyzed": db.unanalyzed_count(),
            "in_progress": db.scalar("SELECT COUNT(*) FROM games WHERE status='解析中'") or 0,
            "games": index_games,
            "last_sync": _last_sync(db),
        },
    )
    write_json(out / "problems.json", problems_payload(db, settings))
    write_json(out / "due.json", due_payload(db, settings))
    write_json(out / "dashboard.json", dashboard(db, settings))
    return out


def _last_sync(db: Database) -> Optional[str]:
    row = db.get_sync("notion_kifu")
    return row["last_synced_at"] if row else None


def game_payload(db: Database, game_id: str) -> dict:
    """棋譜リプレイ 1 局ぶん（FR-07 / FR-08 が必要とする全データ）。"""
    game = db.query_one("SELECT * FROM games WHERE id = ?", (game_id,))
    if not game:
        return {}
    my_color = game["my_color"]
    moves = db.moves_for(game_id)
    bad_rows = {
        r["move_no"]: r
        for r in db.query("SELECT * FROM bad_moves WHERE game_id = ?", (game_id,))
    }
    good = set(good_move_markers(db, game_id, my_color))

    move_list = []
    for row in moves:
        move_no = row["move_no"]
        bad = bad_rows.get(move_no)
        marker = marker_for(bad["severity"]) if bad else None
        if marker is None and move_no in good and row["color"] == my_color:
            marker = "good"
        if bad and game["losing_move_no"] == move_no:
            marker = "losing"
        move_list.append(
            {
                "move_no": move_no,
                "color": row["color"],
                "coord": row["coord"],
                # 勝率は常に自分視点で統一する（FR-07）
                "winrate_before": row["winrate_before"] if row["color"] == my_color else None,
                "winrate": _my_winrate(row, my_color),
                "delta": row["delta"] if row["color"] == my_color else None,
                "best_move": row["best_move"],
                "candidates": loads(row["candidates"], []) or [],
                "marker": marker,
                "severity": bad["severity"] if bad else None,
                "tags": loads(bad["tags"], []) if bad else [],
            }
        )

    variations: dict[str, dict] = {}
    for row in db.query("SELECT * FROM variations WHERE game_id = ?", (game_id,)):
        entry = variations.setdefault(str(row["move_no"]), {})
        entry[row["branch_type"]] = {
            "pv": loads(row["pv_moves"], []) or [],
            "comments": loads(row["pv_comments"], []) or [],
            "end_winrate": row["end_winrate"],
            "end_score": row["end_score"],
            "visits": row["visits"],
        }

    problems = [
        {
            "problem_id": r["id"],
            "move_no": r["move_no"],
            "explanation": r["explanation"],
            "tags": loads(r["tags"], []) or [],
        }
        for r in db.query(
            "SELECT id, move_no, explanation, tags FROM problems WHERE game_id = ? ORDER BY move_no",
            (game_id,),
        )
    ]

    return {
        "game_id": game_id,
        "sgf": game["sgf"],
        "board_size": game["board_size"] or 9,
        "komi": game["komi"],
        "played_at": game["played_at"],
        "my_color": my_color,
        "players": {
            "black": _player_label(db, game, "B"),
            "white": _player_label(db, game, "W"),
        },
        "result": game["result"],
        "margin": game["margin"],
        "end_type": game["end_type"],
        "move_count": game["move_count"],
        "losing_move_no": game["losing_move_no"],
        "main_tags": loads(game["main_tags"], []) or [],
        "moves": move_list,
        "variations": variations,
        "problems": problems,
    }


def _my_winrate(row, my_color: str) -> Optional[float]:
    """常に自分視点の勝率（黒白で反転させない）。"""
    if row["winrate_after"] is None:
        return None
    if row["color"] == my_color:
        return row["winrate_after"]
    # 相手の手のあとの勝率は、相手視点の値を反転して自分視点にする
    return round(100.0 - row["winrate_after"], 2)


def _player_label(db: Database, game, color: str) -> str:
    if game["my_color"] == color:
        return "自分"
    name = game["opponent_name"] or "相手"
    rating = game["opponent_rating"]
    return f"{name}({rating})" if rating else name


def problems_payload(db: Database, settings: Settings) -> dict:
    rows = db.query("SELECT id FROM problems ORDER BY id")
    return {
        "generated_at": _now(),
        "problems": [p for p in (problem_payload(db, r["id"]) for r in rows) if p],
    }


def due_payload(db: Database, settings: Settings) -> dict:
    return {
        "generated_at": _now(),
        "date": datetime.now(timezone.utc).date().isoformat(),
        "limit": settings.daily_review_limit,
        "problems": due_problems(db, settings),
        "tsumego": due_tsumego(db, settings=settings),
    }
