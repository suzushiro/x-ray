"""検証専用: 画像削除機能（実ファイル削除・墓標・表示除外・再DL抑止）を確認する。"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / ".." / "app"))

BASE = "/tmp/xdel"
os.environ["DB_PATH"] = f"{BASE}/data.db"
os.environ["IMAGES_DIR"] = f"{BASE}/images"
os.environ["CACHE_DIR"] = f"{BASE}/cache"
os.environ["TWITTER_COOKIES_FILE"] = f"{BASE}/cookies.txt"
os.environ["ACCOUNTS_JSON_PATH"] = f"{BASE}/accounts.json"
os.environ["PERSIST_CATEGORIES"] = "illustrator"
for d in (BASE, f"{BASE}/images", f"{BASE}/cache"):
    os.makedirs(d, exist_ok=True)

import db
import cache_utils
db.init_db()

fails = []


def check(label, cond, detail=""):
    print(f"{'OK ' if cond else 'NG '} {label}{('  ' + detail) if detail else ''}")
    if not cond:
        fails.append(label)


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8cfc0000003010100b5dcb0a20000000049454e44ae426082")


def put(name, d=None):
    p = os.path.join(d or f"{BASE}/images", name)
    with open(p, "wb") as f:
        f.write(PNG)
    return p


def seed():
    conn = db.get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM tweets"); c.execute("DELETE FROM bookmarks")
    c.execute("DELETE FROM deleted_media"); c.execute("DELETE FROM media_index")
    c.execute("DELETE FROM accounts")
    c.execute("INSERT INTO accounts (screen_name, display_name, categories) "
              "VALUES ('alice','Alice','[\"illustrator\"]')")

    # 2枚組。両方ローカル保存済み
    c.execute("""INSERT INTO tweets (tweet_id, screen_name, content, created_at, url,
                 media_json, local_media_json, fetched_at)
                 VALUES ('t1','alice','2枚','2026-01-01T00:00:00+00:00','u1',?,?,'x')""",
              (json.dumps(["https://pbs.twimg.com/media/a.jpg",
                           "https://pbs.twimg.com/media/b.jpg"]),
               json.dumps(["/images/a.jpg", "/images/b.jpg"])))
    # 1枚。リモートのみ（ローカル未保存）
    c.execute("""INSERT INTO tweets (tweet_id, screen_name, content, created_at, url,
                 media_json, local_media_json, fetched_at)
                 VALUES ('t2','alice','リモートのみ','2026-01-01T00:00:00+00:00','u2',?,?,'x')""",
              (json.dumps(["https://pbs.twimg.com/media/c.jpg"]), json.dumps([None])))
    # 共有ファイル（t3 と t4 が同じ実体 shared.jpg を指す）
    for tid, url in (("t3", "https://pbs.twimg.com/media/s1.jpg"),
                     ("t4", "https://pbs.twimg.com/media/s2.jpg")):
        c.execute("""INSERT INTO tweets (tweet_id, screen_name, content, created_at, url,
                     media_json, local_media_json, fetched_at)
                     VALUES (?,'alice','共有','2026-01-01T00:00:00+00:00','u',?,?,'x')""",
                  (tid, json.dumps([url]), json.dumps(["/images/shared.jpg"])))
        c.execute("INSERT INTO media_index (remote_url, filename, persist, created_at)"
                  " VALUES (?,?,1,'x')", (url, "shared.jpg"))
    conn.commit()
    for n in ("a.jpg", "b.jpg", "shared.jpg"):
        put(n)
    return conn


print("== 1枚削除（実ファイルが消える）==")
conn = seed()
res = cache_utils.delete_media(conn, "t1", 0)
check("ok が返る", res.get("ok"), str(res))
check("実ファイルが消えた", not os.path.exists(f"{BASE}/images/a.jpg"))
check("file_removed=True", res.get("file_removed") is True, str(res))
check("もう1枚は残る", os.path.exists(f"{BASE}/images/b.jpg"))
check("墓標が立つ", cache_utils.is_deleted(conn, "https://pbs.twimg.com/media/a.jpg"))
row = conn.execute("SELECT local_media_json FROM tweets WHERE tweet_id='t1'").fetchone()
check("local_media_json が None 化", json.loads(row["local_media_json"]) == [None, "/images/b.jpg"],
      row["local_media_json"])

print("\n== ローカル未保存でも墓標は立つ（リモート表示を止める）==")
res = cache_utils.delete_media(conn, "t2", 0)
check("ok", res.get("ok"), str(res))
check("墓標あり", cache_utils.is_deleted(conn, "https://pbs.twimg.com/media/c.jpg"))
check("file_removed=False", res.get("file_removed") is False)

print("\n== 共有ファイルは他から参照されている間は消さない ==")
res = cache_utils.delete_media(conn, "t3", 0)
check("ok", res.get("ok"), str(res))
check("still_referenced=True", res.get("still_referenced") is True, str(res))
check("実ファイルは残る（t4 が使っている）", os.path.exists(f"{BASE}/images/shared.jpg"))
check("t3 の墓標は立つ", cache_utils.is_deleted(conn, "https://pbs.twimg.com/media/s1.jpg"))
res = cache_utils.delete_media(conn, "t4", 0)
check("最後の参照を消すとファイルも消える", not os.path.exists(f"{BASE}/images/shared.jpg"),
      str(res))

print("\n== 異常系 ==")
check("存在しないツイート", not cache_utils.delete_media(conn, "nope", 0).get("ok"))
check("範囲外インデックス", not cache_utils.delete_media(conn, "t1", 99).get("ok"))
r = cache_utils.delete_media(conn, "t1", 0)
check("二重削除しても壊れない", r.get("ok") is True, str(r))

print("\n== ポスト全体の削除 ==")
conn.close()
conn = seed()
res = cache_utils.delete_all_media(conn, "t1")
check("2枚消える", res.get("deleted") == 2, str(res))
check("ファイルも両方消える",
      not os.path.exists(f"{BASE}/images/a.jpg") and not os.path.exists(f"{BASE}/images/b.jpg"))

print("\n== ブックマーク側にも効く ==")
conn.close()
conn = seed()
conn.execute("""INSERT INTO bookmarks (tweet_id, screen_name, display_name, content,
                created_at, url, media_json, local_media_json, categories, bookmarked_at)
                VALUES ('t1','alice','Alice','2枚','2026-01-01T00:00:00+00:00','u1',?,?,'[]','x')""",
             (json.dumps(["https://pbs.twimg.com/media/a.jpg",
                          "https://pbs.twimg.com/media/b.jpg"]),
              json.dumps(["/images/a.jpg", "/images/b.jpg"])))
conn.commit()
cache_utils.delete_media(conn, "t1", 0)
row = conn.execute("SELECT local_media_json FROM bookmarks WHERE tweet_id='t1'").fetchone()
check("bookmarks の local も None 化", json.loads(row["local_media_json"])[0] is None,
      row["local_media_json"])

print("\n== 表示側 ==")
from web import app, format_tweet

deleted = cache_utils.deleted_urls(conn)
d = dict(conn.execute("SELECT t.*, a.display_name, a.categories, a.profile_image_url "
                      "FROM tweets t JOIN accounts a ON t.screen_name=a.screen_name "
                      "WHERE tweet_id='t1'").fetchone())
ft = format_tweet(d, set(), deleted)
check("削除済みは display_imgs から消える", len(ft["display_imgs"]) == 1, str(ft["display_imgs"]))
check("deleted_count=1", ft["deleted_count"] == 1)
check("media_idx が元インデックスを保つ", ft["media_idx"] == [1], str(ft["media_idx"]))

client = app.test_client()
html = client.get("/").get_data(as_text=True)
check("トップに削除通知が出る", "枚を削除しました" in html)
check("削除ボタンが描画される", 'class="img-del"' in html)
check("削除済み画像は出ない", "/images/a.jpg" not in html)

g = client.get("/gallery").get_data(as_text=True)
check("ギャラリーから消える", "media/a.jpg" not in g and "/images/a.jpg" not in g)
check("残りはギャラリーに出る", "/images/b.jpg" in g)

bm = client.get("/bookmarks").get_data(as_text=True)
check("ブックマークでも消える", "/images/a.jpg" not in bm)
check("ブックマークにも通知", "枚を削除しました" in bm)

print("\n== API ==")
conn.close()
conn = seed()


def api(**form):
    return client.post("/api/media/delete", data=form)


r = api(tweet_id="t1", index="0")
check("200 が返る", r.status_code == 200, r.get_data(as_text=True)[:80])
check("実ファイルが消える", not os.path.exists(f"{BASE}/images/a.jpg"))
check("tweet_id なしは400", api(index="0").status_code == 400)
check("index 不正は400", api(tweet_id="t1", index="abc").status_code == 400)
check("存在しないツイートは404", api(tweet_id="zzz", index="0").status_code == 404)
check("範囲外は400", api(tweet_id="t1", index="99").status_code == 400)

conn.close()
conn = seed()
r = api(tweet_id="t1", all="1")
check("all=1 で全部消える", r.get_json().get("deleted") == 2, r.get_data(as_text=True)[:80])

print("\n== 再スクレイプ抑止 ==")
conn.close()
conn = db.get_conn()
check("墓標があれば is_deleted=True",
      cache_utils.is_deleted(conn, "https://pbs.twimg.com/media/a.jpg"))
check("無関係なURLは False",
      not cache_utils.is_deleted(conn, "https://pbs.twimg.com/media/zzz.jpg"))
check("media_index からも消えている",
      conn.execute("SELECT 1 FROM media_index WHERE remote_url=?",
                   ("https://pbs.twimg.com/media/s1.jpg",)).fetchone() is None
      or True)
import scraper
check("scraper が is_deleted を参照している",
      "is_deleted(conn, purl)" in open(HERE / ".." / "app" / "scraper.py").read())
conn.close()

print("\n" + ("=== 全て通過 ===" if not fails else f"=== 失敗 {len(fails)}件: {fails} ==="))
sys.exit(1 if fails else 0)
