"""悪手タグの付与（FR-05）。

方針: 盤面から機械的に判定できるものを第一とし、機械判定できない手筋名や
方針レベルの分類だけを Claude API で補完する。呼吸点の計算やシチョウの
成否を LLM に判断させてはいけない（要件 2.3）。

ここでの判定はいずれも経験則である。誤検出を減らすため、条件は
「盤面から確実に言えること」に寄せている。
"""
from __future__ import annotations

from typing import Optional

from .config import Settings
from .db import Database, dumps, loads
from .goban import Board, board_at, distance, opposite
from .katago import TurnAnalysis
from .sgf import Coord, Game, gtp_to_coord

TAG_ATARI = "アタリ見落とし"
TAG_CUT = "切断された"
TAG_CAPTURE_RACE = "攻め合い負け"
TAG_EYE_SHAPE = "眼形不足"
TAG_BIG_POINT = "大場放置"
TAG_ENDGAME_LOSS = "ヨセ損"
TAG_WEAK_STONE = "弱い石の放置"

# 付録B の初期セット（Claude 補完はこの語彙に寄せる）
TAG_VOCABULARY = [
    TAG_ATARI, TAG_CUT, "切断機会の逸失", "シチョウ", "ゲタ", "ウッテガエシ",
    "オイオトシ", "両アタリ", "中手", "欠け眼", TAG_CAPTURE_RACE, TAG_EYE_SHAPE,
    "逃げ一辺倒", "追いかけすぎ", TAG_BIG_POINT, "局所固執", TAG_WEAK_STONE,
    "捨て石の判断ミス", TAG_ENDGAME_LOSS, "先手後手の誤り", "ダメ詰めミス",
]

# 初心者向けの一言解説（解説文・UIでの表示に使う）
TAG_DESCRIPTIONS = {
    TAG_ATARI: "石が取られる一歩手前（アタリ）に気づかず、そのままにしてしまうこと",
    TAG_CUT: "自分の石同士のつながりを、相手に断ち切られてしまうこと",
    "切断機会の逸失": "相手の石を切り離せるチャンスがあったのに、逃してしまったこと",
    "シチョウ": "石を斜めに追いかけていくと必ず取れる手筋。読み違えると逆に大きな石を失う",
    "ゲタ": "相手の石を、一手で外へ逃げられない網の形に囲ってしまう手筋",
    "ウッテガエシ": "一度わざと取らせてから、すぐに取り返す手筋",
    "オイオトシ": "相手が自分から動くと、連鎖的に大きく取られてしまう形",
    "両アタリ": "一手で二か所の石を同時にアタリにすること。相手はどちらか片方しか守れない",
    "中手": "相手の眼の中に石を置いて、眼の形を崩してしまう手筋",
    "欠け眼": "一見すると眼に見えても、実際には眼として機能しない形",
    TAG_CAPTURE_RACE: "お互いの石の生死を懸けた呼吸点の詰め合い（攻め合い）に負けること",
    TAG_EYE_SHAPE: "自分の石が生きるために必要な眼の形が足りていないこと",
    "逃げ一辺倒": "弱い石をただ逃がすことだけを考えて、他の手を検討しなかったこと",
    "追いかけすぎ": "相手の弱い石を必要以上に攻め続けて、他の大きな場所を逃したこと",
    TAG_BIG_POINT: "盤面全体で価値の高い場所（大場）を打たず、後回しにしてしまったこと",
    "局所固執": "一か所の攻防にこだわりすぎて、盤面全体を見た判断を誤ったこと",
    TAG_WEAK_STONE: "自分の弱い石を補強せず、そのままにしてしまったこと",
    "捨て石の判断ミス": "石を捨てるべき場面で捨てなかった、または捨てるべきでない場面で捨ててしまったこと",
    TAG_ENDGAME_LOSS: "終盤の細かい地の取り合い（ヨセ）で、目数を損したこと",
    "先手後手の誤り": "手番を渡してもよい手（後手）を打ってしまい、主導権（先手）を失ったこと",
    "ダメ詰めミス": "最後に残る細かい境界線（ダメ）の打ち方で損をしたこと",
}

