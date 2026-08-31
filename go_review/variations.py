"""変化図の事前生成（FR-08）。

本アプリの中核。「その手が悪い」ではなく「打つとどうなるか」を盤上で見せる。

  best   … 好手を打っていた場合の進行（AI の読み筋）
  punish … 実戦の手に対して相手が最善で咎めた場合の進行

制約（FR-08 の明記事項）:
  - 変化図は双方最善の一本道であり、実際の相手が同じに応じるとは限らない
  - 読み筋は手数が進むほど精度が落ちるため 10 手を超えて提示しない
"""
from __future__ import annotations

from typing import Optional

from .config import Settings
from .db import Database, dumps
from .goban import Board, board_at, opposite
from .katago import TurnAnalysis
from .sgf import Game, coord_to_gtp, gtp_to_coord

BRANCH_BEST = "best"
BRANCH_PUNISH = "punish"


def build_variations(
    db: Database,
    game: Game,
    game_id: str,
    my_color: str,
    bad: list[dict],
    analyses: dict[int, TurnAnalysis],
    settings: Settings,
) -> None:
    """悪手局面ごとに 2 系統の変化図を作って保存する。"""
    for item in bad:
        move_no = item["move_no"]
        before = analyses.get(move_no - 1)
        after = analyses.get(move_no)

        if before and before.best():
            _save(
                db, game_id, move_no, BRANCH_BEST,
                pv=before.best().pv[: settings.pv_max_moves],
                end_winrate=before.best().winrate_for(my_color),
                end_score=before.best().score_for(my_color),
                visits=before.best().visits,
                game=game,
                start_turn=move_no - 1,
                my_color=my_color,
            )

        if after and after.best():
            _save(
                db, game_id, move_no, BRANCH_PUNISH,
                pv=after.best().pv[: settings.pv_max_moves],
                end_winrate=after.best().winrate_for(my_color),
                end_score=after.best().score_for(my_color),
                visits=after.best().visits,
                game=game,
                start_turn=move_no,
                my_color=my_color,
            )
    db.commit()


def _save(
    db: Database,
    game_id: str,
    move_no: int,
    branch: str,
    pv: list[str],
    end_winrate: float,
    end_score: float,
    visits: int,
    game: Game,
    start_turn: int,
    my_color: str,
) -> None:
    comments = pv_comments(game, start_turn, pv, my_color, branch)
    db.execute(
        "INSERT INTO variations (game_id, move_no, branch_type, pv_moves, pv_comments, "
        "end_winrate, end_score, visits) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(game_id, move_no, branch_type) DO UPDATE SET "
        "pv_moves = excluded.pv_moves, pv_comments = excluded.pv_comments, "
        "end_winrate = excluded.end_winrate, end_score = excluded.end_score, "
        "visits = excluded.visits",
        (
            game_id, move_no, branch, dumps(pv), dumps(comments),
            round(end_winrate, 2), round(end_score, 2), visits,
        ),
    )


