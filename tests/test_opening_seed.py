"""序盤問題の登録経路をスタブエンジンで確認する（正解の中身は検証しない）。"""
import tempfile
import unittest
from pathlib import Path

from go_review.config import Settings
from go_review.db import Database, loads
from go_review.katago import StubEngine
from go_review.opening_seed import (
    OPENING_GAME_ID,
    OPENINGS,
    STEPS,
    _sgf,
    _symmetric_equivalents,
    build_opening_problems,
)
from go_review.problems import problem_payload
from go_review.sgf import parse_game
from go_review.srs import due_problems


class SymmetryTest(unittest.TestCase):
    def test_tengen_alone_maps_corner_to_all_four_corners(self):
        game = parse_game(_sgf([("B", "E5")]))
        self.assertEqual(sorted(_symmetric_equivalents(game, 1, "C3")), ["C7", "G3", "G7"])

    def test_asymmetric_position_has_no_equivalents(self):
        game = parse_game(_sgf([("B", "E5"), ("W", "C3"), ("B", "D3")]))
        self.assertEqual(_symmetric_equivalents(game, 3, "C4"), [])


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.settings.data_dir = Path(self.tmp.name)
        self.db = Database(self.settings.db_path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_registers_three_per_opening_and_they_are_due(self):
        count = build_opening_problems(self.db, StubEngine())
        self.assertEqual(count, len(OPENINGS) * STEPS)
        rows = self.db.query("SELECT * FROM problems WHERE game_id = ?", (OPENING_GAME_ID,))
        self.assertEqual(len(rows), count)
        due_ids = {d["problem_id"] for d in due_problems(self.db, self.settings, limit=100)}
        for row in rows:
            self.assertIn(row["id"], due_ids)
            labels = {c["label"] for c in loads(row["correct_moves"], [])}
            self.assertIn("最善", labels)

    def test_payload_hides_game_link(self):
        build_opening_problems(self.db, StubEngine())
        payload = problem_payload(self.db, "O-tengen-1")
        self.assertIsNone(payload["source_game_id"])
        self.assertTrue(any(r["kind"] == "best" for r in payload["refutations"]))

    def test_opening_problem_has_no_actual_sequence(self):
        build_opening_problems(self.db, StubEngine())
        self.assertEqual(problem_payload(self.db, "O-tengen-1")["actual_sequence"], [])

    def test_game_problem_carries_real_continuation(self):
        from tests.fixtures import SAMPLE_SGF

        self.db.execute(
            "INSERT INTO games (id, sgf_hash, sgf, my_color) VALUES (?,?,?,?)",
            ("G-0001", "h", SAMPLE_SGF, "B"),
        )
        self.db.execute(
            "INSERT INTO problems (id, game_id, move_no, correct_moves) VALUES (?,?,?,?)",
            ("P-0001", "G-0001", 3, "[]"),
        )
        self.db.commit()
        payload = problem_payload(self.db, "P-0001")
        game = parse_game(SAMPLE_SGF)
        self.assertEqual(payload["actual_sequence"][0], "A7")   # 3 手目 B[ac]
        self.assertEqual(len(payload["actual_sequence"]), min(10, len(game.moves) - 2))
        self.assertEqual(len(payload["actual_comments"]), len(payload["actual_sequence"]))

    def test_rerun_replaces_instead_of_duplicating(self):
        build_opening_problems(self.db, StubEngine())
        build_opening_problems(self.db, StubEngine())
        n = self.db.scalar("SELECT COUNT(*) FROM problems WHERE game_id = ?", (OPENING_GAME_ID,))
        self.assertEqual(n, len(OPENINGS) * STEPS)


if __name__ == "__main__":
    unittest.main()
