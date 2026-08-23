"""設定の読み込み（.env ＋ 環境変数）。

APIキーはコードにも配信データにも埋め込まない（非機能要件 セキュリティ）。
解析データの置き場所は既定で OneDrive の外に置く。SQLite を同期フォルダに
置くと衝突コピーが生まれ、マスタが壊れるため。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Optional[Path] = None) -> dict[str, str]:
    """.env を読んで os.environ に反映する（既存の環境変数を優先）。"""
    path = path or (PROJECT_ROOT / ".env")
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
        os.environ.setdefault(key, value)
    return values


def _default_data_dir() -> Path:
    override = os.environ.get("GOREVIEW_DATA_DIR")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "go-review"
    return PROJECT_ROOT / "data"


def _f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _i(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _b(key: str, default: bool) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # -------- 本人
    my_player_name: str = "wakame_han"

    # -------- Notion
    notion_token: str = ""
    notion_version: str = "2025-09-03"
    kifu_ds_id: str = ""
    log_ds_id: str = ""
    old_ds_id: str = ""
    kifu_db_id: str = ""

    # -------- 解析
    katago_exe: str = ""
    katago_model: str = ""
    katago_config: str = ""
    pass1_visits: int = 150
    pass2_visits: int = 1500
    quick_visits: int = 100          # 検討モード用（応答 1〜2 秒目標）
    katago_threads: int = 2          # 2コア4スレッド機では 2 固定（5.5）
    max_runtime_minutes: int = 120   # 1 回の実行は最大 2 時間で打ち切る

    # -------- 悪手判定（FR-04）
    dubious_threshold: float = 10.0  # 疑問手
    bad_threshold: float = 20.0      # 悪手
    critical_threshold: float = 30.0 # 敗着候補
    decided_low: float = 5.0         # これ未満は決着後として除外
    decided_high: float = 95.0
    max_problems_per_game: int = 3
    acceptable_delta: float = 5.0    # 許容手とみなす勝率差（FR-06）
    pv_max_moves: int = 10           # 変化図は 10 手まで（FR-08）

    # -------- 復習（FR-10）
    review_intervals: tuple[int, ...] = (1, 3, 7, 14)
    graduate_streak: int = 5
    daily_review_limit: int = 10
    tsumego_graduate_streak: int = 3

    # -------- Claude API
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5"
    claude_enabled: bool = True

    # -------- 配信
    data_dir: Path = field(default_factory=_default_data_dir)
    publish_dir: Optional[Path] = None
    publish_slug: str = ""           # URL 推測困難なパス
    review_base_url: str = ""        # PWA の公開 URL（レビューURL生成用）

    # -------- 検討モード用ローカルサーバ
    local_server_port: int = 8777

    @property
    def db_path(self) -> Path:
        return self.data_dir / "goreview.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def queue_path(self) -> Path:
        return self.data_dir / "processed.json"

    @property
    def claude_available(self) -> bool:
        return bool(self.anthropic_api_key) and self.claude_enabled

    @property
    def katago_available(self) -> bool:
        return bool(self.katago_exe) and Path(self.katago_exe).exists()

    def review_url(self, game_id: str) -> str:
        if not self.review_base_url:
            return ""
        return f"{self.review_base_url.rstrip('/')}/#/game/{game_id}"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def load_settings(env_path: Optional[Path] = None) -> Settings:
    load_dotenv(env_path)
    publish = os.environ.get("PUBLISH_DIR")
    s = Settings(
        my_player_name=os.environ.get("MY_PLAYER_NAME", "wakame_han"),
        notion_token=os.environ.get("NOTION_TOKEN", ""),
        notion_version=os.environ.get("NOTION_VERSION", "2025-09-03"),
        kifu_ds_id=os.environ.get("KIFU_DS_ID", ""),
        log_ds_id=os.environ.get("LOG_DS_ID", ""),
        old_ds_id=os.environ.get("OLD_DS_ID", ""),
        kifu_db_id=os.environ.get("KIFU_DB_ID", ""),
        katago_exe=os.environ.get("KATAGO_EXE", ""),
        katago_model=os.environ.get("KATAGO_MODEL", ""),
        katago_config=os.environ.get("KATAGO_CONFIG", ""),
        pass1_visits=_i("PASS1_VISITS", 150),
        pass2_visits=_i("PASS2_VISITS", 1500),
        quick_visits=_i("QUICK_VISITS", 100),
        katago_threads=_i("KATAGO_THREADS", 2),
        max_runtime_minutes=_i("MAX_RUNTIME_MINUTES", 120),
        dubious_threshold=_f("DUBIOUS_THRESHOLD", 10.0),
        bad_threshold=_f("BAD_THRESHOLD", 20.0),
        critical_threshold=_f("CRITICAL_THRESHOLD", 30.0),
        max_problems_per_game=_i("MAX_PROBLEMS_PER_GAME", 3),
        acceptable_delta=_f("ACCEPTABLE_DELTA", 5.0),
        daily_review_limit=_i("DAILY_REVIEW_LIMIT", 10),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        claude_model=os.environ.get("CLAUDE_MODEL", "claude-opus-5"),
        claude_enabled=_b("CLAUDE_ENABLED", True),
        data_dir=_default_data_dir(),
        publish_dir=Path(publish) if publish else None,
        publish_slug=os.environ.get("PUBLISH_SLUG", ""),
        review_base_url=os.environ.get("REVIEW_BASE_URL", ""),
        local_server_port=_i("LOCAL_SERVER_PORT", 8777),
    )
    return s
