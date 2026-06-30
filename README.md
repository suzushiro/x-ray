# X投稿監視システム

NotionのXアカウントDBを元に、監視対象44アカウントの投稿を30分おきに取得し、
カテゴリ別に切り替えて見られるWebビューア。

## 構成

```
worker (Python + twscrape + cron)
  └─ 30分おきに監視対象の最新ツイートを取得 → SQLiteに保存

web (Flask)
  └─ http://<host>:8501 でカテゴリタブ切り替えビューア
```

## セットアップ手順

### 1. Xアカウント情報を準備

`data/accounts.txt` を編集して、7つの捨て垢の情報を1行ずつ追加：

```
username1	password1	email1@example.com	emailpass1
username2	password2	email2@example.com	emailpass2
...
```

タブ区切りなので注意（スペースだとエラーになる）。

### 2. 起動

```bash
cd x-ray
docker compose build
docker compose up -d
```

初回起動時に自動でDB初期化・44アカウントのシードが入る。

### 3. twscrapeにXアカウントを登録

**方法1: パスワードログイン（不安定な場合あり）**

```bash
docker exec -it x-monitor-worker python scraper.py add-accounts
```

X側のCloudflare/bot対策強化により、`400 Could not log you in now`等で失敗することがある。
その場合は方法2（クッキー認証）を使う。

**方法2: クッキー認証（推奨・安定）**

パスワードログインを行わず、ブラウザで人間として一度ログインしたクッキーをそのまま使う方式。
bot判定を受けにくく、現状もっとも安定する。

1. `data/cookies.txt` を作成（タブ区切り、1行1アカウント）:
   ```
   username1	auth_token=xxxxxxxx; ct0=yyyyyyyy
   username2	auth_token=xxxxxxxx; ct0=yyyyyyyy
   ```

2. 各アカウントのクッキー取得方法:
   - ブラウザのシークレットウィンドウで `https://x.com` を開き、対象アカウントでログイン
   - F12 (開発者ツール) → Application タブ → Cookies → `https://x.com`
   - `auth_token` と `ct0` の値(Value列)をコピー
   - `auth_token=コピーした値; ct0=コピーした値` の形式で1行作る

3. 登録実行:
   ```bash
   docker exec -it x-monitor-worker python scraper.py add-cookies
   ```

`ct0`クッキーが含まれていれば即座にアクティブ状態になり、ログイン処理自体が走らないため
bot判定によるブロックを回避しやすい。

### 4. 初回スクレイプを手動実行（任意、起動時に自動実行もされる）

```bash
docker exec -it x-monitor-worker python scraper.py
```

### 5. ブラウザでアクセス

```
http://<サーバーのIP>:8501
```

カテゴリタブ（ギャル / videogame / clubmusic / artist / developer / illustrator 等）
をタップして投稿を絞り込める。

## 運用Tips

- **ログ確認**: `docker exec -it x-monitor-worker tail -f /var/log/scraper.log`
- **状態確認画面**: `http://<host>:8501/status` でアカウントごとの最終取得時刻・エラー履歴が見れる
- **再ログインが必要になったら**: `docker exec -it x-monitor-worker python scraper.py relogin`
- **監視対象を増やしたい**: `app/seed_accounts.py` の `ACCOUNTS` に追記 → `docker compose restart worker`
  （既存のworker起動時に自動でUPSERTされる）
- **取得間隔を変えたい**: `Dockerfile.worker` の cron 設定（`*/30 * * * *`）を編集してrebuild

## 注意点

- twscrapeはXの内部APIを利用するスクレイピングのため、X側の仕様変更で突然動かなくなる可能性あり
- アカウントが弾かれた場合は`accounts.txt`に追加で捨て垢を足すか、`relogin`を試す
- 取得失敗は`/status`画面のログで確認できる
