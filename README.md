# minimo 直前割 自動化

minimoサロンツールの「直前割」を Playwright で自動設定／解除する GitHub Actions ワークフローです。
GitHub Actions の `schedule:` は数分〜数十分の遅延があるため、**外部スケジューラ（cron-job.org など）から `workflow_dispatch` API を叩いて毎日決まった時刻に起動する方式** を採用しています。

## スケジュール

| 時刻 (JST) | 動作 |
| --- | --- |
| 毎日 00:00 | `set` (10% 割引を適用) |
| 毎日 18:00 | `remove` (直前割を解除) |

## セットアップ

### 1. GitHub Secrets を登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret** から以下3件を登録します。

| Secret 名 | 内容 |
| --- | --- |
| `MINIMO_SALON_ID` | minimoサロンツールのサロンID |
| `MINIMO_PASSWORD` | サロンツールのログインパスワード |
| `MINIMO_STAFF_HASH` | メニューURLに含まれるスタッフハッシュ |

### 2. fine-grained PAT を発行

外部スケジューラから GitHub の `workflow_dispatch` API を叩くための認証トークン。

1. https://github.com/settings/personal-access-tokens/new
2. 設定:
   - **Token name**: `minimo-discount-trigger` などお好みで
   - **Expiration**: 1 年（任意。短いほど安全だが更新が必要）
   - **Repository access**: **Only select repositories** → `nm4th/minimo` だけを選択
   - **Permissions** → **Repository permissions**:
     - **Actions**: **Read and write** （これだけ）
     - 他は触らない（デフォルト No access）
3. **Generate token** → トークン文字列をコピー（**この画面を閉じるともう見えない**）

### 3. cron-job.org で 2 ジョブ作成

1. https://cron-job.org でアカウント作成
2. **Cronjobs → CREATE CRONJOB** でジョブを 2 件作る

#### ジョブ A: 直前割を設定（毎日 00:00 JST）

| 項目 | 値 |
| --- | --- |
| Title | `minimo set` |
| URL | `https://api.github.com/repos/nm4th/minimo/actions/workflows/minimo-discount.yml/dispatches` |
| Schedule | `Every day at 00:00`（**Timezone: Asia/Tokyo** を選択） |
| Request method | `POST` |
| Request headers | `Authorization: Bearer <PAT>`<br>`Accept: application/vnd.github+json`<br>`X-GitHub-Api-Version: 2022-11-28`<br>`Content-Type: application/json` |
| Request body | `{"ref":"main","inputs":{"action":"set"}}` |

#### ジョブ B: 直前割を解除（毎日 18:00 JST）

ジョブ A と同じ URL／ヘッダで、

| 項目 | 値 |
| --- | --- |
| Title | `minimo remove` |
| Schedule | `Every day at 18:00`（**Timezone: Asia/Tokyo**） |
| Request body | `{"ref":"main","inputs":{"action":"remove"}}` |

> `ref` はワークフローファイルが置かれているブランチ名。デフォルトブランチ以外を指定したいときはここを書き換えてください。

#### 動作確認 (`curl`)

cron-job.org の設定が合っているか確認するには、ローカルから同じ POST を叩けば良いです：

```sh
curl -L -X POST \
  -H "Authorization: Bearer $PAT" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "Content-Type: application/json" \
  https://api.github.com/repos/nm4th/minimo/actions/workflows/minimo-discount.yml/dispatches \
  -d '{"ref":"main","inputs":{"action":"set"}}'
```

成功すると HTTP `204 No Content` が返り、Actions タブに新しい Run が現れます。

## 手動実行

### GitHub Actions UI から

**Actions → minimo-discount → Run workflow** で `action` (set/remove) と `menu` (任意) を選んで実行。

### テスト実行（1メニューだけ）

`menu` 欄にメニュー名の一部（例: `人気No.2`）を入れると、そのメニュー1件にだけ `set` / `remove` を試せます。テストモードでは平日限定／新規／除外キーワードのチェックはスキップし、対象ボタン（直前割作成 / 直前割編集）の有無だけ確認します。

ローカルでは `--menu` フラグ:

```sh
python minimo_discount.py set --menu "人気No.2"     # 1件だけ設定
python minimo_discount.py remove --menu "人気No.2"  # 1件だけ解除
```

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

割引率は `10%`。価格はモーダル側で自動計算されます。

## 注意

- ワークフローの `schedule:` トリガーは外していて、**起動は外部スケジューラ依存**。cron-job.org が落ちている間は走りません。
- PAT が漏れると外部から自由にワークフローを叩けてしまいます。Repository access は `nm4th/minimo` のみ、Permissions は `Actions: Read and write` のみに絞ってください。
- 祝日判定は `jpholiday` パッケージを使用しています。
