# IG-Ray の撤収手順（v54）

IG-Ray の機能は X-Ray に全部入った。この手順で IG-Ray 側を畳む。

**前提**: v54 を `git pull` して `docker compose build` 済み。
`~/x-ray/ig-data/` にアーカイブを移設済みで、`/status` と IG のタイムラインが
見えている状態（まだなら先にそこまで済ませる）。

実IPやホスト名はこの文書には書かない。`<自分のDockerホスト>` のような
プレースホルダは手元の値に読み替えること。

---

## 0. いまの状態を確かめる

畳む前に「X-Ray 側だけで足りている」ことを見る。

```bash
cd ~/x-ray
docker compose ps
docker compose exec web python -c "
import sys; sys.path.insert(0,'/app')
import sqlite3, ig_bridge
c = sqlite3.connect('/data/data.db')
print('IG接続:', ig_bridge.attach(c))
print('IG件数:', ig_bridge.stats(c))
print('さかのぼり:', len(ig_bridge.backfill_jobs(c)), '件')
"
```

- `IG接続: True` で `IG件数` が実際の数になっていること
- ブラウザで `/status` を開き「IG 過去投稿のさかのぼり」の節が出ていること
- トップのプルダウンで「IG」を選んで投稿が並ぶこと
- IGカードの t ボタンから Tumblr に1件投稿できること

ここが通らないうちは次に進まない。**IG-Ray を消したら戻せない。**

---

## 1. IG-Ray のコンテナを止めて消す

```bash
cd ~/ig-ray
docker compose down --remove-orphans
docker compose ps -a          # 何も残っていないこと
```

イメージも落とす（容量が要るとき。残しておいても害は無い）。

```bash
docker images | grep -i ig-ray
docker rmi <出てきたイメージ>
```

---

## 2. 外部公開を閉じる

X-Ray は v54 で `/share` 系を撤去したので、**外部に出す必要のあるルートが
ひとつも無くなった。** Tumblr 投稿は API 方式なので外から到達できなくてよい。

cloudflared / リバースプロキシを IG-Ray や共有リンクのために立てていたなら、
ここで止める。

```bash
# systemd ユニットで動かしていた場合
sudo systemctl stop cloudflared
sudo systemctl disable cloudflared
sudo systemctl status cloudflared     # inactive (dead) になること

# docker で動かしていた場合
docker ps | grep -i cloudflared
docker stop <コンテナ名> && docker rm <コンテナ名>
```

止めたあと、外から到達しないことを確かめる。

```bash
# 公開していたホスト名に対して（手元の値に読み替え）
curl -sS -o /dev/null -w '%{http_code}\n' https://<公開していたホスト名>/
```

つながらない／名前が解決しないのが正しい。200 が返るならまだ開いている。

`.env` から要らなくなった行を消す。

```bash
cd ~/x-ray
grep -nE 'PUBLIC_SHARE_BASE_URL|PUBLIC_SHARE_HOST|SHARE_TOKEN_TTL_MIN' .env
# 出てきた行を削除（v54 のコードは読まないので残っていても動くが、紛らわしい）
```

---

## 3. 残っている秘密情報を片付ける

### 3-1. 使っていない Tumblr のトークン

移設した `ig-data/` に、IG-Ray 時代の `tumblr_accounts.json` が入っている
可能性がある。**中身は OAuth トークン＝実質パスワード。** X-Ray は
`data/tumblr_accounts.json` を使うので、`ig-data/` 側は使われない。

```bash
cd ~/x-ray
ls -l ig-data/tumblr_accounts.json 2>/dev/null && echo "→ ある"
```

あれば消す。X-Ray 側（`data/`）のほうは消さないこと。

```bash
shred -u ig-data/tumblr_accounts.json      # shred が無ければ rm
ls -l data/tumblr_accounts.json            # こちらは残っていること
chmod 600 data/tumblr_accounts.json
```

### 3-2. ログインセッション

`ig-data/sessions/` は IG のログインセッション。X-Ray 側の IG 取得が
使うので**消さない**。権限だけ締める。

```bash
chmod 700 ig-data/sessions
chmod 600 ig-data/sessions/*
```

### 3-3. ブラウザプロファイル

