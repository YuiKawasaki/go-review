"""棋譜取り込み（FR-01）。

Notion の棋譜DBは「受信箱」。ユーザーは囲碁クエストから共有するだけで、
プロパティはすべてアプリが埋める。

差分判定は last_edited_time と SGF 本文の SHA-256 の両方で行う。
SGF を抽出できないページは「要確認」を立ててスキップし、同期全体は止めない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import notion as N
from .config import Settings
from .db import Database
from .notion import NotionClient
from .sgf import Game, extract_sgf, parse_game, sgf_hash

Logger = Callable[[str], None]

# Notion 側のプロパティ名。1 文字でも違うと validation_error になる。
PROP_TITLE = "名前"
PROP_DATE = "対局日"
PROP_MY_COLOR = "自分の色"
PROP_OPPONENT = "相手名"
PROP_OPP_RATING = "相手レーティング"
PROP_RESULT = "結果"
PROP_MARGIN = "目数差"
PROP_MOVES = "総手数"
PROP_END_TYPE = "終局種別"
PROP_STATUS = "解析ステータス"
PROP_LOSING_MOVE = "敗着手数"
PROP_MAIN_TAGS = "主要タグ"
PROP_REVIEW_URL = "レビューURL"

STATUS_PENDING = "未解析"
STATUS_DONE = "解析済"
STATUS_CHECK = "要確認"


@dataclass
class IngestReport:
    imported: list[str] = field(default_factory=list)
    skipped_duplicate: int = 0
    needs_check: list[tuple[str, str]] = field(default_factory=list)  # (title, url)
    from_old_db: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"取り込み: {len(self.imported)} 局",
            f"重複スキップ: {self.skipped_duplicate} 件",
            f"要確認: {len(self.needs_check)} 件",
        ]
        if self.from_old_db:
            lines.append(f"旧DBに追加されています: {len(self.from_old_db)} 件")
        if self.errors:
            lines.append(f"エラー: {len(self.errors)} 件")
        return " / ".join(lines)


def build_meta(game: Game, settings: Settings, fallback_date: str = "") -> dict:
    """SGF からプロパティを自動補完する（ユーザーの手入力はゼロ）。"""
    my_color = game.my_color(settings.my_player_name)
    if my_color is None:
        my_color = "B"  # 名義が一致しない場合の既定。要確認として扱う側で拾う。
    opponent_name, opponent_rating = game.opponent(my_color)
    result, margin = game.result_for(my_color)
    played_at = game.date or fallback_date
    return {
        "my_color": my_color,
        "opponent_name": opponent_name,
        "opponent_rating": opponent_rating,
        "result": result,
        "margin": margin,
        "move_count": game.move_count,
        "end_type": game.end_type,
        "played_at": played_at,
        "komi": game.komi,
        "board_size": game.size,
        "matched_name": game.my_color(settings.my_player_name) is not None,
    }


def title_for(meta: dict, game: Game) -> str:
    """「2026-08-18 黒 vs :Go9Bot(985) ●40目負」形式に整形する。"""
    date = (meta.get("played_at") or "")[:10]
    color = "黒" if meta["my_color"] == "B" else "白"
    rating = f"({meta['opponent_rating']})" if meta.get("opponent_rating") else ""
    opponent = f"{meta.get('opponent_name') or '?'}{rating}"
    result = meta.get("result")
    margin = meta.get("margin")
    if result == "勝":
        mark = "○"
    elif result == "負":
        mark = "●"
    else:
        mark = "－"
    if margin is not None and game.end_type == "終局":
        tail = f"{mark}{abs(margin):g}目{result or ''}"
    elif result:
        tail = f"{mark}{game.end_type}{result}"
    else:
        tail = mark
    return f"{date} {color} vs {opponent} {tail}".strip()


def notion_properties(meta: dict, game: Game, settings: Settings, game_id: str = "") -> dict:
    props = {
        PROP_TITLE: N.title(title_for(meta, game)),
        PROP_DATE: N.date((meta.get("played_at") or "")[:10] or None),
        PROP_MY_COLOR: N.select("黒" if meta["my_color"] == "B" else "白"),
        PROP_OPPONENT: N.text(meta.get("opponent_name") or ""),
        PROP_OPP_RATING: N.number(meta.get("opponent_rating")),
        PROP_RESULT: N.select(meta.get("result")),
        PROP_MARGIN: N.number(meta.get("margin")),
        PROP_MOVES: N.number(meta.get("move_count")),
        PROP_END_TYPE: N.select(meta.get("end_type")),
        PROP_STATUS: N.select(STATUS_PENDING),
    }
    if game_id and settings.review_base_url:
        props[PROP_REVIEW_URL] = N.url(settings.review_url(game_id))
    return props


# ---------------------------------------------------------------- 同期


def sync_from_notion(
    db: Database,
    client: NotionClient,
    settings: Settings,
    log: Logger = lambda _m: None,
    include_old_db: bool = True,
) -> IngestReport:
    """棋譜DB（＋当面は旧DB）から未取り込みの棋譜を取り込む。"""
    report = IngestReport()

    if settings.kifu_ds_id:
        _sync_data_source(
            db, client, settings, settings.kifu_ds_id, report, log, is_old=False
        )
    else:
        report.errors.append("KIFU_DS_ID が未設定です")

    # 共有先を間違えたときの受け皿として旧DBも監視する（要件 FR-01）
    if include_old_db and settings.old_ds_id:
        _sync_data_source(
            db, client, settings, settings.old_ds_id, report, log, is_old=True
        )

    db.set_sync(
        "notion_kifu",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "; ".join(report.errors)[:500],
    )
    return report


def _sync_data_source(
    db: Database,
    client: NotionClient,
    settings: Settings,
    data_source_id: str,
    report: IngestReport,
    log: Logger,
    is_old: bool,
) -> None:
    for page in client.iter_pages(data_source_id):
        page_id = page.get("id", "")
        title = N.page_title(page) or page_id
        try:
            text = client.page_text(page_id)
            sgf_text = extract_sgf(text)
            if not sgf_text:
                if not is_old:
                    report.needs_check.append((title, page.get("url", "")))
                    _mark_status(client, page_id, STATUS_CHECK, log)
                continue

            digest = sgf_hash(sgf_text)
            if db.game_exists(digest):
                report.skipped_duplicate += 1
                continue

            game = parse_game(sgf_text)
            meta = build_meta(game, settings, fallback_date=page.get("created_time", ""))
            if not meta["matched_name"]:
                log(f"注意: プレイヤー名 '{settings.my_player_name}' が一致しません（{title}）")

            game_id = db.next_game_id()
            db.insert_game(
                {
                    "id": game_id,
                    "notion_page_id": page_id,
                    "sgf_hash": digest,
                    "sgf": sgf_text,
                    "played_at": meta["played_at"],
                    "my_color": meta["my_color"],
                    "opponent_name": meta["opponent_name"],
                    "opponent_rating": meta["opponent_rating"],
                    "result": meta["result"],
                    "margin": meta["margin"],
                    "move_count": meta["move_count"],
                    "end_type": meta["end_type"],
                    "komi": meta["komi"],
                    "board_size": meta["board_size"],
                    "status": STATUS_PENDING,
                }
            )
            report.imported.append(game_id)
            if is_old:
                report.from_old_db.append(title)
                log(f"旧DBに追加されています: {title}")

            # 受信箱側のプロパティを埋める（ユーザーの手入力はゼロ）
            try:
                client.update_page(
                    page_id, notion_properties(meta, game, settings, game_id)
                )
            except Exception as exc:
                report.errors.append(f"{title}: プロパティ更新に失敗 ({exc})")
                db.enqueue_writeback("game_props", game_id, {"page_id": page_id})

        except Exception as exc:  # 1 ページの失敗で同期全体を止めない
            report.errors.append(f"{title}: {exc}")
            log(f"エラー: {title}: {exc}")


def _mark_status(client: NotionClient, page_id: str, status: str, log: Logger) -> None:
    try:
        client.update_page(page_id, {PROP_STATUS: N.select(status)})
    except Exception as exc:
        log(f"ステータス更新に失敗: {page_id}: {exc}")


# ---------------------------------------------------------------- ローカル取り込み


def import_sgf_file(db: Database, path: Path, settings: Settings) -> Optional[str]:
    """ローカルの .sgf を取り込む（Notion を介さない検証用）。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    sgf_text = extract_sgf(raw) or raw.strip()
    digest = sgf_hash(sgf_text)
    if db.game_exists(digest):
        return None
    game = parse_game(sgf_text)
    meta = build_meta(
        game,
        settings,
        fallback_date=datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
    )
    game_id = db.next_game_id()
    db.insert_game(
        {
            "id": game_id,
            "notion_page_id": None,
            "sgf_hash": digest,
            "sgf": sgf_text,
            "played_at": meta["played_at"],
            "my_color": meta["my_color"],
            "opponent_name": meta["opponent_name"],
            "opponent_rating": meta["opponent_rating"],
            "result": meta["result"],
            "margin": meta["margin"],
            "move_count": meta["move_count"],
            "end_type": meta["end_type"],
            "komi": meta["komi"],
            "board_size": meta["board_size"],
            "status": STATUS_PENDING,
        }
    )
    return game_id
