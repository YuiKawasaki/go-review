"""局面解析（FR-03）と 2 パス方式（要件 5.6）。

1 パス目: 全手を低 visits でスクリーニングし、勝率の落差を検出する。
2 パス目: 悪手候補の局面だけを高 visits で精査し、正解手と読み筋を確定する。

1 手ごとに DB へコミットするので、電源が落ちても途中から再開できる。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from typing import TYPE_CHECKING

from .config import Settings
from .db import Database, dumps
from .goban import board_at
from .katago import Engine, TurnAnalysis, is_stub
from .sgf import Game, coord_to_gtp, parse_game

# 1 局面あたり保存する候補手の数と、その読み筋の長さ。
CANDIDATE_COUNT = 8
CANDIDATE_PV_MOVES = 10

if TYPE_CHECKING:  # 実行時の循環 import を避ける
    from .explain import ClaudeClient

Logger = Callable[[str], None]


@dataclass
class AnalysisResult:
    game_id: str
    analyzed_turns: int
    bad_moves: list[dict]
    problems: list[str] = None  # type: ignore[assignment]
    truncated: bool = False   # 上限時間で打ち切ったか
    stub: bool = False

    def __post_init__(self) -> None:
        if self.problems is None:
            self.problems = []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def analyze_game(
    db: Database,
    engine: Engine,
    game_id: str,
    sgf_text: str,
    my_color: str,
    settings: Settings,
    deadline: Optional[float] = None,
    log: Logger = lambda _msg: None,
    client: Optional["ClaudeClient"] = None,
) -> AnalysisResult:
    """1 局を 2 パス方式で解析し、結果を DB に保存する。"""
    game = parse_game(sgf_text)
    total = game.move_count
    stub = is_stub(engine)
    if stub:
        log("警告: KataGo 未接続のためスタブ解析です。結果を学習に使わないでください。")

    db.update_game(game_id, status="解析中")

    # ---------------- 1 パス目: 全手スクリーニング
    done = db.analyzed_move_numbers(game_id, pass_no=1)
    truncated = False
    cache: dict[int, TurnAnalysis] = {}

    pending = [n for n in range(0, total + 1) if n not in _covered_turns(done, total)]
    # 局面キャッシュ（同一局面の再解析を避ける／FR-03）
    for turn in _chunks(sorted(set(pending)), 24):
        if deadline and time.monotonic() > deadline:
            truncated = True
            log(f"上限時間に達したため中断しました（{game_id}）")
            break
        got = _analyze_turns(db, engine, game, turn, settings.pass1_visits, cache)
        cache.update(got)

    if not truncated:
        _persist_moves(db, game_id, game, my_color, cache, pass_no=1)

    # ---------------- 悪手候補の抽出（FR-04）
    from .badmoves import extract_bad_moves  # 遅延 import（循環回避）

    bad = extract_bad_moves(db, game_id, my_color, settings)

    # ---------------- 2 パス目: 候補局面のみ精査
    refine_turns: list[int] = []
    for item in bad:
        n = item["move_no"]
        refine_turns.extend([n - 1, n])
    refine_turns = sorted({t for t in refine_turns if 0 <= t <= total})

    if refine_turns and not truncated:
        if deadline and time.monotonic() > deadline:
            truncated = True
        else:
            log(f"2 パス目: {len(refine_turns)} 局面を {settings.pass2_visits} visits で精査します")
            refined = _analyze_turns(db, engine, game, refine_turns, settings.pass2_visits, {})
            cache.update(refined)
            _persist_moves(db, game_id, game, my_color, cache, pass_no=2, only=[b["move_no"] for b in bad])
            bad = extract_bad_moves(db, game_id, my_color, settings)

    # ---------------- タグ付与・変化図・問題生成
    problems: list[str] = []
    if bad and not truncated:
        from .problems import generate_problems
        from .tagging import tag_bad_moves
        from .variations import build_variations

        tag_bad_moves(db, game, game_id, my_color, bad, cache, settings)
        build_variations(db, game, game_id, my_color, bad, cache, settings)
        if not stub:
            problems = generate_problems(
                db, game, game_id, my_color, bad, cache, settings, client
            )
        else:
            log("スタブ解析のため問題生成は行いません。")

    status = "解析中" if truncated else "解析済"
    losing = _losing_move(bad)
    db.update_game(
        game_id,
        status=status,
        analyzed_at=_now(),
        losing_move_no=losing["move_no"] if losing else None,
        main_tags=dumps(_collect_tags(db, game_id)),
    )
    return AnalysisResult(
        game_id=game_id,
        analyzed_turns=len(cache),
        bad_moves=bad,
        problems=problems,
        truncated=truncated,
        stub=stub,
    )


# ---------------------------------------------------------------- 内部


def _covered_turns(done_moves: set[int], total: int) -> set[int]:
    """すでに保存済みの手から、解析済みの turn 番号を逆算する。"""
    turns: set[int] = set()
    for n in done_moves:
        turns.add(n - 1)
        turns.add(n)
    if total == 0:
        turns.add(0)
    return turns


def _chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[i:i + size] for i in range(0, len(items), size)] or []


def _analyze_turns(
    db: Database,
    engine: Engine,
    game: Game,
    turns: list[int],
    visits: int,
    known: dict[int, TurnAnalysis],
) -> dict[int, TurnAnalysis]:
    """キャッシュを見つつ turns を解析する。"""
    need: list[int] = []
    out: dict[int, TurnAnalysis] = {}
    for turn in turns:
        if turn in known and known[turn].visits >= visits:
            out[turn] = known[turn]
            continue
        key = _position_key(game, turn)
        cached = db.cache_get(key, visits)
        if cached:
            out[turn] = TurnAnalysis.from_dict(cached, game.size)
        else:
            need.append(turn)

    if need:
        fresh = engine.analyze(game, need, visits)
        for turn, analysis in fresh.items():
            db.cache_put(_position_key(game, turn), visits, analysis.to_dict())
            out[turn] = analysis
        db.commit()
    return out


def _position_key(game: Game, turn: int) -> str:
    board = board_at(game, turn)
    to_move = "B" if turn % 2 == 0 else "W"
    if game.moves and turn > 0:
        last = game.moves[turn - 1].color
        to_move = "W" if last == "B" else "B"
    return f"{game.size}:{game.komi:g}:{to_move}:{board.position_key()}"


def _persist_moves(
    db: Database,
    game_id: str,
    game: Game,
    my_color: str,
    analyses: dict[int, TurnAnalysis],
    pass_no: int,
    only: Optional[list[int]] = None,
) -> None:
    """各手の勝率・候補手を保存する。1 手ごとにコミットする。"""
    targets = range(1, game.move_count + 1) if only is None else only
    for move_no in targets:
        before = analyses.get(move_no - 1)
        after = analyses.get(move_no)
        if before is None or after is None:
            continue
        mv = game.moves[move_no - 1]
        color = mv.color
        wr_before = before.winrate_for(color)
        wr_after = after.winrate_for(color)
        best = before.best()
        # 上位 8 手ぶん残す。練習画面で学習者が押した手に対して
        # 「その手だと相手がどう来るか」を見せるのに使う。KataGo は
        # どのみちこれらを返しているので、解析時間は増えない。
        candidates = [
            {
                "coord": c.gtp,
                "winrate": round(c.winrate_for(color), 2),
                "score": round(c.score_for(color), 2),
                "visits": c.visits,
                "pv": c.pv[:CANDIDATE_PV_MOVES],
            }
            for c in before.moves[:CANDIDATE_COUNT]
        ]
        db.save_move(
            game_id,
            move_no,
            color=color,
            coord=coord_to_gtp(mv.coord, game.size),
            winrate_before=round(wr_before, 3),
            winrate_after=round(wr_after, 3),
            score_before=round(before.score_for(color), 3),
            score_after=round(after.score_for(color), 3),
            delta=round(wr_after - wr_before, 3),
            best_move=best.gtp if best else None,
            candidates=dumps(candidates),
            visits=min(before.visits, after.visits),
            pass_no=pass_no,
        )


def _losing_move(bad: list[dict]) -> Optional[dict]:
    """敗着＝最も勝率低下幅が大きかった悪手。"""
    if not bad:
        return None
    return min(bad, key=lambda b: b["delta"])


def _collect_tags(db: Database, game_id: str) -> list[str]:
    rows = db.query("SELECT tags FROM bad_moves WHERE game_id = ?", (game_id,))
    from .db import loads

    tags: list[str] = []
    for row in rows:
        for tag in loads(row["tags"], []) or []:
            if tag not in tags:
                tags.append(tag)
    return tags
