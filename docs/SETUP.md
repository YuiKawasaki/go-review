# Phase 0.5: 解析機のセットアップと実測

判断ゲート。**ここで結論を出してから先に進む。** 解析機が決まらないまま実装を
進めると、性能前提が崩れたときに設計ごとやり直しになる。

## 0. 着手前の確認

要件定義書 5.4 のチェックのうち、実機で再確認しておくもの。

| 項目 | 基準 | 備考 |
|---|---|---|
| CPU | AVX2 対応 | i5-7300U は対応。合格 |
| メモリ | 8GB | 合格 |
| バッテリー外観 | 膨張・反り・浮きがないこと | いずれか該当したら**この用途では使わない** |
| OS | Windows 10 22H2 + ESU 登録済み | 2027年10月12日まで更新を受けられる |
| **ストレージ空き** | **10GB 以上を推奨** | 本機は現在 **1.6GB**。KataGo 導入前に空ける必要がある |

> ストレージが逼迫していると Windows Update が失敗し、解析どころか OS の
> 更新が止まる。KataGo（モデル込みで数百MB）を入れる前に、まず空き容量を
> 確保すること。

## 1. Python の導入

```powershell
winget install -e --id Python.Python.3.12
```

「python」と打つと Microsoft Store が開く場合は、ストアのエイリアスを無効に
する（設定 → アプリ → アプリ実行エイリアス → python.exe をオフ）。

確認:

```powershell
python --version
cd C:\path\to\go-review
python run_tests.py
```

外部パッケージは不要。Claude API で解説文を作る場合のみ `pip install anthropic`。

## 2. KataGo の導入

1. 公式リリースから **Windows / Eigen（AVX2）版** を取得する（OpenCL 版ではない）
   - 内蔵 GPU の OpenCL は機種により不安定なため当てにしない
2. 九路盤で実用的なネットワーク（`b18` 系など）をダウンロードする
3. 展開先を決める（例: `C:\go-review\katago\`）
4. 同梱の `scripts/analysis.cfg` を同じ場所に置く
5. `.env` に反映する

```
KATAGO_EXE=C:\go-review\katago\katago.exe
KATAGO_MODEL=C:\go-review\katago\model.bin.gz
KATAGO_CONFIG=C:\go-review\katago\analysis.cfg
KATAGO_THREADS=2
```

`analysis.cfg` は `numAnalysisThreads = 2` 固定。2コア4スレッド機でスレッドを
増やしても速度は伸びず、発熱だけが増える。

## 3. 単体での起動確認

```powershell
C:\go-review\katago\katago.exe version
python -m go_review doctor
```

`doctor` が `KataGo: あり` と表示すれば配線は通っている。

## 4. 実測（このフェーズの本題）

サンプル棋譜を 1 局、2パス方式で解析して所要時間を測る。

```powershell
python -m go_review import path\to\sample.sgf
Measure-Command { python -m go_review analyze }
```

判定基準:

| 実測値 | 判断 |
|---|---|
| 20分以内 | **確定。** Surface を解析機として採用する |
| 20〜60分 | `PASS1_VISITS` / `PASS2_VISITS` を下げて再測定。20pt級の悪手検出は維持できる |
| 60分超 | Oracle Cloud Always Free へ移行する（処理は同じ Python バッチなので移行コストは小さい） |

visits を下げるときは 1 パス目から削る。1 パス目は「大きな落差の検出」だけが
目的なので、100 まで下げても 20pt 級は拾える。2 パス目は正解手を決める場所
なので、下げすぎない（1,000 は確保したい）。

## 5. 発熱とスロットリングへの対処

ファンレス機は持続負荷で数分後にクロックが落ちる。

- 本体を平置きせず、スタンド等で底面に空気を通す
- 布団・クッション・カバンの中では**絶対に動かさない**
- 実行中は必ず AC 接続。蓋を閉じてもスリープしない設定にする
- 無人での長時間放置は避ける（1回の実行上限は既定2時間）

## 6. 夜間バッチの登録

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```

登録される設定と意図:

| 設定 | 値 | 意図 |
|---|---|---|
| トリガー | 毎日 2:00 | 個人利用の時間帯を避ける |
| スケジュールを過ぎてから開始 | 有効 | 起動していなかった日の分を次回起動時に消化する |
| AC電源時のみ | 有効 | バッテリー消耗を防ぐ |
| 優先度 | 低 | ネット閲覧中に走っても体感が重くならないようにする |
| 実行時間の上限 | 3時間 | 想定外の長時間実行を止める |

手動実行:

```powershell
Start-ScheduledTask -TaskName "GoReview Nightly"
Get-Content $env:LOCALAPPDATA\go-review\logs\<日付>.log -Tail 30
```

## 7. セキュリティ

- APIキーはユーザー環境変数か `.env` に置く。スクリプトやリポジトリに直接書かない
- 検討モードのローカルサーバは**プライベートネットワークのみ**許可する

```powershell
New-NetFirewallRule -DisplayName "go-review local" -Direction Inbound `
  -LocalPort 8777 -Protocol TCP -Action Allow -Profile Private
```

- 解析機への外部からの着信は不要。ポート開放・固定IP・DDNS はいずれも要らない

## 8. バックアップ

10年前の SSD は書き込み寿命が進んでいる可能性がある。SQLite は定期的に別媒体へ。

```powershell
$src = "$env:LOCALAPPDATA\go-review\goreview.sqlite3"
Copy-Item $src "D:\backup\goreview-$(Get-Date -f yyyyMMdd).sqlite3"
```

解析結果のマスタは常に手元に持ち、いつでもクラウド↔実機を行き来できる状態を保つ。