IG-Ray 側に `tools/profiles/` があれば、生きたログインセッションなので
バックアップに混ぜず消す。

```bash
ls -d ~/ig-ray/tools/profiles 2>/dev/null && rm -rf ~/ig-ray/tools/profiles
```

---

## 4. IG-Ray のコードを消す

`ig-data/` を `~/x-ray/` に移設済みであることを**もう一度**確かめてから。

```bash
ls ~/x-ray/ig-data/          # DB・cache・sessions があること
du -sh ~/x-ray/ig-data/
ls ~/ig-ray/data/ 2>/dev/null   # 空か、もう要らないものだけであること
```

確認できたら消す。

```bash
rm -rf ~/ig-ray
```

GitHub のリポジトリは **削除ではなくアーカイブ**にしておく。
Settings → 最下部 "Archive this repository"。読み取り専用になるだけなので、
あとで経緯を見返せる。

---

## 5. IG の取得を X-Ray 側で動かす

### 5-1. 定期巡回

```bash
cd ~/x-ray
docker compose --profile ig-cron up -d ig-cron
docker compose logs -f ig-cron        # 「IG巡回モード: ...秒ごと」が出る
```

間隔は `.env` の `IG_RAY_INTERVAL`（秒、既定 21600 = 6時間）。
**数時間より短くしないこと。** IG は弾かれやすく、3時間に縮めても新規は
ほとんど増えない。取りこぼし対策なら `IG_RAY_CRON_LIMIT` を上げるほうが安全。

### 5-2. 過去投稿のさかのぼり

常駐ワーカーを上げる。イテレータを生かしたまま常駐させるのが肝で、
「1ページ取って終了」にすると毎回1ページ目を取り直してリクエストが倍増する。

```bash
# 対象を登録（まだなら）
docker compose --profile ig-tools run --rm ig-backfill list
docker compose --profile ig-tools run --rm ig-backfill add <ユーザー名>

# 常駐ワーカーを上げる
docker compose --profile ig-backfill up -d ig-backfill-worker
docker compose logs -f ig-backfill-worker
```

進み具合は **`/status` の「IG 過去投稿のさかのぼり」** で見える。
`backfill list` を叩かなくても、止まっていればそこで分かる。

---

## 6. 仕上げの確認

```bash
cd ~/x-ray
docker compose ps                     # worker / web / ig-cron / ig-backfill-worker
docker compose exec web python -c "
import sys; sys.path.insert(0,'/app')
import web; print('version', web.APP_VERSION)"
```

- `/` の右上が `v54` になっている
- `/status` に X の取得ログと IG のさかのぼりが両方出る
- `/share/x` が 404（外部公開面が無い）
- トップのプルダウンで すべて / X / IG が切り替わる
- `~/ig-ray` が無く、`docker ps` に ig-ray 由来のコンテナが無い

```bash
# WEB_PORT は .env の値（既定 8501）。シェルには自動で入らないので読み込む
set -a; . ./.env; set +a
curl -sS -o /dev/null -w '/share → %{http_code}\n' \
  "http://localhost:${WEB_PORT:-8501}/share/x"
```

`404` が正しい。`200` が返るなら古いイメージのままなので
`docker compose build web && docker compose up -d web` をやり直す。

---

## 消してよいもの・消してはいけないもの

| | |
|---|---|
| 消してよい | `~/ig-ray/`（コード）、IG-Ray のイメージ・コンテナ、`ig-data/tumblr_accounts.json`、`~/ig-ray/tools/profiles/` |
| **消しては駄目** | `~/x-ray/ig-data/`（DB・画像・sessions）、`~/x-ray/data/`、`.env`、`data/tumblr_accounts.json` |
| バックアップに混ぜない | `data/tumblr_accounts.json`、`data/cookies.txt`、`ig-data/sessions/`、`tools/profiles/` — どれも生きた認証情報 |

---

## 戻したくなったら

アーカイブしたリポジトリを clone し直せばコードは戻る。ただし
**`ig-data/` のDBは X-Ray と共有している。** IG-Ray を再び動かすと
同じDBに両方が書き込むので、片方だけにすること。

X-Ray 側で IG 取得を止めるには:

```bash
docker compose stop ig-cron ig-backfill-worker
docker compose rm -f ig-cron ig-backfill-worker
```
