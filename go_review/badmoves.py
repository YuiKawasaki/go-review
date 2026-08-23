"""悪手抽出（FR-04）。

判定は自分の手のみが対象。九路盤は 1 手の価値が大きいため、19 路盤より
閾値を高めに置いている（既定 10 / 20 / 30pt）。閾値は設定で変更できる。
"""
from __future__ import annotations

from typing import Optional

from .config import Settings
from .db import Database, dumps

DUBIOUS = "疑問手"
BAD = "悪手"
CRITICAL = "敗着候補"

SEVERITY_ORDER = {CRITICAL: 0, BAD: 1, DUBIOUS: 2}


def classify(delta: float, settings: Settings) -> Optional[str]:
    """勝率低下幅（負値）から区分を返す。閾値未満なら None。"""
    drop = -delta
    if drop >= settings.critical_threshold:
        return CRITICAL
    if drop >= settings.bad_threshold:
        return BAD
    if drop >= settings.dubious_threshold:
        return DUBIOUS
    return None


def is_decided(winrate_before: float, settings: Settings) -> bool:
    """すでに決着がついている局面は抽出対象から除外する。"""
    return (
        winrate_before < settings.decided_low
        or winrate_before > settings.decided_high
    )


def extract_bad_moves(
    db: Database,
    game_id: str,
    my_color: str,
    settings: Settings,
) -> list[dict]:
    """自分の手から悪手・疑問手を抽出し、bad_moves に保存して返す。"""
    rows = db.query(
        "SELECT move_no, color, coord, winrate_before, winrate_after, delta, best_move "
        "FROM moves WHERE game_id = ? AND color = ? ORDER BY move_no",
        (game_id, my_color),
    )

    found: list[dict] = []
    for row in rows:
        delta = row["delta"]
        before = row["winrate_before"]
        if delta is None or before is None:
            continue
        if is_decided(before, settings):
            continue
        severity = classify(delta, settings)
        if severity is None:
            continue
        found.append(
            {
                "game_id": game_id,
                "move_no": row["move_no"],
                "severity": severity,
                "delta": round(delta, 2),
                "winrate_before": round(before, 2),
                "winrate_after": round(row["winrate_after"], 2),
                "coord": row["coord"],
                "best_move": row["best_move"],
            }
        )

    db.execute("DELETE FROM bad_moves WHERE game_id = ?", (game_id,))
    for item in found:
        db.execute(
            "INSERT INTO bad_moves (game_id, move_no, severity, delta, tags) VALUES (?,?,?,?,?)",
            (game_id, item["move_no"], item["severity"], item["delta"], dumps([])),
        )
    db.commit()
    return found


def problem_candidates(bad: list[dict], settings: Settings) -> list[dict]:
    """問題化する対象を選ぶ。疑問手は記録のみ、1 局あたり最大 3 問。"""
    eligible = [b for b in bad if b["severity"] in (BAD, CRITICAL)]
    eligible.sort(key=lambda b: (SEVERITY_ORDER[b["severity"]], b["delta"]))
    return eligible[: settings.max_problems_per_game]


def losing_move(bad: list[dict]) -> Optional[dict]:
    """敗着＝1 局で最も勝率低下幅が大きかった悪手。"""
    if not bad:
        return None
    return min(bad, key=lambda b: b["delta"])


def marker_for(severity: Optional[str]) -> Optional[str]:
    """リプレイ用のマーカー種別（FR-07）。"""
    return {
        DUBIOUS: "dubious",
        BAD: "bad",
        CRITICAL: "critical",
    }.get(severity or "")


def good_move_markers(
    db: Database,
    game_id: str,
    my_color: str,
    min_gain: float = 8.0,
    spread_limit: float = 4.0,
) -> list[int]:
    """好手マーカーを付ける手を選ぶ（FR-07 の限定方針）。

    - 候補手が拮抗している局面で上位手を選べていた
    - もしくは勝率が明確に上昇した（相手のミスを咎めた）
    乱発を避けるため、この 2 条件のいずれかを満たす手だけを返す。
    """
    from .db import loads

    rows = db.query(
        "SELECT move_no, coord, delta, best_move, candidates FROM moves "
        "WHERE game_id = ? AND color = ? ORDER BY move_no",
        (game_id, my_color),
    )
    out: list[int] = []
    for row in rows:
        delta = row["delta"] or 0.0
        if delta >= min_gain:
            out.append(row["move_no"])
            continue
        candidates = loads(row["candidates"], []) or []
        if len(candidates) < 2 or delta < -1.0:
            continue
        spread = abs(candidates[0]["winrate"] - candidates[-1]["winrate"])
        played = row["coord"]
        if spread <= spread_limit and played in [c["coord"] for c in candidates[:2]]:
            out.append(row["move_no"])
    return out
