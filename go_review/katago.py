"""KataGo Analysis Engine のラッパ（FR-03）。

悪手の判定と正解手の決定は必ずこのエンジンが行う。LLM には決めさせない
（要件 2.3）。勝率の視点は overrideSettings で BLACK に固定し、
設定ファイルの差異で符号が反転しないようにしている。

KataGo が無い環境でもパイプライン全体を通せるよう、決定的なスタブ
エンジンを同梱する（スタブの結果は学習に使ってはいけない）。
"""
from __future__ import annotations

import hashlib
import json
import queue
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from .sgf import Coord, Game, coord_to_gtp, gtp_to_coord


@dataclass
class MoveInfo:
    """候補手 1 つ。勝率・地合いは常に黒視点（0〜100 / 目）。"""
    coord: Optional[Coord]
    gtp: str
    winrate_black: float
    score_lead_black: float
    visits: int
    order: int
    pv: list[str] = field(default_factory=list)

    def winrate_for(self, color: str) -> float:
        return self.winrate_black if color == "B" else 100.0 - self.winrate_black

    def score_for(self, color: str) -> float:
        return self.score_lead_black if color == "B" else -self.score_lead_black


@dataclass
class TurnAnalysis:
    """ある局面（turn 手が終わった時点）の解析結果。"""
    turn: int
    winrate_black: float
    score_lead_black: float
    visits: int
    moves: list[MoveInfo] = field(default_factory=list)
    ownership: list[float] = field(default_factory=list)

    def winrate_for(self, color: str) -> float:
        return self.winrate_black if color == "B" else 100.0 - self.winrate_black

    def score_for(self, color: str) -> float:
        return self.score_lead_black if color == "B" else -self.score_lead_black

    def best(self) -> Optional[MoveInfo]:
        return self.moves[0] if self.moves else None

    def find(self, coord: Optional[Coord]) -> Optional[MoveInfo]:
        for m in self.moves:
            if m.coord == coord:
                return m
        return None

    def to_dict(self) -> dict:
        return {
            "turn": self.turn,
            "winrate_black": self.winrate_black,
            "score_lead_black": self.score_lead_black,
            "visits": self.visits,
            "ownership": self.ownership,
            "moves": [
                {
                    "gtp": m.gtp,
                    "winrate_black": m.winrate_black,
                    "score_lead_black": m.score_lead_black,
                    "visits": m.visits,
                    "order": m.order,
                    "pv": m.pv,
                }
                for m in self.moves
            ],
        }

    @classmethod
    def from_dict(cls, data: dict, size: int) -> "TurnAnalysis":
        moves = []
        for m in data.get("moves", []):
            gtp = m.get("gtp", "")
            moves.append(
                MoveInfo(
                    coord=_safe_coord(gtp, size),
                    gtp=gtp,
                    winrate_black=m.get("winrate_black", 50.0),
                    score_lead_black=m.get("score_lead_black", 0.0),
                    visits=m.get("visits", 0),
                    order=m.get("order", 0),
                    pv=m.get("pv", []),
                )
            )
        return cls(
            turn=data.get("turn", 0),
            winrate_black=data.get("winrate_black", 50.0),
            score_lead_black=data.get("score_lead_black", 0.0),
            visits=data.get("visits", 0),
            moves=moves,
            ownership=data.get("ownership", []),
        )


def _safe_coord(gtp: str, size: int) -> Optional[Coord]:
    try:
        return gtp_to_coord(gtp, size)
    except Exception:
        return None


class Engine(Protocol):
    def analyze(
        self,
        game: Game,
        turns: list[int],
        max_visits: int,
        moves_override: Optional[list[tuple[str, Optional[Coord]]]] = None,
    ) -> dict[int, TurnAnalysis]:
        ...

    def close(self) -> None:
        ...


