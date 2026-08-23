"""Notion API クライアント（API バージョン 2025-09-03）。

重要: 2025-09-03 ではデータベースは「データソース」のコンテナになった。
行の取得は POST /v1/data_sources/{id}/query、ページ作成の parent は
{"type":"data_source_id","data_source_id":...}。databases.query は使わない。

標準ライブラリだけで動かす（pip 不要）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Iterator, Optional

BASE = "https://api.notion.com/v1"
RATE_LIMIT_SLEEP = 0.4       # 概ね毎秒 3 リクエストが上限
MAX_RETRIES = 4
BLOCK_TEXT_LIMIT = 1900      # リッチテキスト 1 ブロックの上限は 2,000 文字


class NotionError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Notion API エラー {status}: {body}")
        self.status = status
        self.body = body


class NotionClient:
    def __init__(self, token: str, version: str = "2025-09-03", timeout: int = 30) -> None:
        if not token:
            raise ValueError("NOTION_TOKEN が設定されていません")
        self.token = token
        self.version = version
        self.timeout = timeout

    # ---------------------------------------------------------- 低レベル

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.version,
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{BASE}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            req = urllib.request.Request(url, data=data, method=method, headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                time.sleep(RATE_LIMIT_SLEEP)
                return payload
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 or exc.code >= 500:
                    retry_after = float(exc.headers.get("Retry-After") or (2 ** attempt))
                    time.sleep(max(retry_after, RATE_LIMIT_SLEEP))
                    last_error = NotionError(exc.code, text)
                    continue
                raise NotionError(exc.code, text) from exc
            except urllib.error.URLError as exc:
                last_error = exc
                time.sleep(2 ** attempt)
        raise last_error or NotionError(0, "不明なエラー")

    # ---------------------------------------------------------- データベース

    def retrieve_database(self, database_id: str) -> dict:
        return self.request("GET", f"/databases/{database_id}")

    def data_source_ids(self, database_id: str) -> list[dict]:
        """データベースオブジェクトの data_sources 配列を返す。"""
        db = self.retrieve_database(database_id)
        return db.get("data_sources", [])

    def query_data_source(
        self,
        data_source_id: str,
        start_cursor: Optional[str] = None,
        page_size: int = 100,
        filter_: Optional[dict] = None,
        sorts: Optional[list] = None,
    ) -> dict:
        body: dict[str, Any] = {"page_size": page_size}
        if start_cursor:
            body["start_cursor"] = start_cursor
        if filter_:
            body["filter"] = filter_
        if sorts:
            body["sorts"] = sorts
        return self.request("POST", f"/data_sources/{data_source_id}/query", body)

    def iter_pages(
        self,
        data_source_id: str,
        filter_: Optional[dict] = None,
        sorts: Optional[list] = None,
    ) -> Iterator[dict]:
        """has_more / next_cursor を処理して全ページを列挙する。"""
        cursor: Optional[str] = None
        while True:
            page = self.query_data_source(data_source_id, cursor, filter_=filter_, sorts=sorts)
            for result in page.get("results", []):
                yield result
            if not page.get("has_more"):
                return
            cursor = page.get("next_cursor")
            if not cursor:
                return

    # ---------------------------------------------------------- ページ

    def retrieve_page(self, page_id: str) -> dict:
        return self.request("GET", f"/pages/{page_id}")

    def create_page(
        self,
        data_source_id: str,
        properties: dict,
        children: Optional[list] = None,
    ) -> dict:
        body: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        if children:
            body["children"] = children
        return self.request("POST", "/pages", body)

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self.request("PATCH", f"/pages/{page_id}", {"properties": properties})

    # ---------------------------------------------------------- ブロック

    def iter_blocks(self, block_id: str) -> Iterator[dict]:
        cursor: Optional[str] = None
        while True:
            path = f"/blocks/{block_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            page = self.request("GET", path)
            for block in page.get("results", []):
                yield block
            if not page.get("has_more"):
                return
            cursor = page.get("next_cursor")
            if not cursor:
                return

    def page_text(self, page_id: str, max_depth: int = 2) -> str:
        """ページ内の全ブロックのテキストを連結する。

        SGF が複数ブロックに分割されている可能性があるため、必ず連結して
        から抽出する（要件 7.1 の注意事項）。
        """
        parts: list[str] = []

        def walk(bid: str, depth: int) -> None:
            for block in self.iter_blocks(bid):
                parts.append(block_text(block))
                if block.get("has_children") and depth < max_depth:
                    walk(block["id"], depth + 1)

        walk(page_id, 0)
        return "".join(parts)


# -------------------------------------------------------------- ヘルパ

_RICH_TEXT_TYPES = (
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do",
    "toggle", "quote", "callout", "code",
)


def block_text(block: dict) -> str:
    btype = block.get("type", "")
    payload = block.get(btype) or {}
    if btype in _RICH_TEXT_TYPES:
        return rich_text_to_plain(payload.get("rich_text", []))
    return ""


def rich_text_to_plain(rich_text: list) -> str:
    return "".join(rt.get("plain_text", "") for rt in rich_text or [])


def prop_plain(page: dict, name: str) -> str:
    """プロパティの値を素朴に文字列化する（title / rich_text / select 等）。"""
    prop = (page.get("properties") or {}).get(name)
    if not prop:
        return ""
    ptype = prop.get("type")
    if ptype == "title":
        return rich_text_to_plain(prop.get("title", []))
    if ptype == "rich_text":
        return rich_text_to_plain(prop.get("rich_text", []))
    if ptype == "select":
        sel = prop.get("select")
        return sel.get("name", "") if sel else ""
    if ptype == "multi_select":
        return ",".join(s.get("name", "") for s in prop.get("multi_select", []))
    if ptype == "number":
        value = prop.get("number")
        return "" if value is None else str(value)
    if ptype == "date":
        date = prop.get("date")
        return date.get("start", "") if date else ""
    if ptype == "url":
        return prop.get("url") or ""
    return ""


def page_title(page: dict) -> str:
    for name, prop in (page.get("properties") or {}).items():
        if prop.get("type") == "title":
            return rich_text_to_plain(prop.get("title", []))
    return ""


# -------------------------------------------------------------- プロパティ生成

def title(value: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": value[:2000]}}]}


def text(value: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": (value or "")[:2000]}}]}


def number(value: Optional[float]) -> dict:
    return {"number": None if value is None else float(value)}


def select(value: Optional[str]) -> dict:
    return {"select": {"name": value} if value else None}


def multi_select(values: list[str]) -> dict:
    seen: list[str] = []
    for v in values or []:
        if v and v not in seen:
            seen.append(v)
    return {"multi_select": [{"name": v} for v in seen[:100]]}


def date(value: Optional[str]) -> dict:
    return {"date": {"start": value} if value else None}


def url(value: Optional[str]) -> dict:
    return {"url": value or None}


def paragraph_blocks(content: str, limit: int = BLOCK_TEXT_LIMIT) -> list[dict]:
    """長文を 2,000 文字未満の段落ブロックに分割する。"""
    content = content or ""
    chunks = [content[i:i + limit] for i in range(0, len(content), limit)] or [""]
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
        }
        for chunk in chunks
    ]
