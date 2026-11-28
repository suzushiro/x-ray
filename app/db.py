"""
スキーマと、既存DBの作り替え（マイグレーション）。

## 作りの方針

テーブルの定義は `SCHEMA`、既存DBに後から入れる変更は `MIGRATIONS`。

以前は `init_db()` が

    try:
        cur.execute("ALTER TABLE tweets ADD COLUMN video_json TEXT")
    except Exception:
        pass

をずらっと並べた形だった。これには2つ問題がある。

- **全部の例外を飲む。** 「もう列がある」も「SQLの書き間違い」も
  「DBが壊れている」も同じ `pass` になる。実際この形のまま
  列の追加が効いていなかったことに気づけなかった箇所がある
- **何がいつ入ったのか残らない。** 手元とサーバでスキーマが違っても
  比べる手段が無い

いまは `add_column()` が `PRAGMA table_info` を見てから足すので、
**例外を飲まずに冪等**になる。そのうえで適用した版を
`schema_migrations` に記録する。

記録だけに頼って「この版は適用済みだから飛ばす」とはしていない。
いま動いているDBには記録表そのものが無く、記録を信じると
既にある列へ `ALTER` を打って落ちる。**冪等であることが正しさの根拠**で、
記録は「何がいつ入ったか」を見るためのもの。
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/data.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


# --- スキーマ。毎回流す（CREATE IF NOT EXISTS なので冪等） -------------

SCHEMA = [
    ("accounts", """
    CREATE TABLE IF NOT EXISTS accounts (
        screen_name TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        categories TEXT NOT NULL,   -- JSON array
        user_id TEXT,               -- X internal numeric id (twscrapeが解決)
        profile_image_url TEXT,     -- アイコン画像URL
        last_scraped_at TEXT
    )"""),

    ("tweets", """
    CREATE TABLE IF NOT EXISTS tweets (
        tweet_id TEXT PRIMARY KEY,
        screen_name TEXT NOT NULL,
        content TEXT,
        created_at TEXT,
        url TEXT,
        like_count INTEGER DEFAULT 0,
        retweet_count INTEGER DEFAULT 0,
        reply_count INTEGER DEFAULT 0,
        media_json TEXT,            -- 画像URLのJSON配列
        video_json TEXT,            -- {thumb, url} のJSON配列
        local_media_json TEXT,      -- ローカル保存画像のパス
        reply_to_tweet_id TEXT,     -- 自己リプライ元のtweet_id
        -- 引用RTの引用元スナップショット。引用元が消えても表示が残るよう
        -- 内容ごと保存する
        quoted_json TEXT,
        fetched_at TEXT,
        FOREIGN KEY (screen_name) REFERENCES accounts(screen_name)
    )"""),

    # ブックマーク。投稿の内容をコピーして保存し、元が消えても残す
    ("bookmarks", """
    CREATE TABLE IF NOT EXISTS bookmarks (
        tweet_id TEXT PRIMARY KEY,
        screen_name TEXT,
        display_name TEXT,
        content TEXT,
        created_at TEXT,
        url TEXT,
        media_json TEXT,
        local_media_json TEXT,
        video_json TEXT,
        categories TEXT,
        profile_image_url TEXT,
        quoted_json TEXT,
        bookmarked_at TEXT
    )"""),

    # アカウント単位の取得結果。**実アカウントだけ**が入る。
    # 巡回そのものや出口IPの状態は run_log（下）に入れる。
    ("scrape_log", """
    CREATE TABLE IF NOT EXISTS scrape_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT,
        screen_name TEXT,
        status TEXT,        -- ok / error
        message TEXT,
        new_tweets INTEGER DEFAULT 0
    )"""),

    # 巡回そのものの記録。アカウント単位ではないもの。
    #
    # 以前は scrape_log の screen_name に `__run__` / `__egress__` という
    # 擬似アカウント名で相乗りさせていた。そのため
    #   - 「エラーが続いているアカウント」の一覧から擬似名を除く条件を
    #     全クエリに配る必要があり、書き損じてトップページを500にした
    #   - 「最後に取れた時刻」を見るのに実アカウントだけに絞る必要があった
    #   - アカウント名の列に、アカウントでないものが入っている
    # kind は 'run'（巡回まるごと）/ 'egress'（出口IP）。
    ("run_log", """
    CREATE TABLE IF NOT EXISTS run_log (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at  TEXT,
        kind    TEXT NOT NULL,
        status  TEXT,
        message TEXT
    )"""),

    # 画像の重複排除用インデックス。
    # リポスト/引用RTでは同じ pbs.twimg.com URL が何度も出てくるため、
    # URL→保存済みファイル名 を引けるようにして再DLせずハードリンクを張る。
    ("media_index", """
    CREATE TABLE IF NOT EXISTS media_index (
        remote_url TEXT PRIMARY KEY,
        filename   TEXT NOT NULL,
        persist    INTEGER NOT NULL DEFAULT 0,
        sha256     TEXT,
        created_at TEXT
    )"""),

    # Tumblr投稿のキャプション／タグを、投稿主ごとに覚えておく。
    # 同じ人の投稿を上げるたびに同じ編集をやり直さなくて済むようにする。
    # caption は本文だけを保存する。投稿時に付く元ポストへのリンクは
    # 投稿ごとに違うので、保存せず毎回その投稿のURLから作り直す。
    ("tumblr_presets", """
    CREATE TABLE IF NOT EXISTS tumblr_presets (
        screen_name TEXT PRIMARY KEY,
        caption     TEXT NOT NULL DEFAULT '',
        tags        TEXT NOT NULL DEFAULT '',
        updated_at  TEXT
    )"""),

    # 一度だけやる処理の済み印。
    # 「ig-ray から引き継いだプリセットの取り込み」のように、毎回やると
    # 消したものが復活してしまう類の処理に使う。DBと一緒に移動するので、
    # ファイルで持つより取り違えが起きにくい。
    ("app_meta", """
    CREATE TABLE IF NOT EXISTS app_meta (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at TEXT
    )"""),

    # 手動で削除した画像の墓標。remote_url をキーにすることで
    # tweets / bookmarks の両方、および再スクレイプ時の再DLを一括で抑止する。
    # ここに行があれば「その画像は二度と表示も取得もしない」。
    ("deleted_media", """
    CREATE TABLE IF NOT EXISTS deleted_media (
        remote_url  TEXT PRIMARY KEY,
        tweet_id    TEXT,
        screen_name TEXT,
        deleted_at  TEXT
    )"""),

    # 適用済みマイグレーションの記録。何がいつ入ったかを見るためのもの
    ("schema_migrations", """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version    INTEGER PRIMARY KEY,
        note       TEXT,
        applied_at TEXT
    )"""),
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tweets_created ON tweets(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tweets_screen_name ON tweets(screen_name)",
    # ユーザーページ用の複合インデックス
    "CREATE INDEX IF NOT EXISTS idx_tweets_sn_created"
    " ON tweets(screen_name, created_at DESC)",
    # scrape_log は 15分 x アカウント数 で急速に増える。
    # トップページが run_at で絞り込むためインデックス必須。
    "CREATE INDEX IF NOT EXISTS idx_scrape_log_run_at"
    " ON scrape_log(run_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_media_index_sha ON media_index(sha256)",
    "CREATE INDEX IF NOT EXISTS idx_run_log_kind_id ON run_log(kind, id DESC)",
]


# --- マイグレーションの道具 -------------------------------------------

def table_columns(conn, table):
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def has_table(conn, table):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view')"
        " AND name=?", (table,)).fetchone())


def add_column(conn, table, name, decl):
    """
    列が無ければ足す。あれば何もしない。

    `try: ALTER / except: pass` と違い、**例外を飲まない**。
    「もう列がある」と「SQLの書き間違い」を区別できる。
    """
    if not has_table(conn, table):
        return False
    if name in table_columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    return True


# --- マイグレーション本体 ---------------------------------------------
#
# (版, 説明, 関数)。関数は冪等に書くこと。
# 版を飛ばさない・既にある版の中身を書き換えない（記録と食い違う）。

def _m1(conn):
    add_column(conn, "accounts", "profile_image_url", "TEXT")


def _m2(conn):
    for name in ("video_json", "reply_to_tweet_id", "local_media_json",
                 "quoted_json"):
        add_column(conn, "tweets", name, "TEXT")


def _m3(conn):
    add_column(conn, "bookmarks", "quoted_json", "TEXT")


def _m4(conn):
    """
    旧 share_tokens（シェアツール方式の一時公開トークン）を落とす。

    Tumblr投稿をAPI方式に固定した時点で不要になっていたのに、
    テーブルとルートだけが残って**X-Ray で唯一の外部公開面**を
    開いたままにしていた。中身は一時トークンのみで、消して困るものは無い。
    """
    conn.execute("DROP TABLE IF EXISTS share_tokens")


def _m6(conn):
    """
    全文検索の索引（tweets_fts）を落とす。

    既定のトークナイザ（unicode61）は日本語の連なりをひとかたまりの
    トークンにするので、「猫」「写真」のような部分一致が**1件も返らない**。
    しかも例外を投げずに0件を返すため、LIKE へのフォールバックも
    働いていなかった（＝日本語の検索が黙って機能していなかった）。

    索引の大きさは tweets 本体より上（23万行で本体25MBに対し30MB）で、
    取得のたびに書き込みも発生していた。検索は本文の LIKE で足りる
    （同規模で LIMIT 付き 0.1ms、全件でも62ms）。

    外部テーブルではないので、落とせば付随する shadow table も消える。
    """
    conn.execute("DROP TABLE IF EXISTS tweets_fts")


def _m5(conn):
    """
    擬似アカウントの行を scrape_log から run_log へ移す。

    `__run__` / `__egress__` は「アカウント単位の取得結果」ではないのに
    screen_name に相乗りしていた。移してから元を消す。
    件数は多くない（巡回1回あたり高々2行）。

    移す元が無くなれば何もしないので、二度流しても害は無い。
    """
    if not has_table(conn, "scrape_log") or not has_table(conn, "run_log"):
        return
    conn.execute("""
        INSERT INTO run_log (run_at, kind, status, message)
        SELECT run_at,
               CASE screen_name WHEN '__run__' THEN 'run'
                                WHEN '__egress__' THEN 'egress'
                                ELSE 'run' END,
               status, message
        FROM scrape_log
        WHERE screen_name GLOB '__*__'
    """)
    conn.execute("DELETE FROM scrape_log WHERE screen_name GLOB '__*__'")


MIGRATIONS = [
    (1, "accounts に profile_image_url", _m1),
    (2, "tweets に video_json / reply_to_tweet_id /"
        " local_media_json / quoted_json", _m2),
    (3, "bookmarks に quoted_json", _m3),
    (4, "share_tokens を撤去（v54）", _m4),
    (5, "擬似アカウントの行を run_log へ移す（v58）", _m5),
    (6, "全文検索の索引 tweets_fts を撤去（v58）", _m6),
]


def init_db(verbose=False):
    conn = get_conn()
    try:
        for _name, ddl in SCHEMA:
            conn.execute(ddl)

        applied = {r["version"] for r in
                   conn.execute("SELECT version FROM schema_migrations")}
        now = datetime.now(timezone.utc).isoformat()
        for version, note, fn in MIGRATIONS:
            # 記録があっても流す。中身は冪等なので二度流しても害は無く、
            # 「記録が無いが列はある」既存DBもこれで通る。
            fn(conn)
            if version not in applied:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations"
                    " (version, note, applied_at) VALUES (?,?,?)",
                    (version, note, now))
                if verbose:
                    print(f"[*] マイグレーション {version}: {note}")

        # インデックスはマイグレーションの**後**に作る。
        # 先に作ると、後から足す列を参照するインデックス
        # （idx_tweets_created は created_at を見る）が
        # 「no such column」で落ちる。順番を逆にしていて実際に落とした。
        for ddl in INDEXES:
            conn.execute(ddl)

        conn.commit()
    finally:
        conn.close()


def schema_version(conn=None):
    """適用済みの最大版。記録が無ければ0。"""
    own = conn is None
    conn = conn or get_conn()
    try:
        r = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
        ).fetchone()
        return r["v"] if r else 0
    except sqlite3.Error:
        return 0
    finally:
        if own:
            conn.close()


def seed_accounts():
    from seed_accounts import ACCOUNTS

    conn = get_conn()
    cur = conn.cursor()
    for screen_name, display_name, categories in ACCOUNTS:
        cur.execute("""
        INSERT INTO accounts (screen_name, display_name, categories)
        VALUES (?, ?, ?)
        ON CONFLICT(screen_name) DO UPDATE SET
            display_name=excluded.display_name,
            categories=excluded.categories
        """, (screen_name, display_name,
              json.dumps(categories, ensure_ascii=False)))
    conn.commit()
    conn.close()
    print(f"Seeded {len(ACCOUNTS)} accounts.")


if __name__ == "__main__":
    init_db(verbose=True)
    seed_accounts()
    print(f"DB initialized at {DB_PATH} (schema v{schema_version()})")
