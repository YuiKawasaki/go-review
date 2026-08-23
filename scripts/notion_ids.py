"""Step 4-3: データベースIDからデータソースIDを取得する。

  python scripts/notion_ids.py <DATABASE_ID> [<DATABASE_ID> ...]

データベースIDとデータソースIDは別物で、互いに置き換えて使えない。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from go_review.config import load_settings   # noqa: E402
from go_review.notion import NotionClient    # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    settings = load_settings()
    if not settings.notion_token:
        print("NOTION_TOKEN が未設定です。")
        return 2
    client = NotionClient(settings.notion_token, settings.notion_version)
    for database_id in argv:
        try:
            sources = client.data_source_ids(database_id)
        except Exception as exc:
            print(f"{database_id}: 取得に失敗しました: {exc}")
            continue
        print(f"データベース {database_id}")
        for source in sources:
            print(f"  データソースID: {source.get('id')}  名前: {source.get('name')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
