"""解説文の生成（Claude API）。

LLM の役割は、AI エンジンが出した数値結果を日本語の解説文に翻訳すること、
および弱点タグを言語化することに限定する（要件 2.3）。
正解手・勝率・呼吸点などの事実は必ず呼び出し側が数値で渡し、
モデルには「読み」をさせない。

API キーが無い場合はテンプレート生成にフォールバックする。
月額固定費 0 円が必須要件のため、API なしでもアプリが成立すること。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config import Settings
from .tagging import TAG_DESCRIPTIONS, TAG_LESSONS, TAG_VOCABULARY

SYSTEM_PROMPT = """あなたは九路盤の囲碁を学ぶ級位者のための解説者です。

厳守事項:
- 与えられた数値（勝率・目数・呼吸点・座標）だけを根拠に書く。盤面を自分で読み直さない。
- 正解手は与えられたものが唯一の正解。別の手を提案しない。
- 変化図に触れるときは「相手が最善で応じれば」という前提を必ず書く。
- 数値の丸めや言い換えはしてよいが、事実の追加・推測はしない。
- 出力は日本語。読み手は級位者なので、専門用語を使ったら必ずその場で
  かっこ書きの言い換えを添える（例: 呼吸点（石のとなりの空き交点））。
- 「勝率」ではなく「勝ちやすさ」のように、日常語に寄せて書く。"""

# 解説文に並べる読み筋の手数。これ以上は文章では追えない。
NARRATION_LINES = 6

EXPLANATION_INSTRUCTION = """次の5構成で、全体800字以内で書いてください。見出しはこの5つを使ってください。

何が起きたか:
相手の狙い:
自分の見落とし:
どう打つべきだったか:
次に似た場面が来たら:

