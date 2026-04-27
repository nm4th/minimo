# minimo 直前割 自動化

minimoサロンツールの「直前割」を GitHub Actions のスケジュールで自動設定／解除します。

## スケジュール

| 時刻 (JST) | 時刻 (UTC) | 動作 |
| --- | --- | --- |
| 毎日 00:00 | 前日 15:00 | `set` (10% 割引を適用) |
| 毎日 18:00 | 09:00 | `remove` (直前割を解除) |

> GitHub Actions の `schedule:` は混雑時に数分〜数十分遅延することがあります。許容できない場合は外部スケジューラから `workflow_dispatch` API を叩く方式に切り替えてください。

## セットアップ

### 1. GitHub Secrets を登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret** から以下を登録：

| Secret 名 | 内容 |
| --- | --- |
| `MINIMO_SALON_ID` | minimoサロンツールのサロンID |
| `MINIMO_PASSWORD` | サロンツールのログインパスワード |
| `MINIMO_STAFF_HASH` | メニューURLに含まれるスタッフハッシュ |

### 2. ワークフローの有効化

`.github/workflows/minimo-discount.yml` がデフォルトブランチに含まれていれば自動でスケジュール実行されます。

## 手動実行

GitHub の **Actions → minimo-discount → Run workflow** から `set` / `remove` を選んで即時実行できます。

### テスト実行（1メニューだけ）

挙動確認用に、対象メニューを1件に絞れます。

- **GitHub Actions UI**: Run workflow 画面の `menu` 欄にメニュー名の一部（例: `人気No.2`）を入力 → そのメニュー1件にだけ `set` / `remove` を実行
- **ローカル**: `--menu` フラグ

```sh
python minimo_discount.py set --menu "人気No.2"     # 1件だけ直前割を設定
python minimo_discount.py remove --menu "人気No.2"  # 1件だけ解除
```

テストモードでは平日限定／新規／除外キーワードのチェックはスキップし、対象ボタン（直前割作成 / 直前割編集）の有無だけ確認して実行します。

### 通常のローカル実行

```sh
pip install -r requirements.txt
playwright install chromium
export MINIMO_SALON_ID=...
export MINIMO_PASSWORD=...
export MINIMO_STAFF_HASH=...
python minimo_discount.py set     # または remove
```

## 直前割設定の対象条件 (`set`)

すべての条件を満たすメニューに 10% 割引を適用します：

1. メニュー名または説明文に **「平日限定」**（平日実行時）または **「土日祝限定」**（土日祝実行時）を含む
2. メニュー名または説明文に **「新規」** を含む
3. **「公開停止中」** ではない
4. メニュー名に **「韓国風」** または **「パリジェンヌ」** を含まない
5. **「直前割作成」** ボタンが表示されている（既設定はスキップ）

割引価格 = `floor(基準価格 × 0.9)`

## 注意

- セレクタは要件書からの推測ベースです。初回実行で要素が見つからない場合は `minimo_discount.py` 内のセレクタ (`menu_cards`, モーダル内 input など) を実際のDOMに合わせて調整してください。
- 祝日判定は `jpholiday` パッケージを使用しています。