class KataGoEngine:
    """`katago analysis` を子プロセスとして起動し、JSON 行で対話する。"""

    def __init__(
        self,
        exe: str,
        model: str,
        config: str,
        threads: int = 2,
        timeout: float = 900.0,
    ) -> None:
        for label, path in (("KATAGO_EXE", exe), ("KATAGO_MODEL", model), ("KATAGO_CONFIG", config)):
            if not path or not Path(path).exists():
                raise FileNotFoundError(f"{label} が見つかりません: {path or '(未設定)'}")
        self.timeout = timeout
        self._lock = threading.Lock()
        self._counter = 0
        self.proc = subprocess.Popen(
            [exe, "analysis", "-config", config, "-model", model,
             "-override-config", f"numAnalysisThreads={threads}"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        # stdout.readline() は応答が来なければ永久に返らない。Windows のパイプは
        # select() できないので、読み取りは専用スレッドに任せてキュー経由で受け取り、
        # 本体側は timeout 付きで待つ（省電力の待機などで固まったまま朝を迎えないように）。
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            assert self.proc.stdout
            for line in self.proc.stdout:
                self._lines.put(line)
        except Exception:
            pass
        finally:
            self._lines.put(None)   # 応答の終わり（プロセス終了）

    def _next_id(self) -> str:
        self._counter += 1
        return f"q{self._counter}"

    def analyze(
        self,
        game: Game,
        turns: list[int],
        max_visits: int,
        moves_override: Optional[list[tuple[str, Optional[Coord]]]] = None,
    ) -> dict[int, TurnAnalysis]:
        if not turns:
            return {}
        size = game.size
        moves = moves_override if moves_override is not None else [
            (m.color, m.coord) for m in game.moves
        ]
        query = {
            "id": self._next_id(),
            "boardXSize": size,
            "boardYSize": size,
            "komi": game.komi,
            "rules": (game.rules or "chinese").lower(),
            "moves": [[color, coord_to_gtp(c, size)] for color, c in moves],
            "initialStones": (
                [["B", coord_to_gtp(c, size)] for c in game.setup_black]
                + [["W", coord_to_gtp(c, size)] for c in game.setup_white]
            ),
            "analyzeTurns": sorted(set(turns)),
            "maxVisits": max_visits,
            "includeOwnership": True,
            "includePolicy": False,
            # 視点を固定する。設定ファイル依存で符号が反転すると学習が有害になる。
            "overrideSettings": {"reportAnalysisWinratesAs": "BLACK"},
        }

        with self._lock:
            if self.proc.poll() is not None:
                raise RuntimeError("KataGo プロセスが終了しています")
            assert self.proc.stdin and self.proc.stdout
            self.proc.stdin.write(json.dumps(query) + "\n")
            self.proc.stdin.flush()

            expected = len(query["analyzeTurns"])
            results: dict[int, TurnAnalysis] = {}
            # 1 回の問い合わせで何十手ぶんも頼むので、全体にかける制限時間だと
            # 正常な解析まで打ち切ってしまう（実測でパス1に 29 分かかる局もある）。
            # 測るべきは「応答が途絶えたか」なので、1 件受け取るたびに待ち直す。
            while len(results) < expected:
                try:
                    line = self._lines.get(timeout=self.timeout)
                except queue.Empty:
                    raise TimeoutError(
                        f"KataGo が {self.timeout:.0f} 秒のあいだ何も返しませんでした"
                        f"（{len(results)}/{expected} 件受信済み）"
                    ) from None
                if line is None:
                    raise RuntimeError("KataGo からの応答が途絶しました")
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if payload.get("id") != query["id"]:
                    continue
                if "error" in payload:
                    raise RuntimeError(f"KataGo エラー: {payload['error']}")
                if "warning" in payload and "moveInfos" not in payload:
                    continue
                turn = payload.get("turnNumber")
                if turn is None:
                    continue
                results[turn] = _parse_turn(payload, size)
        return results

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def _parse_turn(payload: dict, size: int) -> TurnAnalysis:
    root = payload.get("rootInfo") or {}
    moves: list[MoveInfo] = []
    for info in payload.get("moveInfos") or []:
        gtp = info.get("move", "")
        moves.append(
            MoveInfo(
                coord=_safe_coord(gtp, size),
                gtp=gtp,
                winrate_black=float(info.get("winrate", 0.5)) * 100.0,
                score_lead_black=float(info.get("scoreLead", 0.0)),
                visits=int(info.get("visits", 0)),
                order=int(info.get("order", 0)),
                pv=list(info.get("pv") or []),
            )
        )
    moves.sort(key=lambda m: m.order)
    return TurnAnalysis(
        turn=int(payload.get("turnNumber", 0)),
        winrate_black=float(root.get("winrate", 0.5)) * 100.0,
        score_lead_black=float(root.get("scoreLead", 0.0)),
        visits=int(root.get("visits", 0)),
        moves=moves,
        ownership=[float(v) for v in (payload.get("ownership") or [])],
    )


class StubEngine:
    """KataGo 未導入でも配線を確認するための決定的ダミー。

    盤面から擬似乱数を作るだけで、囲碁的な意味はまったくない。
    本番解析に使うと誤った「正解手」を教えることになるため、
    analysis.engine には必ずスタブである旨を記録する。
    """

    is_stub = True

    def __init__(self, size: int = 9) -> None:
        self.size = size

    @staticmethod
    def _rand(seed: str) -> float:
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") / 0xFFFFFFFF

    def analyze(
        self,
        game: Game,
        turns: list[int],
        max_visits: int,
        moves_override: Optional[list[tuple[str, Optional[Coord]]]] = None,
    ) -> dict[int, TurnAnalysis]:
        size = game.size
        moves = moves_override if moves_override is not None else [
            (m.color, m.coord) for m in game.moves
        ]
        out: dict[int, TurnAnalysis] = {}
        for turn in sorted(set(turns)):
            seed = f"{game.sgf[:64]}|{turn}|{len(moves)}"
            winrate = 20.0 + self._rand(seed) * 60.0
            candidates: list[MoveInfo] = []
            occupied = {c for _, c in moves[:turn] if c is not None}
            free = [
                (col, row)
                for row in range(size)
                for col in range(size)
                if (col, row) not in occupied
            ]
            free.sort(key=lambda c: self._rand(f"{seed}|{c}"))
            for order, coord in enumerate(free[:5]):
                candidates.append(
                    MoveInfo(
                        coord=coord,
                        gtp=coord_to_gtp(coord, size),
                        winrate_black=max(0.0, min(100.0, winrate + (2 - order) * 1.5)),
                        score_lead_black=(winrate - 50.0) / 5.0,
                        visits=max(1, max_visits // (order + 2)),
                        order=order,
                        pv=[coord_to_gtp(c, size) for c in free[order:order + 6]],
                    )
                )
            out[turn] = TurnAnalysis(
                turn=turn,
                winrate_black=winrate,
                score_lead_black=(winrate - 50.0) / 5.0,
                visits=max_visits,
                moves=candidates,
                ownership=[0.0] * (size * size),
            )
        return out

    def close(self) -> None:
        return None


def get_engine(settings, allow_stub: bool = True) -> Engine:
    """設定から解析エンジンを作る。KataGo が無ければスタブ（許可時のみ）。"""
    if settings.katago_available:
        return KataGoEngine(
            exe=settings.katago_exe,
            model=settings.katago_model,
            config=settings.katago_config,
            threads=settings.katago_threads,
        )
    if not allow_stub:
        raise FileNotFoundError(
            "KataGo が見つかりません。.env の KATAGO_EXE / KATAGO_MODEL / KATAGO_CONFIG を確認してください。"
        )
    return StubEngine()


def is_stub(engine: Engine) -> bool:
    return getattr(engine, "is_stub", False)
