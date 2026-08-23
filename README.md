# 囲碁 棋譜レビュー & オリジナル問題生成アプリ

要件定義書 v1.0 の Phase 0〜Phase 4 を実装したもの。

- 解析機（Surface / Windows 10）で夜間バッチを回し、スマホは閲覧・演習に徹する構成
- 悪手の判定と正解手の決定は **KataGo が行う**。Claude API は数値を日本語に翻訳する役だけ
- Claude API と KataGo が無くても、アプリとしては動く（解説はテンプレート生成に切り替わる）

## 全体の流れ

```
囲碁クエスト →[棋譜を共有]→ Notion 棋譜DB（受信箱）
                                   │ ① 差分取得
                                   ▼
                       解析機のバッチ（python -m go_review run）
                       ├ SGF パース・メタ情報の自動補完
                       ├ KataGo 2パス解析（全手スクリーニング → 悪手候補を精査）
                       ├ 悪手抽出・タグ付与・変化図の事前生成
                       ├ Claude API で解説文を生成
                       ├ Notion へ書き戻し
                       └ 静的 JSON を書き出し
                                   ▼
                    配信（GitHub Pages 等）→ PWA（Android / iOS）
                                   └ 学習ログは復帰時に自動送信
```

## セットアップ

### 1. 実行環境

Python 3.10 以上が必要です（現時点で本機には未導入）。

```powershell
winget install -e --id Python.Python.3.12
```

導入後、動作確認:

```powershell
python run_tests.py
python -m go_review doctor
```

外部パッケージは **不要** です（Notion API は標準ライブラリの urllib で呼んでいます）。
解説文の生成に Claude API を使う場合のみ:

```powershell
pip install anthropic
```

### 2. 設定

`.env.example` を `.env` にコピーして埋めます。`.env` は `.gitignore` 済みです。

| 変数 | 内容 |
|---|---|
| `NOTION_TOKEN` | インテグレーションシークレット |
| `KIFU_DS_ID` / `LOG_DS_ID` / `OLD_DS_ID` | 各DBの**データソースID**（データベースIDとは別物） |
| `MY_PLAYER_NAME` | `PB`/`PW` との照合に使う自分の名義 |
| `KATAGO_EXE` / `KATAGO_MODEL` / `KATAGO_CONFIG` | Phase 0.5 で導入 |
| `ANTHROPIC_API_KEY` | 未設定ならテンプレートで解説文を作ります |

データソースIDの取得:

```powershell
python scripts/notion_ids.py <DATABASE_ID>
```

### 3. Phase 0（Notion の整備と移行）

詳細は [docs/PHASE0.md](docs/PHASE0.md)。移行スクリプトは既定が dry-run です。

```powershell
python scripts/migrate_notion.py            # 件数と抽出結果だけ表示
python scripts/migrate_notion.py --execute  # 実際に移行
```

### 4. Phase 0.5（KataGo の導入と実測）

[docs/SETUP.md](docs/SETUP.md) を参照。1局20分以内なら確定、60分超ならクラウドへ切り替えます。

### 5. 夜間バッチの登録

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```

毎日 2:00 に低優先度で実行し、起動していなかった日の分は次回起動時にまとめて消化します。

## コマンド

| コマンド | 内容 |
|---|---|
| `python -m go_review run` | 取り込み → 解析 → 書き出し → 書き戻し（バッチ本体） |
| `python -m go_review sync` | Notion から取り込むだけ |
| `python -m go_review analyze` | 未解析キューを処理（`--allow-stub` で KataGo 無しでも配線確認） |
| `python -m go_review export` | PWA 用 JSON を書き出す |
| `python -m go_review writeback` | Notion へ書き戻す |
| `python -m go_review status` | 未解析件数・正答率など |
| `python -m go_review import <file.sgf>` | ローカルの SGF を取り込む |
| `python -m go_review tsumego --solved 20 --wrong 3 --themes "切断された"` | 詰碁の記録 |
| `python -m go_review serve` | 検討モード用ローカルサーバ（自宅Wi-Fi限定） |
| `python -m go_review doctor` | 環境の自己診断 |

## PWA

`web/` 以下が静的ファイル一式です。ビルド不要（依存パッケージなし）。

- 動作確認: `python -m http.server 4173 --directory web` → `http://localhost:4173/`
- 配信: `web/` を GitHub Pages / Cloudflare Pages に置く
- `web/index.html` の `GOREVIEW_CONFIG` で、データの場所と解析機の URL を指定します

`web/data/` には現在サンプルデータが入っています（各 JSON に `"sample": true`）。
`export` を実行すると実データで上書きされます。

## 実装と要件の対応

| 要件 | 実装 |
|---|---|
| FR-01 棋譜取り込み | `go_review/ingest.py` |
| FR-02 SGFパース | `go_review/sgf.py` |
| FR-03 局面解析（2パス・中断耐性） | `go_review/analysis.py`, `katago.py` |
| FR-04 悪手抽出 | `go_review/badmoves.py` |
| FR-05 タグ付与 | `go_review/tagging.py`（機械判定） + `explain.py`（Claude 補完） |
| FR-06 問題生成 | `go_review/problems.py` |
| FR-07 リプレイビューア | `web/js/app.js`, `board.js` |
| FR-08 変化図 | `go_review/variations.py`, `web/js/app.js` |
| FR-09 出題・回答UI | `web/js/app.js` |
| FR-10 復習スケジューリング | `go_review/srs.py` |
| FR-11 学習記録・突合分析 | `go_review/learning.py` |
| FR-12 Notion書き戻し | `go_review/writeback.py` |
| FR-13 ダッシュボード | `go_review/learning.py`, `web/js/app.js` |
| US-07 自己診断の強制 | `web/js/app.js`（棋譜画面のゲート） |

Phase 5（部分盤の詰碁生成 / 相手の悪手の問題化 / 19路盤）は範囲外のため未実装です。

## 設計上の約束

- **正解手は KataGo の数値だけで決める。** Claude には解説とタグ補完しかさせない
- **勝率は常に自分視点**で保存・表示する（黒白で反転させない）
- **解析は1手ごとにコミット**する。電源断や Windows Update の再起動で中断しても再開できる
- **1回の実行は最大2時間**で打ち切り、残りは次回に回す（放置による過熱を防ぐ）
- **1局あたり最大3問・1日10問**の上限を守る
- **SQLite は OneDrive の外**（既定 `%LOCALAPPDATA%\go-review`）に置く。同期フォルダでは壊れる
