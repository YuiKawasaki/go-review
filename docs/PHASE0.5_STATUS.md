# Phase 0.5 の進捗（`docs/SETUP.md` に対応）

## 完了（2026-08-23）

| 項目 | 状態 |
|---|---|
| CPU（AVX2） | 確認済み（Intel Core i5-7300U, 2コア4スレッド） |
| メモリ | 8GB（合格） |
| ディスク空き容量 | 5.4GB（10GB 未満だが KataGo 導入後もこの水準を維持） |
| KataGo 本体の導入 | 完了（v1.17.1, Eigen AVX2 版） |
| ニューラルネットワークの導入 | 完了（`kata1-b18c384nbt-s9996604416-d4316597426.bin.gz`, 約93MB） |
| `.env` への反映 | 完了 |
| 単体起動確認（`katago version` / `doctor`） | 完了。`doctor` が「KataGo: あり」を表示 |
| 実測（2パス方式の所要時間） | 完了。判断ゲート通過（下記） |
| 夜間タスクの登録 | 完了 |

### 導入したファイル

- `C:\go-review\katago\katago.exe`（v1.17.1, Eigen backend, AVX2/FMA対応）
- `C:\go-review\katago\model.bin.gz`（b18c384nbt, Elo 13622.5±14.1）
- `C:\go-review\katago\analysis.cfg`（`scripts/analysis.cfg` をコピー。numAnalysisThreads=2 固定）

いずれも OneDrive 同期対象外の `C:\go-review\` 配下に置いた（100MB近いバイナリを
プロジェクトフォルダ＝OneDrive同期対象に置くと同期の無駄が生じるため）。

### 実測結果

対局は本番の Notion データ（棋譜DBに移行済みの実対局）から抽出。手数はいずれも
実際の9路盤対局の分布内（15局中 30〜90手、中央値56手）。

| 試行 | 対局 | 設定 | パス1 | パス2（フラグ数） | 合計 | 検出結果 |
|---|---|---|---|---|---|---|
| 1回目 | G-0001（56手） | PASS1=150 / PASS2=1500 | 26.2分 | 36.0分（6局面） | **62.1分** | 悪手3件 / 問題2問 |
| 2回目 | G-0014（52手） | PASS1=100 / PASS2=1500 | 21.8分 | 51.4分（8局面） | **73.2分** | 悪手3件 / 問題1問 |

**わかったこと**: 全体の所要時間はパス1のvisits設定よりも、パス2でフラグされる
局面数（対局内容に依存し、事前に制御できない変数）に強く支配される。今回
PASS1_VISITSを150→100に下げたが、たまたま2局面多くフラグされたため合計は
むしろ悪化した。真に効くレバーはPASS2_VISITS（現状1500、下限目安1000）だが、
1回の検証に60〜70分かかるためこれ以上の反復検証は費用対効果が薄いと判断した。

### 判断（判断ゲートの結論）

**設定は既定値（PASS1_VISITS=150 / PASS2_VISITS=1500）のまま確定し、Surface を
解析機として採用する。**

- 実測は60〜75分程度で、要件の「20分以内」の基準からは外れるが、夜間バッチの
  1回の実行上限（120分）には収まる
- 1日1局程度の対局ペースであれば実用上問題ないと判断
- 対局頻度が上がりバックログが恒常的に溜まるようであれば、その時点で
  PASS2_VISITS の引き下げ、または Oracle Cloud Always Free への移行を再検討する

### 夜間タスクの登録

`scripts/register_task.ps1` を実行して登録済み。

| 設定 | 実際の値 |
|---|---|
| トリガー | 毎日 02:00 |
| StartWhenAvailable | True |
| AllowStartIfOnBatteries | False（AC電源時のみ） |
| ExecutionTimeLimit | 3時間（PT3H） |
| Priority | 7（低優先度） |
| MultipleInstances | IgnoreNew |

**副次的に見つかった不具合を修正**: `scripts/register_task.ps1` が BOM無しUTF-8で
保存されていたため、Windows PowerShell 5.1 がスクリプト内の日本語文字列を
システムのコードページ（Shift-JIS）で誤読し、`Register-ScheduledTask` の引数解釈が
壊れて `RunLevel` パラメータへの型変換エラーで失敗していた。UTF-8 with BOM で
保存し直して解消（他の `.ps1` を新規に書く場合も同様の注意が必要）。

## 残り（手作業が必要）

1. **ファイアウォールルールの追加**（検討モード用ローカルサーバ、ポート8777、
   プライベートネットワークのみ許可）。セキュリティ設定の変更にあたるため
   ユーザー自身で実行する。

   ```powershell
   New-NetFirewallRule -DisplayName "go-review local" -Direction Inbound `
     -LocalPort 8777 -Protocol TCP -Action Allow -Profile Private
   ```

2. **バックアップ**: 10年落ちのSSDのため、SQLiteを定期的に別媒体へコピーする
   （`docs/SETUP.md` セクション8参照）。頻度と保存先はユーザーの環境（外付けドライブ
   の有無など）次第のため、方針を決めた上で設定する。

3. 実際に夜間02:00に自動実行されることを2〜3日運用して確認する
   （`Get-Content $env:LOCALAPPDATA\go-review\logs\<日付>.log` で確認可能）。
