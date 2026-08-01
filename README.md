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

## 検証用スクリプト（dev/）

コンテナには含まれない（`Dockerfile.*` は `app/` のみ COPY する）開発用ツール。

```bash
cd dev
export DB_PATH=/tmp/xtest/data.db IMAGES_DIR=/tmp/xtest/images CACHE_DIR=/tmp/xtest/cache
export TWITTER_COOKIES_FILE=/tmp/xtest/cookies.txt ACCOUNTS_JSON_PATH=/tmp/xtest/accounts.json
export PERSIST_CATEGORIES="ギャル,illustrator,photographer,gadget" CACHE_RETENTION_DAYS=30

python seed_dummy.py    # 引用RT/画像/動画/自己リプ/ブックマーク入りのダミーDBを作成
python route_check.py   # 全ルートを叩いてステータス＋引用カードの描画を確認
```

`requirements-web.txt`（flask + waitress のみ）だけを入れた venv で実行すると、
web が twscrape 非依存であることも同時に検証できる。

## クッキー収集ツール（tools/）

`F12 → ストレージ → コピー → タブ区切りで貼る` の手作業を置き換える。
ブラウザが必要なのでコンテナには入らない（Dockerfile は `app/` のみ COPY）。
GUI のあるマシンで動かすことを想定。

### 準備

```bash
cd tools
pip install -r requirements-harvester.txt
playwright install chromium
vi accounts.txt          # 1行1ユーザー名（cookies.txt のユーザー名と一致させる）
```

### 使い方

```bash
# 初回。垢ごとにブラウザが開くので手でログインする
python cookie_harvester.py --out ../data/cookies.txt

# 2周目以降。プロファイルが生きているので無操作で取り直せる
python cookie_harvester.py --refresh --out ../data/cookies.txt

# 別マシンから epi1-ubu-1 の web に直接反映
python cookie_harvester.py --refresh --push http://epi1-ubu-1:8501

# 書き込まずに状態だけ見る（期限切れ検知）
python cookie_harvester.py --check

# 切れた垢だけ入れ直す
python cookie_harvester.py --only xconnecter03
```

ログイン操作は人間がやる（パスワードはスクリプトに渡さない）。
垢ごとに `tools/profiles/<username>/` へセッションが永続化されるため、
2回目以降はログイン画面が出ない。

### 挙動のポイント

- 一部の垢だけ取得できた場合、残りは**前回の cookies.txt の値を維持**する。
  生きているセッションを巻き込んで消さないため。前回値が無い場合は中止（`--force` で強行）
- `--out` 指定時は上書き前に `.bak` を作り、パーミッションを 600 にする
- `tools/profiles/` はログインセッションの実体。`.gitignore` 済み。バックアップにも含めないこと
- 終了コード: `0` 全垢OK / `1` 要ログインあり・POST失敗 / `2` アカウント0件 / `3` playwright 未導入

## Firefox 拡張（extension/）

Firefox の **コンテナ（Multi-Account Containers）** ごとに X のクッキーを集めて、
ボタン1発で `/api/cookies/update` に送る。7垢を1ウィンドウにログインしたまま維持できるので、
プロファイルを切り替えて回る必要がない。

`auth_token` は **HttpOnly** なので `document.cookie` やブックマークレットからは読めない。
拡張の `browser.cookies` API 経由でのみ取得できる。

### 導入

1. Firefox に Multi-Account Containers を入れ、垢ごとにコンテナを作る
   （コンテナ名を X のユーザー名と同じにしておくと設定が楽）
2. 各コンテナで x.com にログインしておく
3. 拡張を読み込む
   - お試し: `about:debugging#/runtime/this-firefox` →「一時的なアドオンを読み込む」→ `extension/manifest.json`
     （**再起動で消える**）
   - 恒久: AMO で unlisted 署名を取って `.xpi` をインストール。
     または Developer Edition / ESR で `xpinstall.signatures.required=false`
4. 拡張の設定を開き、サーバーURL（例 `http://epi1-ubu-1:8501`）と
   コンテナ→ユーザー名の対応を設定する。「コンテナ名をそのまま垢名にする」で一括入力できる

### 使い方

ツールバーのアイコンを押すと、コンテナごとの状態（OK / 要ログイン）と
`auth_token` の残り日数が一覧で出る。

- **送信** — `merge=1` で POST。取得できなかった垢の行はサーバー側で維持される
- **コピー** — cookies.txt 形式でクリップボードへ（手で貼りたい時用）
- **再取得** — 読み直し

トークンは拡張のストレージに保存しない。欠落検知のためにユーザー名だけ記録する。

## /api/cookies/update の merge

`merge=1` を付けて POST すると、送った行だけをユーザー名単位で上書きし、
送らなかった垢の行はそのまま残す。一部の垢しか取得できなかった時に、
生きているセッションを巻き込んで消さないため。

レスポンス: `{ok, count(合計), updated(今回更新), kept(維持), merged, previous}`

`merge` 無しは従来どおり全置き換え。`manage` 画面のフォームにはチェックボックスがあり、
既定は merge（貼った垢だけ更新）。外すと全置き換えになり、確認ダイアログが出る。

## twscrape のバージョン

issue #320（`Couldn't get XClientTxId indices script` / `XClIdGen` エラー）は
**0.19.2 で修正済み**。フォーク指定は不要になったので `twscrape[curl]>=0.19.2` に戻し、
`Dockerfile.worker` から `git` も外した。

手元の `requirements.txt` がまだ `git+https://github.com/jonsuh/twscrape.git@...` を
指している場合は、この版で上書きしてから worker を再ビルドすること:

```bash
docker compose build worker --no-cache
docker compose up -d worker
docker exec -it x-ray-worker python scraper.py add-cookies
```

## 画像削除

投稿の画像にホバーすると出る 🗑 ボタン、またはギャラリーの各画像から削除できる。
**サーバー上の実ファイルも消える。復元不可。**

削除すると:

- 実ファイル（`/data/images` と `/data/cache`）を削除
- `deleted_media` テーブルに墓標を残す
- `tweets` / `bookmarks` の `local_media_json` の該当箇所を `None` にする
- 元の投稿には「画像N枚を削除しました」と表示される
- ギャラリーからも消える
- **次回スクレイプで再ダウンロードされない**（墓標が残る限り永久に）

### 設計上の注意

- 墓標のキーは **画像のリモートURL**。これによりブックマーク側、同じ画像を含む
  別の投稿、再スクレイプのすべてに一括で効く。
  `local_media_json` を `None` にするだけでは表示がリモートURLへフォールバックして
  画像が戻ってしまうため、墓標が必須
- dedupe でハードリンク共有されているファイルは、**他からの参照が残っている間は
  実ファイルを消さない**（`still_referenced` で返る）。最後の参照が消えた時に削除される
- API: `POST /api/media/delete` に `tweet_id` + `index`（1枚）または
  `tweet_id` + `all=1`（投稿の画像全部）
