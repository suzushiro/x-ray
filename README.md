# X-RAY

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

### 1. ファイルを配置

```bash
cd ~
unzip x-ray.zip
cd x-ray   # ← 以降のコマンドは必ずここで実行（重要）
```

### 2. ビルド・起動

```bash
docker compose build
docker compose up -d
```

初回起動時に自動でDB初期化・監視対象アカウントのシードが入る。

### 3. クッキーを取得して `data/cookies.txt` に記入

X側のbot対策により、パスワードログインは現状ほぼブロックされるため、
**クッキー認証が実質必須**。7垢分、1アカウントずつ繰り返す。

1. Firefoxの**プライベートウィンドウ**（Ctrl+Shift+P）で `https://x.com` を開く
2. 捨て垢でログイン
3. **F12** → **ストレージ** タブ → **Cookie** → `https://x.com`
4. `auth_token` と `ct0` の値（Value列）をコピー
5. `data/cookies.txt` に以下の形式で追記（区切りは**タブ**、スペース不可）：

```
xconnecter01	auth_token=コピーした値; ct0=コピーした値
xconnecter02	auth_token=コピーした値; ct0=コピーした値
```

Chromeの場合は F12 → **Application** タブ → **Cookies** → `https://x.com` で同じ値を取得できる。

### 4. クッキーを登録

```bash
docker exec -it x-ray-worker python scraper.py add-cookies
```

`[+] 7/7 件のアカウントをクッキー認証で登録しました` と出ればOK。

### 5. 初回スクレイプを手動実行

```bash
docker exec -it x-ray-worker python scraper.py
```

`[*] 全件取得完了` まで待つ（数分かかる）。以後は30分おきにcronで自動実行される。

### 6. ブラウザで確認

```
http://<サーバーのIP>:8501
```

投稿一覧が表示されて、カテゴリタブが切り替えられればセットアップ完了。

## 運用Tips

- **ログ確認**: `docker exec -it x-ray-worker tail -f /var/log/scraper.log`
- **状態確認画面**: `http://<host>:8501/status` でアカウントごとの最終取得時刻・エラー履歴が見れる
- **再ログインが必要になったら**: `docker exec -it x-ray-worker python scraper.py relogin`
- **監視対象を増やしたい・編集したい**: `app/seed_accounts.py` を編集 → `docker compose restart worker`
  （既存のworker起動時に自動でUPSERTされる）

  `ACCOUNTS` リストに `(screen_name, display_name, [カテゴリ一覧])` のタプルを追加する：

  ```python
  ACCOUNTS = [
      ("nemoto_nagi", "根本凪", ["ギャル"]),
      # ↓ 新規追加はこのように1行足す
      ("new_account_id", "表示名", ["カテゴリ名"]),
      ...
  ]
  ```

  - `screen_name`: Xのユーザー名（`@`は付けない、例: `nemoto_nagi`）
  - `display_name`: 画面に表示される名前（日本語OK）
  - `categories`: タブ分類用のカテゴリ。1つでも複数（`["ギャル", "artist"]`等）でもOK

  **新しいカテゴリを使う場合**は、同ファイル下部の `ALL_CATEGORIES` リストにもカテゴリ名を追記しないと、
  タブ一覧に表示されない（投稿自体は取得・保存されるが、絞り込みタブが出ない状態になる）：

  ```python
  ALL_CATEGORIES = [
      "ギャル", "videogame", "clubmusic", "artist",
      "writer", "developer", "illustrator", "news", "photographer",
      "gadget", "R18",
      "新しいカテゴリ名",  # ← 追加
  ]
  ```

  既存アカウントの `screen_name` を変更（Xアカウント名変更時など）した場合、`screen_name` が
  主キーになっているため、古い名前の行は自動では消えない。手動で消したい場合：

  ```bash
  docker exec -it x-ray-worker python -c "
  from db import get_conn
  conn = get_conn()
  conn.execute(\"DELETE FROM accounts WHERE screen_name='古いscreen_name'\")
  conn.execute(\"DELETE FROM tweets WHERE screen_name='古いscreen_name'\")
  conn.commit()
  print('削除完了')
  "
  ```

- **取得間隔を変えたい**: `Dockerfile.worker` の cron 設定（`*/30 * * * *`）を編集してrebuild

## 注意点

- twscrapeはXの内部APIを利用するスクレイピングのため、X側の仕様変更で突然動かなくなる可能性あり
- アカウントが弾かれた場合は`accounts.txt`に追加で捨て垢を足すか、`relogin`を試す
- 取得失敗は`/status`画面のログで確認できる
