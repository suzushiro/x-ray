"""
スキーマと既存DBの作り替え（マイグレーション）の検査。

いま動いているDBは20万行以上あって作り直せない。**既存DBを壊さずに
昇格できること**がここで一番大事な性質で、新規作成が通るだけでは足りない。

確かめること:
  - 新規作成が通る／二度流しても壊れない
  - 列が足りない古いDBを、中身を落とさずに昇格できる
  - 記録表が無い既存DB（いま動いているもの）でも通る
  - `add_column` が例外を飲まない（飲むと書き間違いに気づけない）
  - 一度入った版の中身を後から書き換えていない
"""
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("SCHEMA_CHECK_DIR") or tempfile.mkdtemp(prefix="schchk")
os.makedirs(BASE, exist_ok=True)
os.environ["DB_PATH"] = os.path.join(BASE, "data.db")
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import db                                                # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("OK  " if ok else "NG  ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


# ---------------------------------------------------------------------
print("== [1] 新規作成 ==")

db.init_db()
c = db.get_conn()
for t in ("accounts", "tweets", "bookmarks", "scrape_log", "media_index",
          "tumblr_presets", "app_meta", "deleted_media", "schema_migrations"):
    check(f"{t} がある", db.has_table(c, t))
check("tweets に quoted_json がある", "quoted_json" in cols(c, "tweets"))
check("bookmarks に quoted_json がある", "quoted_json" in cols(c, "bookmarks"))
check("share_tokens は作らない", not db.has_table(c, "share_tokens"))
check("版が記録されている", db.schema_version(c) == max(m[0] for m in db.MIGRATIONS),
      str(db.schema_version(c)))
c.close()

# 二度流しても壊れない
db.init_db()
c = db.get_conn()
check("二度流しても通る", db.has_table(c, "tweets"))
check("記録が重複しない",
      c.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
      == len(db.MIGRATIONS),
      str(c.execute("SELECT COUNT(*) AS n FROM schema_migrations"
                    ).fetchone()["n"]))
c.close()

# ---------------------------------------------------------------------
print("\n== [2] 古いDBの昇格（中身を落とさない）==")

# v1 相当の、列が足りないDBを手で作る。
# いま動いているDBには schema_migrations が無いので、その状態も再現する。
OLD = os.path.join(BASE, "old.db")
oc = sqlite3.connect(OLD)
oc.executescript("""
CREATE TABLE accounts (
    screen_name TEXT PRIMARY KEY, display_name TEXT NOT NULL,
    categories TEXT NOT NULL, user_id TEXT, last_scraped_at TEXT);
CREATE TABLE tweets (
    tweet_id TEXT PRIMARY KEY, screen_name TEXT NOT NULL, content TEXT,
    created_at TEXT, url TEXT, like_count INTEGER DEFAULT 0,
    retweet_count INTEGER DEFAULT 0, reply_count INTEGER DEFAULT 0,
    media_json TEXT, fetched_at TEXT);
CREATE TABLE bookmarks (
    tweet_id TEXT PRIMARY KEY, screen_name TEXT, display_name TEXT,
    content TEXT, created_at TEXT, url TEXT, media_json TEXT,
    local_media_json TEXT, video_json TEXT, categories TEXT,
    profile_image_url TEXT, bookmarked_at TEXT);
CREATE TABLE scrape_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, screen_name TEXT,
    status TEXT, message TEXT, new_tweets INTEGER DEFAULT 0);
-- 撤去したはずのものが残っている状態
CREATE TABLE share_tokens (token TEXT PRIMARY KEY, tweet_id TEXT);
INSERT INTO accounts VALUES ('alice','Alice','["art"]','1',NULL);
INSERT INTO tweets (tweet_id, screen_name, content, created_at, like_count)
VALUES ('100','alice','むかしの投稿','2024-01-01T00:00:00+09:00',7);
INSERT INTO bookmarks (tweet_id, screen_name, content)
VALUES ('100','alice','むかしの投稿');
INSERT INTO scrape_log (run_at, screen_name, status) VALUES ('x','alice','ok');
INSERT INTO share_tokens VALUES ('tok','100');
""")
oc.commit()
oc.close()

_before = sqlite3.connect(OLD)
_n_tweets = _before.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
_before.close()

os.environ["DB_PATH"] = OLD
import importlib                                          # noqa: E402
importlib.reload(db)
db.init_db()

c = db.get_conn()
# 足りなかった列が入る
check("accounts に profile_image_url が入る",
      "profile_image_url" in cols(c, "accounts"))
for name in ("video_json", "reply_to_tweet_id", "local_media_json",
             "quoted_json"):
    check(f"tweets に {name} が入る", name in cols(c, "tweets"))
check("bookmarks に quoted_json が入る", "quoted_json" in cols(c, "bookmarks"))
# 後から足したテーブルも入る
for t in ("media_index", "tumblr_presets", "app_meta", "deleted_media"):
    check(f"{t} が追加される", db.has_table(c, t))
# 撤去したものは消える
check("share_tokens が落ちる", not db.has_table(c, "share_tokens"))

# ここが本番。中身が残っていること。
check("tweets の中身が残る",
      c.execute("SELECT COUNT(*) AS n FROM tweets").fetchone()["n"] == _n_tweets,
      str(c.execute("SELECT COUNT(*) AS n FROM tweets").fetchone()["n"]))
_r = c.execute("SELECT content, like_count FROM tweets"
               " WHERE tweet_id='100'").fetchone()
check("本文が壊れていない", _r and _r["content"] == "むかしの投稿",
      str(dict(_r) if _r else None))