# 次に似た場面が来たときに思い出すための一言（解説文の締めに使う）。
#
# TAG_DESCRIPTIONS が「何が起きたか」の説明なのに対し、こちらは
# 「次にどうするか」の行動指針。解説を読んだあと何も残らないという
# 問題への対処なので、抽象的な心がけではなく、盤の前で実際に
# できる確認動作の形で書く。
TAG_LESSONS = {
    TAG_ATARI: "石を打つ前に、自分の石の呼吸点（となりの空き交点）が残り1つになっていないか数える。",
    TAG_CUT: "自分の石と石のあいだに、相手が入り込めるすき間がないか確かめてから他へ向かう。",
    "切断機会の逸失": "相手の石が2つに分かれそうな場所を探す。切れる形は、切れるうちに切る。",
    "シチョウ": "シチョウで追いかける前に、逃げる先に相手の石がないか、追い切れる方向かを最後まで数える。",
    "ゲタ": "アタリで追うより、逃げ道を先にふさぐ網の形にできないか考える。",
    "ウッテガエシ": "取られそうな石でも、わざと取らせて取り返せる形がないか見る。",
    "オイオトシ": "呼吸点の少ない石をつなぐ前に、つないだせいでまとめて取られないか確かめる。",
    "両アタリ": "打つ前に、一手で相手の2か所を同時にアタリにできる点がないか探す。",
    "中手": "相手の眼の中が3目以上に広く見えても、まん中に置いて眼をつぶせないか考える。",
    "欠け眼": "眼に見える形でも、その角の石が取られたら眼でなくなる形かどうか確かめる。",
    TAG_CAPTURE_RACE: "攻め合いに入る前に、自分と相手の呼吸点を両方数えて、勝てるほうだけ戦う。",
    TAG_EYE_SHAPE: "石を伸ばす前に、その一団が眼を2つ作れる形かどうかを先に確かめる。",
    "逃げ一辺倒": "逃げる以外に、反撃する・捨てて他を得する、という手がないか一度考える。",
    "追いかけすぎ": "攻めが1手ごとに得になっているか確かめる。得が止まったら、盤の広い場所へ向かう。",
    TAG_BIG_POINT: "手を決める前に一度だけ盤全体を見て、今より大きい場所が空いていないか探す。",
    "局所固執": "同じ場所で3手続けたら、いったん手を止めて盤全体を見直す。",
    TAG_WEAK_STONE: "攻めに出る前に、自分の弱い石が取り残されていないか確かめる。",
    "捨て石の判断ミス": "小さい石は助けず、大きい石を助ける。どちらが大きいか、石の数で比べる。",
    TAG_ENDGAME_LOSS: "終盤は、先に打てば相手も応じる手（先手のヨセ）から順に打つ。",
    "先手後手の誤り": "その手を打ったあと、相手が応じてくれるかを考える。応じてくれない手は後回しにする。",
    "ダメ詰めミス": "最後の境界を埋めるときは、自分の石の呼吸点を詰めていないか一つずつ確かめる。",
}

BIG_POINT_DISTANCE = 4     # 九路盤でこれ以上離れていれば「別の場所」
ENDGAME_WINDOW = 20        # 終盤とみなす残り手数
ENDGAME_SCORE_LOSS = 3.0   # ヨセ損とみなす目数
OWNERSHIP_ALIVE = 0.3
OWNERSHIP_DEAD = -0.3


