"""SGF のパース・正規化（FR-02）。

外部依存なし。囲碁クエストの九路盤棋譜を主対象とするが、
分岐つき SGF も読める（本線＝各ノードの第1子を辿る）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterator, Optional

# ---------------------------------------------------------------- 座標

Coord = tuple[int, int]  # (col, row) 左上原点・0 起点


class SGFError(ValueError):
    """SGF を解釈できなかった。"""


def sgf_to_coord(s: str, size: int) -> Optional[Coord]:
    """SGF の 2 文字座標を (col, row) に。空文字・盤外はパス扱い。"""
    if len(s) != 2:
        return None
    col = ord(s[0]) - ord("a")
    row = ord(s[1]) - ord("a")
    if not (0 <= col < size and 0 <= row < size):
        # 'tt' 等は伝統的にパスを表す
        return None
    return (col, row)


def coord_to_sgf(c: Optional[Coord]) -> str:
    if c is None:
        return ""
    return chr(ord("a") + c[0]) + chr(ord("a") + c[1])


# KataGo / GTP は左下原点・列に I を使わない
_GTP_COLS = "ABCDEFGHJKLMNOPQRST"


def coord_to_gtp(c: Optional[Coord], size: int) -> str:
    if c is None:
        return "pass"
    col, row = c
    return f"{_GTP_COLS[col]}{size - row}"


def gtp_to_coord(s: str, size: int) -> Optional[Coord]:
    s = (s or "").strip().upper()
    if s in ("PASS", "RESIGN", ""):
        return None
    col = _GTP_COLS.find(s[0])
    if col < 0:
        raise SGFError(f"GTP 座標を解釈できません: {s}")
    try:
        row = size - int(s[1:])
    except ValueError as exc:
        raise SGFError(f"GTP 座標を解釈できません: {s}") from exc
    if not (0 <= col < size and 0 <= row < size):
        raise SGFError(f"盤外の GTP 座標: {s}")
    return (col, row)


# ---------------------------------------------------------------- パーサ


class SGFNode:
    __slots__ = ("props", "children")

    def __init__(self) -> None:
        self.props: dict[str, list[str]] = {}
        self.children: list["SGFNode"] = []

    def get(self, key: str, default: str = "") -> str:
        v = self.props.get(key)
        return v[0] if v else default


class _Parser:
    def __init__(self, text: str) -> None:
        self.t = text
        self.i = 0
        self.n = len(text)

    def _ws(self) -> None:
        while self.i < self.n and self.t[self.i] in " \t\r\n":
            self.i += 1

    def parse_gametree(self) -> SGFNode:
        self._ws()
        if self.i >= self.n or self.t[self.i] != "(":
            raise SGFError("'(' で始まっていません")
        self.i += 1
        nodes = self._parse_nodes()
        if not nodes:
            raise SGFError("ノードがありません")
        # 同一階層のノードは連結済み。'(' が続けば分岐（子ゲームツリー）
        while True:
            self._ws()
            if self.i < self.n and self.t[self.i] == "(":
                nodes[-1].children.append(self.parse_gametree())
            else:
                break
        self._ws()
        if self.i < self.n and self.t[self.i] == ")":
            self.i += 1
        return nodes[0]

    def _parse_nodes(self) -> list[SGFNode]:
        nodes: list[SGFNode] = []
        while True:
            self._ws()
            if self.i >= self.n or self.t[self.i] != ";":
                break
            self.i += 1
            node = self._parse_node()
            if nodes:
                nodes[-1].children.append(node)
            nodes.append(node)
        return nodes

    def _parse_node(self) -> SGFNode:
        node = SGFNode()
        while True:
            self._ws()
            if self.i >= self.n or not self.t[self.i].isalpha():
                break
            start = self.i
            while self.i < self.n and self.t[self.i].isalpha():
                self.i += 1
            ident = self.t[start:self.i].upper()
            values: list[str] = []
            while True:
                self._ws()
                if self.i >= self.n or self.t[self.i] != "[":
                    break
                values.append(self._parse_value())
            node.props.setdefault(ident, []).extend(values)
        return node

    def _parse_value(self) -> str:
        self.i += 1  # 先頭の '['
        out: list[str] = []
        while self.i < self.n:
            ch = self.t[self.i]
            if ch == "\\":
                self.i += 1
                if self.i < self.n:
                    nxt = self.t[self.i]
                    if nxt != "\n":  # soft line break は削除する
                        out.append(nxt)
                    self.i += 1
                continue
            if ch == "]":
                self.i += 1
                break
            out.append(ch)
            self.i += 1
        return "".join(out)


SGF_STRICT = re.compile(r"\(\s*;\s*GM\[1\].*?\)\s*$", re.DOTALL)
SGF_LOOSE = re.compile(r"\(\s*;[^()]*?GM\[1\][^\x00]*", re.DOTALL)
SGF_ANY = re.compile(r"\(\s*;[^\x00]*", re.DOTALL)


def extract_sgf(text: str) -> Optional[str]:
    """任意のテキストから SGF 本文を抜き出す（FR-01）。

    Notion のページ本文は複数ブロックに割れることがあるため、
    呼び出し側で全ブロックを連結してから渡すこと。
    """
    if not text:
        return None
    for pattern in (SGF_STRICT, SGF_LOOSE, SGF_ANY):
        m = pattern.search(text)
        if not m:
            continue
        candidate = m.group(0).strip()
        depth = 0
        for idx, ch in enumerate(candidate):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return candidate[: idx + 1]
        return candidate
    return None


def sgf_hash(sgf_text: str) -> str:
    """空白差を無視した SGF 本文の SHA-256（差分判定・冪等性の鍵）。"""
    normalized = re.sub(r"\s+", "", sgf_text or "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- ゲーム


@dataclass
class Move:
    color: str                # 'B' or 'W'
    coord: Optional[Coord]    # None はパス

    @property
    def is_pass(self) -> bool:
        return self.coord is None


@dataclass
class Game:
    size: int = 9
    komi: float = 7.0
    rules: str = ""
    result: str = ""
    pb: str = ""
    pw: str = ""
    date: str = ""
    handicap: int = 0
    setup_black: list[Coord] = field(default_factory=list)
    setup_white: list[Coord] = field(default_factory=list)
    moves: list[Move] = field(default_factory=list)
    sgf: str = ""

    # -------------------------------------------------- 派生情報

    def player_name(self, color: str) -> str:
        return self.pb if color == "B" else self.pw

    def my_color(self, my_name: str) -> Optional[str]:
        """PB/PW と自分のプレイヤー名を照合（大文字小文字は無視）。"""
        target = (my_name or "").strip().lower()
        if not target:
            return None
        if target in self.pb.lower():
            return "B"
        if target in self.pw.lower():
            return "W"
        return None

    def opponent(self, my_color: str) -> tuple[str, Optional[int]]:
        """相手の (名前, レーティング)。`名前 (数値)` 形式を分解する。"""
        raw = self.pw if my_color == "B" else self.pb
        return split_player(raw)

    @property
    def move_count(self) -> int:
        return len(self.moves)

    def result_for(self, my_color: str) -> tuple[Optional[str], Optional[float]]:
        """(勝/負/引分, 目数差) を自分視点で返す。負けなら目数差は負値。"""
        return parse_result(self.result, my_color)

    @property
    def end_type(self) -> str:
        """終局種別: 終局 / 投了 / 時間切れ / 不明。"""
        r = (self.result or "").upper()
        if r.endswith("+R") or "RESIGN" in r:
            return "投了"
        if r.endswith("+T") or "TIME" in r:
            return "時間切れ"
        if len(self.moves) >= 2 and all(m.is_pass for m in self.moves[-2:]):
            return "終局"
        if not r:
            return "不明"
        return "終局"


_PLAYER_RE = re.compile(r"^(.*?)\s*\((\d+(?:\.\d+)?)\)\s*$")


def split_player(raw: str) -> tuple[str, Optional[int]]:
    """`:Go9Bot (985)` → (':Go9Bot', 985)。括弧がなければ (raw, None)。"""
    raw = (raw or "").strip()
    m = _PLAYER_RE.match(raw)
    if not m:
        return raw, None
    return m.group(1).strip(), int(float(m.group(2)))


def parse_result(result: str, my_color: str) -> tuple[Optional[str], Optional[float]]:
    """RE を自分視点の (結果, 目数差) に。W+40.0 で自分が黒なら ('負', -40.0)。"""
    r = (result or "").strip().upper()
    if not r:
        return None, None
    if r in ("DRAW", "0"):
        return "引分", 0.0
    if r.startswith("VOID") or r == "?":
        return None, None
    winner = r[0]
    if winner not in ("B", "W"):
        return None, None
    outcome = "勝" if winner == my_color else "負"
    margin: Optional[float] = None
    tail = r[2:] if len(r) > 2 and r[1] == "+" else ""
    m = re.match(r"[\d.]+", tail) if tail else None
    if m:
        try:
            margin = float(m.group(0))
        except ValueError:
            margin = None
    if margin is not None and outcome == "負":
        margin = -margin
    return outcome, margin


def parse_game(sgf_text: str) -> Game:
    """SGF 本文 1 局を Game に変換する（本線のみ）。"""
    root = _Parser(sgf_text).parse_gametree()

    size = 9
    raw_size = root.get("SZ")
    if raw_size:
        try:
            size = int(raw_size.split(":")[0])
        except ValueError:
            size = 9

    def _float(key: str, default: float) -> float:
        raw = root.get(key)
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    raw_ha = root.get("HA") or ""
    game = Game(
        size=size,
        komi=_float("KM", 0.0),
        rules=root.get("RU"),
        result=root.get("RE"),
        pb=root.get("PB"),
        pw=root.get("PW"),
        date=root.get("DT"),
        handicap=int(raw_ha) if raw_ha.isdigit() else 0,
        sgf=sgf_text,
    )
    for c in root.props.get("AB", []):
        pos = sgf_to_coord(c, size)
        if pos:
            game.setup_black.append(pos)
    for c in root.props.get("AW", []):
        pos = sgf_to_coord(c, size)
        if pos:
            game.setup_white.append(pos)

    for node in _main_line(root):
        for color in ("B", "W"):
            if color in node.props:
                raw = node.props[color][0]
                game.moves.append(Move(color, sgf_to_coord(raw, size)))
                break
    return game


def _main_line(root: SGFNode) -> Iterator[SGFNode]:
    node: Optional[SGFNode] = root
    while node is not None:
        yield node
        node = node.children[0] if node.children else None


def position_sgf(game: Game, upto: int) -> str:
    """先頭から upto 手までを含む SGF を組み立てる（問題の局面保存用）。"""
    head = [
        "(;GM[1]FF[4]",
        f"SZ[{game.size}]",
        f"KM[{game.komi:g}]",
    ]
    if game.rules:
        head.append(f"RU[{game.rules}]")
    if game.setup_black:
        head.append("AB" + "".join(f"[{coord_to_sgf(c)}]" for c in game.setup_black))
    if game.setup_white:
        head.append("AW" + "".join(f"[{coord_to_sgf(c)}]" for c in game.setup_white))
    body = "".join(f";{m.color}[{coord_to_sgf(m.coord)}]" for m in game.moves[:upto])
    return "".join(head) + body + ")"
