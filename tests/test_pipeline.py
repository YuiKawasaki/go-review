"""取り込み → 解析 → 書き出し までの結線をスタブエンジンで確認する。

スタブの勝率は囲碁的な意味を持たないので、ここで検証するのは
「配線が通っているか」「保存形式が壊れていないか」のみ。
"""
import json
import tempfile
import unittest
from pathlib import Path

from go_review.analysis import analyze_game
from go_review.config import Settings
from go_review.db import Database, loads
from go_review.export import export_all, game_payload
from go_review.ingest import build_meta, title_for
from go_review.katago import StubEngine
from go_review.learning import (
    cross_analysis,
    record_tsumego_session,
    refresh_daily_log,
)
from go_review.sgf import parse_game, sgf_hash
from go_review.tagging import machine_tags
from tests.fixtures import CAPTURE_SGF, SAMPLE_SGF


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings()
        self.settings.data_dir = root
        self.settings.publish_dir = root / "publish"
        self.settings.my_player_name = "wakame_han"
        self.db = Database(self.settings.db_path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _insert_sample(self) -> str:
        game = parse_game(SAMPLE_SGF)
        meta = build_meta(game, self.settings)
        self.db.insert_game(
            {
                "id": "G-0001",
                "notion_page_id": None,
                "sgf_hash": sgf_hash(SAMPLE_SGF),
                "sgf": SAMPLE_SGF,
                "played_at": meta["played_at"],
                "my_color": meta["my_color"],
                "opponent_name": meta["opponent_name"],
                "opponent_rating": meta["opponent_rating"],
                "result": meta["result"],
                "margin": meta["margin"],
                "move_count": meta["move_count"],
                "end_type": meta["end_type"],
                "komi": meta["komi"],
                "board_size": meta["board_size"],
                "status": "未解析",
            }
        )
        return "G-0001"


class TestIngestMeta(PipelineTestCase):
    def test_meta_from_sgf(self):
        game = parse_game(SAMPLE_SGF)
        meta = build_meta(game, self.settings)
        self.assertEqual(meta["my_color"], "B")
        self.assertEqual(meta["opponent_name"], ":Go9Bot")
        self.assertEqual(meta["opponent_rating"], 985)
        self.assertEqual(meta["result"], "負")
        self.assertEqual(meta["margin"], -40.0)
        self.assertEqual(meta["end_type"], "終局")

    def test_title_format(self):
        game = parse_game(SAMPLE_SGF)
        title = title_for(build_meta(game, self.settings), game)
        self.assertIn("2026-08-18", title)
        self.assertIn("黒", title)
        self.assertIn(":Go9Bot(985)", title)
        self.assertIn("●", title)

    def test_duplicate_hash_detected(self):
        self._insert_sample()
        self.assertTrue(self.db.game_exists(sgf_hash(SAMPLE_SGF)))


class TestAnalysisWiring(PipelineTestCase):
    def test_analysis_saves_every_move(self):
        game_id = self._insert_sample()
        result = analyze_game(
            db=self.db,
            engine=StubEngine(),
            game_id=game_id,
            sgf_text=SAMPLE_SGF,
            my_color="B",
            settings=self.settings,
        )
        rows = self.db.moves_for(game_id)
        self.assertEqual(len(rows), 34)
        self.assertTrue(all(r["winrate_before"] is not None for r in rows))
        self.assertEqual(result.stub, True)

    def test_status_becomes_analyzed(self):
        game_id = self._insert_sample()
        analyze_game(
            db=self.db, engine=StubEngine(), game_id=game_id, sgf_text=SAMPLE_SGF,
            my_color="B", settings=self.settings,
        )
        status = self.db.scalar("SELECT status FROM games WHERE id = ?", (game_id,))
        self.assertEqual(status, "解析済")

    def test_resume_skips_finished_moves(self):
        game_id = self._insert_sample()
        analyze_game(
            db=self.db, engine=StubEngine(), game_id=game_id, sgf_text=SAMPLE_SGF,
            my_color="B", settings=self.settings,
        )
        first = self.db.moves_for(game_id)[0]["winrate_after"]
        analyze_game(   # 2 回目は途中から
            db=self.db, engine=StubEngine(), game_id=game_id, sgf_text=SAMPLE_SGF,
            my_color="B", settings=self.settings,
        )
        second = self.db.moves_for(game_id)[0]["winrate_after"]
        self.assertEqual(first, second)   # 決定的スタブなので一致する

    def test_no_problems_generated_for_stub(self):
        game_id = self._insert_sample()
        result = analyze_game(
            db=self.db, engine=StubEngine(), game_id=game_id, sgf_text=SAMPLE_SGF,
            my_color="B", settings=self.settings,
        )
        self.assertEqual(result.problems, [])


class TestExport(PipelineTestCase):
    def test_export_writes_files(self):
        game_id = self._insert_sample()
        analyze_game(
            db=self.db, engine=StubEngine(), game_id=game_id, sgf_text=SAMPLE_SGF,
            my_color="B", settings=self.settings,
        )
        out = export_all(self.db, self.settings)
        index = json.loads((out / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(index["games"]), 1)
        self.assertTrue((out / "games" / f"{game_id}.json").exists())
        self.assertTrue((out / "problems.json").exists())
        self.assertTrue((out / "dashboard.json").exists())

    def test_winrate_is_always_my_perspective(self):
        game_id = self._insert_sample()
        analyze_game(
            db=self.db, engine=StubEngine(), game_id=game_id, sgf_text=SAMPLE_SGF,
            my_color="B", settings=self.settings,
        )
        payload = game_payload(self.db, game_id)
        self.assertEqual(payload["my_color"], "B")
        for move in payload["moves"]:
            if move["winrate"] is not None:
                self.assertGreaterEqual(move["winrate"], 0.0)
                self.assertLessEqual(move["winrate"], 100.0)


class TestTagging(PipelineTestCase):
    def test_atari_tag_on_capture_game(self):
        game = parse_game(CAPTURE_SGF)
        # 白 bb は最後に取られる。その直前の白の手（4手目 W[ii]）は
        # 自分の石を助けなかった手なので、少なくとも例外なくタグ判定できること
        tags = machine_tags(game, 4, "W", {}, {"delta": -25.0, "coord": "J1"})
        self.assertIsInstance(tags, list)

    def test_tagging_never_raises_on_passes(self):
        game = parse_game(SAMPLE_SGF)
        tags = machine_tags(game, game.move_count, "W", {}, {"delta": -30.0, "coord": "pass"})
        self.assertIsInstance(tags, list)


class TestLearningRecords(PipelineTestCase):
    def test_tsumego_session_and_daily_log(self):
        record_tsumego_session(self.db, solved=20, wrong=3, themes=["アタリ見落とし"], source="詰碁アプリ")
        summary = refresh_daily_log(self.db)
        self.assertEqual(summary["tsumego_count"], 20)
        self.assertEqual(summary["tsumego_wrong"], 3)

    def test_cross_analysis_shape(self):
        record_tsumego_session(self.db, solved=10, wrong=1, themes=["切断された"])
        rows = cross_analysis(self.db)
        self.assertIsInstance(rows, list)
        for row in rows:
            self.assertIn("diagnosis", row)
            self.assertIn("action", row)


if __name__ == "__main__":
    unittest.main()