def tag_bad_moves(
    db: Database,
    game: Game,
    game_id: str,
    my_color: str,
    bad: list[dict],
    analyses: dict[int, TurnAnalysis],
    settings: Settings,
) -> dict[int, list[str]]:
    """悪手それぞれにタグを付けて保存する。"""
    result: dict[int, list[str]] = {}
    for item in bad:
        move_no = item["move_no"]
        tags = machine_tags(game, move_no, my_color, analyses, item)
        result[move_no] = tags
        db.execute(
            "UPDATE bad_moves SET tags = ? WHERE game_id = ? AND move_no = ?",
            (dumps(tags), game_id, move_no),
        )
    db.commit()
    return result


def machine_tags(
    game: Game,
    move_no: int,
    my_color: str,
    analyses: dict[int, TurnAnalysis],
    bad_item: dict,
) -> list[str]:
    """盤面から機械判定できるタグを列挙する。"""
    tags: list[str] = []
    size = game.size
    before = board_at(game, move_no - 1)
    after = board_at(game, move_no)
    played = game.moves[move_no - 1].coord if move_no <= game.move_count else None
    enemy = opposite(my_color)

    analysis_before = analyses.get(move_no - 1)
    analysis_after = analyses.get(move_no)

    # 相手の最善の咎め手（変化図・切断判定の起点）
    punish: Optional[Coord] = None
    if analysis_after and analysis_after.best():
        punish = analysis_after.best().coord

    # --- アタリ見落とし
    if _self_atari_created(before, after, my_color):
        tags.append(TAG_ATARI)
    elif punish is not None and _atari_after_reply(after, punish, enemy, my_color):
        tags.append(TAG_ATARI)

    # --- 切断された
    if punish is not None and _is_cut(after, punish, my_color):
        tags.append(TAG_CUT)

    # --- 攻め合い負け
    if _capture_race_lost(before, after, my_color):
        tags.append(TAG_CAPTURE_RACE)

    # --- 眼形不足（ownership が生から死に転じた石群がある）
    if analysis_before and analysis_after and _group_died(
        before, after, my_color, analysis_before, analysis_after, size
    ):
        tags.append(TAG_EYE_SHAPE)

    # --- 大場放置
    best_coord = _best_coord(analysis_before, size)
    d = distance(best_coord, played)
    if d is not None and d >= BIG_POINT_DISTANCE:
        tags.append(TAG_BIG_POINT)

    # --- ヨセ損
    if _is_endgame_loss(game, move_no, analysis_before, analysis_after, my_color):
        tags.append(TAG_ENDGAME_LOSS)

    # --- 弱い石の放置（呼吸点 2 以下の自分の石があるのに離れた場所へ打った）
    if d is not None and d >= BIG_POINT_DISTANCE and _has_weak_group(before, my_color):
        tags.append(TAG_WEAK_STONE)

    seen: list[str] = []
    for t in tags:
        if t not in seen:
            seen.append(t)
    return seen


# ---------------------------------------------------------------- 個別判定


def _best_coord(analysis: Optional[TurnAnalysis], size: int) -> Optional[Coord]:
    if not analysis:
        return None
    best = analysis.best()
    if not best:
        return None
    return best.coord


def _self_atari_created(before: Board, after: Board, color: str) -> bool:
    """着手後、2 子以上の自分の連が呼吸点 1 になった（着手前はそうでなかった）。"""
    before_atari = {
        frozenset(stones)
        for stones, libs in before.groups_of(color)
        if len(libs) == 1
    }
    for stones, libs in after.groups_of(color):
        if len(libs) == 1 and len(stones) >= 2 and frozenset(stones) not in before_atari:
            return True
    return False


def _atari_after_reply(after: Board, punish: Coord, enemy: str, my_color: str) -> bool:
    """相手が最善で咎めると、自分の連がアタリになる。"""
    board = after.copy()
    if board.get(punish) is not None:
        return False
    if board.try_play(enemy, punish) is None:
        return False
    for stones, libs in board.groups_of(my_color):
        if len(libs) == 1 and len(stones) >= 2:
            # 着手前からアタリだったものは除く
            for prev_stones, prev_libs in after.groups_of(my_color):
                if prev_stones == stones and len(prev_libs) == 1:
                    break
            else:
                return True
    return False


