"""攻め合いの形の読み取りと手数計算の確認。

KataGo を使う判定（judge_with_engine）はここでは試さない。エンジンが要る
うえに 1 局面で数分かかるため、盤面図の読み取りと数え方だけを見る。

このプロジェクトでは「構造として正しい」ことと「碁として成立する」ことが
別だと何度も分かっているので、ここで守るのは前者だけ。後者は KataGo の
判定と突き合わせて確かめる。
"""
import unittest

from go_review.semeai import (
    check_shape,
    parse_diagram,
    predict,
    to_sgf,
)

# 入れ子の攻め合い: 外側の黒（生き）→ 白の輪 → 中の黒
NESTED_SEKI = [
    ".........",
    ".BBBBBBB.",
    ".BwwwwwB.",
    ".BwbbbwB.",
    ".Bw...wB.",
    ".BwwwwwB.",
    ".BBBBBBB.",
    ".........",
    ".........",
]

# 上の形の右に穴を開けて、白へ外ダメを 1 つ与えたもの
WHITE_HAS_OUTSIDE = [
    ".........",
    ".BBBBBBB.",
    ".BwwwwwB.",
    ".BwbbbwB.",
    ".Bw...w..",
    ".BwwwwwB.",
    ".BBBBBBB.",
    ".........",
    ".........",
]


class TestParsing(unittest.TestCase):
    def test_counts_outside_and_shared_dame(self):
        s = check_shape(parse_diagram("nested", NESTED_SEKI))
        self.assertEqual(s.faults, [])
        self.assertEqual(s.shared, 3)
        self.assertEqual(s.black_outside, 0)
        self.assertEqual(s.white_outside, 0)

    def test_gap_becomes_white_outside_dame(self):
        s = check_shape(parse_diagram("gap", WHITE_HAS_OUTSIDE))
        self.assertEqual(s.faults, [])
        self.assertEqual(s.white_outside, 1)
        self.assertEqual(s.shared, 3)

    def test_rejects_racing_stones_touching_own_wall(self):
        """攻め合いの石が自分の壁とつながっていたら弾く。

        これを通すと「1 つの連の生死」ではなくなり、攻め合いにならない。
        最初に書いた図が実際これで、検査に助けられた。
        """
        bad = [
            "BBBBBBBBB",
            "B...bbbbB",
            "BWWWbwwwB",
            "BW...w..B",
            "BWWWWWWWB",
            "BBBBBBBBB",
            ".........",
            ".........",
            ".........",
        ]
        s = check_shape(parse_diagram("bad", bad))
        self.assertTrue(s.faults)
        self.assertTrue(any("連になっていません" in f for f in s.faults))

    def test_rejects_wrong_sized_diagram(self):
        s = check_shape(parse_diagram("short", ["...", "..."]))
        self.assertTrue(s.faults)

    def test_rejects_capturable_wall(self):
        """まわりの壁が取られる形なら、攻め合い以前の問題として弾く。"""
        bad = [
            "wB.......",
            "B........",
            ".........",
            ".........",
            ".........",
            ".........",
            ".........",
            ".........",
            ".........",
        ]
        s = check_shape(parse_diagram("weak-wall", bad))
        self.assertTrue(s.faults)

    def test_sgf_contains_all_stones(self):
        s = check_shape(parse_diagram("nested", NESTED_SEKI))
        sgf = to_sgf(s)
        self.assertIn("AB[", sgf)
        self.assertIn("AW[", sgf)
        self.assertIn("SZ[9]", sgf)


class TestPredict(unittest.TestCase):
    """資料の規則どおりに数えられていること。

    予測が碁として正しいかは、ここではなく KataGo の判定で確かめる
    （資料の規則自体、両者に外ダメがある前提が抜けている疑いがある）。
    """

    class _Fake:
        def __init__(self, b, w, shared):
            self.black_outside = b
            self.white_outside = w
            self.shared = shared

    def test_no_inside_dame_more_outside_wins(self):
        r = predict(self._Fake(4, 3, 0))
        self.assertEqual(r["outcome"], "black")

    def test_no_inside_dame_equal_first_player_wins(self):
        r = predict(self._Fake(4, 4, 0))
        self.assertEqual(r["outcome"], "black")     # 黒先

    def test_single_inside_dame_is_ignored(self):
        """内ダメ 1 口は中央の 1 手として無視してよい（資料 3-2）。"""
        r = predict(self._Fake(3, 4, 1))
        self.assertEqual(r["outcome"], "white")

    def test_fewer_side_ahead_means_seki_not_a_win(self):
        """外ダメの少ない側が上回っても、勝ちではなくセキ止まり。"""
        r = predict(self._Fake(5, 5, 4))
        self.assertEqual(r["outcome"], "seki")

    def test_document_example_b(self):
        """資料 図B: 白8、黒4＋4−1＝7 → 黒が取られる。"""
        r = predict(self._Fake(4, 8, 4))
        self.assertEqual(r["black_count"], 7)
        self.assertEqual(r["white_count"], 8)
        self.assertEqual(r["outcome"], "white")

    def test_document_example_c_black_first_is_seki(self):
        """資料 図C: 白7、黒4＋4−1＝7 → 黒先ならセキ。"""
        r = predict(self._Fake(4, 7, 4), to_play="B")
        self.assertEqual(r["outcome"], "seki")

    def test_document_example_c_white_first_black_dies(self):
        r = predict(self._Fake(4, 7, 4), to_play="W")
        self.assertEqual(r["outcome"], "white")

    def test_document_example_d(self):
        """資料 図D: 白7、黒5＋4−1＝8 → セキ。"""
        r = predict(self._Fake(5, 7, 4))
        self.assertEqual(r["black_count"], 8)
        self.assertEqual(r["outcome"], "seki")


if __name__ == "__main__":
    unittest.main()
