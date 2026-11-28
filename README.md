# X-Ray

監視対象のXアカウントの投稿を定期的に取得してアーカイブし、
カテゴリ別に切り替えて閲覧できるセルフホスト型のWebビューア。

自分用のツールとして作っているため、動作は無保証。
X側の仕様変更で壊れることがある。

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
| **動作環境** | Linux + Docker | セルフホスト想定 |

## 構成

```
worker (Python + twscrape + cron)
  └─ 15分おきに監視対象の最新ツイートを取得 → SQLiteに保存

web (Flask)
  └─ http://<host>:<WEB_PORT> でカテゴリタブ切り替えビューア
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
http://<サーバーのIP>:<WEB_PORT>   # WEB_PORT の既定は 8501
```

投稿一覧が表示されて、カテゴリタブが切り替えられればセットアップ完了。

## 多重起動の防止

cron のスクレイプは `flock -n` で保護している。前回の実行がまだ終わっていなければ
その回はスキップされる。

これが無いと、1回の実行が詰まったときに15分ごとにプロセスが積み上がり、
SQLite が `database is locked` で全滅する（実際に35重起動まで積んだことがある）。

所要時間の目安は 87アカウントで約5分半。15分間隔には収まっている。
増やす場合は `docker compose exec worker python -c "
import subprocess, time; t=time.time(); subprocess.run(['python','scraper.py']);
print(time.time()-t)"` で測っておくとよい。

### database is locked が出たら

まず所有者を疑う。`docker compose run` を使うと root でファイルが作られることがあり、
以降 worker が書けなくなる。

```bash
ls -la data/*.db*                              # 所有者を確認
sudo chown $(id -u):$(id -g) data/*.db*        # ずれていたら直す
docker compose restart worker
```

スクレイプ開始時に書き込み可否を検査しているので、権限がずれていれば
`database is locked` ではなく明示的なメッセージで停止する。

コンテナ内の実行中プロセスは以下で確認できる（`ps` は入っていない）。

```bash
docker compose exec worker sh -c 'ls /proc/[0-9]*/cmdline | while read f; do tr "\0" " " < $f; echo; done | grep scraper'
```

## 運用Tips

- **ログ確認**: `docker exec -it x-ray-worker tail -f /var/log/scraper.log`
- **状態確認画面**: `http://<host>:<WEB_PORT>/status` でアカウントごとの最終取得時刻・エラー履歴が見れる
- **再ログインが必要になったら**: `docker exec -it x-ray-worker python scraper.py relogin`
- **監視対象を増やしたい・編集したい**:

  **方法A: Web UI（推奨）** — `http://<host>:<WEB_PORT>/manage` の管理ページから
  アカウントの追加・削除ができる。追加は即DBに反映され、`data/accounts.json` にも保存される。

  **方法B: accounts.json を直接編集** — `data/accounts.json` を編集して `docker compose restart worker`。
  ```json
  {
    "categories": ["art", "gadget", "..."],
    "accounts": [
      {"screen_name": "example_user", "display_name": "表示名", "categories": ["art"]}
    ]
  }
  ```

  - `screen_name`: Xのユーザー名（`@`は付けない）
  - `display_name`: 画面に表示される名前（日本語OK）
  - `categories`: タブ分類用カテゴリ。新カテゴリを使う場合は `categories` 配列にも追記する

  監視対象マスタは `data/accounts.json` で一元管理される（`seed_accounts.py` の `ACCOUNTS_FALLBACK`
  は accounts.json が存在しない場合の初期データとしてのみ使用される）。

- **クッキー更新**: `http://<host>:<WEB_PORT>/manage` の管理ページからブラウザで貼り付けて更新できる。
  更新後、サーバーで `docker exec -it x-ray-worker python scraper.py add-cookies` を実行して反映。

- **取得間隔を変えたい**: `Dockerfile.worker` の cron 設定（デフォルト `*/15 * * * *` = 15分おき）を編集してrebuild。
  垢数に余裕があるので10分程度まで縮められるが、5分以下はbot検知リスクが上がる。

## 注意点

- twscrapeはXの内部APIを利用するスクレイピングのため、X側の仕様変更で突然動かなくなる可能性あり
- アカウントが弾かれた場合は`accounts.txt`に追加で捨て垢を足すか、`relogin`を試す
- 取得失敗は`/status`画面のログで確認できる

## 検証用スクリプト（dev/）

コンテナには含まれない（`Dockerfile.*` は `app/` のみ COPY する）開発用ツール。

### まとめて走らせる（v57）

```bash
python dev/run_all.py              # 全部（26スイート・30秒ほど）
python dev/run_all.py route ig     # 名前に含まれるものだけ
python dev/run_all.py --list       # 一覧
python dev/run_all.py -v           # 落ちたスイートの出力も出す
python dev/run_all.py --json       # 機械で読む形
```

環境変数とダミーデータは**ランナーが用意する**。1スイートにつき
使い捨てのディレクトリを作って `seed_dummy.py` を流すので、
走らせる順番に依存しない。終了コードは失敗があれば1。

出力の例:

```
  通過    x-ray/route_check    51件   0.3秒
  失敗    x-ray/ig_check       2件失敗 5.1秒
  依存欠け x-ray/scraper_check  twscrape  0.1秒

=== 通過 24 / 失敗 1 / 依存欠け 1  （計 26 件）===
```

**依存欠けは検査の失敗と区別する。** `ModuleNotFoundError` を本物の
失敗と同じ見た目にすると、コンテナを作り直した直後に取り違える
（実際に取り違えて、無関係なコードを疑った）。

#### なぜランナーを作ったか

それまではスイートを手で並べて回し、**合否を人間が目で見ていた**。