check("数値が壊れていない", _r and _r["like_count"] == 7)
check("足した列は NULL", c.execute(
    "SELECT quoted_json FROM tweets WHERE tweet_id='100'"
).fetchone()["quoted_json"] is None)
check("ブックマークが残る",
      c.execute("SELECT COUNT(*) AS n FROM bookmarks").fetchone()["n"] == 1)
check("ログが残る",
      c.execute("SELECT COUNT(*) AS n FROM scrape_log").fetchone()["n"] == 1)
check("記録表が無かったDBでも版が入る",
      db.schema_version(c) == max(m[0] for m in db.MIGRATIONS),
      str(db.schema_version(c)))
c.close()

# もう一度流しても壊れない（毎回の起動でここを通る）
db.init_db()
c = db.get_conn()
check("昇格後に再度流しても中身が残る",
      c.execute("SELECT COUNT(*) AS n FROM tweets").fetchone()["n"] == _n_tweets)
c.close()

# ---------------------------------------------------------------------
print("\n== [2b] 擬似アカウントの行を run_log へ移す ==")

# v58 まで scrape_log の screen_name に `__run__` / `__egress__` を
# 相乗りさせていた。移行で run_log に移し、元を消す。
PS = os.path.join(BASE, "pseudo.db")
pc = sqlite3.connect(PS)
pc.executescript("""
CREATE TABLE accounts (
    screen_name TEXT PRIMARY KEY, display_name TEXT NOT NULL,
    categories TEXT NOT NULL);
-- 初版の tweets。created_at は初版から有るのでここにも置く
-- （無い形で書いたらインデックス作成で落ちた。実在しない形だった）
CREATE TABLE tweets (
    tweet_id TEXT PRIMARY KEY, screen_name TEXT NOT NULL, content TEXT,
    created_at TEXT, url TEXT, media_json TEXT, fetched_at TEXT);
CREATE TABLE bookmarks (tweet_id TEXT PRIMARY KEY, screen_name TEXT,
    created_at TEXT);
CREATE TABLE scrape_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, screen_name TEXT,
    status TEXT, message TEXT, new_tweets INTEGER DEFAULT 0);
INSERT INTO scrape_log (run_at, screen_name, status, message) VALUES
 ('2026-11-01T00:00:00+00:00','alice','ok',''),
 ('2026-11-01T00:01:00+00:00','bob','error','こけた'),
 ('2026-11-01T00:02:00+00:00','__run__','run_error','3件すべて失敗'),
 ('2026-11-01T00:03:00+00:00','__egress__','warn','出口が変わった'),
 ('2026-11-01T00:04:00+00:00','__run__','run_ok','2/3件成功');
""")
pc.commit()
pc.close()

os.environ["DB_PATH"] = PS
importlib.reload(db)
db.init_db()

c = db.get_conn()
check("run_log ができる", db.has_table(c, "run_log"))
_rl = [dict(r) for r in c.execute(
    "SELECT kind, status, message FROM run_log ORDER BY id")]
check("擬似行が3件移る", len(_rl) == 3, str(_rl))
check("__run__ は kind=run になる",
      [r["kind"] for r in _rl] == ["run", "egress", "run"], str(_rl))
check("メッセージも移る",
      any("3件すべて失敗" == r["message"] for r in _rl), str(_rl))
check("状態も移る",
      {r["status"] for r in _rl} == {"run_error", "warn", "run_ok"}, str(_rl))

_sl = [dict(r) for r in c.execute(
    "SELECT screen_name, status FROM scrape_log ORDER BY id")]
check("scrape_log から擬似行が消える",
      [r["screen_name"] for r in _sl] == ["alice", "bob"], str(_sl))
check("実アカウントの行は残る",
      any(r["screen_name"] == "bob" and r["status"] == "error" for r in _sl),
      str(_sl))
c.close()

# 二度流しても増えない（毎回の起動でここを通る）
db.init_db()
c = db.get_conn()
check("二度流しても run_log が増えない",
      c.execute("SELECT COUNT(*) AS n FROM run_log").fetchone()["n"] == 3,
      str(c.execute("SELECT COUNT(*) AS n FROM run_log").fetchone()["n"]))
c.close()

# ---------------------------------------------------------------------
print("\n== [3] add_column は例外を飲まない ==")

# `try: ALTER / except: pass` だと「もう列がある」と「書き間違い」が
# 同じ pass になる。実際にそれで列の追加が効いていないことに
# 気づけなかった箇所があった。
c = db.get_conn()
check("既にある列なら False を返す",
      db.add_column(c, "tweets", "content", "TEXT") is False)
check("無い列なら True を返す",
      db.add_column(c, "tweets", "_zzz_probe", "TEXT") is True)
c.execute("ALTER TABLE tweets DROP COLUMN _zzz_probe")

_raised = False
try:
    # 型指定がSQLとして壊れている。飲まずに投げること
    db.add_column(c, "tweets", "_zzz_bad", "NOT A TYPE (((")
except sqlite3.Error:
    _raised = True
check("SQLの書き間違いは投げる", _raised)

check("知らない表には何もしない",
      db.add_column(c, "_zzz_nosuch", "x", "TEXT") is False)
c.close()

# ---------------------------------------------------------------------
print("\n== [4] マイグレーションの並び ==")

vers = [m[0] for m in db.MIGRATIONS]
check("版が重複していない", len(set(vers)) == len(vers), str(vers))
check("版が昇順", vers == sorted(vers), str(vers))
check("1から連番", vers == list(range(1, len(vers) + 1)), str(vers))
check("全部に説明がある", all(str(m[1]).strip() for m in db.MIGRATIONS))
check("全部が呼べる", all(callable(m[2]) for m in db.MIGRATIONS))

print()
if fails:
    print(f"=== 失敗 {len(fails)}件: {fails} ===")
    sys.exit(1)
print("=== 全て通過 ===")
