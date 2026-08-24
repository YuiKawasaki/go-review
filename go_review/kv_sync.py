"""Cloudflare KV に溜まった PWA からの回答を取り込む。

PWA（Cloudflare Pages）は自宅の外からでも使える一方、正のデータベースは
Surface のローカル SQLite にしかない。そこで回答はまず Cloudflare KV に
一時保管してもらい（web/functions/api/*.js）、夜間バッチがここで読み出して
ローカル DB へ反映する。取り込めたキーは KV から削除する（受信箱の運用）。

wrangler CLI（Node.js 版）を子プロセスとして呼び出す。wrangler が無い環境
（Node 未導入など）では静かにスキップする。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .config import Settings
from .db import Database
from .learning import record_tsumego_answer, record_tsumego_session, set_note
from .srs import record_answer

Logger = Callable[[str], None]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BINDING = "ANSWERS_KV"


def _wrangler_cmd() -> Optional[str]:
    exe = shutil.which("wrangler") or shutil.which("wrangler.cmd")
    if exe:
        return exe
    # PATH が通っていないタスクスケジューラ環境向けのフォールバック
    import os

    candidate = Path(os.environ.get("APPDATA", "")) / "npm" / "wrangler.cmd"
    return str(candidate) if candidate.exists() else None


def _run(args: list[str]) -> Optional[str]:
    exe = _wrangler_cmd()
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _list_keys() -> list[str]:
    out = _run(["kv", "key", "list", f"--binding={BINDING}", "--remote"])
    if not out:
        return []
    try:
        entries = json.loads(out)
    except ValueError:
        return []
    return [e["name"] for e in entries if "name" in e]


def _get_value(key: str) -> Optional[dict]:
    out = _run(["kv", "key", "get", key, f"--binding={BINDING}", "--remote"])
    if out is None:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def _delete_key(key: str) -> None:
    _run(["kv", "key", "delete", key, f"--binding={BINDING}", "--remote"])


def pull_answers(db: Database, settings: Settings, log: Logger = lambda _m: None) -> dict:
    """KV の受信箱を読み、ローカル DB へ反映する。"""
    if _wrangler_cmd() is None:
        return {"skipped": "wrangler が見つかりません"}

    keys = _list_keys()
    counts = {"answer": 0, "tsumego": 0, "tsumego_answer": 0, "note": 0, "errors": 0}
    for key in keys:
        kind = key.split(":", 1)[0]
        payload = _get_value(key)
        if payload is None:
            log(f"KVの値を読めませんでした（スキップ）: {key}")
            counts["errors"] += 1
            continue
        try:
            if kind == "answer":
                record_answer(
                    db,
                    problem_id=payload["problem_id"],
                    answer_coord=payload.get("coord", ""),
                    think_seconds=float(payload.get("seconds") or 0),
                    settings=settings,
                    hint_used=bool(payload.get("hint_used")),
                )
            elif kind == "tsumego":
                record_tsumego_session(
                    db,
                    solved=int(payload.get("solved") or 0),
                    wrong=int(payload.get("wrong") or 0),
                    themes=payload.get("themes") or [],
                    source=payload.get("source") or "",
                    on_date=payload.get("date"),
                )
            elif kind == "tsumego_answer":
                record_tsumego_answer(
                    db,
                    tsumego_id=payload["tsumego_id"],
                    is_correct=bool(payload.get("is_correct")),
                    settings=settings,
                    seconds=float(payload.get("seconds") or 0),
                    hint_used=bool(payload.get("hint_used")),
                )
            elif kind == "note":
                set_note(db, payload["date"], payload.get("note", ""))
            else:
                log(f"未知の種類のKV項目（スキップ）: {key}")
                continue
            _delete_key(key)
            counts[kind] = counts.get(kind, 0) + 1
        except Exception as exc:
            log(f"KV取り込みに失敗（次回リトライ）: {key}: {exc}")
            counts["errors"] += 1
    return counts