- `dev/` 配下は環境変数8個と `seed_dummy.py` の用意が要る。忘れると
  「no such table: tweets」という、変更とは無関係な失敗に見える
- `ig/dev/` 配下は合否の文言が `PASS n / FAIL n` で `dev/` 側と違う
- スイート名を手で並べていたので**追加したものが漏れた**。
  `.js` の2本（`extension_check` / `tumblr_ui_check`）は長い間
  一度も走っていなかった
- `grep 失敗` で判定していたため、「取得に失敗したら止める」のような
  **項目名に「失敗」を含む通過**を失敗と誤判定した

ランナー自身の合否判定は `dev/runner_check.py` が検査している。
他の25スイートの合否をこれが決めるので、ここが間違っていると
「全部通っている」という嘘が出る。わざと落ちる／依存が無い／
途中で死ぬ／紛らわしい語を含むスイートを作って分類を確かめている。

### 個別に走らせる

```bash
cd dev
export DB_PATH=/tmp/xtest/data.db IMAGES_DIR=/tmp/xtest/images CACHE_DIR=/tmp/xtest/cache
export TWITTER_COOKIES_FILE=/tmp/xtest/cookies.txt ACCOUNTS_JSON_PATH=/tmp/xtest/accounts.json
export PERSIST_CATEGORIES="art,gadget" CACHE_RETENTION_DAYS=30

python seed_dummy.py    # 引用RT/画像/動画/自己リプ/ブックマーク入りのダミーDBを作成
python route_check.py   # 全ルートを叩いてステータス＋引用カードの描画を確認
```

`requirements-web.txt`（flask + waitress のみ）だけを入れた venv で実行すると、
web が twscrape 非依存であることも同時に検証できる。

### スイートを足すとき

ファイル名を `*_check.py` か `*_check.js` にして `dev/` か `ig/dev/` に
置くだけ。ランナーが拾う。最後に `=== 全て通過 ===` か
`=== 失敗 N件: [...] ===` を出し、成功なら終了コード0にすること。

## クッキー収集ツール（tools/）

`F12 → ストレージ → コピー → タブ区切りで貼る` の手作業を置き換える。
ブラウザが必要なのでコンテナには入らない（Dockerfile は `app/` のみ COPY）。
GUI のあるマシンで動かすことを想定。

### 準備

```bash
cd tools
pip install -r requirements-harvester.txt
playwright install chromium
cp accounts.example.txt accounts.txt && vi accounts.txt          # 1行1ユーザー名（cookies.txt のユーザー名と一致させる）
```

### 使い方

```bash
# 初回。垢ごとにブラウザが開くので手でログインする
python cookie_harvester.py --out ../data/cookies.txt

# 2周目以降。プロファイルが生きているので無操作で取り直せる
python cookie_harvester.py --refresh --out ../data/cookies.txt

# 別マシンから <host> の web に直接反映
python cookie_harvester.py --refresh --push http://<host>:<WEB_PORT>

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
4. 拡張の設定を開き、サーバーURL（例 `http://<サーバー>:<WEB_PORT>`）と
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

現在は **0.21.0 以上**を要求している。

0.21.0 の破壊的変更のうち、X-Ray に効くのは2つ。

**1. `[curl]` extra が廃止**
curl-cffi が必須依存になった。`twscrape[curl]` と書いても
`does not provide the extra 'curl'` の警告が出るだけで入るが、紛らわしいので外した。
httpx に戻したい場合は環境変数 `TWS_HTTP_BACKEND=httpx`。

**2. 失敗が例外で飛ぶようになった**
以前は取得失敗・パース失敗で `None` を返したり黙って反復を終えたりしていたが、
0.21.0 からは `XClIdParseError` / `UserParseError` / `NoAccountError` 等の
専用例外が送出される。`None` や空リストは「Xにデータが無い」を意味するようになった。

`scraper.py` の取得ループは監視対象1件ずつ `try/except Exception` で囲んであるので、
**1件の失敗が全体を巻き込むことはない**（失敗した分は `scrape_log` に error として残る）。
ログには例外の型名も残すようにしてある。旧版は Cloudflare に弾かれても
`user not found` としか出ず、2026-08の全停止で原因を誤認する一因になった。

### 取得件数が少し減ることについて

0.21.0 では「引用ツイートがタイムラインの単独項目として現れない」「ページをまたいだ
重複が二重に yield されない」という修正が入った。どちらも旧版が余計に返していた分なので、
**1アカウントあたりの取得件数が以前より減るのは正常**。新規保存件数には影響しない。

### 過去の経緯

issue #320（`Couldn't get XClientTxId indices script` / `XClIdGen` エラー）は
**0.19.2 で修正済み**。フォーク指定は不要になったので
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

## スクレイピング出口の分離（任意）

IPレピュテーションが落ちた時にメイン回線を巻き込まないよう、スクレイピングの出口を
別回線やVPNに逃がせる。使わない場合は `PROXY_URL` を空にしておけば素通しになる。

`.env` に設定する。

```
PROXY_URL=http://<プロキシのアドレス>:<ポート>
EXPECTED_EGRESS_IP=<想定する出口のグローバルIP>
```

worker にのみproxy環境変数が渡る（web はスクレイピングしないため不要）。
大文字・小文字の両方と、twscrape専用の `TWS_PROXY` を設定している。

### 出口の確認

```bash
docker compose exec worker curl -s https://ifconfig.me     # 想定IPが返ればOK
docker compose exec worker python scraper.py check-egress  # 想定IPと自動照合
```

`EXPECTED_EGRESS_IP` を設定していると、**スクレイプ開始前に自動で出口を照合する**。
設定漏れで気づかずメイン回線から出続けるのを防ぐため。無効化するには空にする。

想定と違ったときは、proxyを外した出口IPと比べて切り分ける。

