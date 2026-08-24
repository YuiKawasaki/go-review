"""コマンドライン入口。タスクスケジューラからは `run` を呼ぶ。

  python -m go_review run          取り込み → 解析 → 書き出し → 書き戻し
  python -m go_review sync         Notion から取り込むだけ
  python -m go_review analyze      未解析のキューを処理
  python -m go_review export       PWA 用 JSON を書き出す
  python -m go_review status       未解析件数などの状況
  python -m go_review serve        検討モード用ローカルサーバ（自宅Wi-Fi限定）
  python -m go_review doctor       環境の自己診断
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import Settings, load_settings
from .db import Database
from .katago import get_engine, is_stub


# ---------------------------------------------------------------- ログ


class Log:
    """日付別のログファイル。通知やポップアップは出さない（要件 5.4）。"""

    def __init__(self, settings: Settings, quiet: bool = False) -> None:
        settings.ensure_dirs()
        stamp = datetime.now().strftime("%Y-%m-%d")
        self.path = settings.log_dir / f"{stamp}.log"
        self.quiet = quiet

    def __call__(self, message: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')} {message}"
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        if not self.quiet:
            print(line, flush=True)


def _notion(settings: Settings):
    from .notion import NotionClient

    return NotionClient(settings.notion_token, settings.notion_version)


# ---------------------------------------------------------------- 各コマンド


def cmd_sync(settings: Settings, log: Log, args) -> int:
    from .ingest import sync_from_notion

    if not settings.notion_token:
        log("NOTION_TOKEN が未設定です。.env を確認してください。")
        return 2
    with Database(settings.db_path) as db:
        report = sync_from_notion(db, _notion(settings), settings, log)
    log(report.summary())
    for title, url in report.needs_check:
        log(f"  要確認: {title} {url}")
    return 0


def cmd_analyze(settings: Settings, log: Log, args) -> int:
    from .analysis import analyze_game
    from .explain import ClaudeClient

    deadline = time.monotonic() + settings.max_runtime_minutes * 60
    client = ClaudeClient(settings)
    if not client.available:
        log(f"解説文はテンプレート生成になります（{client.unavailable_reason}）")

    with Database(settings.db_path) as db:
        pending = db.unanalyzed_games()
        if args.game_id:
            pending = [r for r in pending if r["id"] == args.game_id]
        if not pending:
            log("未解析の棋譜はありません。")
            return 0

        try:
            engine = get_engine(settings, allow_stub=args.allow_stub)
        except FileNotFoundError as exc:
            log(str(exc))
            return 2
        if is_stub(engine):
            log("警告: スタブエンジンで実行します。結果は学習に使えません。")

        log(f"{len(pending)} 局を解析します（上限 {settings.max_runtime_minutes} 分）")
        try:
            for row in pending:
                if time.monotonic() > deadline:
                    log("上限時間に達しました。残りは次回に回します。")
                    break
                started = time.monotonic()
                log(f"--- {row['id']} を解析中")
                try:
                    result = analyze_game(
                        db=db,
                        engine=engine,
                        game_id=row["id"],
                        sgf_text=row["sgf"],
                        my_color=row["my_color"] or "B",
                        settings=settings,
                        deadline=deadline,
                        log=log,
                        client=client,
                    )
                except Exception as exc:
                    log(f"解析に失敗しました: {row['id']}: {exc}")
                    log(traceback.format_exc())
                    continue
                elapsed = time.monotonic() - started
                log(
                    f"{row['id']}: 悪手 {len(result.bad_moves)} 件 / "
                    f"問題 {len(result.problems)} 問 / {elapsed / 60:.1f} 分"
                )
        finally:
            engine.close()
    return 0


def cmd_export(settings: Settings, log: Log, args) -> int:
    from .export import export_all

    with Database(settings.db_path) as db:
        out = export_all(db, settings)
    log(f"書き出しました: {out}")
    return 0


def cmd_writeback(settings: Settings, log: Log, args) -> int:
    from .writeback import flush_queue, pull_notes, push_daily_log, push_game

    if not settings.notion_token:
        log("NOTION_TOKEN が未設定のため書き戻しをスキップします。")
        return 0
    client = _notion(settings)
    today = datetime.now(timezone.utc).date().isoformat()
    with Database(settings.db_path) as db:
        rows = db.query(
            "SELECT id FROM games WHERE status = '解析済' AND notion_page_id IS NOT NULL"
        )
        pushed = sum(1 for r in rows if push_game(db, client, settings, r["id"], log))
        push_daily_log(db, client, settings, today, log)
        notes = pull_notes(db, client, settings, log)
        retried = flush_queue(db, client, settings, log)
    log(f"書き戻し: 棋譜 {pushed} 件 / 気づき取り込み {notes} 件 / 再送 {retried} 件")
    return 0


def cmd_pull_answers(settings: Settings, log: Log, args) -> int:
    """Cloudflare KV に溜まった演習・詰碁の回答をローカル DB へ取り込む。"""
    from .kv_sync import pull_answers

    with Database(settings.db_path) as db:
        result = pull_answers(db, settings, log)
    if "skipped" in result:
        log(f"回答の取り込みをスキップしました: {result['skipped']}")
        return 0
    total = sum(v for k, v in result.items() if k != "errors")
    log(
        f"回答を取り込みました: 演習 {result['answer']} 件 / 詰碁記録 {result['tsumego']} 件 / "
        f"詰碁回答 {result['tsumego_answer']} 件 / 気づき {result['note']} 件"
        + (f" / エラー {result['errors']} 件" if result["errors"] else "")
    )
    return 0


def cmd_run(settings: Settings, log: Log, args) -> int:
    """夜間バッチの本体。取り込み → 解析 → 書き出し → 書き戻し。"""
    log("=== バッチ開始 ===")
    if settings.notion_token:
        cmd_sync(settings, log, args)
    else:
        log("NOTION_TOKEN が未設定のため取り込みをスキップします。")
    cmd_pull_answers(settings, log, args)
    cmd_analyze(settings, log, args)
    cmd_export(settings, log, args)
    if settings.notion_token:
        cmd_writeback(settings, log, args)
    log("=== バッチ終了 ===")
    return 0


def cmd_status(settings: Settings, log: Log, args) -> int:
    from .srs import stats

    with Database(settings.db_path) as db:
        total = db.scalar("SELECT COUNT(*) FROM games") or 0
        done = db.scalar("SELECT COUNT(*) FROM games WHERE status='解析済'") or 0
        pending = db.unanalyzed_count()
        srs = stats(db, settings)
        sync = db.get_sync("notion_kifu")

    log(f"棋譜: {total} 局（解析済 {done} / 未解析 {pending}）")
    log(f"問題: {srs['total_problems']} 問（卒業 {srs['graduated']} / 本日 {srs['due_today']}）")
    if srs["accuracy_first"] is not None:
        log(f"初見正答率: {srs['accuracy_first']}%")
    log(f"最終同期: {sync['last_synced_at'] if sync else '未実施'}")
    log(f"DB: {settings.db_path}")
    return 0


def cmd_import(settings: Settings, log: Log, args) -> int:
    from .ingest import import_sgf_file

    paths = [Path(p) for p in args.paths]
    with Database(settings.db_path) as db:
        for path in paths:
            if not path.exists():
                log(f"見つかりません: {path}")
                continue
            game_id = import_sgf_file(db, path, settings)
            log(f"{path.name}: {game_id or '重複のためスキップ'}")
    return 0


def cmd_tsumego(settings: Settings, log: Log, args) -> int:
    from .learning import record_tsumego_session

    themes = [t.strip() for t in (args.themes or "").split(",") if t.strip()]
    with Database(settings.db_path) as db:
        record_tsumego_session(db, args.solved, args.wrong, themes, args.source or "")
    log(f"詰碁を記録しました: 解答 {args.solved} / 誤答 {args.wrong} / テーマ {themes}")
    return 0


def cmd_serve(settings: Settings, log: Log, args) -> int:
    from .server import serve

    log(f"検討モード用サーバを起動します（プライベートネットワークのみ）: ポート {settings.local_server_port}")
    serve(settings, log)
    return 0


def cmd_doctor(settings: Settings, log: Log, args) -> int:
    """環境の自己診断。Phase 0.5 の実測前に使う。"""
    import shutil

    log(f"Python: {sys.version.split()[0]}")
    log(f"データ置き場: {settings.data_dir}")
    free = shutil.disk_usage(settings.data_dir.anchor or "C:\\").free / (1024 ** 3)
    log(f"ディスク空き: {free:.1f} GB" + ("  ← 10GB を下回っています" if free < 10 else ""))
    log(f"KataGo: {'あり ' + settings.katago_exe if settings.katago_available else 'なし（スタブのみ）'}")
    log(f"Notion トークン: {'設定済み' if settings.notion_token else '未設定'}")
    log(f"棋譜データソース: {settings.kifu_ds_id or '未設定'}")

    from .explain import ClaudeClient

    client = ClaudeClient(settings)
    log(f"Claude API: {'利用可' if client.available else '利用不可（' + client.unavailable_reason + '）'}")
    log(f"プレイヤー名: {settings.my_player_name}")
    try:
        with Database(settings.db_path) as db:
            log(f"DB: OK（棋譜 {db.scalar('SELECT COUNT(*) FROM games')} 局）")
    except Exception as exc:
        log(f"DB: 異常 {exc}")
    return 0


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="go_review", description="囲碁 棋譜レビュー")
    parser.add_argument("--quiet", action="store_true", help="標準出力へ出さない")
    parser.add_argument("--env", type=Path, default=None, help=".env のパス")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="バッチ一式")
    p_run.add_argument("--game-id", default=None)
    p_run.add_argument("--allow-stub", action="store_true", help="KataGo 無しでも動かす")
    p_run.set_defaults(func=cmd_run)

    p_sync = sub.add_parser("sync", help="Notion から取り込む")
    p_sync.set_defaults(func=cmd_sync)

    p_an = sub.add_parser("analyze", help="未解析の棋譜を解析する")
    p_an.add_argument("--game-id", default=None)
    p_an.add_argument("--allow-stub", action="store_true")
    p_an.set_defaults(func=cmd_analyze)

    p_ex = sub.add_parser("export", help="PWA 用 JSON を書き出す")
    p_ex.set_defaults(func=cmd_export)

    p_wb = sub.add_parser("writeback", help="Notion へ書き戻す")
    p_wb.set_defaults(func=cmd_writeback)

    p_pa = sub.add_parser("pull-answers", help="Cloudflare KV の回答をローカルDBへ取り込む")
    p_pa.set_defaults(func=cmd_pull_answers)

    p_st = sub.add_parser("status", help="状況を表示")
    p_st.set_defaults(func=cmd_status)

    p_im = sub.add_parser("import", help="ローカルの SGF を取り込む")
    p_im.add_argument("paths", nargs="+")
    p_im.set_defaults(func=cmd_import)

    p_ts = sub.add_parser("tsumego", help="詰碁セッションを記録")
    p_ts.add_argument("--solved", type=int, required=True)
    p_ts.add_argument("--wrong", type=int, default=0)
    p_ts.add_argument("--themes", default="")
    p_ts.add_argument("--source", default="")
    p_ts.set_defaults(func=cmd_tsumego)

    p_sv = sub.add_parser("serve", help="検討モード用ローカルサーバ")
    p_sv.set_defaults(func=cmd_serve)

    p_dr = sub.add_parser("doctor", help="環境の自己診断")
    p_dr.set_defaults(func=cmd_doctor)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.env)
    settings.ensure_dirs()
    log = Log(settings, quiet=args.quiet)
    if not hasattr(args, "allow_stub"):
        args.allow_stub = False
    if not hasattr(args, "game_id"):
        args.game_id = None
    try:
        return args.func(settings, log, args)
    except KeyboardInterrupt:
        log("中断しました。次回起動時に続きから処理します。")
        return 130
    except Exception as exc:
        log(f"想定外のエラー: {exc}")
        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
