"""手順データ（refutations）と、解説文の作り直しの確認。

ここで守りたいのは 2 点。

  1. どの手順も「問題の局面」から始まり、1 手目が学習者の押した手になる
     （UI が kind を見ずに同じ描画をできるという前提そのもの）
  2. 解説の作り直しが、KataGo なしで既存データだけから成立する

スタブエンジンの勝率に囲碁的な意味は無いので、数値の正しさではなく
配線と形式だけを見る。
"""
import tempfile
import unittest
from pathlib import Path

from go_review.config import Settings
from go_review.db import Database, dumps, loads
from go_review.explain import MoveContext, template_explanation
from go_review.problems import problem_payload, regenerate_explanation
from go_review.refutations import build_all, load_refutations
from go_review.sgf import parse_game, sgf_hash
from go_review.ingest import build_meta
from tests.fixtures import SAMPLE_SGF

MOVE_NO = 10
BEST_PV = ["G7", "H7", "G6", "H6", "G5"]
PUNISH_PV = ["G7", "H6", "G6", "J6"]
CANDIDATE_PV = ["J5", "J4", "H5", "H4"]


class RefutationTestCase(unittest.TestCase):
    """解析済みの 1 局を DB へ直接組み立てる。

    スタブ解析からは問題を作らない仕様（誤った正解手を教えないため）なので、
    ここでは analyze_game を通さず、解析後に残るはずの行を自分で置く。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings()
        self.settings.data_dir = root
        self.settings.my_player_name = "wakame_han"
        self.db = Database(self.settings.db_path)

        game = parse_game(SAMPLE_SGF)
        meta = build_meta(game, self.settings)
        self.db.insert_game({
            "id": "G-0001",
            "notion_page_id": None,
            "sgf_hash": sgf_hash(SAMPLE_SGF),
            "sgf": SAMPLE_SGF,
            "played_at": meta["played_at"],
            "my_color": "B",
            "opponent_name": meta["opponent_name"],
            "opponent_rating": meta["opponent_rating"],
            "result": meta["result"],
            "margin": meta["margin"],
            "move_count": meta["move_count"],
            "end_type": meta["end_type"],
            "komi": meta["komi"],
            "board_size": meta["board_size"],
            "status": "解析済",
        })
        self.db.execute(
            "INSERT INTO moves (game_id, move_no, color, coord, winrate_before, "
            "winrate_after, score_before, score_after, delta, best_move, candidates, "
            "visits) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("G-0001", MOVE_NO, "B", "H7", 60.0, 30.0, 2.0, -4.0, -30.0, "G7",
             dumps([
                 {"coord": "G7", "winrate": 58.0, "score": 1.5, "visits": 900,
                  "pv": BEST_PV},
                 {"coord": "J5", "winrate": 41.0, "score": -1.0, "visits": 120,
                  "pv": CANDIDATE_PV},
             ]), 1500),
        )
        self.db.execute(
            "INSERT INTO problems (id, game_id, move_no, position_sgf, player_to_move, "
            "actual_move, actual_delta, correct_moves, tags, hints, explanation, "
            "difficulty, position_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("P-0001", "G-0001", MOVE_NO, "", "B", "H7", -30.0,
             dumps([{"coord": "G7", "label": "最善"}]),
             dumps(["アタリ見落とし"]), dumps([]), "（作り直し前）", 3, "k1"),
        )
        for branch, pv in (("best", BEST_PV), ("punish", PUNISH_PV)):
            self.db.execute(
                "INSERT INTO variations (game_id, move_no, branch_type, pv_moves, "
                "pv_comments, end_winrate, end_score, visits) VALUES (?,?,?,?,?,?,?,?)",
                ("G-0001", MOVE_NO, branch, dumps(pv), dumps([""] * len(pv)),
                 58.0 if branch == "best" else 12.0, 1.5, 900),
            )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _problem_ids(self):
        return [r["id"] for r in self.db.query("SELECT id FROM problems ORDER BY id")]


class TestBuild(RefutationTestCase):
    def test_builds_something(self):
        self.assertGreater(build_all(self.db, self.settings), 0)

    def test_covers_best_candidate_and_actual(self):
        build_all(self.db, self.settings)
        kinds = {r["kind"] for r in load_refutations(self.db, "P-0001")}
        self.assertEqual(kinds, {"best", "candidate", "actual"})

    def test_actual_move_is_prepended_to_punish(self):
        """punish は実戦の手のあとから始まる読み筋なので、先頭に足す必要がある。"""
        build_all(self.db, self.settings)
        actual = next(
            r for r in load_refutations(self.db, "P-0001") if r["kind"] == "actual"
        )
        self.assertEqual(actual["pv"][0], "H7")
        self.assertEqual(actual["pv"][1:], PUNISH_PV)

    def test_first_move_of_pv_is_the_move_itself(self):
        """UI がこの約束に依存している。崩れると盤の手順が 1 手ずれる。"""
        build_all(self.db, self.settings)
        rows = self.db.query("SELECT problem_id, move, pv_moves FROM refutations")
        self.assertTrue(rows)
        for row in rows:
            pv = loads(row["pv_moves"], []) or []
            self.assertTrue(pv, f"{row['problem_id']} {row['move']}: 手順が空")
            self.assertEqual(
                pv[0].upper(), row["move"].upper(),
                f"{row['problem_id']}: 手順の 1 手目が {row['move']} でない",
            )

    def test_pv_respects_max_length(self):
        self.settings.pv_max_moves = 4
        build_all(self.db, self.settings)
        for row in self.db.query("SELECT pv_moves FROM refutations"):
            self.assertLessEqual(len(loads(row["pv_moves"], []) or []), 4)

    def test_comment_count_matches_pv(self):
        build_all(self.db, self.settings)
        for row in self.db.query("SELECT pv_moves, pv_comments FROM refutations"):
            self.assertEqual(
                len(loads(row["pv_moves"], []) or []),
                len(loads(row["pv_comments"], []) or []),
            )

    def test_best_is_listed_first(self):
        build_all(self.db, self.settings)
        for problem_id in self._problem_ids():
            refs = load_refutations(self.db, problem_id)
            if any(r["kind"] == "best" for r in refs):
                self.assertEqual(refs[0]["kind"], "best")

    def test_payload_carries_refutations(self):
        build_all(self.db, self.settings)
        payload = problem_payload(self.db, self._problem_ids()[0])
        self.assertIn("refutations", payload)
        self.assertIsInstance(payload["refutations"], list)


class TestRegenerate(RefutationTestCase):
    def test_regenerates_without_katago(self):
        problem_id = self._problem_ids()[0]
        text = regenerate_explanation(self.db, problem_id, self.settings, client=None)
        self.assertIsNotNone(text)
        for heading in ("何が起きたか:", "相手の狙い:", "自分の見落とし:",
                        "どう打つべきだったか:", "次に似た場面が来たら:"):
            self.assertIn(heading, text)

    def test_saved_to_db(self):
        problem_id = self._problem_ids()[0]
        text = regenerate_explanation(self.db, problem_id, self.settings, client=None)
        self.db.commit()
        stored = self.db.scalar(
            "SELECT explanation FROM problems WHERE id = ?", (problem_id,)
        )
        self.assertEqual(stored, text)


class TestTemplateWording(unittest.TestCase):
    def _context(self, **kwargs):
        base = dict(
            move_no=10, my_color="B", actual_move="C3", actual_winrate_drop=30.0,
            winrate_before=60.0, winrate_after=30.0, best_move="D4",
            best_winrate=58.0, total_moves=40,
        )
        base.update(kwargs)
        return MoveContext(**base)

    def test_has_all_sections(self):
        text = template_explanation(self._context())
        for heading in ("何が起きたか:", "相手の狙い:", "自分の見落とし:",
                        "どう打つべきだったか:", "次に似た場面が来たら:"):
            self.assertIn(heading, text)

    def test_lesson_follows_the_tag(self):
        text = template_explanation(self._context(tags=["アタリ見落とし"]))
        self.assertIn("呼吸点", text.split("次に似た場面が来たら:")[1])

    def test_long_pv_is_trimmed_in_text(self):
        pv = [f"A{i}" for i in range(1, 11)]
        text = template_explanation(self._context(
            best_pv=pv, best_pv_comments=[""] * len(pv),
        ))
        self.assertIn("盤面で確認できます", text)

    def test_no_score_line_when_score_missing(self):
        text = template_explanation(self._context(score_before=None, score_after=None))
        self.assertNotIn("目 の損", text)


class TestTsumegoImport(unittest.TestCase):
    """再検証で詰碁を入れ直しても、復習の積み上げが消えないこと。

    候補の作り方を変えると同じ形に別の id が付く。id で照合すると
    同じ問題が 2 問に増え、しかも streak が 0 に戻ってしまう。
    """

    POSITION = (
        "(;GM[1]FF[4]SZ[9]KM[7.0]AB[ab][bb]AW[aa][ba]PL[B])"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.settings.data_dir = Path(self.tmp.name)
        self.db = Database(self.settings.db_path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _item(self, tsumego_id):
        return {
            "id": tsumego_id,
            "theme": "死活（三目の急所）",
            "difficulty": 2,
            "position_sgf": self.POSITION,
            "player_to_move": "B",
            "correct_moves": [{"coord": "A9", "label": "最善", "note": "A9 が正解です。"}],
            "hints": [],
            "gap_winrate": 30.0,
            "gap_score": 8.0,
            "refutations": [
                {"move": "A9", "kind": "best", "pv": ["A9", "B9", "C9"],
                 "winrate": 90.0, "score": 8.0, "visits": 800},
            ],
        }

    def test_same_position_reuses_row_and_keeps_streak(self):
        from go_review.tsumego_seed import import_verified

        import_verified(self.db, [self._item("T-old-name")])
        self.db.execute(
            "UPDATE tsumego SET streak = 3, next_due_at = '2026-09-10' WHERE id = ?",
            ("T-old-name",),
        )
        self.db.commit()

        # 名前を変えて入れ直す（候補の作り方を変えた状況）
        import_verified(self.db, [self._item("T-new-name")])

        rows = self.db.query("SELECT id, streak, next_due_at FROM tsumego")
        self.assertEqual(len(rows), 1, "同じ配石が 2 問に増えてはいけない")
        self.assertEqual(rows[0]["id"], "T-old-name")
        self.assertEqual(rows[0]["streak"], 3)
        self.assertEqual(rows[0]["next_due_at"], "2026-09-10")

    def test_refutations_saved_for_tsumego(self):
        from go_review.tsumego_seed import import_verified

        import_verified(self.db, [self._item("T-x")])
        refs = load_refutations(self.db, "T-x")
        self.assertEqual([r["kind"] for r in refs], ["best"])
        self.assertEqual(refs[0]["pv"][0], "A9")
        self.assertEqual(len(refs[0]["comments"]), len(refs[0]["pv"]))