| 状況 | 判定 | 動作 |
|---|---|---|
| proxy経由と直接の出口が**違う** | proxyは効いている。回線側のIPが変わっただけ | **続行**し、警告を記録 |
| proxy経由と直接の出口が**同じ** | proxyを迂回している（分離が崩れた） | **中止** |
| proxy環境変数が未設定 | 分離されていない | **中止** |
| 直接の出口IPが取れない | 判定できない | 安全側に倒して**中止** |

住宅向け回線はグローバルIPが変わることがあり、そこで毎回中止すると
**取得が死んだことに誰も気づけない**（実際に12日間止まった）。そのため、
分離が効いていることが確認できたときは続行し、警告だけ残す。

警告・中止はどちらも `scrape_log` に記録され、トップ画面のバナーと `/status` に出る。
`.env` を直したあとバナーを消すには:

```bash
docker compose exec worker python scraper.py clear-egress-log
```

### 注意点

- ホストがIPv6グローバルを持つ場合、proxy環境変数を設定していないコンテナはIPv6で
  メイン回線から出る。**設定漏れが分離の抜け穴になる**ので、プロキシ側でIPv6を
  無効化しておくとよい
- curl-cffi（twscrape）も urllib（画像DL）も標準のproxy環境変数を尊重することは実測済み
  （大文字・小文字の両方、`NO_PROXY` の除外も動作確認）
- 出口のグローバルIPが変わったら `.env` の `EXPECTED_EGRESS_IP` を更新する

## IG取得（ig/ 配下）

IG-Ray の取得系を `ig/` に取り込んである。**リポジトリと compose は1つ、
ただしイメージは分ける**（instaloader は Python 3.12 固定で、twscrape とは
依存の系統が違う。同居させると片方の更新でもう片方が壊れる）。

詳細は `ig/README.md`。設定は `.env` の `IG_RAY_*`。
IG-Ray 側を畳む手順は `RETIRE_IG_RAY.md`。

```bash
# profile 付きのサービスは素の build ではビルドされない
docker compose --profile ig-cron --profile ig-tools --profile ig-backfill build
docker compose run --rm ig-ipcheck        # 出口IPの確認
docker compose --profile ig-cron up -d    # 巡回を有効化
```

### IG側の異常もトップに出す

取得を取り込むと、**IG側の異常がどこにも表示されない**状態になる。
IG-Ray で24日、X-Ray で12日、どちらも「止まっているのに画面が正常」で
気づけなかったので、出口・ログイン・無音の3つをトップに出す。

- 出口の異常（`__egress__`）
- ログイン失敗（`__login__`）
- 無音（`IG_RAY_INTERVAL` × `IG_RAY_STALE_INTERVALS` を超えて成功が無い）

### 異常は「OK一覧に無いもの」で判定しない

`__login__` / `__egress__` の行は、**異常なステータスを明示的に列挙して**判定する
（`ig_bridge.BAD_LOGIN_STATUSES` / `BAD_EGRESS_STATUSES`）。

引き算で判定すると、取得側が新しい"正常"ステータスを足すたびに誤報が出る。
実際、IG-Ray がログイン失敗からの復帰を記録する `login_ok`（メッセージは
「取得できました」）を失敗として扱い、
**「IG: ログインに失敗しています 取得できました」**という矛盾した表示を出した。

同じ理由で `backfill_done` は成功扱い（IG-Ray でこれを失敗と数えて赤字の誤報を
出している）。擬似アカウントの行は「取れた」と数えない。

ステータスを増やしたら `ig/app/` 側の `log_scrape` を grep して、
この2つの定数に反映すること。

## 取得が黙って止まったことに気づく

出口IPの判定で **12日間**、取得が1件も走っていなかったことがある。
そのとき入れたのは「出口の異常を `scrape_log` に残す」で、
**原因を1つ塞いだだけ**だった。想定していない死に方は必ず残る。

そこで**原因に依存しない見張り**を別に置いている。
最後に取得できた時刻だけを見て、`SCRAPE_STALE_HOURS`（既定3時間）を
超えていたらトップに赤いバナーを出す。15分巡回なので12回ぶん空いた計算。
`0` で無効。

**止まっているときは「✓ 正常稼働中」を出さない。** 止まっているのに緑が
出続けるのが、気づけなかった原因そのものだった。

### 成功ログが刈られていても気づく

`scrape_log` は `SCRAPE_LOG_RETENTION_DAYS`（既定7日）で刈られる。
素直に「最後の成功ログ」だけを見ると、**7日以上止まったときに
成功ログごと消えて「何も無い＝正常」に見える**。一番警告が要る場面で黙る。

そのため成功ログが1件も無いときは、保存済みツイートの `fetched_at` で
代用する。この経路で判定したときはバナーに
「実行ログが残っていないほど長く止まっています」と添える。

IG-Ray 側も同じ見張りを持っているが（向こうは24日気づかなかった）、
この刈られたときの代用は X-Ray 側で足したもの。

## IG-Ray のアーカイブを取り込む（任意）

