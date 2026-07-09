# X-Ray

NotionのXアカウントDBを元に、監視対象アカウントの投稿を15分おきに取得し、
カテゴリ別に切り替えて見られるWebビューア。

## 技術スタック

| レイヤー | 技術 | 備考 |
|---|---|---|
| **スクレイピング** | [twscrape](https://github.com/vladkens/twscrape) | XのモバイルAPIを叩く非公式ライブラリ |
| **HTTPバックエンド** | curl-cffi | CloudflareのTLSフィンガープリント対策 |
| **認証方式** | クッキー認証（auth_token + ct0） | パスワードログインはbot判定でブロックされるため |
| **データストア** | SQLite | WALモードで運用 |
| **Webフレームワーク** | Flask | Jinja2テンプレートでSSRレンダリング |
| **インフラ** | Docker Compose | workerとwebの2コンテナ構成 |
| **定期実行** | cron（コンテナ内） | 15分おきにスクレイプ |
| **フロントエンド** | Vanilla JS + CSS | フレームワーク不使用 |
| **アイコン取得** | [unavatar.io](https://unavatar.io) | RT元アカウントのアイコン取得に使用 |
| **動作環境** | Ubuntu Linux + Docker | 自宅サーバー想定 |

## 構成

```
worker (Python + twscrape + cron)
  └─ 15分おきに監視対象の最新ツイートを取得 → SQLiteに保存

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

`[*] 全件取得完了` まで待つ（数分かかる）。以後は15分おきにcronで自動実行される。

### 6. ブラウザで確認

```
http://<サーバーのIP>:8501
```

投稿一覧が表示されて、カテゴリタブが切り替えられればセットアップ完了。

## 運用Tips

- **ログ確認**: `docker exec -it x-ray-worker tail -f /var/log/scraper.log`
- **状態確認画面**: `http://<host>:8501/status` でアカウントごとの最終取得時刻・エラー履歴が見れる
- **再ログインが必要になったら**: `docker exec -it x-ray-worker python scraper.py relogin`
- **監視対象を増やしたい・編集したい**:

  **方法A: Web UI（推奨）** — `http://<host>:8501/manage` の管理ページから
  アカウントの追加・削除ができる。追加は即DBに反映され、`data/accounts.json` にも保存される。

  **方法B: accounts.json を直接編集** — `data/accounts.json` を編集して `docker compose restart worker`。
  ```json
  {
    "categories": ["ギャル", "videogame", "..."],
    "accounts": [
      {"screen_name": "nemoto_nagi", "display_name": "根本凪", "categories": ["ギャル"]}
    ]
  }
  ```

  - `screen_name`: Xのユーザー名（`@`は付けない）
  - `display_name`: 画面に表示される名前（日本語OK）
  - `categories`: タブ分類用カテゴリ。新カテゴリを使う場合は `categories` 配列にも追記する

  監視対象マスタは `data/accounts.json` で一元管理される（`seed_accounts.py` の `ACCOUNTS_FALLBACK`
  は accounts.json が存在しない場合の初期データとしてのみ使用される）。

- **クッキー更新**: `http://<host>:8501/manage` の管理ページからブラウザで貼り付けて更新できる。
  更新後、サーバーで `docker exec -it x-ray-worker python scraper.py add-cookies` を実行して反映。

- **取得間隔を変えたい**: `Dockerfile.worker` の cron 設定（デフォルト `*/15 * * * *` = 15分おき）を編集してrebuild。
  垢数に余裕があるので10分程度まで縮められるが、5分以下はbot検知リスクが上がる。

## 注意点

- twscrapeはXの内部APIを利用するスクレイピングのため、X側の仕様変更で突然動かなくなる可能性あり
- アカウントが弾かれた場合は`accounts.txt`に追加で捨て垢を足すか、`relogin`を試す
- 取得失敗は`/status`画面のログで確認できる
