#!/usr/bin/env python3
"""
検証専用: 取得が黙って止まったことに気づけるか。

経緯:
  出口IPの判定で12日間、取得が1件も走っていなかった。
  そのとき入れた対策は「出口の異常を scrape_log に残す」で、
  **原因を1つ塞いだだけ**だった。想定していない死に方は必ず残る。
  そこで原因に依存しない見張り（最後に取れた時刻だけを見る）を足した。

  IG-Ray でも同じことが24日起きている。向こうが先に一般化した対策を
  入れていたので、それを持ち込んだ形。

ここで固定すること:
  1. しきい値を超えたら警告が**画面に出る**こと（文字列ではなく実レンダリング）
  2. 止まっているのに「正常稼働中」と出さないこと
  3. **成功ログが刈られていても気づけること**（ここがIG-Ray版に無い穴）
  4. 正常なときは出さないこと

  python3 dev/stale_check.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "app"

fails = []


def check(label, cond, detail=""):
    print(f"{'OK ' if cond else 'NG '} {label}{'  ' + str(detail) if detail else ''}")
    if not cond:
        fails.append(label)


BASE = tempfile.mkdtemp(prefix="xray-stale-")


def build(last_ok_hours=None, last_fetch_hours=None, errors=False):
    """
    last_ok_hours    : 何時間前に成功ログを置くか（None なら置かない）
    last_fetch_hours : ツイートの fetched_at を何時間前にするか
    """
    d = os.path.join(BASE, f"c{abs(hash((last_ok_hours, last_fetch_hours, errors)))}")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "DB_PATH": os.path.join(d, "data.db"),
        "IMAGES_DIR": os.path.join(d, "images"),
        "CACHE_DIR": os.path.join(d, "cache"),
        "TWITTER_COOKIES_FILE": os.path.join(d, "c.txt"),
        "ACCOUNTS_JSON_PATH": os.path.join(d, "a.json"),
        "TWSCRAPE_DB": os.path.join(d, "pool.db"),
        "IG_DB_PATH": "", "IG_CACHE_DIR": "",
    })
    code = f'''
import sys, os, json, sqlite3
from datetime import datetime, timedelta, timezone
sys.path.insert(0, {str(APP)!r})
import db
db.init_db()
c = db.get_conn()
now = datetime.now(timezone.utc)
c.execute("INSERT OR REPLACE INTO accounts (screen_name, display_name, categories)"
          " VALUES ('alice','Alice','[\\"illustrator\\"]')")
fetched = (now - timedelta(hours={last_fetch_hours if last_fetch_hours is not None else 0})).isoformat()
c.execute("INSERT OR REPLACE INTO tweets (tweet_id, screen_name, content, created_at,"
          " url, media_json, local_media_json, fetched_at)"
          " VALUES ('t1','alice','x',?,?,'[]','[]',?)",
          (fetched, "https://x.com/alice/status/t1", fetched))
if {last_ok_hours is not None!r}:
    c.execute("INSERT INTO scrape_log (run_at, screen_name, status, message, new_tweets)"
              " VALUES (?,?,'ok','',1)",
              ((now - timedelta(hours={last_ok_hours if last_ok_hours is not None else 0})).isoformat(), "alice"))
if {bool(errors)!r}:
    for _ in range(3):
        c.execute("INSERT INTO scrape_log (run_at, screen_name, status, message, new_tweets)"
                  " VALUES (?,?,'error','boom',0)", (now.isoformat(), "alice"))
c.commit(); c.close()

import web
h = web.app.test_client().get("/").get_data(as_text=True)
print("STALE", "取得できていません" in h)
print("HEALTHY", "正常稼働中" in h)
print("LOGGONE", "実行ログが残っていないほど" in h)
'''
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         env=env, text=True, timeout=180)
    return out.stdout + out.stderr


print("== 正常なとき ==")
o = build(last_ok_hours=0.2, last_fetch_hours=0.2)
check("警告を出さない", "STALE False" in o, o.strip()[-200:])
check("正常稼働中と出る", "HEALTHY True" in o, o.strip()[-200:])

print("\n== しきい値を超えたとき ==")
o = build(last_ok_hours=12, last_fetch_hours=12)
check("警告を出す", "STALE True" in o, o.strip()[-200:])
# ここが本題。止まっているのに緑が出るのが、気づけなかった原因そのもの。
check("「正常稼働中」を同時に出さない", "HEALTHY False" in o, o.strip()[-200:])

print("\n== 成功ログが刈られていても気づけるか ==")
# scrape_log は7日で刈られる。7日以上止まると成功ログごと消えて
# 「何も無い＝正常」に見える。一番警告が要る場面で黙る穴。
o = build(last_ok_hours=None, last_fetch_hours=24 * 10)
check("成功ログが無くても警告を出す", "STALE True" in o, o.strip()[-200:])
check("ログが無いことを画面で断る", "LOGGONE True" in o, o.strip()[-200:])
check("この場合も正常稼働中と言わない", "HEALTHY False" in o, o.strip()[-200:])

print("\n== エラーが出ているときは従来の警告も残る ==")
o = build(last_ok_hours=0.2, last_fetch_hours=0.2, errors=True)
check("止まってはいないので停止警告は出さない", "STALE False" in o, o.strip()[-200:])

print("\n== 入れたばかりのとき ==")
# 1件も取ったことがない状態で警告を出すと、初回起動が常に赤くなる
o = build(last_ok_hours=None, last_fetch_hours=None)
_empty = subprocess.run(
    [sys.executable, "-c", f'''
import sys, os, tempfile
d = tempfile.mkdtemp()
os.environ.update({{"DB_PATH": d + "/data.db", "IMAGES_DIR": d + "/i",
  "CACHE_DIR": d + "/c", "TWITTER_COOKIES_FILE": d + "/ck.txt",
  "ACCOUNTS_JSON_PATH": d + "/a.json", "TWSCRAPE_DB": d + "/p.db",
  "IG_DB_PATH": "", "IG_CACHE_DIR": ""}})
sys.path.insert(0, {str(APP)!r})
import db; db.init_db()
import web
h = web.app.test_client().get("/").get_data(as_text=True)
print("STALE", "取得できていません" in h)
'''], capture_output=True, text=True, timeout=180).stdout
check("1件も取っていなければ警告を出さない", "STALE False" in _empty,
      _empty.strip()[-200:])

print("\n== 無効化できるか ==")
env = dict(os.environ)
env["SCRAPE_STALE_HOURS"] = "0"
d = os.path.join(BASE, "off")
os.makedirs(d, exist_ok=True)
env.update({"DB_PATH": os.path.join(d, "data.db"),
            "IMAGES_DIR": os.path.join(d, "i"), "CACHE_DIR": os.path.join(d, "c"),
            "TWITTER_COOKIES_FILE": os.path.join(d, "ck.txt"),
            "ACCOUNTS_JSON_PATH": os.path.join(d, "a.json"),
            "TWSCRAPE_DB": os.path.join(d, "p.db"),
            "IG_DB_PATH": "", "IG_CACHE_DIR": ""})
out = subprocess.run([sys.executable, "-c", f'''
import sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, {str(APP)!r})
import db
db.init_db()
c = db.get_conn()
old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
c.execute("INSERT OR REPLACE INTO accounts (screen_name, display_name, categories)"
          " VALUES ('alice','Alice','[]')")
c.execute("INSERT OR REPLACE INTO tweets (tweet_id, screen_name, content, created_at,"
          " url, media_json, local_media_json, fetched_at)"
          " VALUES ('t1','alice','x',?,?,'[]','[]',?)", (old, "u", old))
c.commit(); c.close()
import web
print("LIMIT", web.health.STALE_HOURS)   # 設定の持ち主は health
print("STALE", "取得できていません" in web.app.test_client().get("/").get_data(as_text=True))
'''], capture_output=True, env=env, text=True, timeout=180).stdout
check("SCRAPE_STALE_HOURS=0 で無効になる",
      "LIMIT 0" in out and "STALE False" in out, out.strip()[-200:])

shutil.rmtree(BASE, ignore_errors=True)
print("\n" + ("=== 全て通過 ===" if not fails
             else f"=== 失敗 {len(fails)}件: {fails} ==="))
sys.exit(1 if fails else 0)
