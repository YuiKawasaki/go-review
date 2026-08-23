import tempfile
import unittest
from datetime import date
from pathlib import Path

from go_review.badmoves import BAD, CRITICAL, DUBIOUS, classify, is_decided, problem_candidates
from go_review.config import Settings
from go_review.db import Database, dumps
from go_review.problems import compute_difficulty, _region
from go_review.katago import MoveInfo, TurnAnalysis
from go_review.srs import (
    VERDICT_ACCEPTABLE,
    VERDICT_CORRECT,
    VERDICT_WRONG,
    due_problems,
    judge,
    next_due,
    record_answer,
)


def make_settings(tmp: Path) -> Settings:
    s = Settings()
    s.data_dir = tmp
    return s


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.s = Settings()

    def test_thresholds(self):
        self.assertIsNone(classify(-9.9, self.s))
        self.assertEqual(classify(-10.0, self.s), DUBIOUS)
        self.assertEqual(classify(-19.9, self.s), DUBIOUS)
        self.assertEqual(classify(-20.0, self.s), BAD)
        self.assertEqual(classify(-29.9, self.s), BAD)
        self.assertEqual(classify(-30.0, self.s), CRITICAL)

    def test_gain_is_not_a_mistake(self):
        self.assertIsNone(classify(+15.0, self.s))

    def test_decided_positions_excluded(self):
        self.assertTrue(is_decided(4.0, self.s))
        self.assertTrue(is_decided(96.0, self.s))
        self.assertFalse(is_decided(50.0, self.s))

    def test_problem_cap_and_priority(self):
        bad = [
            {"move_no": 10, "severity": BAD, "delta": -21.0},
            {"move_no": 20, "severity": CRITICAL, "delta": -35.0},
            {"move_no": 30, "severity": DUBIOUS, "delta": -12.0},
            {"move_no": 40, "severity": BAD, "delta": -25.0},
            {"move_no": 50, "severity": CRITICAL, "delta": -31.0},
        ]
        picked = problem_candidates(bad, self.s)
        self.assertEqual(len(picked), 3)                     # 1 局最大 3 問
        self.assertEqual(picked[0]["move_no"], 20)           # 敗着候補が先
        self.assertNotIn(30, [p["move_no"] for p in picked])  # 疑問手は問題化しない


class TestJudge(unittest.TestCase):
    def setUp(self):
        self.correct = [
            {"coord": "D4", "winrate_delta": 0.0, "label": "最善"},
            {"coord": "E5", "winrate_delta": -4.1, "label": "許容"},
        ]

    def test_best_move(self):
        self.assertEqual(judge("D4", self.correct), VERDICT_CORRECT)

    def test_acceptable_move(self):
        self.assertEqual(judge("E5", self.correct), VERDICT_ACCEPTABLE)

    def test_case_insensitive(self):
        self.assertEqual(judge("d4", self.correct), VERDICT_CORRECT)

    def test_wrong_move(self):
        self.assertEqual(judge("A1", self.correct), VERDICT_WRONG)


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.s = Settings()
        self.today = date(2026, 8, 18)

    def test_interval_progression(self):
        self.assertEqual(next_due(1, self.s, self.today), "2026-08-19")   # 翌日
        self.assertEqual(next_due(2, self.s, self.today), "2026-08-21")   # 3日後
        self.assertEqual(next_due(3, self.s, self.today), "2026-08-25")   # 7日後
        self.assertEqual(next_due(4, self.s, self.today), "2026-09-01")   # 14日後

    def test_graduation(self):
        self.assertIsNone(next_due(5, self.s, self.today))


class TestReviewFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.tmp.name))
        self.db = Database(self.settings.db_path)
        self.db.execute(
            "INSERT INTO problems (id, game_id, move_no, correct_moves, difficulty) VALUES (?,?,?,?,?)",
            (
                "P-0001", "G-0001", 34,
                dumps([{"coord": "D4", "label": "最善"}, {"coord": "E5", "label": "許容"}]),
                3,
            ),
        )
        self.db.execute(
            "INSERT INTO problem_state (problem_id, streak, next_due_at, graduated) VALUES ('P-0001',0,NULL,0)"
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_correct_answer_advances_streak(self):
        result = record_answer(self.db, "P-0001", "D4", 12.5, self.settings)
        self.assertTrue(result["is_correct"])
        self.assertEqual(result["streak"], 1)
        self.assertIsNotNone(result["next_due_at"])

    def test_wrong_answer_resets_streak(self):
        record_answer(self.db, "P-0001", "D4", 5.0, self.settings)
        record_answer(self.db, "P-0001", "D4", 5.0, self.settings)
        result = record_answer(self.db, "P-0001", "A1", 5.0, self.settings)
        self.assertFalse(result["is_correct"])
        self.assertEqual(result["streak"], 0)

    def test_graduates_after_five(self):
        for _ in range(5):
            result = record_answer(self.db, "P-0001", "D4", 3.0, self.settings)
        self.assertTrue(result["graduated"])
        self.assertIsNone(result["next_due_at"])
        self.assertEqual(due_problems(self.db, self.settings), [])

    def test_hint_used_does_not_advance_streak(self):
        record_answer(self.db, "P-0001", "D4", 3.0, self.settings, hint_used=True)
        record_answer(self.db, "P-0001", "D4", 3.0, self.settings, hint_used=True)
        state = self.db.query_one("SELECT streak FROM problem_state WHERE problem_id='P-0001'")
        self.assertEqual(state["streak"], 1)

    def test_unseen_problem_is_due(self):
        due = due_problems(self.db, self.settings)
        self.assertEqual(len(due), 1)
        self.assertTrue(due[0]["first_time"])


class TestDifficulty(unittest.TestCase):
    def _analysis(self, gap: float) -> TurnAnalysis:
        return TurnAnalysis(
            turn=10,
            winrate_black=60.0,
            score_lead_black=2.0,
            visits=1500,
            moves=[
                MoveInfo(None, "D4", 60.0, 2.0, 1000, 0),
                MoveInfo(None, "E5", 60.0 - gap, 1.0, 400, 1),
            ],
        )

    def test_close_candidates_are_harder(self):
        s = Settings()
        tight = compute_difficulty(-22.0, self._analysis(1.0), "B", s)
        clear = compute_difficulty(-22.0, self._analysis(20.0), "B", s)
        self.assertGreater(tight, clear)

    def test_range_is_bounded(self):
        s = Settings()
        value = compute_difficulty(-80.0, self._analysis(0.5), "B", s)
        self.assertLessEqual(value, 5)
        self.assertGreaterEqual(value, 1)


class TestRegion(unittest.TestCase):
    def test_regions(self):
        self.assertEqual(_region("A9", 9), "上左")
        self.assertEqual(_region("E5", 9), "中央")


if __name__ == "__main__":
    unittest.main()
