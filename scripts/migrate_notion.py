"""Phase 0: 旧データベース → 新棋譜DB への移行スクリプト。

  python scripts/migrate_notion.py            dry-run（既定・書き込みなし）
  python scripts/migrate_notion.py --execute  実際に移行する

安全側の設計:
  - 既定は dry-run。--execute を明示しない限り一切書き込まない
  - 移行元のページは読むだけ。削除も編集もしない
  - 処理済みハッシュを processed.json に逐次追記し、再実行は続きから
  - 例外が出ても全体を止めず、そのページをスキップして続行する
  - API バージョンは 2025-09-03。行の取得は data_sources エンドポイント
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from go_review import notion as N                      # noqa: E402
from go_review.config import load_settings             # noqa: E402
from go_review.ingest import build_meta, notion_properties  # noqa: E402
from go_review.notion import NotionClient              # noqa: E402
from go_review.sgf import extract_sgf, parse_game, sgf_hash  # noqa: E402


def load_processed(path: Path) -> dict:
    if not path.exists():
        return {"hashes": [], "pages": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("hashes", [])
        data.setdefault("pages", [])
        return data
    except ValueError:
        return {"hashes": [], "pages": []}


def save_processed(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def existing_hashes(client: NotionClient, kifu_ds_id: str) -> set[str]:
    """移行先にすでにある SGF のハッシュを集める（二重登録の防止）。"""
    found: set[str] = set()
    for page in client.iter_pages(kifu_ds_id):
        try:
            text = client.page_text(page["id"])
            sgf_text = extract_sgf(text)
            if sgf_text:
                found.add(sgf_hash(sgf_text))
        except Exception:
            continue
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Notion 棋譜の移行（既定は dry-run）")
    parser.add_argument("--execute", action="store_true", help="実際に書き込む")
    parser.add_argument("--limit", type=int, default=0, help="処理件数の上限（0 は無制限）")
    parser.add_argument("--state", type=Path, default=None, help="processed.json のパス")
    args = parser.parse_args(argv)

    settings = load_settings()
    if not settings.notion_token:
        print("NOTION_TOKEN が未設定です。.env を確認してください。")
        return 2
    if not settings.old_ds_id or not settings.kifu_ds_id:
        print("OLD_DS_ID と KIFU_DS_ID の両方が必要です。")
        return 2

    settings.ensure_dirs()
    state_path = args.state or settings.queue_path
    state = load_processed(state_path)
    processed_hashes = set(state["hashes"])

    client = NotionClient(settings.notion_token, settings.notion_version)

    mode = "本実行" if args.execute else "dry-run（書き込みません）"
    print(f"=== 移行 {mode} ===")
    print("移行先の既存棋譜を確認しています…")
    already = existing_hashes(client, settings.kifu_ds_id) | processed_hashes
    print(f"  既存 {len(already)} 件")

    migrated = 0
    skipped_no_sgf: list[tuple[str, str]] = []
    skipped_dup = 0
    errors: list[str] = []

    for page in client.iter_pages(settings.old_ds_id):
        if args.limit and migrated >= args.limit:
            print(f"上限 {args.limit} 件に達したため停止します。")
            break

        page_id = page.get("id", "")
        title = N.page_title(page) or page_id
        try:
            text = client.page_text(page_id)
            sgf_text = extract_sgf(text)
            if not sgf_text:
                skipped_no_sgf.append((title, page.get("url", "")))
                continue

            digest = sgf_hash(sgf_text)
            if digest in already:
                skipped_dup += 1
                continue

            game = parse_game(sgf_text)
            # 囲碁クエストの SGF に DT は入っていない。移行元（日報DB）の
            # 「日付」プロパティが実際の対局日なので、それを最優先で使う。
            fallback = (
                N.prop_plain(page, "日付")
                or page.get("created_time", "")
            )
            meta = build_meta(game, settings, fallback_date=fallback)
            props = notion_properties(meta, game, settings)

            if args.execute:
                client.create_page(
                    settings.kifu_ds_id,
                    props,
                    children=N.paragraph_blocks(sgf_text),
                )
                already.add(digest)
                state["hashes"].append(digest)
                state["pages"].append({"title": title, "source_page_id": page_id})
                save_processed(state_path, state)
            migrated += 1
            print(f"  {'移行' if args.execute else '対象'}: {title} "
                  f"（{meta['move_count']}手 / {meta.get('result') or '結果不明'}）")

        except Exception as exc:  # 1 ページの失敗で全体を止めない
            errors.append(f"{title}: {exc}")
            print(f"  スキップ（エラー）: {title}: {exc}")

    print("\n=== レポート ===")
    print(f"{'移行した' if args.execute else '移行対象の'}棋譜: {migrated} 件")
    print(f"重複スキップ: {skipped_dup} 件")
    print(f"SGF が見つからずスキップ: {len(skipped_no_sgf)} 件")
    for title, url in skipped_no_sgf[:50]:
        print(f"  - {title} {url}")
    if len(skipped_no_sgf) > 50:
        print(f"  … 他 {len(skipped_no_sgf) - 50} 件")
    if errors:
        print(f"エラー: {len(errors)} 件")
        for line in errors[:20]:
            print(f"  - {line}")
    if not args.execute:
        print("\n書き込みは行っていません。問題なければ --execute を付けて再実行してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
