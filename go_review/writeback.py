"""Notion への書き戻し（FR-12）。

学習記録のマスタはローカル DB。Notion は閲覧・振り返り用のミラーで、
双方向同期は行わない。例外は「気づき」欄と詰碁の手入力のみ取り込む。

書き戻しは冪等（同一日付のログは上書き）。API 障害時はローカルの
writeback_queue に積み、次回同期でリトライする。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from . import notion as N
from .config import Settings
from .db import Database, loads
from .ingest import (
    PROP_LOSING_MOVE,
    PROP_MAIN_TAGS,
    PROP_REVIEW_URL,
    PROP_STATUS,
    STATUS_DONE,
)
from .learning import refresh_daily_log, set_note
from .notion import NotionClient

Logger = Callable[[str], None]

# 学習ログDBのプロパティ名
LOG_NAME = "名前"          # Notion はタイトル列が必須。日付文字列を入れる
LOG_DATE = "日付"
LOG_GAMES = "対局数"
LOG_TSUMEGO = "詰碁解答数"
LOG_TSUMEGO_WRONG = "詰碁誤答数"
LOG_ACCURACY = "問題正答率"
LOG_TOP_TAGS = "最頻タグ"
LOG_NOTE = "気づき"


def push_game(
    db: Database,
    client: NotionClient,
    settings: Settings,
    game_id: str,
    log: Logger = lambda _m: None,
) -> bool:
    """解析結果を棋譜DBへ書き戻す。"""
    row = db.query_one("SELECT * FROM games WHERE id = ?", (game_id,))
    if not row or not row["notion_page_id"]:
        return False

    props = {
        PROP_STATUS: N.select(STATUS_DONE if row["status"] == "解析済" else row["status"]),
        PROP_LOSING_MOVE: N.number(row["losing_move_no"]),
        PROP_MAIN_TAGS: N.multi_select(loads(row["main_tags"], []) or []),
    }
    if settings.review_base_url:
        props[PROP_REVIEW_URL] = N.url(settings.review_url(game_id))

    try:
        client.update_page(row["notion_page_id"], props)
        return True
    except Exception as exc:
        log(f"書き戻しに失敗（キューに保存）: {game_id}: {exc}")
        db.enqueue_writeback("game", game_id, {"page_id": row["notion_page_id"]})
        return False


def push_daily_log(
    db: Database,
    client: NotionClient,
    settings: Settings,
    on_date: str,
    log: Logger = lambda _m: None,
) -> bool:
    """学習ログDBへ 1 日分を書き戻す（同一日付は上書き＝冪等）。"""
    if not settings.log_ds_id:
        return False

    summary = refresh_daily_log(db, on_date)
    row = db.query_one("SELECT * FROM daily_logs WHERE date = ?", (on_date,))
    if not row:
        return False

    top_tags = [row["top_tag"]] if row["top_tag"] else []
    props = {
        LOG_NAME: N.title(on_date),
        LOG_DATE: N.date(on_date),
        LOG_GAMES: N.number(row["games"]),
        LOG_TSUMEGO: N.number(row["tsumego_count"]),
        LOG_TSUMEGO_WRONG: N.number(row["tsumego_wrong"]),
        LOG_ACCURACY: N.number(row["problem_accuracy"]),
        LOG_TOP_TAGS: N.multi_select(top_tags),
    }

    try:
        existing = _find_log_page(client, settings.log_ds_id, on_date)
        if existing:
            client.update_page(existing["id"], props)
        else:
            props[LOG_NOTE] = N.text(row["note"] or "")
            client.create_page(settings.log_ds_id, props)
        db.execute(
            "UPDATE daily_logs SET synced_at = ? WHERE date = ?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), on_date),
        )
        db.commit()
        return True
    except Exception as exc:
        log(f"学習ログの書き戻しに失敗（キューに保存）: {on_date}: {exc}")
        db.enqueue_writeback("daily_log", on_date, summary)
        return False


def pull_notes(
    db: Database,
    client: NotionClient,
    settings: Settings,
    log: Logger = lambda _m: None,
) -> int:
    """「気づき」欄だけ Notion 側から取り込む（唯一の双方向項目）。"""
    if not settings.log_ds_id:
        return 0
    count = 0
    try:
        for page in client.iter_pages(settings.log_ds_id):
            on_date = N.prop_plain(page, LOG_DATE)[:10]
            note = N.prop_plain(page, LOG_NOTE).strip()
            if not on_date or not note:
                continue
            current = db.scalar("SELECT note FROM daily_logs WHERE date = ?", (on_date,))
            if (current or "") != note:
                set_note(db, on_date, note)
                count += 1
    except Exception as exc:
        log(f"気づきの取り込みに失敗: {exc}")
    return count


def _find_log_page(client: NotionClient, data_source_id: str, on_date: str) -> Optional[dict]:
    filter_ = {"property": LOG_DATE, "date": {"equals": on_date}}
    try:
        page = client.query_data_source(data_source_id, filter_=filter_, page_size=1)
        results = page.get("results", [])
        return results[0] if results else None
    except Exception:
        # フィルタが使えない場合は全件走査にフォールバック
        for candidate in client.iter_pages(data_source_id):
            if N.prop_plain(candidate, LOG_DATE)[:10] == on_date:
                return candidate
        return None


def flush_queue(
    db: Database,
    client: NotionClient,
    settings: Settings,
    log: Logger = lambda _m: None,
) -> int:
    """滞留している書き戻しをまとめて再送する。"""
    rows = db.query("SELECT * FROM writeback_queue ORDER BY id LIMIT 200")
    done = 0
    for row in rows:
        kind = row["kind"]
        ok = False
        try:
            if kind in ("game", "game_props"):
                ok = push_game(db, client, settings, row["ref_id"], log)
            elif kind == "daily_log":
                ok = push_daily_log(db, client, settings, row["ref_id"], log)
        except Exception as exc:
            db.execute(
                "UPDATE writeback_queue SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (str(exc)[:300], row["id"]),
            )
        if ok:
            db.execute("DELETE FROM writeback_queue WHERE id = ?", (row["id"],))
            done += 1
    db.commit()
    return done
