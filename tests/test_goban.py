import unittest

from go_review.goban import Board, IllegalMove, board_at, distance, weakest_group
from go_review.sgf import parse_game
from tests.fixtures import CAPTURE_SGF, SAMPLE_SGF


class TestLiberties(unittest.TestCase):
    def test_corner_stone_has_two_liberties(self):
        board = Board(9)
        board.play("B", (0, 0))
        self.assertEqual(board.liberty_count((0, 0)), 2)

    def test_center_stone_has_four(self):
        board = Board(9)
        board.play("B", (4, 4))
        self.assertEqual(board.liberty_count((4, 4)), 4)

    def test_connected_group_shares_liberties(self):
        board = Board(9)
        board.play("B", (4, 4))
        board.play("B", (4, 5))
        stones, libs = board.group((4, 4))
        self.assertEqual(len(stones), 2)
        self.assertEqual(len(libs), 6)


class TestCapture(unittest.TestCase):
    def test_single_stone_capture(self):
        board = Board(9)
        board.play("W", (1, 1))
        board.play("B", (1, 0))
        board.play("B", (0, 1))
        board.play("B", (2, 1))
        captured = board.play("B", (1, 2))
        self.assertEqual(captured, [(1, 1)])
        self.assertIsNone(board.get((1, 1)))
        self.assertEqual(board.captures["B"], 1)

    def test_capture_from_sgf(self):
        game = parse_game(CAPTURE_SGF)
        board = board_at(game, game.move_count)
        # 白 bb=(1,1) は取り上げられている
        self.assertIsNone(board.get((1, 1)))

    def test_suicide_is_rejected(self):
        board = Board(9)
        board.play("W", (1, 0))
        board.play("W", (0, 1))
        with self.assertRaises(IllegalMove):
            board.play("B", (0, 0))

    def test_capture_takes_precedence_over_suicide(self):
        """置いた瞬間は呼吸点 0 でも、相手を取れるなら合法。"""
        board = Board(9)
        board.play("W", (0, 0))
        board.play("W", (0, 2))
        board.play("W", (1, 1))
        board.play("B", (1, 0))     # ここで W(0,0) の呼吸点は (0,1) だけ
        # B(0,1) は四方を石に囲まれるが、W(0,0) を取るので合法
        captured = board.play("B", (0, 1))
        self.assertEqual(captured, [(0, 0)])
        self.assertIsNone(board.get((0, 0)))
        self.assertEqual(board.liberty_count((0, 1)), 1)

    def test_occupied_point_is_rejected(self):
        board = Board(9)
        board.play("B", (3, 3))
        with self.assertRaises(IllegalMove):
            board.play("W", (3, 3))

    def test_replay_same_coordinate_after_capture(self):
        """取られた後の同一座標への再着手は正常系（要件 FR-02）。

        2 子を取ると空点が 2 つ空くため、そこへ打ち直しても自殺手にならない。
        （1 子だけ取った跡は相手の眼になるので、打ち直せないのが正しい挙動）
        """
        board = Board(9)
        for c in ((1, 0), (0, 1), (0, 2), (2, 1), (2, 2)):
            board.play("B", c)
        board.play("W", (1, 1))
        board.play("W", (1, 2))          # 白 2 子。呼吸点は (1,3) だけ
        captured = board.play("B", (1, 3))
        self.assertEqual(sorted(captured), [(1, 1), (1, 2)])

        board.play("W", (1, 1))          # 取られた跡へ打ち直せる
        self.assertEqual(board.get((1, 1)), "W")
        self.assertEqual(board.liberty_count((1, 1)), 1)


class TestBoardAt(unittest.TestCase):
    def test_board_at_zero_is_empty(self):
        game = parse_game(SAMPLE_SGF)
        board = board_at(game, 0)
        self.assertEqual(board.stones(), [])

    def test_board_at_counts_stones(self):
        game = parse_game(SAMPLE_SGF)
        board = board_at(game, 4)
        self.assertEqual(len(board.stones()), 4)

    def test_passes_do_not_place_stones(self):
        game = parse_game(SAMPLE_SGF)
        full = board_at(game, game.move_count)
        before_passes = board_at(game, game.move_count - 2)
        self.assertEqual(len(full.stones()), len(before_passes.stones()))


class TestHelpers(unittest.TestCase):
    def test_distance_is_chebyshev(self):
        self.assertEqual(distance((0, 0), (3, 1)), 3)
        self.assertEqual(distance((4, 4), (4, 4)), 0)
        self.assertIsNone(distance(None, (1, 1)))

    def test_weakest_group_picks_fewest_liberties(self):
        board = Board(9)
        board.play("B", (0, 0))       # 隅: 呼吸点 2
        board.play("B", (4, 4))       # 中央: 呼吸点 4
        stones, libs = weakest_group(board, "B")
        self.assertEqual(stones, {(0, 0)})
        self.assertEqual(len(libs), 2)

    def test_position_key_differs_by_position(self):
        a, b = Board(9), Board(9)
        a.play("B", (0, 0))
        b.play("B", (1, 1))
        self.assertNotEqual(a.position_key(), b.position_key())


if __name__ == "__main__":
    unittest.main()