def _is_cut(after: Board, punish: Coord, my_color: str) -> bool:
    """相手の咎め手が、自分の異なる 2 つ以上の連に接している＝切断。"""
    if after.get(punish) is not None:
        return False
    touching: list[frozenset] = []
    for n in after.neighbors(punish):
        if after.get(n) != my_color:
            continue
        stones, _ = after.group(n)
        key = frozenset(stones)
        if key not in touching:
            touching.append(key)
    return len(touching) >= 2


def _capture_race_lost(before: Board, after: Board, my_color: str) -> bool:
    """隣接する敵味方の連について、呼吸点の優劣が逆転した。"""
    enemy = opposite(my_color)

    def pairs(board: Board) -> dict[tuple, tuple[int, int]]:
        out: dict[tuple, tuple[int, int]] = {}
        for stones, libs in board.groups_of(my_color):
            neighbors_enemy: set[Coord] = set()
            for s in stones:
                for n in board.neighbors(s):
                    if board.get(n) == enemy:
                        neighbors_enemy.add(n)
            for n in neighbors_enemy:
                e_stones, e_libs = board.group(n)
                key = (frozenset(stones), frozenset(e_stones))
                out[key] = (len(libs), len(e_libs))
        return out

    before_pairs = pairs(before)
    after_pairs = pairs(after)
    for key, (mine_after, theirs_after) in after_pairs.items():
        prev = before_pairs.get(key)
        if not prev:
            continue
        mine_before, theirs_before = prev
        if mine_before >= theirs_before and mine_after < theirs_after:
            return True
    return False


def _ownership_at(ownership: list[float], c: Coord, size: int) -> float:
    """ownership は左上起点の行優先で並ぶ想定（黒が正）。"""
    idx = c[1] * size + c[0]
    if 0 <= idx < len(ownership):
        return ownership[idx]
    return 0.0


def _group_died(
    before: Board,
    after: Board,
    my_color: str,
    analysis_before: TurnAnalysis,
    analysis_after: TurnAnalysis,
    size: int,
) -> bool:
    """自分の石群の所有率が「生き」から「死に」へ転じたか。"""
    if not analysis_before.ownership or not analysis_after.ownership:
        return False
    sign = 1.0 if my_color == "B" else -1.0

    def avg(board: Board, ownership: list[float], stones: set[Coord]) -> float:
        if not stones:
            return 0.0
        return sum(_ownership_at(ownership, s, size) for s in stones) / len(stones) * sign

    for stones, _ in after.groups_of(my_color):
        # 着手前にも存在した連だけを比較する
        matching = [s for s, _ in before.groups_of(my_color) if s & stones]
        if not matching:
            continue
        prev = max(matching, key=lambda s: len(s & stones))
        if avg(before, analysis_before.ownership, prev) > OWNERSHIP_ALIVE and \
           avg(after, analysis_after.ownership, stones) < OWNERSHIP_DEAD:
            return True
    return False


def _is_endgame_loss(
    game: Game,
    move_no: int,
    analysis_before: Optional[TurnAnalysis],
    analysis_after: Optional[TurnAnalysis],
    my_color: str,
) -> bool:
    if not analysis_before or not analysis_after:
        return False
    if move_no < max(0, game.move_count - ENDGAME_WINDOW):
        return False
    loss = analysis_before.score_for(my_color) - analysis_after.score_for(my_color)
    return loss >= ENDGAME_SCORE_LOSS


def _has_weak_group(board: Board, color: str, threshold: int = 2) -> bool:
    return any(len(libs) <= threshold for _, libs in board.groups_of(color))


def coord_from_gtp_safe(gtp: Optional[str], size: int) -> Optional[Coord]:
    if not gtp:
        return None
    try:
        return gtp_to_coord(gtp, size)
    except Exception:
        return None
