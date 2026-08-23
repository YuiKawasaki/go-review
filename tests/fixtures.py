"""テスト用の棋譜。

SAMPLE_SGF は付録A のメタ情報（九路 / コミ7 / 中国ルール / 白40目勝ち /
両者パスによる終局）を再現した合成譜。着手内容そのものは実戦ではない。
"""

SAMPLE_SGF = (
    "(;GM[1]FF[4]CA[UTF-8]SZ[9]KM[7]RU[Chinese]RE[W+40.0]"
    "PB[wakame_han (837)]PW[:Go9Bot (985)]DT[2026-08-18]"
    ";B[aa];W[bb];B[ac];W[bd];B[ae];W[bf];B[ag];W[bh]"
    ";B[ai];W[db];B[ca];W[dd];B[cc];W[df];B[ce];W[dh]"
    ";B[cg];W[fb];B[ci];W[fd];B[ea];W[ff];B[ec];W[fh]"
    ";B[ee];W[hb];B[eg];W[hd];B[ei];W[hf];B[ga];W[hh]"
    ";B[];W[])"
)

# 取り上げが起きる最小の譜: 白 bb を黒 4 子で囲んで取る
CAPTURE_SGF = (
    "(;GM[1]FF[4]SZ[9]KM[7]RU[Chinese]RE[B+R]PB[wakame_han]PW[opponent]"
    ";B[ba];W[bb];B[ab];W[ii];B[cb];W[hi];B[bc])"
)

# 分岐つき（本線だけを読むことの確認用）
BRANCHED_SGF = (
    "(;GM[1]FF[4]SZ[9]KM[7]PB[wakame_han]PW[bot]"
    ";B[ee];W[ec]"
    "(;B[gc];W[cc])"
    "(;B[cc];W[gc]))"
)

# 本文に紛れ込んだ SGF（Notion のページ本文を模したもの）
NOISY_PAGE_TEXT = (
    "2026/08/18 の対局です。\n"
    "囲碁クエストから共有しました。\n"
    + SAMPLE_SGF
    + "\nメモ: 序盤で押されていた気がする"
)
