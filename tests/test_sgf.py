import unittest

from go_review.sgf import (
    coord_to_gtp,
    coord_to_sgf,
    extract_sgf,
    gtp_to_coord,
    parse_game,
    parse_result,
    position_sgf,
    sgf_hash,
    sgf_to_coord,
    split_player,
)
from tests.fixtures import BRANCHED_SGF, CAPTURE_SGF, NOISY_PAGE_TEXT, SAMPLE_SGF


class TestCoordinates(unittest.TestCase):
    def test_sgf_round_trip(self):
        for s in ("aa", "ee", "ii"):
            coord = sgf_to_coord(s, 9)
            self.assertIsNotNone(coord)
            self.assertEqual(coord_to_sgf(coord), s)

    def test_pass_forms(self):
        self.assertIsNone(sgf_to_coord("", 9))
        self.assertIsNone(sgf_to_coord("tt", 9))   # 盤外はパス扱い

    def test_gtp_conversion(self):
        # 左上 aa は 9 路盤で A9、左下 ai は A1
        self.assertEqual(coord_to_gtp((0, 0), 9), "A9")
        self.assertEqual(coord_to_gtp((0, 8), 9), "A1")
        # GTP は I 列を使わない
        self.assertEqual(coord_to_gtp((8, 0), 9), "J9")
        self.assertEqual(gtp_to_coord("J9", 9), (8, 0))
        self.assertEqual(gtp_to_coord("A1", 9), (0, 8))

    def test_gtp_round_trip(self):
        for col in range(9):
            for row in range(9):
                gtp = coord_to_gtp((col, row), 9)
                self.assertEqual(gtp_to_coord(gtp, 9), (col, row))

    def test_pass_to_gtp(self):
        self.assertEqual(coord_to_gtp(None, 9), "pass")
        self.assertIsNone(gtp_to_coord("pass", 9))


class TestParsing(unittest.TestCase):
    def test_metadata(self):
        game = parse_game(SAMPLE_SGF)
        self.assertEqual(game.size, 9)
        self.assertEqual(game.komi, 7.0)
        self.assertEqual(game.rules, "Chinese")
        self.assertEqual(game.result, "W+40.0")
        self.assertEqual(game.pb, "wakame_han (837)")
        self.assertEqual(game.pw, ":Go9Bot (985)")

    def test_move_count_includes_passes(self):
        game = parse_game(SAMPLE_SGF)
        self.assertEqual(game.move_count, 34)
        self.assertTrue(game.moves[-1].is_pass)
        self.assertTrue(game.moves[-2].is_pass)

    def test_my_color_and_opponent(self):
        game = parse_game(SAMPLE_SGF)
        self.assertEqual(game.my_color("wakame_han"), "B")
        self.assertEqual(game.my_color("WAKAME_HAN"), "B")   # 大文字小文字は無視
        self.assertIsNone(game.my_color("someone_else"))
        name, rating = game.opponent("B")
        self.assertEqual(name, ":Go9Bot")
        self.assertEqual(rating, 985)

    def test_end_type_double_pass(self):
        self.assertEqual(parse_game(SAMPLE_SGF).end_type, "終局")

    def test_end_type_resign(self):
        self.assertEqual(parse_game(CAPTURE_SGF).end_type, "投了")

    def test_branch_takes_main_line(self):
        game = parse_game(BRANCHED_SGF)
        # 本線は第1分岐: ee, ec, gc, cc
        self.assertEqual(game.move_count, 4)
        self.assertEqual(coord_to_sgf(game.moves[2].coord), "gc")

    def test_position_sgf_truncates(self):
        game = parse_game(SAMPLE_SGF)
        partial = parse_game(position_sgf(game, 5))
        self.assertEqual(partial.move_count, 5)
        self.assertEqual(partial.size, 9)
        self.assertEqual(partial.komi, 7.0)


class TestResult(unittest.TestCase):
    def test_loss_margin_is_negative_for_me(self):
        self.assertEqual(parse_result("W+40.0", "B"), ("負", -40.0))

    def test_win_margin_is_positive(self):
        self.assertEqual(parse_result("W+40.0", "W"), ("勝", 40.0))

    def test_resignation_has_no_margin(self):
        outcome, margin = parse_result("B+R", "B")
        self.assertEqual(outcome, "勝")
        self.assertIsNone(margin)

    def test_unknown_result(self):
        self.assertEqual(parse_result("", "B"), (None, None))
        self.assertEqual(parse_result("Void", "B"), (None, None))


class TestPlayerSplit(unittest.TestCase):
    def test_name_with_rating(self):
        self.assertEqual(split_player(":Go9Bot (985)"), (":Go9Bot", 985))

    def test_name_without_rating(self):
        self.assertEqual(split_player("plain_name"), ("plain_name", None))


class TestExtraction(unittest.TestCase):
    def test_extract_from_noisy_text(self):
        extracted = extract_sgf(NOISY_PAGE_TEXT)
        self.assertIsNotNone(extracted)
        self.assertTrue(extracted.startswith("(;GM[1]"))
        self.assertTrue(extracted.endswith(")"))
        self.assertEqual(parse_game(extracted).move_count, 34)

    def test_extract_handles_split_blocks(self):
        # Notion で複数ブロックに割れた本文を連結した想定
        half = len(SAMPLE_SGF) // 2
        joined = SAMPLE_SGF[:half] + SAMPLE_SGF[half:]
        self.assertEqual(extract_sgf(joined), SAMPLE_SGF)

    def test_extract_returns_none(self):
        self.assertIsNone(extract_sgf("SGF はありません"))
        self.assertIsNone(extract_sgf(""))

    def test_hash_ignores_whitespace(self):
        spaced = SAMPLE_SGF.replace(";B[aa]", ";B[aa]\n  ")
        self.assertEqual(sgf_hash(SAMPLE_SGF), sgf_hash(spaced))


if __name__ == "__main__":
    unittest.main()