def pv_comments(
    game: Game,
    start_turn: int,
    pv: list[str],
    my_color: str,
    branch: str,
) -> list[str]:
    """読み筋の各手に、盤面から言える範囲の短い意図を添える。

    ここでは LLM を使わない。石を取る・アタリにする・切るといった事実は
    盤面から確定できるため、機械的に書いたほうが安全で速い。
    """
    board = board_at(game, start_turn)
    to_move = _next_color(game, start_turn)
    out: list[str] = []

    for gtp in pv:
        try:
            coord = gtp_to_coord(gtp, game.size)
        except Exception:
            coord = None
        if coord is None:
            out.append("パス")
            to_move = opposite(to_move)
            continue

        enemy = opposite(to_move)
        before_libs = _min_liberties(board, enemy)
        captured = board.try_play(to_move, coord)
        if captured is None:
            out.append("")
            to_move = opposite(to_move)
            continue

        # 「打ち手から見た相手」と「学習者から見た相手」が入れ替わるため、
        # 打たれる側を毎回どちらなのか言い直す。ここを "相手" で固定すると、
        # 相手の着手を説明する行で自分の石を指してしまい意味が反転する。
        side = "自分" if to_move == my_color else "相手"
        target = "相手" if to_move == my_color else "あなた"

        parts: list[str] = []
        if captured:
            parts.append(f"{target}の石を {len(captured)} 子取る")
        _, libs = board.group(coord)
        if _puts_in_atari(board, coord, enemy):
            parts.append(f"{target}をアタリにする")
        if _connects(board, coord, to_move):
            parts.append("継いで切断を防ぐ")
        if len(libs) <= 2 and not captured:
            parts.append("この石は呼吸点が少なく薄い")
        after_libs = _min_liberties(board, enemy)
        if after_libs < before_libs and not parts:
            parts.append(f"{target}の呼吸点を詰める")
        if not parts:
            # ここまでで何も言えないと「この地点を占める」ばかりが並んで
            # 手順を読んでも意味が取れない。盤面から確実に言えることだけ足す。
            touches_own = any(board.get(n) == to_move for n in board.neighbors(coord))
            touches_enemy = any(board.get(n) == enemy for n in board.neighbors(coord))
            if touches_own and touches_enemy:
                parts.append("自分の石を伸ばして相手に迫る")
            elif touches_own:
                parts.append("自分の石を伸ばす")
            elif touches_enemy:
                parts.append(f"{target}の石に迫る")
            else:
                parts.append("離れた場所に新しく打つ")

        text = "／".join(parts) if parts else "この地点を占める"
        out.append(f"{side}: {text}")
        to_move = opposite(to_move)

    if out:
        head = (
            "相手が最善で応じれば、という前提の進行です。"
            if branch == BRANCH_BEST
            else "相手が最善で咎めてきた場合の進行です。"
        )
        out[0] = f"{head} {out[0]}"
    return out


def _next_color(game: Game, turn: int) -> str:
    if turn <= 0:
        return "B"
    if turn <= game.move_count:
        return opposite(game.moves[turn - 1].color)
    return "B"


def _min_liberties(board: Board, color: str) -> int:
    groups = board.groups_of(color)
    if not groups:
        return 99
    return min(len(libs) for _, libs in groups)


def _puts_in_atari(board: Board, coord, enemy: str) -> bool:
    for n in board.neighbors(coord):
        if board.get(n) == enemy:
            _, libs = board.group(n)
            if len(libs) == 1:
                return True
    return False


def _connects(board: Board, coord, color: str) -> bool:
    """この着手が自分の 2 つ以上の連をつないだか。"""
    groups: list[frozenset] = []
    stones, _ = board.group(coord)
    for n in board.neighbors(coord):
        if board.get(n) != color:
            continue
        g, _ = board.group(n)
        key = frozenset(g)
        if key not in groups:
            groups.append(key)
    return len(groups) == 1 and len(stones) >= 3


def opponent_missed_punishment(
    game: Game,
    move_no: int,
    analyses: dict[int, TurnAnalysis],
    my_color: str,
    tolerance: float = 8.0,
) -> Optional[bool]:
    """実戦で相手が咎めてきたかどうか。

    咎めてこなかった場合「相手も見落としていた」と明示する（FR-08 B）。
    判定できない場合は None。
    """
    after = analyses.get(move_no)
    next_pos = analyses.get(move_no + 1)
    if not after or not next_pos or move_no >= game.move_count:
        return None
    best = after.best()
    if not best:
        return None
    actual = game.moves[move_no].coord if move_no < game.move_count else None
    if actual == best.coord:
        return False
    recovered = next_pos.winrate_for(my_color) - after.winrate_for(my_color)
    return recovered >= tolerance


def comparison_table(
    db: Database,
    game_id: str,
    move_no: int,
) -> dict:
    """好手を打った場合と実戦の比較表（FR-08 C）。"""
    rows = {
        row["branch_type"]: row
        for row in db.query(
            "SELECT * FROM variations WHERE game_id = ? AND move_no = ?",
            (game_id, move_no),
        )
    }
    best = rows.get(BRANCH_BEST)
    punish = rows.get(BRANCH_PUNISH)
    return {
        "best": {
            "winrate": best["end_winrate"] if best else None,
            "score": best["end_score"] if best else None,
        },
        "actual": {
            "winrate": punish["end_winrate"] if punish else None,
            "score": punish["end_score"] if punish else None,
        },
    }