[IG-Ray](https://github.com/suzushiro/ig-ray) で集めた Instagram のアーカイブを、
X-Ray のタイムラインとギャラリーに**混ぜて表示**できる。

**取得は持ち込まない。** instaloader・cron・backfill は ig-ray 側に残したままで、
X-Ray からは**読むだけ**。IGの投稿にはブックマーク・画像削除のボタンを出さない
（X-Rayの書き込み経路はXのテーブルを前提にしているため）。

**Tumblr への投稿だけは IG のカードからもできる。** ig-ray のDBには
一切書かず、読んだ実ファイルを送るだけなので読み取り専用の前提を崩さない。
キャプションの記憶は **X-Ray 側のテーブル**に `ig:<ユーザー名>` のキーで持つ。
ig-ray 側（4.20 以降）に既に覚えてあるぶんは、X-Ray 側に無いときだけ
読み取りで引き継ぐ。

### メディアのパスの読み替え

**ig-ray は `local_path` を自分のコンテナ基準の絶対パスで記録している**
（既定 `/data/cache/...`）。X-Ray は同じディレクトリを別の場所にマウント
しているので、素直に相対パスを取ると `../../data/cache/...` になって外れる。
外れると画像がリモートの Instagram URL にフォールバックし、**URLが失効した
古い投稿は表示できなくなる**（v42〜v46 で実際にそうなっていた）。

`ig_bridge.cache_rel()` が、書いた側の root 名に依存せず
キャッシュ root 以降だけを取り出す。`dev/ig_check.py` のフィクスチャは
**本番と同じ `/data/cache/...` 表記**で作ってあり、実パスで作ると
この読み替えを検証しないまま通ってしまう。

### 設定

`.env` に3つ。ig-ray を同じホストで動かしている前提。

```
IG_RAY_DATA_DIR=/home/youruser/ig-ray/data
IG_DB_PATH=/ig-data/ig_ray.db
IG_CACHE_DIR=/ig-data/cache
```

`IG_RAY_DATA_DIR` はホスト側のパス、残り2つは**コンテナ内から見たパス**
（`/ig-data` にマウントされる）。

**DBのファイル名は自動で探す。** ig-ray は v4.1 で `ig_ray.db` に改名したが、
既存DBは作り直さず旧 `insta_ray.db` を使い続ける仕様なので、長く運用している
環境では実体が旧名のままになる。`ig_ray.db` → `insta_ray.db` の順に探すので、
`IG_DB_PATH` は空のままでもマウント先から見つかる。
見つからないときはナビの ⚠ にカーソルを乗せると理由が出る。

統合を無効にするには `IG_RAY_DATA_DIR` を空にする。

```bash
docker compose build web && docker compose up -d web
```

### 読み取り専用マウントにしてはいけない

compose のマウントに `:ro` を付けたくなるが、**付けると壊れる。**

SQLite の WAL モードは読むだけでも `-shm`（共有メモリ索引）を必要とする。
ig-ray が `-wal` を残したまま落ちて `-shm` が消えている状態だと、
読み取り専用では -shm を作れず **DBを開くことすらできず、X-Rayの
タイムラインごと落ちる**。`immutable=1` を付ければ開けるが、これは
「このファイルは一生変化しない」と SQLite に嘘をつく指定で、ig-ray が
動き出すと古い値や壊れた読みを返しうる。

そこで**書き込み可でマウントし、`PRAGMA query_only = 1` で守る**。
`app/ig_bridge.py` が ATTACH の直後にこれを立てるので、書き込みは
`attempt to write a readonly database` で確実に弾かれる。
マウント権限ではなく SQLite 側で担保する形。
`dev/ig_check.py` の [8] がこの状況を実際に作って読めることを固定している。

### 対応している画面

| 画面 | IG |
|---|---|
| タイムライン / ギャラリー | 混ぜて表示。プルダウンで出自を切り替え |
| ユーザーページ | `/user/ig:<ユーザー名>` |
| 検索 | 本文（caption）を LIKE で引いて混ぜる。X 側は FTS |
| ストレージ | 件数と容量を**別枠**で表示（X-Rayの合計には含めない） |
| Tumblr投稿 | できる |
| ブックマーク | できる。画像の実体を X-Ray の永続領域へコピーする |
| ミュート | できる。`/ig-mutes`。**IG-Ray と設定を共有**（向こうの画面にも出る） |
| 画像削除 | **できない**（実体は IG-Ray の持ち物。削除は向こうで） |

### ブックマークは実体をコピーする

IG のキャッシュは IG-Ray の保持期間で消える。参照だけ持つと
「ブックマークしたのに画像が消える」になるので、ブックマーク時に
**実体を X-Ray の永続領域（`/data/images/ig_<shortcode>_<n>.<ext>`）へコピー**する。
X 側がキャッシュから永続へ昇格させているのと同じ考え方で、コピー先は
X-Ray の持ち物なので読み取り専用の前提は崩れない。

元が IG-Ray 側から消えても、ブックマークしてあれば Tumblr へ投稿できる。

### ミュートだけは IG-Ray のDBに書く

ミュートは `accounts.is_muted` を直接更新する。**IG-Ray 本体と同じ列**なので
向こうの `/mutes` とも揃う。書き込みは読み取りとは別の接続で行い、
**読み取り側は `query_only` のまま**にしてある（表示経路のバグが
IG-Ray のデータを壊さないようにするため）。

### 表示の決まり

- **監視対象（`accounts.is_target = 1`）以外とミュートは出さない。** ig-ray 本体と
  同じ規則。共同投稿では `owner_username` が相手側になるので、絞らないと
  登録していないアカウントの投稿が並ぶ
- `is_target` 列が無い古いDBのときは**絞り込みをしない**。空集合と
  「列が無い」を混同すると投稿が全部消えて障害に見える
- カテゴリのタブとバッジはXとIGの合算。表示されているのに数に入っていないと
  食い違って見えるため
- IGのカードには出自バッジ（IG）が付き、リンクは「Instagramで見る →」になる

### 出自の切り替え

ナビの「🏠 ホーム」の隣にプルダウンが出る（すべて / X / Instagram）。
TLとギャラリーにだけ表示し、IG未設定なら出さない。
`option` の `value` が遷移先URLそのもので、JSは `location.href` を入れるだけ。
URLの組み立てをテンプレとJSに分けると、片方だけ直して壊す事故が起きるため。

**混ぜるだけでは足りない。** IGの取得が止まっていると、IGの最新投稿より
新しいXの投稿が全部手前に来る。Xが1日500件増えるなら52日ぶんで
26,000件＝260ページ先になり、事実上見えない。
切り替えで「IGだけ」に飛べるようにしてある。

カテゴリのタブと件数バッジも選択中の出自に追従する
（Xだけ表示しているのにIG込みの件数が出ると食い違って見えるため）。
`ig-ray` のDBを読めていないときは切り替えの横に警告が出る。

### 時刻の正規化

XとIGで `created_at` のオフセットが揃っていない（`+09:00` と `+00:00` が
混在しうる）。**素の文字列比較で並べると時系列が狂い、片方が上に固まる。**
`strftime('%Y-%m-%dT%H:%M:%S', created_at)` でUTCに畳んでから並べている。

このぶん並べ替えにインデックスが効かない。X 20万件 ＋ IG 3万件で
1ページ 100件の取得が約 180ms（正規化なしなら約 110ms）。個人用途なら
許容できる差として正確さを採った。

### ig-ray が無い・壊れていても動く

ig-ray は別リポジトリとして進化し続けるので、**向こうの都合でこちらが
落ちないこと**を優先している。DBが無い・開けない・スキーマが変わった、
いずれの場合もIGが出なくなるだけでX-Ray本体は動く。
混合クエリが失敗したらX単独にフォールバックする。

## Tumblr投稿（API直接 / 推奨）

`.env` に Tumblr のアプリキーを設定すると、確認画面から**そのまま投稿できる**。
画像はローカルファイルを直接アップロードするので、**外部公開（トンネル）は不要**。
複数アカウント（メイン/サブ）の切り替えと、下書き保存の切り替えができる。

### セットアップ

1. https://www.tumblr.com/oauth/apps でアプリを登録する
   - コールバックURL と OAuth2リダイレクトURL は `http://localhost:8765/callback`
   - 発行された Consumer Key / Secret を `.env` に入れる

```
TUMBLR_CONSUMER_KEY=xxxx
TUMBLR_CONSUMER_SECRET=yyyy
```

2. アカウントごとに認可する（1回だけ。OAuth1のトークンは失効しない）

```bash
cd tools
pip install -r requirements-tumblr.txt
export TUMBLR_CONSUMER_KEY=xxxx TUMBLR_CONSUMER_SECRET=yyyy

python tumblr_auth.py --label main --out ../data/tumblr_accounts.json
python tumblr_auth.py --label sub  --out ../data/tumblr_accounts.json   # 別アカウントでログインして許可

python tumblr_auth.py --list --out ../data/tumblr_accounts.json         # 確認
```

ブラウザが開くので、そのラベルで使いたいアカウントでログインして許可する。
別々のTumblrアカウントでも、ラベルを分ければ両方登録できる。

3. `docker compose up -d web`

`data/tumblr_accounts.json` には**アクセストークンが入る**。gitignore 済み。
バックアップにも含めないこと。

### 使い方

投稿カードの `t` ボタン → 確認画面で画像・投稿先・キャプション・タグを決めて「投稿する」。
トップ・検索・アカウント別ページに加えて、**ブックマーク一覧からも投稿できる**
（元ツイートが `tweets` から消えていても、ブックマークに残っていれば投稿できる）。

- **画像は複数選べる。** 既定で全選択。サムネのクリックで個別に外せる。
  バッジの数字はTumblrに添付される順序（選んだ順）
- 「全解除 / 全選択」でまとめて切り替えられる
- **一定枚数以上を選ぶと自動的に下書きになる**（既定3枚、`TUMBLR_DRAFT_THRESHOLD` で変更）。
  枚数の多い投稿を一旦人の目に通すための安全弁。チェックを手で操作すれば
  以降その投稿では自動判定しない
- 「下書きとして投稿する」がONだとボタンは「下書きに保存」に変わる
出典はXの元投稿になる。

センシティブな画像をサブに投げてメインからリブログする運用なら、
投稿先で `sub` を選んで投稿し、リブログはTumblr側で行う。

### タグの変換

カテゴリ名がそのままタグになるが、内部名をそのまま使いたくないものは
`.env` の `TUMBLR_TAG_MAP` で置き換えられる（既定は `{"ギャル":"girl"}`）。

```
TUMBLR_TAG_MAP={"ギャル":"girl","R18":"nsfw","news":""}
```

空文字を指定するとそのカテゴリはタグにしない。確認画面で手直しもできる。

### キャプションとタグを投稿主ごとに覚える

同じ人の投稿を上げるたびに同じ編集をやり直さずに済むよう、**投稿が成功したときの
キャプションとタグを、Xの投稿主ごとに覚えておく**（`tumblr_presets` テーブル）。
次に同じ投稿主の投稿でtボタンを押すと、既定値（ハンドル名＋カテゴリ）ではなく
前回の内容が入った状態で開く。

- キーは**Xの投稿主**。Tumblrのメイン／サブは問わず共通
- 覚えるのは**キャプション本文だけ**。投稿時に付く元ポストへのリンクは
  投稿ごとに違うので保存せず、毎回その投稿のURLから付け直す
- 投稿が**失敗したときは覚えない**（次回それが出てくると紛らわしいため）
- キャプション欄の「既定に戻す」で、覚えている内容を捨てて
  ハンドル名＋カテゴリに戻せる
- 覚えた内容はページを開いた時点でまとめて読み込む。tボタンを押してから
  取りに行くと、モーダルが開いたあとに値が書き換わって入力と競合する

ブックマークにしか残っていない投稿でも、`bookmarks.screen_name` から
同じように投稿主を引く。

### 注意

- レート制限は1日250投稿・250画像アップロード。手動投稿では当たらない
- `TUMBLR_CONSUMER_KEY` が未設定なら投稿ボタンは出ない。
  旧方式（共有リンク／トンネル）へのフォールバックは v54 で無くなった

## Tumblr共有（トンネル方式）— v54 で撤去

かつて `/share` `/share-img` `/api/share/prepare` を外部公開し、Tumblr の
シェアツールに画像URLを拾わせる方式があった。API方式に一本化した時点で
投稿ボタンからは呼ばれなくなっていたが、**ルートとテーブルだけが残り、
X-Ray で唯一の外部公開面を開いたままにしていた。** v54 で以下を撤去した。

- ルート `/share/<token>` `/share-img/<token>/<n>` `/api/share/prepare`
- テーブル `share_tokens`（既存DBからも `DROP` する。中身は一時トークンのみ）
- 環境変数 `PUBLIC_SHARE_BASE_URL` `PUBLIC_SHARE_HOST` `SHARE_TOKEN_TTL_MIN`
- `app/templates/share.html` / `dev/share_check.py` / `deploy/`

撤去後は X-Ray に外部公開が必要なルートが**ひとつも無い**。
cloudflared やリバースプロキシは Tumblr 投稿のためには要らない。
`dev/route_check.py` が「`/share*` が404であること」「`share_tokens` が
無いこと」を検査しているので、うっかり復活すると落ちる。

## ファイルの分かれ方（v58）

`app/web.py` が全部入りだったのを、役割ごとに分けた。
**ルートは web.py に置いたまま**（Blueprint は間接層が増えるだけで、
ルーティングの一覧性が落ちる）。

| ファイル | 持つもの |
|---|---|
| `web.py` | ルートだけ。1879行 → 1300行 |
| `sources.py` | 出自（X / IG）の違い。共通列の約束 |
| `presenters.py` | DBの行を画面に出す形へ整える |
| `health.py` | 「黙って止まっている」の見張り |
| `media_utils.py` | ローカル画像の取り出し（web と sources の両方が使う） |
| `ig_migrate.py` | IG-Ray から引き継ぐ一度だけの移行 |
| `db.py` | スキーマとマイグレーション |

分けたときに2つ見つかった。

- **ブックマーク画面が `format_tweet` を通さず自前で整形していた。**
  画像の出し分け・base64・JSTの変換・スペースURLの検出が二重にあり、
  しかも**引用RTのカードだけ落ちていた**（`bookmarks.quoted_json` は
  保存しているのに表示されない）。format_tweet に寄せて直った
- `index()` のローカル変数 `health` がモジュール `health` を隠していて
  `UnboundLocalError`。ローカル側を `recent` に改名した

設定を持つモジュールを分けたことで罠が1つ増えた。**環境変数を変えて
`web` だけ reload しても、設定を持つモジュール（`presenters` /
`health`）は再利用されるので古い値が残る。** 設定を持つほうから順に
reload すること（`dev/tumblr_check.py` に注記あり）。

## 投稿の出自（app/sources.py）— v55

一覧・検索・ユーザーページ・ギャラリー・カテゴリ件数は、**出自を
並べてループする**形になっている。X / IG の違いは `app/sources.py`
の中だけで閉じる。

### なぜこうしたか

IG を取り込んだとき、IG側だけが `ig_bridge` というモジュールを持ち、
X側のSQLは `web.py` に直書きのままだった。この非対称のせいで、機能を
足すたびに「X用の分岐」と「IG用の分岐」が web.py に溜まった。v54 時点で:

- IG分岐を持つルートが **13本**
- IGの17列射影が **3箇所に複製**（`ig_bridge.select_sql` / `/search` /
  IGユーザーページ）
- カテゴリ件数はほぼ同形のGROUP BYが2本
- `db_ro()` が `ig_bridge.attach()` を名指し

列を1つ足すのに3箇所直す必要があり、実際に `quoted_json` を落として
引用RTが黙って壊れたことがある。

### 列の約束

`POST_COLUMNS` が唯一の正。1行が `(共通列名, X側の式, IG側の式)` で、
**共通名と両方の式が横に並ぶ**ので、片方だけ直す事故が起きない。
射影は必ず `projection()` から生成する（手書き禁止）。

UNION ALL は列名ではなく**位置**で対応するため、並びがずれると
「いいね数の所に返信数が出る」壊れ方をする。いまは並びを入れ替えても
全ページがバイト単位で変わらない（実測済み）ので、この種のずれは
構造的に起きない。

### 出自を足すとき

`sources.Source` を継承して `register()` するだけ。**web.py は触らない。**
最低限いるのは `name` / `id_prefix` / `from_sql()` / `attach(conn)` /
`available(conn)` / `projection()`（自前で書く場合）/ `owner_where()` /
`date_column()` / `text_where()` / `filter_sql()` / `category_counts()`。

`dev/sources_check.py` が、架空の第3の出自（Tumblr もどき）を登録して
**実リクエスト経路で**トップ・ギャラリー・検索に出ることを検査している。
`db_ro()` が `ig_bridge.attach()` を名指ししていた間はここが落ちた。

### 登録は名前で冪等

`register()` は同名なら差し替える。最初は append で持っていて、
テストが環境変数を変えて web を再importするたびに同じ出自が積まれ、
**8重に登録されて UNION が8回重なり同じ投稿が8件ずつ出た。**
足し算で持つと「何回読み込まれたか」に答えが依存する。

### 書き込み系も出自に委譲（v56）

ブックマーク・Tumblr投稿・ミュート・メディア配信も `Source` の口に移した。

| | 以前 | いま |
|---|---|---|
| ブックマーク | `_bookmark_ig()` という別経路＋INSERT文が2本 | `Source.bookmark_payload()`、INSERTは1箇所 |
| Tumblr投稿の添付 | `is_ig` で分岐 | `Source.post_media()` |
| ユーザーページ | `_ig_user_profile()` でページ丸ごとコピー | `owner_profile()` / `owner_stats()` / `owner_rows()` |
| ミュート | `/api/ig/mute` `/ig-mutes` | `/api/mute` `/mutes`（`supports_mute` を名乗る出自だけ） |
| メディア配信 | `/ig-cache/<path>` | `/media/<出自>/<path>`（`media_dirs()`） |
| 画像の手動削除 | `is_ig_id` で拒否 | `supports_media_delete`（X-Ray 自身のデータだけ） |

**能力で判定する。** X にミュートは無い（カテゴリで出し分ける）し、
外から ATTACH しているアーカイブに書くと向こうを壊す。出自ごとに
`supports_mute` / `supports_media_delete` を名乗らせて、持たない出自は
400 で断る。

### URL の変更（旧URLは残す）

| 旧 | 新 | 旧URLの扱い |
|---|---|---|
| `/ig-mutes` | `/mutes` | 301 リダイレクト |
| `/api/ig/mute` | `/api/mute` | 同じ処理を呼ぶ別名（古いタブ対策） |
| `/ig-cache/<path>` | `/media/ig/<path>` | **そのまま配信**（301にしない） |

メディアの旧URLを 301 にしてはいけない。`/ig-cache/../../x/data.db` の
ような遡上がリダイレクトでそのまま通り、`send_from_directory` の
安全判定を飛び越えてしまう（実際にそれで検査が落ちた）。

`/media/` のパス判定は `send_from_directory` に任せる。自分で
`os.path.exists` を先に見て判断すると、ディレクトリの外にあるファイルを
「ある」と判定してしまう。

## 検索（v58 で作り直した）

**全文検索（FTS5）はやめて、本文の LIKE 一本にした。** ブックマークも
検索の対象にした。

### なぜやめたか

既定のトークナイザ（`unicode61`）は日本語の連なりを**ひとかたまりの
トークン**として扱う。実測:

| 検索語 | FTS5 (unicode61) | FTS5 (trigram) | LIKE |
|---|---|---|---|
| 猫（1文字） | **0件** | 0件 | 見つかる |
| 写真（2文字） | **0件** | 0件 | 見つかる |
| かわいい（4文字） | 0件 | 見つかる | 見つかる |
| 猫がかわいい写真を撮った（全文一致） | 見つかる | 見つかる | 見つかる |

本文が日本語主体の X-Ray では、**X側の検索が黙って機能していなかった。**
しかも FTS は例外ではなく0件を返すので、`except` に書いた LIKE への
フォールバックも働かなかった（IG側は最初から LIKE だったので引けていて、
同じ語で出自によって結果が違う状態だった）。

速度も索引の負けだった（23万行で計測）:

| | 時間 | 索引の大きさ |
|---|---|---|
| `LIKE '%写真%' LIMIT 200` | **0.1ms** | — |
| `LIKE '%写真%'` 全件（8.2万件ヒット） | 62ms | — |
| FTS5 unicode61 | 0.0ms（**0件**） | 30MB |
| FTS5 trigram | 0.2ms（4文字以上のみ） | 44MB |

`tweets` 本体が25MBなので、**索引のほうが中身より大きい**。取得のたびに
書き込みも発生していた。v58 で `tweets_fts` を撤去した（マイグレーション6）。

### ブックマークも探せるようにした

ブックマークは「元が消えても残す」ための仕組みなのに、検索は `tweets`
からしか引いていなかった。**元の投稿が消えると探せなくなる** —
残しているのに見つからないのでは意味が無い。

同じ投稿が `tweets` とブックマークの両方にある場合は、`tweet_id` で
重複を潰す。

### この版でやっていないこと

検索語の AND / OR、投稿主での絞り込み、期間指定。いまは本文の
部分一致だけ。

## スキーマとマイグレーション（v58）

テーブルの定義は `db.py` の `SCHEMA`、既存DBに後から入れる変更は
`MIGRATIONS`（連番＋`schema_migrations` に記録）。

以前は `try: ALTER / except: pass` をずらっと並べていた。これは
**全部の例外を飲む**ので、「もう列がある」も「SQLの書き間違い」も
「DBが壊れている」も同じ `pass` になる。いまは `add_column()` が
`PRAGMA table_info` を見てから足すので、**例外を飲まずに冪等**。

記録だけに頼って「適用済みだから飛ばす」とはしていない。いま動いている
DBには記録表そのものが無く、記録を信じると既にある列へ `ALTER` を
打って落ちる。**冪等であることが正しさの根拠**で、記録は
「何がいつ入ったか」を見るためのもの。

インデックスはマイグレーションの**後**に作る。先に作ると、後から足す列を
参照するインデックス（`idx_tweets_created` は `created_at` を見る）が
「no such column」で落ちる。

`dev/schema_check.py` が、列が足りない古いDBを**中身を落とさずに**
昇格できることを検査している（20万行のDBは作り直せないので、
ここが一番大事な性質）。

## 巡回の記録（run_log）— v58

巡回そのものと出口IPの状態は `run_log`（`kind` 列で `run` / `egress`）に入る。
`scrape_log` には**実アカウントだけ**が入る。

以前は `scrape_log.screen_name` に `__run__` / `__egress__` という
擬似アカウント名で相乗りさせていた。そのため:

- 「エラーが続いているアカウント」の一覧から擬似名を除く条件を
  全クエリに配る必要があり、その `ESCAPE` 指定を書き損じて
  トップページを500にした
- 「最後に取れた時刻」を見るのにも実アカウントだけに絞る必要があった
- アカウント名の列に、アカウントでないものが入っていた

いまは除外条件そのものが要らない。`/status` では別の表として出す
（混ぜると「`__run__` というアカウントが失敗している」ように見える）。

## IG向け設定の名前（v58）

IG向けの設定は `IG_*` で書く。**旧 `IG_RAY_*` / `INSTA_RAY_*` も
引き続き読む**ので、既存の `.env` を書き換える必要は無い
（両方あるときは `IG_*` が優先）。

compose が `${IG_X:-${IG_RAY_X:-既定値}}` の形で両方を受け、
**コンテナに渡す側の名前は `IG_RAY_*` のまま**にしてある。
`ig/app/` は無改変が原則なので、あちらを直さずに済ませるため。

## 全滅が続いたときの待機（v54）

15分ごとに回しているので、Xの仕様変更やクッキー失効で全滅し始めると、
失敗したまま延々と叩き続ける。叩き続けるほど弾かれやすくもなるので、
**1回の実行で1件も取れなかったら次回以降の間隔を空ける。**

- 連続 n 回の全滅で `15 * 2^(n-1)` 分あける（15 / 30 / 60 / 120 …）
- `SCRAPE_BACKOFF_MAX_MIN`（既定 240）で頭打ち
- **1件でも取れたら即解除**。部分的な失敗では待たない
- 記録は `scrape_log` の擬似アカウント `__run__`（`run_ok` / `run_error`）

擬似アカウント（`__run__` / `__egress__`）は「エラーが続いているアカウント」の
一覧には出さない。一覧に出す条件は `NOT_PSEUDO_SQL` 一箇所にまとめてある。
`LIKE` だと `_` がワイルドカードで `ESCAPE` 指定が必要になるので `GLOB` を使う
（そのエスケープを書き損じてトップページを500にしたことがある）。

## IG の進み具合を `/status` に出す（v54）

過去投稿のさかのぼり（バックフィル）は ig-ray の `backfill list` を
叩かないと見えず、止まっていても気づけなかった。`/status` に一覧を出す。

ig-ray 側の版によって列が増減するので、**ある列だけを読む。**
無い列を名指しすると一覧ごと空になる（ミュート一覧で一度踏んだ）。

## Tumblrプリセットの引き継ぎ（v54）

ig-ray 側に覚えてあったキャプション／タグは、**web 起動時に一度だけ**
X-Ray の `tumblr_presets` に取り込む。済み印は `app_meta` に残す。

以前は `/api/tumblr/presets` が毎回 ig-ray のDBを読んで穴埋めしていた。
その作りには2つ問題がある。

- X-Ray 側で消したプリセットが、ig-ray 側に残っていると次回復活する
- ig-ray のDBを外した日に、覚えていた内容がまとめて消える

ig-ray はもうメンテしないので、取り込みは一度で終わらせ、
以後は X-Ray 側だけを見る。

## 設定と秘密情報

環境ごとの値は `.env` に置く。`docker compose` が自動で読み込む。

```bash
cp .env.example .env
vi .env
```

リポジトリに含めないもの（`.gitignore` 済み）:

- `data/` — **クッキー（生きたセッショントークン）**・DB・保存画像
- `ig-data/` — IGのアーカイブ（DB・画像・ログインセッション）
- `.env` — IPアドレスなど環境固有の値
- `tools/profiles/` — ブラウザのログインセッション
- `dev/secrets.local.txt` — 公開ファイルに混ざってはいけない語の一覧
- `data/accounts_fallback.json` — 監視対象の初期データ（任意）

`docker-compose.yml` には実IPを書かず、`${PROXY_URL}` のように `.env` を参照させること。

環境固有の手順や構成のメモは `NOTES.local.md`（`.gitignore` 済み）に置いている。
公開リポジトリに含めたくない実IP・ホスト名・監視対象などはそちらに書く。

### 公開ファイルの走査（v54 で作り直した）

`dev/egress_check.py` が、ツリーのテキストファイル全部を走査して
実ホスト名・実IP・実アカウント名が混ざっていないかを見る。

```bash
cp dev/secrets.local.example.txt dev/secrets.local.txt
vi dev/secrets.local.txt        # 実際の値を1行1語で書く
cd dev && python egress_check.py
```

`secrets.local.txt` が無くても、RFC予約外の IPv4 らしき文字列は
一般形で検知する（オクテットが0-255かを見るので、SVGのパスデータや
`Chrome/124.0.0.0` のようなバージョン表記は拾わない）。

**v54 でこの仕組みを作り直した理由。** それまでは

1. 禁止語リストを `dev/egress_check.py` に**直接書いていた**。
   公開ファイルを検査するスクリプト自身が、実ISP名・実ホスト名・実IP・
   実アカウント名を全部並べて公開していた（検査する側が漏らしていた）。
2. 走査対象を**ファイル名の手書きリスト**で持っていた。そのリストに
   載っていない `app/seed_accounts.py` に監視対象62件がベタ書きされたまま、
   `app/templates/manage.html` と `ig/dev/` に実アカウント名が残ったまま
   通っていた。

名簿を手で保つ方式だと、新しいファイルは必ず検査の外から生まれる。
いまは「ツリー全部を見て、秘密の語は別ファイルから読む」形にしてある。

### 監視対象の置き場所

真のマスタは `data/accounts.json`（`.gitignore` 済み）。追加・編集は
`/manage` 画面から行う。`app/seed_accounts.py` の `ACCOUNTS_FALLBACK` は
**空**にしてある（以前は実アカウント62件がここに残っていた）。

`accounts.json` が無い状態で初期データを入れたいときだけ、
`data/accounts_fallback.json` を同じ形式で置く。

```json
{"accounts": [{"screen_name": "jack", "display_name": "Jack",
               "categories": ["news"]}]}
```
