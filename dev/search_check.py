"""
検索の検査。

v58 までの状態:

  - X側は FTS5（tweets_fts）を先に試していた。既定のトークナイザ
    （unicode61）は日本語の連なりを**ひとかたまりのトークン**にするので、
    「猫」「写真」のような部分一致が**1件も返らない**。しかも例外では
    なく0件なので、LIKE へのフォールバックも働かなかった。
    本文が日本語主体の X-Ray では、X側の検索が黙って機能していなかった。
  - IG側は LIKE だったので引けた。**同じ語で出自によって結果が違う。**
  - ブックマークは検索の対象外。元が消えても残す仕組みなのに、
    消えた投稿は探せなかった（tweets から消えるため）。

いまは全部 LIKE で、ブックマークも対象。索引は撤去した
（23万行で tweets 本体25MBに対し索引30MB、LIKE+LIMIT は 0.1ms）。
"""
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("SEARCH_CHECK_DIR") or tempfile.mkdtemp(prefix="srchchk")
for sub in ("images", "cache", "igcache"):
    os.makedirs(os.path.join(BASE, sub), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(BASE, "data.db")
os.environ["IMAGES_DIR"] = os.path.join(BASE, "images")
os.environ["CACHE_DIR"] = os.path.join(BASE, "cache")
os.environ["TWITTER_COOKIES_FILE"] = os.path.join(BASE, "c.txt")
os.environ["ACCOUNTS_JSON_PATH"] = os.path.join(BASE, "a.json")
os.environ["IG_DB_PATH"] = os.path.join(BASE, "ig_ray.db")
os.environ["IG_CACHE_DIR"] = os.path.join(BASE, "igcache")
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import db                                                # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("OK  " if ok else "NG  ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


# --- フィクスチャ -----------------------------------------------------

db.init_db()
c = db.get_conn()
c.execute("INSERT INTO accounts (screen_name, display_name, categories)"
          " VALUES ('alice','Alice','[\"art\"]')")
c.executemany("""
    INSERT INTO tweets (tweet_id, screen_name, content, created_at, url,
                        media_json, local_media_json, fetched_at)
    VALUES (?,?,?,?,?,'[]','[]',?)
""", [
    ("1", "alice", "猫がかわいい写真を撮った", "2026-11-20T10:00:00+09:00",
     "https://x.com/alice/status/1", "2026-11-20T10:00:00+09:00"),
    ("2", "alice", "新しいカメラを買った", "2026-11-19T10:00:00+09:00",
     "https://x.com/alice/status/2", "2026-11-19T10:00:00+09:00"),
    ("3", "alice", "hello world of cats", "2026-11-18T10:00:00+09:00",
     "https://x.com/alice/status/3", "2026-11-18T10:00:00+09:00"),
])
# 元の投稿が消えているブックマーク（tweets には無い）
c.execute("""
    INSERT INTO bookmarks (tweet_id, screen_name, display_name, content,
                           created_at, url, media_json, local_media_json,
                           categories, bookmarked_at)
    VALUES ('gone1','ghost','Ghost','消えた投稿だが猫の話',
            '2026-10-01T10:00:00+09:00','https://x.com/ghost/status/gone1',
            '[]','[]','[]','2026-10-02T00:00:00+09:00')
""")
c.commit()
c.close()

# IG側
ig = sqlite3.connect(os.environ["IG_DB_PATH"])
ig.executescript("""
CREATE TABLE accounts (username TEXT PRIMARY KEY, full_name TEXT,
  categories_json TEXT, profile_pic_local TEXT, is_target INTEGER DEFAULT 1,
  is_muted INTEGER DEFAULT 0);
CREATE TABLE posts (shortcode TEXT PRIMARY KEY, owner_username TEXT,
  caption TEXT, date_utc TEXT, likes INTEGER, comments INTEGER,
  media_json TEXT);
INSERT INTO accounts VALUES ('iguser','IG User','["art"]',NULL,1,0);
CREATE TABLE media_index (shortcode TEXT, media_index INTEGER,
  remote_url TEXT, local_path TEXT, PRIMARY KEY (shortcode, media_index));
INSERT INTO posts VALUES
 ('IG1','iguser','IGにも猫の写真','2026-11-17T01:00:00+00:00',5,1,'[]');
""")
ig.commit()
ig.close()

import web                                               # noqa: E402

cl = web.app.test_client()


def found(q):
    return cl.get(f"/search?q={q}").get_data(as_text=True)


# ---------------------------------------------------------------------
print("== [1] 日本語の部分一致が引ける ==")

# ここが本題。v58 までは X 側が FTS を使っていて0件だった。
h = found("猫")
check("1文字でも引ける", "猫がかわいい写真を撮った" in h)
h = found("写真")
check("2文字でも引ける", "猫がかわいい写真を撮った" in h)
h = found("かわいい")
check("語の途中からでも引ける", "猫がかわいい写真を撮った" in h)
h = found("カメラ")
check("別の投稿も引ける", "新しいカメラを買った" in h)
h = found("ない語")
check("無い語では出ない", "猫がかわいい" not in h)

print("\n== [2] 出自によって結果が変わらない ==")

# 同じ語で X と IG の両方が出ること。
# v58 までは X が FTS・IG が LIKE で、同じ語でも片方しか出なかった。
h = found("猫")
check("Xの投稿が出る", "猫がかわいい写真を撮った" in h)
check("IGの投稿も出る", "IGにも猫の写真" in h, )
h = found("cats")
check("英語も引ける", "hello world of cats" in h)

print("\n== [3] ブックマークも探せる ==")

# ブックマークは元が消えても残す仕組み。残しているのに探せないのでは
# 意味が無い（v58 までは tweets からしか引いていなかった）。
h = found("消えた")
check("元が消えたブックマークが出る", "消えた投稿だが猫の話" in h)
h = found("猫")
check("猫でもブックマークが混ざる", "消えた投稿だが猫の話" in h)
check("同時にXの投稿も出る", "猫がかわいい写真を撮った" in h)

# 同じ投稿が二重に出ないこと（tweets とブックマークの両方にある場合）
c = db.get_conn()
c.execute("""
    INSERT INTO bookmarks (tweet_id, screen_name, display_name, content,
                           created_at, url, media_json, local_media_json,
                           categories, bookmarked_at)
    VALUES ('1','alice','Alice','猫がかわいい写真を撮った',
            '2026-11-20T10:00:00+09:00','https://x.com/alice/status/1',
            '[]','[]','["art"]','2026-11-21T00:00:00+09:00')
""")
c.commit()
c.close()
h = found("かわいい")
check("両方にある投稿が二重に出ない",
      h.count("猫がかわいい写真を撮った") == 1,
      str(h.count("猫がかわいい写真を撮った")))

print("\n== [4] 全文検索の索引は持たない ==")

c = db.get_conn()
_fts = c.execute(
    "SELECT name FROM sqlite_master WHERE name LIKE 'tweets_fts%'").fetchall()
c.close()
check("tweets_fts が無い", _fts == [], str([r[0] for r in _fts]))

_src = open(os.path.join(HERE, "..", "app", "scraper.py")).read()
check("スクレイパーが索引に書かない",
      "INSERT INTO tweets_fts" not in _src)
_web = open(os.path.join(HERE, "..", "app", "web.py")).read()
check("検索が MATCH を使わない", "MATCH" not in _web)

# 既にある索引は移行で落とす
OLD = os.path.join(BASE, "withfts.db")
oc = sqlite3.connect(OLD)
oc.executescript("""
CREATE TABLE accounts (screen_name TEXT PRIMARY KEY, display_name TEXT NOT NULL,
  categories TEXT NOT NULL);
CREATE TABLE tweets (tweet_id TEXT PRIMARY KEY, screen_name TEXT NOT NULL,
  content TEXT, created_at TEXT, fetched_at TEXT);
CREATE TABLE bookmarks (tweet_id TEXT PRIMARY KEY, created_at TEXT);
CREATE TABLE scrape_log (id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT,
  screen_name TEXT, status TEXT, message TEXT, new_tweets INTEGER DEFAULT 0);
CREATE VIRTUAL TABLE tweets_fts USING fts5(tweet_id UNINDEXED, content,
  screen_name UNINDEXED, display_name UNINDEXED);
INSERT INTO tweets (tweet_id, screen_name, content) VALUES ('9','alice','x');
INSERT INTO tweets_fts (tweet_id, content) VALUES ('9','x');
""")
oc.commit()
oc.close()
os.environ["DB_PATH"] = OLD
import importlib                                          # noqa: E402
importlib.reload(db)
db.init_db()
c = db.get_conn()
check("移行で索引が落ちる",
      c.execute("SELECT name FROM sqlite_master"
                " WHERE name LIKE 'tweets_fts%'").fetchall() == [])
check("投稿は残る",
      c.execute("SELECT COUNT(*) AS n FROM tweets").fetchone()["n"] == 1)
c.close()

print("\n== [5] 件数の上限 ==")

check("上限が1箇所で決まっている", hasattr(web, "SEARCH_LIMIT"))
check("上限が妥当", 50 <= web.SEARCH_LIMIT <= 1000, str(web.SEARCH_LIMIT))

print()
if fails:
    print(f"=== 失敗 {len(fails)}件: {fails} ===")
    sys.exit(1)
print("=== 全て通過 ===")