「相手の狙い」と「どう打つべきだったか」では、渡された読み筋を
1手ずつ順になぞって、その手が何をしている手なのかを書いてください。
「次に似た場面が来たら」は、盤の前で実際にできる確認動作を1つだけ書いてください。"""


@dataclass
class MoveContext:
    """解説生成に渡す事実。すべて解析エンジン由来の数値。"""
    move_no: int
    my_color: str
    actual_move: str
    actual_winrate_drop: float
    winrate_before: float
    winrate_after: float
    best_move: str
    best_winrate: float
    score_before: Optional[float] = None
    score_after: Optional[float] = None
    tags: list[str] = field(default_factory=list)
    best_pv: list[str] = field(default_factory=list)
    punish_pv: list[str] = field(default_factory=list)
    # 読み筋の各手に添える一言（variations.pv_comments と同じ並び）。
    # 盤面から機械的に出しているので、API が無くても使える。
    best_pv_comments: list[str] = field(default_factory=list)
    punish_pv_comments: list[str] = field(default_factory=list)
    punish_end_winrate: Optional[float] = None
    opponent_missed: Optional[bool] = None
    total_moves: int = 0

    def to_prompt(self) -> str:
        color = "黒" if self.my_color == "B" else "白"
        lines = [
            f"手数: {self.move_no} / {self.total_moves}（自分は{color}番）",
            f"実際に打った手: {self.actual_move}",
            f"着手前の自分の勝率: {self.winrate_before:.1f}%",
            f"着手後の自分の勝率: {self.winrate_after:.1f}%"
            f"（{self.actual_winrate_drop:.1f}pt 低下）",
            f"AI の最善手: {self.best_move}（その場合の自分の勝率 {self.best_winrate:.1f}%）",
        ]
        if self.score_before is not None and self.score_after is not None:
            lines.append(
                f"想定地合い: {self.score_before:+.1f}目 → {self.score_after:+.1f}目"
            )
        if self.tags:
            lines.append(f"機械判定された失敗の種類: {', '.join(self.tags)}")
        if self.best_pv:
            lines.append(f"最善手を打った場合の読み筋: {' '.join(self.best_pv)}")
            for line in _pv_prompt_lines(self.best_pv, self.best_pv_comments):
                lines.append(f"  {line}")
        if self.punish_pv:
            lines.append(f"実戦の手を相手が咎める読み筋: {' '.join(self.punish_pv)}")
            for line in _pv_prompt_lines(self.punish_pv, self.punish_pv_comments):
                lines.append(f"  {line}")
        if self.punish_end_winrate is not None:
            lines.append(
                f"その読み筋を打ち切った時点の自分の勝率: {self.punish_end_winrate:.1f}%"
            )
        if self.opponent_missed is True:
            lines.append("補足: 実戦では相手もこの咎め方を見落としていた。")
        return "\n".join(lines)


def _pv_prompt_lines(
    pv: list[str], comments: list[str], limit: Optional[int] = None
) -> list[str]:
    """読み筋を「座標＋一言」の行に並べる。プロンプトと解説文で共用する。

    解説文では limit で頭のほうだけに絞る。10 手ぶん文章で並べても読めない。
    盤面のプレイヤーでは最後まで送れるので、そちらで確認してもらう。
    """
    out: list[str] = []
    for i, gtp in enumerate(pv[:limit] if limit else pv):
        actor, text = _split_comment(comments[i] if i < len(comments) else "")
        label = f"{gtp}（{actor}）" if actor else gtp
        out.append(f"{i + 1}. {label} {text}".rstrip())
    return out


def _split_comment(comment: str) -> tuple[str, str]:
    """pv_comments の 1 要素を「打ち手」と「内容」に分ける。

    variations.pv_comments は "自分: 石を 2 子取る" の形。先頭要素にだけ
    「相手が最善で応じれば〜」という前置きが付くので、それは取り除く。
    """
    text = (comment or "").strip()
    for head in ("相手が最善で応じれば、という前提の進行です。",
                 "相手が最善で咎めてきた場合の進行です。"):
        if text.startswith(head):
            text = text[len(head):].strip()
    for actor in ("自分", "相手"):
        if text.startswith(f"{actor}:"):
            return actor, text[len(actor) + 1:].strip()
    return "", text


class ClaudeClient:
    """Claude API の薄いラッパ。SDK 未導入・キー無しでも落ちない。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None
        self._unavailable_reason: Optional[str] = None
        if not settings.claude_available:
            self._unavailable_reason = "ANTHROPIC_API_KEY が未設定、または CLAUDE_ENABLED=false"
            return
        try:
            import anthropic  # type: ignore

            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        except ImportError:
            self._unavailable_reason = "anthropic パッケージが未導入（pip install anthropic）"
        except Exception as exc:  # 認証設定の不備など
            self._unavailable_reason = f"Claude クライアントを初期化できません: {exc}"

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason or ""

    def complete(self, prompt: str, max_tokens: int = 8000) -> Optional[str]:
        """1 往復だけ。失敗したら None を返し、呼び出し側でフォールバックする。

        max_tokens は思考ぶんも含めた上限。解説は 400 字程度だが、
        余裕を持たせないと思考で使い切って本文が切れることがある。
        """
        if not self._client:
            return None
        try:
            response = self._client.messages.create(
                model=self.settings.claude_model,
                max_tokens=max_tokens,
                system=SYSTEM_PROMPT,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:
            return None

        # 拒否は content を読む前に判定する
        if getattr(response, "stop_reason", None) == "refusal":
            return None
        parts = [
            block.text
            for block in getattr(response, "content", [])
            if getattr(block, "type", "") == "text"
        ]
        text = "".join(parts).strip()
        return text or None


def generate_explanation(
    client: Optional[ClaudeClient],
    context: MoveContext,
) -> str:
    """解説文を生成する。API が使えなければテンプレートで組み立てる。"""
    if client and client.available:
        prompt = f"{context.to_prompt()}\n\n{EXPLANATION_INSTRUCTION}"
        text = client.complete(prompt)
        if text:
            return text
    return template_explanation(context)


def template_explanation(context: MoveContext) -> str:
    """API なしでも成立する解説。

    5 段構成にしてあるのは、以前の 3 段構成が「勝率が何 pt 下がった」で
    終わっていて、読んでも次に何をすればよいか分からなかったため。
    数値だけでなく、読み筋を 1 手ずつなぞり、最後に次回使える確認動作を
    1 つ残す。使う事実はすべて解析エンジン由来で、ここで読みはしない。
    """
    color = "黒" if context.my_color == "B" else "白"
    move = context.actual_move or "パス"
    lines: list[str] = []

    lines.append("何が起きたか:")
    head = (
        f"{context.move_no}手目、{color}番のあなたは {move} と打ちました。"
        f"AI の見立てでは、この一手であなたの勝ちやすさが "
        f"{context.winrate_before:.0f}% から {context.winrate_after:.0f}% へ、"
        f"{context.actual_winrate_drop:.0f}ポイント下がりました。"
    )
    if context.score_before is not None and context.score_after is not None:
        loss = context.score_before - context.score_after
        if loss >= 1.0:
            head += f"地の見込みでいうと、およそ {loss:.0f}目 の損です。"
    lines.append(head)

    lines.append("")
    lines.append("相手の狙い:")
    if context.punish_pv:
        lines.append(
            f"{move} のあと、相手がいちばん厳しく打ってくると、次のように進みます。"
        )
        lines.extend(
            _pv_prompt_lines(context.punish_pv, context.punish_pv_comments, NARRATION_LINES)
        )
        if len(context.punish_pv) > NARRATION_LINES:
            lines.append(f"（このあと {len(context.punish_pv) - NARRATION_LINES} 手続きます。盤面で確認できます）")
        if context.punish_end_winrate is not None:
            lines.append(
                "ここまで進んだ時点で、あなたの勝ちやすさは "
                f"{context.punish_end_winrate:.0f}% です。"
            )
        lines.append(
            "これは双方が最善で打った場合の一本道です。"
            "実際の相手が同じように打ってくるとは限りません。"
        )
    else:
        lines.append(
            f"{move} を打った直後、あなたの勝ちやすさは "
            f"{context.winrate_after:.0f}% まで下がっています。"
            "この局面の具体的な咎め方は、今回は記録できていません。"
        )

    lines.append("")
    lines.append("自分の見落とし:")
    lines.append(_oversight_text(context))

    lines.append("")
    lines.append("どう打つべきだったか:")
    gain = context.best_winrate - context.winrate_after
    best_head = (
        f"ここでは {context.best_move} と打てば、勝ちやすさを "
        f"{context.best_winrate:.0f}% に保てました。"
    )
    if gain >= 1.0:
        best_head += f"実戦との差は {gain:.0f}ポイントです。"
    lines.append(best_head)
    if context.best_pv:
        lines.append("相手が最善で応じても、次のように進みます。")
        lines.extend(
            _pv_prompt_lines(context.best_pv, context.best_pv_comments, NARRATION_LINES)
        )
        if len(context.best_pv) > NARRATION_LINES:
            lines.append(f"（このあと {len(context.best_pv) - NARRATION_LINES} 手続きます。盤面で確認できます）")

    lines.append("")
    lines.append("次に似た場面が来たら:")
    lines.append(_lesson_text(context))

    if context.opponent_missed is True:
        lines.append("")
        lines.append(
            "補足: 実戦では相手もこの咎め方に気づいていませんでした。"
            f"結果として損はしていませんが、{move} 自体は不利になる手です。"
        )
    return "\n".join(lines)


def _oversight_text(context: MoveContext) -> str:
    base = (
        f"{context.actual_move or 'この手'} で勝ちやすさを "
        f"{context.actual_winrate_drop:.0f}ポイント落としました。"
    )
    if not context.tags:
        return base + "今回は、決まったミスの型には当てはまりませんでした。"
    glosses = [
        f"「{tag}」（{TAG_DESCRIPTIONS[tag]}）" if tag in TAG_DESCRIPTIONS else f"「{tag}」"
        for tag in context.tags
    ]
    return base + f"これは {'、'.join(glosses)} にあたるミスです。"


def _lesson_text(context: MoveContext) -> str:
    """次回の場面で実際にできる確認動作を 1 つだけ返す。

    タグが複数付いていても 1 つに絞る。3 つ並べると結局どれも残らない。
    """
    for tag in context.tags:
        if tag in TAG_LESSONS:
            return TAG_LESSONS[tag]
    return (
        "手を決める前に、相手にいちばん厳しく打たれたらどうなるかを"
        "一度だけ考えてから打つ。"
    )


def suggest_tags(
    client: Optional[ClaudeClient],
    context: MoveContext,
    machine_tags: list[str],
) -> list[str]:
    """機械判定で拾えない手筋名・方針レベルの分類を補完する。

    語彙は付録B の初期セットに限定し、勝手な語を増やさない。
    """
    if not client or not client.available:
        return []
    vocabulary = "、".join(TAG_VOCABULARY)
    prompt = (
        f"{context.to_prompt()}\n\n"
        f"すでに機械判定で付いたタグ: {', '.join(machine_tags) or 'なし'}\n\n"
        f"次の語彙からのみ選び、追加すべきタグを最大2つ、カンマ区切りで出力してください。"
        f"該当なしなら「なし」とだけ出力してください。他の文章は書かないでください。\n"
        f"語彙: {vocabulary}"
    )
    text = client.complete(prompt, max_tokens=4000)
    if not text:
        return []
    if "なし" in text and "," not in text:
        return []
    out: list[str] = []
    for raw in text.replace("、", ",").split(","):
        tag = raw.strip().strip("。 　")
        if tag in TAG_VOCABULARY and tag not in machine_tags and tag not in out:
            out.append(tag)
    return out[:2]
