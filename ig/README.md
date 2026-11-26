# ig/ — IG取得系（IG-Ray 4.20 から持ち込み）

X-Ray に IG-Ray を畳むにあたって、**取得側のコードをそのまま持ち込んだ領域**。
表示と Tumblr 投稿は X-Ray 本体（`app/web.py` + `app/ig_bridge.py`）が担当する。

## なぜ書き直さないか

このコードは実データでしか出ない不具合を潰してきた結果で、書き直すと
**その知見ごと落とす**。具体的には:

- `location` / `video_url` / `Post.comments` は、**プロパティに触るだけで
  追加のHTTPリクエストが飛ぶ**。投稿1件につき1〜4回増える
- `freeze()` / `thaw()` の再開は1件だけ重複再生する（取りこぼし防止の仕様）。
  `thaw()` は再開1回につき1リクエスト無駄にする
- `test_login()` が叩く旧クエリは廃止済みで、有効なセッションでも必ず失敗する
  （上流 issue #2792）。代わりに `verify_session()` が巡回と同じ経路で確かめる
- 一度429を食うとペナルティが全クエリ種別に波及する

`ig/dev/` のテストはこれらを**実際に仕掛けを置いて**固定している
（例: `video_check.py` はプロパティに触ったら例外を投げる tripwire）。

## 構成

```
ig/
  app/    取得に要るもの。IG-Ray 4.20 と無改変
  dev/    同じテスト。X-Ray 取り込みに伴う差分のみ変更（下記）
  tools/  診断用（コンテナには入れない）
```

`web.py` / `tumblr_client.py` / `templates/` は持ち込んでいない。

## IG-Ray からの変更点

無改変が原則。X-Ray に置いたことで必要になった分だけ直してある。

| ファイル | 変更 |
|---|---|
| `app/requirements.txt` | 4.16 を下回らせない理由をコメントで明記 |
| `dev/egress_check.py` | compose の位置とサービス名（`ig-` 接頭辞）に追随 |
| `dev/silence_check.py` | 同上。走査先を `ig/app/` に（X-Ray の `app/` ではない） |
| `dev/il416_check.py` | バージョン固定の検査を「固定行だけ」に（コメントで誤検知していた） |
| `dev/target_check.py` | 表示層のテストを外した（下記） |

**表示層のテストは外してある。** IG-Ray の `web.py` を import するもの
（`egress_check.test_banner_render` / `silence_check.test_render` ほか）は、
表示が X-Ray 本体に移ったのでここでは走らない。コード上はコメントアウトして
理由を書いてあり、**同等の検証は X-Ray 側の `dev/ig_check.py` が持つ**。

## DBは共有

`ig/app/db.py` が書くDBは、X-Ray の `app/ig_bridge.py` が読むのと同じファイル。
取得側がここに書き、表示側は `query_only` で読む。
**一本化はしていない**（`backfill_jobs.resume_json` のような IG 固有の構造を
畳む意味がないため）。

## 使い方

```bash
# profile 付きのサービスは素の build ではビルドされない
docker compose --profile ig-cron --profile ig-tools --profile ig-backfill build

docker compose run --rm ig-import-cookies    # セッション取り込み
docker compose run --rm ig-ipcheck           # 出口IPの確認
docker compose run --rm ig-accounts list     # 監視対象の管理
docker compose run --rm ig-scrape ユーザー名 --limit 3 --no-media
docker compose --profile ig-cron up -d       # 定期巡回を有効化
```

## テスト

```bash
cd ig && for t in dev/*.py; do echo "--- $t"; python3 "$t"; done
```
