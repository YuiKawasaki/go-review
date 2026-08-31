# 引継ぎメモ（2026-08-31 更新）

前セッションぶんに、この日の改善作業の状況を追記した。

## 今すぐ確認すること

1. **解析バッチが動いているか。**
   バックグラウンドタスク `b71eufosr` が
   `python -m go_review --quiet run && wrangler pages deploy web --project-name=go-review --branch=main --commit-dirty=true`
   を実行中（2026-08-31 20:57 開始）。開始時点で 未解析3 / 解析中1 / 解析済33。

2. **バッチが終わったら、続きの一式を流す。** ユーザーから「通してください」と許可済み。
   ```
   bash <scratchpad>/post_batch.sh
   ```
   中身は次の順（KataGo を使うものが先）:
   1. `python -m go_review deepen-problems` … 既存73問の局面を再解析し、候補手を上位8手まで揃える（20〜40分）
   2. `python -m go_review seed-tsumego` … 詰碁候補34件を検証して登録（30〜60分）
   3. `python -m go_review build-refutations`
   4. `python -m go_review regenerate-explanations`
   5. `python -m go_review export`
   6. `wrangler pages deploy web --project-name=go-review --branch=main --commit-dirty=true`

3. **git はまだコミットしていない。** push はユーザーの明示許可が出てから。

## この日の改善作業（設計は docs/DESIGN-2026-08-31-improve.md）

ユーザーの要望は3つ。解説が定型文すぎる／正解手と誤答手の進行を盤で見たい／詰碁が少ない。

### 決まった方針

- **Claude API は使わない**（`.env` の `ANTHROPIC_API_KEY` は空のまま）。テンプレート解説を作り込む方向。
  `anthropic` パッケージはこのセッションで導入済み。キーを入れれば LLM 側の経路も動く。
- 誤答への咎め手順は、KataGo が候補に挙げた手のぶんだけ事前生成する
- 手順は 1 手ずつ ◀▶ で送る
- 詰碁は手作りシードを増やす（目標20問前後）

### 実装済み（テスト82件・ブラウザ動作確認とも通過）

- `go_review/explain.py` … 解説を5段構成に。`NARRATION_LINES = 6` で文章に並べる読み筋を頭6手に絞る
- `go_review/tagging.py` … `TAG_LESSONS`（次回の確認動作を21タグぶん）を追加
- `go_review/refutations.py` **（新規）** … `refutations` テーブルの読み書き。既存の
  `moves.candidates` と `variations` から手順を組み立てる。**KataGo は使わない**
- `go_review/db.py` … `refutations` テーブル追加（`executescript` で既存DBにも入る）
- `go_review/analysis.py` … 保存する候補手を 3 → 8 手、読み筋を 8 → 10 手に拡大
- `go_review/cli.py` … `build-refutations` / `deepen-problems` / `regenerate-explanations` を追加
- `go_review/tsumego_seed.py` … 候補を8 → 34件。白の石と眼形の空き地だけ書けば黒の囲いは
  計算する方式に変更し、KataGo に渡す前の自己検査 `check_ld_shape()` を追加
- `web/js/sequence.js` **（新規）** … 手順プレイヤー。`web/js/glossary.js` **（新規）** … 用語辞書
- `web/js/app.js` … 練習画面・詰碁画面の答え合わせに手順プレイヤーと用語注釈を組み込み
- `web/sw.js` … キャッシュ名を v5 に上げ、新しい JS を追加

### 途中で見つけて直した不具合

- **`variations.pv_comments()` の主語が反転していた。** 「相手の呼吸点を詰める」が、相手の
  着手を説明する行では自分の石を指してしまっていた。打ち手に応じて「あなた／相手」を
  言い分けるよう修正。既存データは `build-refutations` が作り直す。
- **用語注釈が「6手目」の「目」に当たっていた。** 1文字の語は載せない方針にした
  （日本語は語の区切りが無いので、短い語ほど誤って当たる）。
- **詰碁の再検証で復習履歴が消える問題。** `import_verified` は id で照合していたため、
  候補の作り方を変えると同じ形が別 id で二重登録され、streak も 0 に戻っていた。
  **配石で照合して既存行を引き継ぐ**ように変更（`tests/test_refutations.py` で固定）。

### 設計上の約束（壊すと画面がずれる）

`refutations.pv_moves[0]` は必ずその行の `move` 自身。読み筋は必ず「問題の局面」から始まり、
1手目が学習者の押した手になる。`punish` は本来「実戦の手のあと」から始まるので、
`starts_with_move=False` を渡して先頭に実戦の手を足している。
座標の一致で判断していないのは、たまたま同じ座標が来たときに1手ずれるため。

## 既知のハマりどころ（前セッションから継続）

- **`scripts/run_batch.cmd` は使わない。** cmd.exe 経由で日本語 REM が化けてコマンドが壊れる。
- **長時間実行は Bash ツールの `run_in_background: true`。** PowerShell の `Start-Job` は残らない。
- **ログの文字化け**: PowerShell で読むときは `Get-Content -Encoding OEM`。
  Bash から Python を動かすときは `PYTHONIOENCODING=utf-8` を付ける。
- **Bash のヒアドキュメント内でバックスラッシュが潰れることがある。**
  Python ソースを書き出すときに `"\n"` が実際の改行になって構文エラーになった。
  `chr(92)` などで組み立てるか、書き出し後に必ず import して確認すること。
- **KataGo のタイムアウトは「無応答の継続時間」で測る**（`go_review/katago.py`）。
- **`cmd_analyze` の120分上限は「対局と対局の間」でしかチェックされない**（`go_review/cli.py`）。
- **push 前に必ず差分をシークレットスキャン**:
  `api[_-]?key|token|secret|password|ntn_|sk-ant|Bearer |ykwsk|@gmail`
- **DB を触る作業をバッチと同時にやらない**（SQLite のロック）。
  確認だけなら `sqlite3` のバックアップAPIでコピーを取って、そちらに対して行う。

## Cloudflare Access（前セッションから継続・未対応あり）

2026-08-29 のインシデントは復旧確認済み（`/` と `/data/index.json` とも302でログイン画面）。
ただし当初の目的だった「`/sw.js` だけを Access の外に出す」対応は未実施。
**次に Access 設定を触るときは、既存アプリを編集せず必ず新規アプリを作ること。**

## 参考: ファイル構成

- `go_review/katago.py` — KataGo との通信
- `go_review/analysis.py` — 解析と moves 保存（`CANDIDATE_COUNT` / `CANDIDATE_PV_MOVES`）
- `go_review/refutations.py` — 手順データの組み立て
- `go_review/tsumego_seed.py` — 詰碁候補の生成・検証・登録
- `go_review/explain.py` — 解説文の生成
- `go_review/variations.py` — 変化図と読み筋の一言
- `web/js/app.js` / `sequence.js` / `glossary.js` / `store.js` / `sw.js` — PWA
- デプロイ: `wrangler pages deploy web --project-name=go-review --branch=main --commit-dirty=true`
