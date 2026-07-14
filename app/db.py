import sqlite3
import json
import os

DB_PATH = os.environ.get("DB_PATH", "/data/data.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        screen_name TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        categories TEXT NOT NULL,   -- JSON array
        user_id TEXT,               -- X internal numeric id (resolved by twscrape)
        profile_image_url TEXT,     -- アイコン画像URL
        last_scraped_at TEXT
    )
    """)

    # 既存DBへのマイグレーション
    try:
        cur.execute("ALTER TABLE accounts ADD COLUMN profile_image_url TEXT")
    except Exception:
        pass

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tweets (
        tweet_id TEXT PRIMARY KEY,
        screen_name TEXT NOT NULL,
        content TEXT,
        created_at TEXT,
        url TEXT,
        like_count INTEGER DEFAULT 0,
        retweet_count INTEGER DEFAULT 0,
        reply_count INTEGER DEFAULT 0,
        media_json TEXT,            -- JSON array of photo urls
        video_json TEXT,            -- JSON array of {thumb, url} for videos
        reply_to_tweet_id TEXT,     -- 自己リプライ元のtweet_id
        fetched_at TEXT,
        FOREIGN KEY (screen_name) REFERENCES accounts(screen_name)
    )
    """)

    # 既存DBへのマイグレーション
    try:
        cur.execute("ALTER TABLE tweets ADD COLUMN video_json TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE tweets ADD COLUMN reply_to_tweet_id TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE tweets ADD COLUMN local_media_json TEXT")  # ローカル保存画像パス
    except Exception:
        pass

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_tweets_created
    ON tweets(created_at DESC)
    """)

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_tweets_screen_name
    ON tweets(screen_name)
    """)

    # ブックマークテーブル（ツイート内容をコピー保存して永続化）
    cur.execute("""
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
        bookmarked_at TEXT
    )
    """)

    # 全文検索用 FTS5 仮想テーブル
    try:
        cur.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS tweets_fts USING fts5(
            tweet_id UNINDEXED,
            content,
            screen_name UNINDEXED,
            display_name UNINDEXED
        )
        """)
    except Exception as e:
        print(f"[!] FTS5テーブル作成失敗（SQLiteがFTS5非対応の可能性）: {e}")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS scrape_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT,
        screen_name TEXT,
        status TEXT,        -- ok / error
        message TEXT,
        new_tweets INTEGER DEFAULT 0
    )
    """)

    # 画像の重複排除用インデックス。
    # リポスト/引用RTでは同じ pbs.twimg.com URL が何度も出てくるため、
    # URL→保存済みファイル名 を引けるようにして再DLせずハードリンクを張る。
    cur.execute("""
    CREATE TABLE IF NOT EXISTS media_index (
        remote_url TEXT PRIMARY KEY,
        filename   TEXT NOT NULL,
        persist    INTEGER NOT NULL DEFAULT 0,
        sha256     TEXT,
        created_at TEXT
    )
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_media_index_sha ON media_index(sha256)
    """)

    # scrape_log は 15分 x アカウント数 で急速に増える。
    # トップページが run_at で絞り込むためインデックス必須。
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_scrape_log_run_at
    ON scrape_log(run_at DESC)
    """)

    # ユーザーページ用の複合インデックス
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_tweets_sn_created
    ON tweets(screen_name, created_at DESC)
    """)

    conn.commit()
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
        """, (screen_name, display_name, json.dumps(categories, ensure_ascii=False)))
    conn.commit()
    conn.close()
    print(f"Seeded {len(ACCOUNTS)} accounts.")


if __name__ == "__main__":
    init_db()
    seed_accounts()
    print("DB initialized at", DB_PATH)
