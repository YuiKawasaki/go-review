"""盤面のルール処理（連・呼吸点・取り上げ）。

FR-05 の機械判定タグはすべてここの呼吸点計算に依存する。
LLM には盤面の読みをさせない、という設計上の前提（2.3）の実装面での担保。
"""
from __future__ import annotations

from typing import Iterable, Optional

from .sgf import Coord, Game, Move

EMPTY = None
BLACK = "B"
WHITE = "W"


def opposite(color: str) -> str:
    return WHITE if color == BLACK else BLACK


class IllegalMove(ValueError):
    """自殺手など、盤上に置けない着手。"""


class Board:
    """九路盤想定の単純な碁盤。コウの再現はしない（実戦譜の再生が目的）。"""

    __slots__ = ("size", "grid", "captures")

    def __init__(self, size: int = 9) -> None:
        self.size = size
        self.grid: list[Optional[str]] = [EMPTY] * (size * size)
        # 取った石の数（取った側の色をキーにする）
        self.captures: dict[str, int] = {BLACK: 0, WHITE: 0}

    # ---------------------------------------------------------- 基本操作

    def index(self, c: Coord) -> int:
        return c[1] * self.size + c[0]

    def get(self, c: Coord) -> Optional[str]:
        return self.grid[self.index(c)]

    def set(self, c: Coord, color: Optional[str]) -> None:
        self.grid[self.index(c)] = color

    def on_board(self, c: Coord) -> bool:
        return 0 <= c[0] < self.size and 0 <= c[1] < self.size

    def neighbors(self, c: Coord) -> list[Coord]:
        col, row = c
        out = []
        for d_col, d_row in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            n = (col + d_col, row + d_row)
            if self.on_board(n):
                out.append(n)
        return out

    def copy(self) -> "Board":
        b = Board(self.size)
        b.grid = list(self.grid)
        b.captures = dict(self.captures)
        return b

    # ---------------------------------------------------------- 連

    def group(self, c: Coord) -> tuple[set[Coord], set[Coord]]:
        """(連を構成する石, その連の呼吸点) を返す。空点なら両方 空集合。"""
        color = self.get(c)
        if color is EMPTY:
            return set(), set()
        stones: set[Coord] = set()
        liberties: set[Coord] = set()
        stack = [c]
        while stack:
            cur = stack.pop()
            if cur in stones:
                continue
            stones.add(cur)
            for n in self.neighbors(cur):
                val = self.get(n)
                if val is EMPTY:
                    liberties.add(n)
                elif val == color and n not in stones:
                    stack.append(n)
        return stones, liberties

    def liberty_count(self, c: Coord) -> int:
        return len(self.group(c)[1])

    def groups_of(self, color: str) -> list[tuple[set[Coord], set[Coord]]]:
        """指定色の全連を (石, 呼吸点) のリストで返す。"""
        seen: set[Coord] = set()
        out = []
        for row in range(self.size):
            for col in range(self.size):
                c = (col, row)
                if c in seen or self.get(c) != color:
                    continue
                stones, libs = self.group(c)
                seen |= stones
                out.append((stones, libs))
        return out

    # ---------------------------------------------------------- 着手

    def play(self, color: str, coord: Optional[Coord]) -> list[Coord]:
        """着手して、取り上げた相手石の座標リストを返す。パスは何もしない。"""
        if coord is None:
            return []
        if not self.on_board(coord):
            raise IllegalMove(f"盤外です: {coord}")
        if self.get(coord) is not EMPTY:
            raise IllegalMove(f"すでに石があります: {coord}")

        self.set(coord, color)
        enemy = opposite(color)
        captured: list[Coord] = []
        for n in self.neighbors(coord):
            if self.get(n) != enemy:
                continue
            stones, libs = self.group(n)
            if not libs:
                for s in stones:
                    self.set(s, EMPTY)
                captured.extend(stones)

        if not captured:
            # 自殺手チェック（取れていないのに自分の呼吸点が 0）
            _, libs = self.group(coord)
            if not libs:
                self.set(coord, EMPTY)
                raise IllegalMove(f"自殺手です: {coord}")

        if captured:
            self.captures[color] = self.captures.get(color, 0) + len(captured)
        return captured

    def try_play(self, color: str, coord: Optional[Coord]) -> Optional[list[Coord]]:
        """play の例外を握りつぶす版。打てなければ None。"""
        try:
            return self.play(color, coord)
        except IllegalMove:
            return None

    # ---------------------------------------------------------- 補助

    def stones(self) -> list[tuple[Coord, str]]:
        out = []
        for row in range(self.size):
            for col in range(self.size):
                v = self.grid[row * self.size + col]
                if v is not EMPTY:
                    out.append(((col, row), v))
        return out

    def position_key(self) -> str:
        """局面のハッシュ用キー（解析キャッシュに使う）。"""
        return "".join(v or "." for v in self.grid)


def board_at(game: Game, move_no: int) -> Board:
    """move_no 手を打ち終えた時点の盤面を作る（0 は初期盤面）。"""
    board = Board(game.size)
    for c in game.setup_black:
        board.set(c, BLACK)
    for c in game.setup_white:
        board.set(c, WHITE)
    for mv in game.moves[:move_no]:
        board.try_play(mv.color, mv.coord)
    return board


def replay(game: Game) -> Iterable[tuple[int, Move, Board]]:
    """(手数, 着手, その手を打った直後の盤面) を順に返す。"""
    board = Board(game.size)
    for c in game.setup_black:
        board.set(c, BLACK)
    for c in game.setup_white:
        board.set(c, WHITE)
    for i, mv in enumerate(game.moves, start=1):
        board.try_play(mv.color, mv.coord)
        yield i, mv, board


def distance(a: Optional[Coord], b: Optional[Coord]) -> Optional[int]:
    """チェビシェフ距離（大場放置の判定に使う）。"""
    if a is None or b is None:
        return None
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def weakest_group(board: Board, color: str) -> Optional[tuple[set[Coord], set[Coord]]]:
    """呼吸点が最も少ない連を返す（ヒント生成用）。"""
    groups = board.groups_of(color)
    if not groups:
        return None
    return min(groups, key=lambda g: (len(g[1]), -len(g[0])))
