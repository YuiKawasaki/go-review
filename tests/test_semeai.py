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

    def test_rejects_group_that_can_run_away(self):
        """呼吸点が盤の広い空き地へつながっている形は弾く。

        壁に穴を開けて「外ダメ」を作ったつもりの図。呼吸点の数だけ見れば
        1 つだが、その先が盤全体につながっているので、その石は走って
        逃げられる。攻め合いとして成立しない。
        """
        s = check_shape(parse_diagram("gap", WHITE_HAS_OUTSIDE))
        self.assertTrue(s.faults)
        self.assertTrue(any("逃げ出せる" in f for f in s.faults))

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


EYELESS_13 = [
    ".............",
    ".WWWWWW......",
    ".WbbbbW......",
    ".Wb..bW......",
    ".BwwwwB......",
    ".BBBBBB......",
    ".............",
    ".............",
    ".............",
    ".............",
    ".............",
    ".............",
    ".............",
]


class TestLargerBoard(unittest.TestCase):
    """9 路に収まらない形。盤の大きさは図の行数から決める。

    攻め合いを「両者とも眼を作れない」形にするには、どちらの石も
    単独では空間を囲わないようにする必要がある。9 路の入れ子では
    外側の石が必ず内側に眼のスペースを持ってしまうため、場所を広げて
    黒をアーチ、白を直線にし、間の 2 点だけを共有させている。
    """

    def test_reads_13x13(self):
        s = check_shape(parse_diagram("eyeless", EYELESS_13))
        self.assertEqual(s.size, 13)
        self.assertEqual(s.faults, [])
        self.assertEqual(s.shared, 2)
        self.assertEqual(s.black_outside, 0)
        self.assertEqual(s.white_outside, 0)

    def test_sgf_declares_the_right_size(self):
        s = check_shape(parse_diagram("eyeless", EYELESS_13))
        self.assertIn("SZ[13]", to_sgf(s))

    def test_neither_group_encloses_space(self):
        """どちらの石も単独では空間を囲っていない（＝眼ができない）。

        入れ子で作ると外側の石が内側に眼のスペースを持ってしまい、
        資料 §3 が前提とする「両者に目もない攻め合い」にならない。
        """
        s = check_shape(parse_diagram("eyeless", EYELESS_13))
        # 共有ダメは黒にも白にも接している＝どちらの眼にもならない
        self.assertEqual(s.shared, 2)
        self.assertEqual(s.black_outside + s.white_outside, 0)


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
