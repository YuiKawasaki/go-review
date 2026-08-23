"""ローカル DB（SQLite）。要件 7.3 のテーブル定義。

学習記録のマスタはここ。Notion は閲覧用のミラー（FR-12）。
1手ごとにコミットして中断耐性を確保する（FR-03）。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS games (
    id              TEXT PRIMARY KEY,
    notion_page_id  TEXT,
    sgf_hash        TEXT UNIQUE NOT NULL,
    sgf             TEXT NOT NULL,
    played_at       TEXT,
    my_color        TEXT,
    opponent_name   TEXT,
    opponent_rating INTEGER,
    result          TEXT,
    margin          REAL,
    move_count      INTEGER,
    end_type        TEXT,
    komi            REAL,
    board_size      INTEGER DEFAULT 9,
    status          TEXT DEFAULT '未解析',
    analyzed_at     TEXT,
    losing_move_no  INTEGER,
    main_tags       TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS moves (
    game_id         TEXT NOT NULL,
    move_no         INTEGER NOT NULL,
    color           TEXT,
    coord           TEXT,
    winrate_before  REAL,
    winrate_after   REAL,
    score_before    REAL,
    score_after     REAL,
    delta           REAL,
    best_move       TEXT,
    candidates      TEXT,
    visits          INTEGER,
    pass_no         INTEGER DEFAULT 1,
    PRIMARY KEY (game_id, move_no)
);

CREATE TABLE IF NOT EXISTS bad_moves (
    game_id     TEXT NOT NULL,
    move_no     INTEGER NOT NULL,
    severity    TEXT NOT NULL,
    delta       REAL,
    tags        TEXT,
    PRIMARY KEY (game_id, move_no)
);

CREATE TABLE IF NOT EXISTS problems (
    id              TEXT PRIMARY KEY,
    game_id         TEXT NOT NULL,
    move_no         INTEGER NOT NULL,
    position_sgf    TEXT,
    player_to_move  TEXT,
    actual_move     TEXT,
    actual_delta    REAL,
    correct_moves   TEXT,
    tags            TEXT,
    hints           TEXT,
    explanation     TEXT,
    difficulty      INTEGER,
    position_key    TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id      TEXT NOT NULL,
    reviewed_at     TEXT NOT NULL,
    answer_coord    TEXT,
    is_correct      INTEGER,
    verdict         TEXT,
    think_seconds   REAL,
    hint_used       INTEGER DEFAULT 0,
    streak          INTEGER DEFAULT 0,
    next_due_at     TEXT
);

CREATE TABLE IF NOT EXISTS problem_state (
    problem_id  TEXT PRIMARY KEY,
    streak      INTEGER DEFAULT 0,
    next_due_at TEXT,
    graduated   INTEGER DEFAULT 0,
    last_result TEXT
);

CREATE TABLE IF NOT EXISTS variations (
    game_id     TEXT NOT NULL,
    move_no     INTEGER NOT NULL,
    branch_type TEXT NOT NULL,
    pv_moves    TEXT,
    pv_comments TEXT,
    end_winrate REAL,
    end_score   REAL,
    visits      INTEGER,
    PRIMARY KEY (game_id, move_no, branch_type)
);

CREATE TABLE IF NOT EXISTS tsumego (
    id          TEXT PRIMARY KEY,
    source      TEXT,
    theme_tag   TEXT,
    image_path  TEXT,
    answer_note TEXT,
    streak      INTEGER DEFAULT 0,
    next_due_at TEXT,
    graduated   INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tsumego_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tsumego_id  TEXT,
    solved_at   TEXT,
    is_correct  INTEGER,
    seconds     REAL,
    hint_used   INTEGER DEFAULT 0,
    streak      INTEGER,
    next_due_at TEXT
);

CREATE TABLE IF NOT EXISTS tsumego_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    solved      INTEGER DEFAULT 0,
    wrong       INTEGER DEFAULT 0,
    themes      TEXT,
    source      TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_logs (
    date             TEXT PRIMARY KEY,
    games            INTEGER DEFAULT 0,
    tsumego_count    INTEGER DEFAULT 0,
    tsumego_wrong    INTEGER DEFAULT 0,
    problem_accuracy REAL,
    study_minutes    REAL DEFAULT 0,
    top_tag          TEXT,
    note             TEXT,
    synced_at        TEXT
);

CREATE TABLE IF NOT EXISTS analysis_cache (
    position_key TEXT PRIMARY KEY,
    visits       INTEGER,
    payload      TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_state (
    key            TEXT PRIMARY KEY,
    last_synced_at TEXT,
    last_error     TEXT
);

CREATE TABLE IF NOT EXISTS writeback_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    ref_id     TEXT NOT NULL,
    payload    TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    attempts   INTEGER DEFAULT 0,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_moves_game     ON moves(game_id);
CREATE INDEX IF NOT EXISTS idx_problems_game  ON problems(game_id);
CREATE INDEX IF NOT EXISTS idx_reviews_prob   ON reviews(problem_id);
CREATE INDEX IF NOT EXISTS idx_state_due      ON problem_state(next_due_at);
CREATE INDEX IF NOT EXISTS idx_games_played   ON games(played_at);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------- 基本

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def commit(self) -> None:
        self.conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.query_one(sql, params)
        return row[0] if row else None

    # ---------------------------------------------------------- 棋譜

    def game_exists(self, sgf_hash: str) -> bool:
        return self.scalar("SELECT 1 FROM games WHERE sgf_hash = ?", (sgf_hash,)) is not None

    def next_game_id(self) -> str:
        n = self.scalar("SELECT COUNT(*) FROM games") or 0
        while True:
            candidate = f"G-{n + 1:04d}"
            if self.scalar("SELECT 1 FROM games WHERE id = ?", (candidate,)) is None:
                return candidate
            n += 1

    def next_problem_id(self) -> str:
        n = self.scalar("SELECT COUNT(*) FROM problems") or 0
        while True:
            candidate = f"P-{n + 1:04d}"
            if self.scalar("SELECT 1 FROM problems WHERE id = ?", (candidate,)) is None:
                return candidate
            n += 1

    def insert_game(self, row: dict[str, Any]) -> None:
        keys = list(row)
        placeholders = ",".join("?" for _ in keys)
        self.execute(
            f"INSERT OR IGNORE INTO games ({','.join(keys)}) VALUES ({placeholders})",
            [row[k] for k in keys],
        )
        self.commit()

    def update_game(self, game_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ",".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE games SET {assignments} WHERE id = ?",
            list(fields.values()) + [game_id],
        )
        self.commit()

    def unanalyzed_games(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM games WHERE status IN ('未解析','解析中') ORDER BY played_at, id"
        )

    def unanalyzed_count(self) -> int:
        return self.scalar("SELECT COUNT(*) FROM games WHERE status = '未解析'") or 0

    # ---------------------------------------------------------- 手

    def save_move(self, game_id: str, move_no: int, **fields: Any) -> None:
        """1 手ぶんを upsert する。中断耐性のためここで毎回コミットする。"""
        fields["game_id"] = game_id
        fields["move_no"] = move_no
        keys = list(fields)
        placeholders = ",".join("?" for _ in keys)
        updates = ",".join(f"{k} = excluded.{k}" for k in keys if k not in ("game_id", "move_no"))
        self.execute(
            f"INSERT INTO moves ({','.join(keys)}) VALUES ({placeholders}) "
            f"ON CONFLICT(game_id, move_no) DO UPDATE SET {updates}",
            [fields[k] for k in keys],
        )
        self.commit()

    def moves_for(self, game_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM moves WHERE game_id = ? ORDER BY move_no", (game_id,))

    def analyzed_move_numbers(self, game_id: str, pass_no: int = 1) -> set[int]:
        rows = self.query(
            "SELECT move_no FROM moves WHERE game_id = ? AND pass_no >= ? AND winrate_after IS NOT NULL",
            (game_id, pass_no),
        )
        return {r["move_no"] for r in rows}

    # ---------------------------------------------------------- キャッシュ

    def cache_get(self, position_key: str, min_visits: int) -> Optional[dict]:
        row = self.query_one(
            "SELECT payload FROM analysis_cache WHERE position_key = ? AND visits >= ?",
            (position_key, min_visits),
        )
        return json.loads(row["payload"]) if row else None

    def cache_put(self, position_key: str, visits: int, payload: dict) -> None:
        self.execute(
            "INSERT INTO analysis_cache (position_key, visits, payload) VALUES (?,?,?) "
            "ON CONFLICT(position_key) DO UPDATE SET visits = excluded.visits, "
            "payload = excluded.payload WHERE excluded.visits > analysis_cache.visits",
            (position_key, visits, json.dumps(payload, ensure_ascii=False)),
        )

    # ---------------------------------------------------------- 同期状態

    def get_sync(self, key: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM sync_state WHERE key = ?", (key,))

    def set_sync(self, key: str, last_synced_at: str, last_error: str = "") -> None:
        self.execute(
            "INSERT INTO sync_state (key, last_synced_at, last_error) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET last_synced_at = excluded.last_synced_at, "
            "last_error = excluded.last_error",
            (key, last_synced_at, last_error),
        )
        self.commit()

    def enqueue_writeback(self, kind: str, ref_id: str, payload: dict) -> None:
        self.execute(
            "INSERT INTO writeback_queue (kind, ref_id, payload) VALUES (?,?,?)",
            (kind, ref_id, json.dumps(payload, ensure_ascii=False)),
        )
        self.commit()


@contextmanager
def open_db(path: Path) -> Iterator[Database]:
    db = Database(path)
    try:
        yield db
    finally:
        db.close()


def loads(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
