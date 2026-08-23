"""検討モード用のローカルサーバ（要件 5.3 / FR-08 D）。

同一 Wi-Fi 上のスマホから解析機の KataGo に問い合わせるためのもの。
外部公開はしない。Windows のファイアウォールでは
「プライベートネットワークのみ」を許可すること。

PWA 本体と配信 JSON も同じサーバから配れるので、開発時の確認にも使える。
"""
from __future__ import annotations

import json
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Callable, Optional

from .config import Settings
from .db import Database
from .katago import get_engine, is_stub
from .sgf import Game, Move, coord_to_gtp, gtp_to_coord, parse_game

Logger = Callable[[str], None]

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


class _Handler(SimpleHTTPRequestHandler):
    settings: Settings
    log_fn: Logger
    engine = None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    # 既定のアクセスログは黙らせる（作業の妨げにしない）
    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    # ------------------------------------------------------------ GET

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/health"):
            engine_ready = self.settings.katago_available
            self._json(
                {
                    "ok": True,
                    "katago": engine_ready,
                    "mode": "local",
                    "quick_visits": self.settings.quick_visits,
                }
            )
            return
        if self.path.startswith("/api/status"):
            with Database(self.settings.db_path) as db:
                self._json(
                    {
                        "unanalyzed": db.unanalyzed_count(),
                        "games": db.scalar("SELECT COUNT(*) FROM games") or 0,
                    }
                )
            return
        super().do_GET()

    # ------------------------------------------------------------ POST

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/api/analyze"):
            self._handle_analyze()
        elif self.path.startswith("/api/answer"):
            self._handle_answer()
        elif self.path.startswith("/api/tsumego"):
            self._handle_tsumego()
        elif self.path.startswith("/api/note"):
            self._handle_note()
        else:
            self._json({"error": "unknown endpoint"}, 404)

    def _handle_analyze(self) -> None:
        """検討モード: 並べた石をその場で評価する（visits 少なめの高速モード）。"""
        if not self.settings.katago_available:
            self._json(
                {"error": "KataGo が利用できません。検討モードは自宅の解析機起動中のみ使えます。"},
                503,
            )
            return
        payload = self._body()
        sgf_text = payload.get("sgf") or ""
        extra = payload.get("moves") or []      # 追加で並べた手 [["B","D4"], ...]
        visits = int(payload.get("visits") or self.settings.quick_visits)

        try:
            game = parse_game(sgf_text)
        except Exception as exc:
            self._json({"error": f"SGF を解釈できません: {exc}"}, 400)
            return

        moves: list[tuple[str, Optional[tuple[int, int]]]] = [
            (m.color, m.coord) for m in game.moves
        ]
        for item in extra:
            try:
                color = str(item[0]).upper()[0]
                coord = gtp_to_coord(str(item[1]), game.size)
                moves.append((color, coord))
            except Exception:
                continue

        engine = _Handler.engine
        if engine is None:
            self._json({"error": "解析エンジンが起動していません"}, 503)
            return
        try:
            result = engine.analyze(game, [len(moves)], visits, moves_override=moves)
        except Exception as exc:
            self._json({"error": f"解析に失敗しました: {exc}"}, 500)
            return

        analysis = result.get(len(moves))
        if not analysis:
            self._json({"error": "解析結果が空でした"}, 500)
            return

        to_move = "B" if not moves else ("W" if moves[-1][0] == "B" else "B")
        self._json(
            {
                "to_move": to_move,
                "winrate_black": round(analysis.winrate_black, 2),
                "score_lead_black": round(analysis.score_lead_black, 2),
                "visits": analysis.visits,
                "candidates": [
                    {
                        "coord": m.gtp,
                        "winrate_black": round(m.winrate_black, 2),
                        "score_lead_black": round(m.score_lead_black, 2),
                        "visits": m.visits,
                        "pv": m.pv[:6],
                    }
                    for m in analysis.moves[:5]
                ],
                "note": "検討モードで並べた手は棋譜として保存されません。",
            }
        )

    def _handle_answer(self) -> None:
        from .srs import record_answer

        payload = self._body()
        try:
            with Database(self.settings.db_path) as db:
                result = record_answer(
                    db,
                    problem_id=payload["problem_id"],
                    answer_coord=payload.get("coord", ""),
                    think_seconds=float(payload.get("seconds") or 0),
                    settings=self.settings,
                    hint_used=bool(payload.get("hint_used")),
                )
            self._json(result)
        except KeyError as exc:
            self._json({"error": str(exc)}, 404)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def _handle_tsumego(self) -> None:
        from .learning import record_tsumego_session

        payload = self._body()
        try:
            with Database(self.settings.db_path) as db:
                session_id = record_tsumego_session(
                    db,
                    solved=int(payload.get("solved") or 0),
                    wrong=int(payload.get("wrong") or 0),
                    themes=payload.get("themes") or [],
                    source=payload.get("source") or "",
                    on_date=payload.get("date"),
                )
            self._json({"ok": True, "session_id": session_id})
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def _handle_note(self) -> None:
        from .learning import set_note

        payload = self._body()
        try:
            with Database(self.settings.db_path) as db:
                set_note(db, payload["date"], payload.get("note", ""))
            self._json({"ok": True})
        except Exception as exc:
            self._json({"error": str(exc)}, 500)


def serve(settings: Settings, log: Logger = lambda _m: None) -> None:
    _Handler.settings = settings
    _Handler.log_fn = log
    try:
        _Handler.engine = get_engine(settings, allow_stub=False)
        log("KataGo を起動しました（検討モード有効）")
    except FileNotFoundError:
        _Handler.engine = None
        log("KataGo が無いため検討モードは無効です（閲覧・演習のみ利用できます）")

    server = HTTPServer(("0.0.0.0", settings.local_server_port), _Handler)
    log(f"http://<このPCのIP>:{settings.local_server_port}/ で待ち受けます。Ctrl+C で終了。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("停止します")
    finally:
        server.server_close()
        if _Handler.engine is not None:
            _Handler.engine.close()
