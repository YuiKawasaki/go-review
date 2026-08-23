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
from .tagging import TAG_VOCABULARY

SYSTEM_PROMPT = """あなたは九路盤の囲碁を学ぶ人のための解説者です。

厳守事項:
- 与えられた数値（勝率・目数・呼吸点・座標）だけを根拠に書く。盤面を自分で読み直さない。
- 正解手は与えられたものが唯一の正解。別の手を提案しない。
- 変化図に触れるときは「相手が最善で応じれば」という前提を必ず書く。
- 数値の丸めや言い換えはしてよいが、事実の追加・推測はしない。
- 出力は日本語。専門用語は初段前後の学習者に伝わる範囲で使う。"""

EXPLANATION_INSTRUCTION = """次の3構成で、全体400字以内で書いてください。見出しはこの3つを使ってください。

相手の狙い:
自分の見落とし:
正しい手の理由:"""


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
        if self.punish_pv:
            lines.append(f"実戦の手を相手が咎める読み筋: {' '.join(self.punish_pv)}")
        if self.opponent_missed is True:
            lines.append("補足: 実戦では相手もこの咎め方を見落としていた。")
        return "\n".join(lines)


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
    """API なしでも成立する解説（数値の言い換えのみ）。"""
    tags = "、".join(context.tags) if context.tags else "分類なし"
    lines = [
        "相手の狙い:",
        (
            f"この手のあと、相手が最善で応じると自分の勝率は "
            f"{context.winrate_after:.0f}% まで下がります。"
            + (
                f" 咎め筋は {' '.join(context.punish_pv[:4])} の進行です。"
                if context.punish_pv
                else ""
            )
        ),
        "",
        "自分の見落とし:",
        (
            f"{context.actual_move} は勝率を {context.actual_winrate_drop:.0f}pt 落としました。"
            f" 機械判定された失敗の種類は「{tags}」です。"
        ),
        "",
        "正しい手の理由:",
        (
            f"{context.best_move} なら勝率 {context.best_winrate:.0f}% を保てました。"
            + (
                f" 相手が最善で応じれば {' '.join(context.best_pv[:4])} と進みます。"
                if context.best_pv
                else ""
            )
        ),
    ]
    if context.opponent_missed is True:
        lines.append("")
        lines.append("補足: 実戦では相手もこの咎め方を見落としていました。"
                     "咎められなかっただけで、手そのものは損をしています。")
    return "\n".join(lines)


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
