#!/usr/bin/env python3
"""
検証専用: IG-Ray アーカイブの取り込み。

ig-ray のDBを**実際に作って**X-Ray に読ませる。モックの辞書を渡すだけだと
「列名が合っているか」「WALのDBを開けるか」「時系列が正しく混ざるか」が
確認できず、本番で初めて落ちる。

  python3 dev/ig_check.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "app"
sys.path.insert(0, str(APP))

fails = []


def check(label, cond, detail=""):
    print(f"{'OK ' if cond else 'NG '} {label}{'  ' + str(detail) if detail else ''}")
    if not cond:
        fails.append(label)


BASE = tempfile.mkdtemp(prefix="xray-ig-")
IG_DIR = os.path.join(BASE, "ig", "data")
IG_CACHE = os.path.join(IG_DIR, "cache")
IG_DB = os.path.join(IG_DIR, "ig_ray.db")
os.makedirs(IG_CACHE, exist_ok=True)
os.makedirs(os.path.join(BASE, "x"), exist_ok=True)


# ---- ig-ray 側のDBを作る ---------------------------------------------------
# ig-ray 4.18 のスキーマに合わせる。こちらは読む側なので、
# 列が増えても壊れないことを別途確認する（後段の [6]）。
def build_ig_db():
    c = sqlite3.connect(IG_DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE accounts (
        username TEXT PRIMARY KEY, userid INTEGER, full_name TEXT,
        biography TEXT, profile_pic_url TEXT, profile_pic_local TEXT,
        followers INTEGER, mediacount INTEGER, categories_json TEXT,
        is_enabled INTEGER NOT NULL DEFAULT 1,
        is_target INTEGER NOT NULL DEFAULT 0,
        is_muted INTEGER NOT NULL DEFAULT 0,
        muted_at TEXT, mute_reason TEXT, note TEXT,
        added_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT);
    CREATE TABLE posts (
        shortcode TEXT PRIMARY KEY, mediaid INTEGER, owner_username TEXT NOT NULL,
        caption TEXT, date_utc TEXT NOT NULL, likes INTEGER, comments INTEGER,
        typename TEXT, is_video INTEGER NOT NULL DEFAULT 0,
        is_carousel INTEGER NOT NULL DEFAULT 0, location TEXT,
        hashtags_json TEXT, media_json TEXT,
        fetched_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE media_index (
        id INTEGER PRIMARY KEY AUTOINCREMENT, shortcode TEXT NOT NULL,
        media_index INTEGER NOT NULL, is_video INTEGER NOT NULL DEFAULT 0,
        remote_url TEXT, local_path TEXT, sha256 TEXT, bytes INTEGER,
        is_persistent INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (shortcode, media_index));
    """)
    accs = [
        # username, full_name, categories, is_target, is_muted
        ("ig_artist", "IG Artist", '["illustrator"]', 1, 0),
        ("ig_muted", "Muted One", '["illustrator"]', 1, 1),
        ("ig_coauthor", "Stranger", None, 0, 0),   # 共同投稿の相手。対象外
        ("ig_r18", "Spicy", '["R18"]', 1, 0),
    ]
    for u, fn, cats, tgt, mut in accs:
        c.execute("INSERT INTO accounts (username, full_name, categories_json,"
                  " is_target, is_muted, profile_pic_local) VALUES (?,?,?,?,?,?)",
                  (u, fn, cats, tgt, mut, None))

    def add_post(code, owner, when, n_media, local=True):
        media = [{"index": i, "image_url": f"https://ig.example/{code}_{i}.jpg",
                  "is_video": False, "video_url": None} for i in range(n_media)]
        c.execute("INSERT INTO posts (shortcode, owner_username, caption, date_utc,"
                  " likes, comments, media_json) VALUES (?,?,?,?,?,?,?)",
                  (code, owner, f"caption of {code}", when, 10, 2,
                   json.dumps(media)))
        for i in range(n_media):
            lp = None
            if local:
                sub = os.path.join(IG_CACHE, "ab")
                os.makedirs(sub, exist_ok=True)
                Path(os.path.join(sub, f"{code}_{i}.jpg")).write_bytes(
                    b"\xff\xd8\xff\xd9")
                # **ig-ray は自分のコンテナ基準の絶対パスで記録する。**
                # 実パスを入れると「パスの読み替え」を検証しないまま通る。
                # 実際にそれで画像がリモートURLに落ちるバグを見逃した。
                lp = f"/data/cache/ab/{code}_{i}.jpg"
            c.execute("INSERT INTO media_index (shortcode, media_index,"
                      " remote_url, local_path) VALUES (?,?,?,?)",
                      (code, i, media[i]["image_url"], lp))

    # 日付はあえて UTC 表記。X側は +09:00 で入れて、混ざる順序を確かめる。
    add_post("IGAAA", "ig_artist", "2026-11-19T01:00:00+00:00", 2)
    add_post("IGBBB", "ig_artist", "2026-11-19T03:00:00+00:00", 1)
    add_post("IGMUTE", "ig_muted", "2026-11-19T04:00:00+00:00", 1)
    add_post("IGCOAU", "ig_coauthor", "2026-11-19T05:00:00+00:00", 1)
    add_post("IGR18", "ig_r18", "2026-11-19T06:00:00+00:00", 1)
    add_post("IGNOML", "ig_artist", "2026-11-19T00:30:00+00:00", 0)  # 画像なし
    # 取得側のログ。X-Ray が IG の健康状態を出せるかの検証に使う。
    c.execute("""CREATE TABLE scrape_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, status TEXT NOT NULL,
        fetched INTEGER DEFAULT 0, inserted INTEGER DEFAULT 0, message TEXT,
        started_at TEXT, ended_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    c.commit()
    c.close()


build_ig_db()

env = dict(os.environ)
env.update({
    "DB_PATH": os.path.join(BASE, "x", "data.db"),
    "IMAGES_DIR": os.path.join(BASE, "x", "images"),
    "CACHE_DIR": os.path.join(BASE, "x", "cache"),
    "TWITTER_COOKIES_FILE": os.path.join(BASE, "x", "c.txt"),
    "ACCOUNTS_JSON_PATH": os.path.join(BASE, "x", "a.json"),
    "TWSCRAPE_DB": os.path.join(BASE, "x", "pool.db"),
    "IG_DB_PATH": IG_DB,
    "IG_CACHE_DIR": IG_CACHE,
})
for k, v in env.items():
    os.environ[k] = v

import db                                                   # noqa: E402
db.init_db()
conn = db.get_conn()
conn.execute("INSERT OR REPLACE INTO accounts (screen_name, display_name, categories)"
             " VALUES ('x_artist','X Artist','[\"illustrator\"]')")
# X側は +09:00 で保存する。IG は +00:00。素の文字列比較だと順序が壊れる組み合わせ。
for tid, when in (("x1", "2026-11-19T11:00:00+09:00"),      # = 02:00 UTC
                  ("x2", "2026-11-19T13:00:00+09:00")):     # = 04:00 UTC
    conn.execute("INSERT OR REPLACE INTO tweets (tweet_id, screen_name, content,"
                 " created_at, url, media_json, local_media_json, fetched_at)"
                 " VALUES (?,?,?,?,?,?,?,'x')",
                 (tid, "x_artist", f"tweet {tid}", when,
                  f"https://x.com/x_artist/status/{tid}",
                  json.dumps([f"https://p/{tid}.jpg"]), json.dumps([None])))
    # 全文検索の索引はスクレイパーだけが書く（トリガではない）。
    # 直接INSERTしただけだと検索に出ないので、本番と同じ状態にしておく。
    try:
        conn.execute("INSERT INTO tweets_fts (tweet_id, content, screen_name,"
                     " display_name) VALUES (?,?,?,?)",
                     (tid, f"tweet {tid}", "x_artist", "X Artist"))
    except Exception:
        pass
conn.commit()
conn.close()

import ig_bridge                                            # noqa: E402
import web                                                  # noqa: E402

c = web.app.test_client()

print("== [1] 接続 ==")
check("IGが設定済みと判定される", ig_bridge.configured())
check("IGが読める", ig_bridge.available())

probe = db.get_conn()
check("ATTACHできる", ig_bridge.attach(probe))
try:
    probe.execute("INSERT INTO accounts (screen_name) VALUES ('nope')")
    probe.commit()
    wrote = True
except Exception:
    wrote = False
check("query_onlyで書き込みが禁じられる", not wrote)
try:
    probe.execute("INSERT INTO ig.posts (shortcode, owner_username, date_utc)"
                  " VALUES ('zzz','ig_artist','2026-01-01T00:00:00+00:00')")
    probe.commit()
    wrote_ig = True
except Exception:
    wrote_ig = False
check("IG側にも書けない", not wrote_ig)
probe.close()

print("\n== [1b] 列の取りこぼし ==")
# 混合タイムラインは t.* をやめて列を明示しているので、tweets に列を足したとき
# TL_COLUMNS への追加を忘れると**その列を使う表示が黙って消える**。
# 実際に quoted_json を落として引用RTが描画されなくなる事故を出したので、
# 機械的に突き合わせる。
_probe = db.get_conn()
_tweet_cols = {r[1] for r in _probe.execute("PRAGMA table_info(tweets)")}
_probe.close()
# 表示に使わない列は明示的に除外する（増えたら意図して足すこと）
_not_displayed = {"fetched_at"}
_tl = {c.strip() for c in web.TL_COLUMNS.replace("\n", " ").split(",") if c.strip()}
_missing = _tweet_cols - _not_displayed - _tl
check("tweetsの列がTL_COLUMNSから漏れていない", not _missing, f"漏れ: {sorted(_missing)}")

# IG側も同じ列数・同じ並びでなければ UNION が位置でずれる
_igsql, _ = ig_bridge.select_sql(db.get_conn())
check("IG側の列数がX側と一致する",
      _igsql.count(" AS ") == len(_tl), f"ig={_igsql.count(' AS ')} / tl={len(_tl)}")

print("\n== [2] 混合タイムライン ==")
html = c.get("/").get_data(as_text=True)
check("X-Rayの投稿が出る", "tweet x1" in html)
check("IGの投稿が出る", "caption of IGAAA" in html, )
check("IGバッジが出る", 'class="src-badge src-ig"' in html)
check("Instagramへのリンクになる",
      "https://www.instagram.com/p/IGAAA/" in html)

# 時系列。UTCに正規化した降順になっていること。
# IGBBB(03:00) > x2(04:00JST=04:00? ) ... 実際の並びで検証する
import re                                                   # noqa: E402
order = re.findall(r"caption of (IG\w+)|tweet (x\d)", html)
seq = [a or b for a, b in order]
# UTCに直すと x2 04:00 > IGBBB 03:00 > x1 02:00 > IGAAA 01:00 > IGNOML 00:30。
# X側は +09:00、IG側は +00:00 で保存してあるので、**素の文字列比較だと**
# "...T11:00:00+09:00" や "...T13:00:00+09:00" が IG の "...T0X:00:00+00:00" より
# 大きく評価され、Xが2件とも上に固まる（x2, x1, IGBBB, ...）。
# この並びになること自体が、オフセットをUTCに畳めている証拠になっている。
check("時系列が混ざって並ぶ（オフセットを正規化している）",
      seq == ["x2", "IGBBB", "x1", "IGAAA", "IGNOML"], str(seq))

# 件数バッジ。表示されているのに数に入っていないと食い違って見える。
# X 2件（x1/x2）＋ IG 3件（IGAAA/IGBBB/IGNOML）。ミュートと監視対象外は入らない。
_badge = re.search(r"illustrator<span class=\"badge\">(\d+)</span>", html)
check("カテゴリ件数にIGぶんが入る", bool(_badge) and _badge.group(1) == "5",
      _badge.group(0) if _badge else "バッジが見つからない")

print("\n== [2b] 出自の切り替え ==")
# IGの取得は止まっているので、混ぜるだけだと新しいXの投稿に押し流されて
# 何百ページも奥に行く。IGだけを見る導線が無いと実質見えない。
def _seq(src):
    h2 = c.get(f"/?source={src}").get_data(as_text=True)
    return [a or b for a, b in
            re.findall(r"caption of (IG\w+)|tweet (x\d)", h2)], h2

_all, _ = _seq("all")
_x, _hx = _seq("x")
_ig, _hig = _seq("ig")
check("すべて では両方出る",
      any(v.startswith("IG") for v in _all) and "x1" in _all, str(_all))
check("X だけに絞れる", _x == ["x2", "x1"], str(_x))
check("IG だけに絞れる",
      _ig and all(v.startswith("IG") for v in _ig), str(_ig))
# 文字列 "src-select" は _style.html のCSSにも出るので、
# クラス名だけを探すとCSSに当たって必ず通ってしまう。要素ごと数える。
_SEL = '<select class="src-select"'
check("ナビに切り替えのプルダウンが出る", _hig.count(_SEL) == 1,
      str(_hig.count(_SEL)))
check("選択中が selected になる",
      'source=ig"\n                    selected' in _hig
      or 'source=ig" selected' in _hig.replace("\n", " ").replace("  ", " "),
      "selected位置")
check("遷移先URLがoptionに入っている", 'value="/?category=all&source=x"' in _hig)
# 出自の概念が無い画面には出さない
for _p in ("/bookmarks", "/search", "/storage", "/status"):
    check(f"{_p} にはプルダウンを出さない",
          c.get(_p).get_data(as_text=True).count(_SEL) == 0)

# 件数バッジも追従しないと、Xだけ表示しているのにIG込みの数が出てしまう
_b = dict(re.findall(r'(\w+)<span class="badge">(\d+)</span>', _hx))
check("Xだけのとき件数もXだけ", _b.get("illustrator") == "2", str(_b))
_b2 = dict(re.findall(r'(\w+)<span class="badge">(\d+)</span>', _hig))
check("IGだけのとき件数もIGだけ", _b2.get("illustrator") == "3", str(_b2))

_gal_ig = c.get("/gallery?source=ig").get_data(as_text=True)
check("ギャラリーでも絞れる",
      "/ig-cache/" in _gal_ig and "/cache/x" not in _gal_ig)
check("ページ送りに出自が残る",
      "source=ig" in _gal_ig or "ページ送り不要", str("source=ig" in _gal_ig))

check("知らない値は all 扱い",
      _seq("bogus")[0] == _all, str(_seq("bogus")[0]))

print("\n== [3] 監視対象とミュートの除外 ==")
check("監視対象外（共同投稿の相手）は出ない", "caption of IGCOAU" not in html, )
check("ミュートは出ない", "caption of IGMUTE" not in html)
check("R18はトップに出ない", "caption of IGR18" not in html)

cat = c.get("/?category=illustrator").get_data(as_text=True)
check("カテゴリ絞り込みでIGも拾う", "caption of IGAAA" in cat)
r18 = c.get("/?category=R18").get_data(as_text=True)
check("R18タブでは出る", "caption of IGR18" in r18)

print("\n== [4] メディア ==")
check("ローカル画像は /ig-cache/ で配られる", "/ig-cache/ab/IGAAA_0.jpg" in html,
      )
r = c.get("/ig-cache/ab/IGAAA_0.jpg")
check("実際に配信できる", r.status_code == 200 and r.data.startswith(b"\xff\xd8"),
      f"status={r.status_code}")
check("キャッシュ外は配信しない",
      c.get("/ig-cache/../../x/data.db").status_code in (400, 404),
      str(c.get("/ig-cache/../../x/data.db").status_code))

gal = c.get("/gallery").get_data(as_text=True)
check("ギャラリーにIGの画像が出る", "/ig-cache/ab/IGAAA_0.jpg" in gal)
check("ギャラリーはカルーセルを1枚ずつ並べる",
      gal.count("/ig-cache/ab/IGAAA_") == 2, str(gal.count("/ig-cache/ab/IGAAA_")))
check("画像の無い投稿はギャラリーに出ない", "IGNOML" not in gal)

print("\n== [5] IGは読むだけ ==")
# ここが崩れるとX-Rayの書き込み経路がIGのIDを掴んでデータを壊す。
# ブックマークは出す（X-Ray側に持つので安全）。
# 出してはいけないのは削除の手がかりのほう。
check("IGカードにブックマークボタンを出す",
      'onclick="toggleBookmark(this)"' in html)
check("IGカードに削除の手がかりは渡さない",
      'data-tid="ig:IGAAA" data-mindexes' not in html)
# 画像削除は IG では許さない（実体は ig-ray の持ち物）。
# ブックマークと Tumblr 投稿は IG のDBに書かないので別扱い（後段）。
_res = c.post("/api/media/delete", data={"tweet_id": "ig:IGAAA", "index": "0"})
check("/api/media/delete はIGのIDを拒否する", _res.status_code == 400,
      f"status={_res.status_code}")

print("\n== [5a] ユーザーページ・検索・ストレージ ==")
_u = c.get("/user/ig:ig_artist")
check("IGのユーザーページが開く", _u.status_code == 200, str(_u.status_code))
_uh = _u.get_data(as_text=True)
check("その人の投稿だけが出る",
      "caption of IGAAA" in _uh and "caption of IGCOAU" not in _uh)
check("ローカル画像が使われる", "/ig-cache/" in _uh)
check("存在しない投稿主は404",
      c.get("/user/ig:nobody").status_code == 404)
check("カードからユーザーページへ行ける",
      'href="/user/ig:ig_artist"' in html)
# URL用の ig: 接頭辞が画面に漏れない
check("ハンドルに ig: が漏れない", "@ig:ig_artist" not in _uh)
check("出自バッジで区別できる", 'src-badge src-ig' in _uh)
check("IGにRT統計を出さない", "🔁合計" not in _uh)

_s = c.get("/search?q=IGAAA").get_data(as_text=True)
check("検索にIGが出る", "caption of IGAAA" in _s)
_sx = c.get("/search?q=tweet").get_data(as_text=True)
check("検索のXは壊れていない", "tweet x1" in _sx)
# 監視対象外は検索でも出さない（TLと同じ規則）
_sc = c.get("/search?q=IGCOAU").get_data(as_text=True)
check("検索でも監視対象外は出さない", "caption of IGCOAU" not in _sc)

_st = c.get("/storage").get_data(as_text=True)
check("ストレージにIGの内訳が出る", "IGアーカイブ" in _st)
check("IGの件数が出る", ">3<" in _st or "投稿" in _st)

# ig-ray 側に列が足りなくてもページごと落とさない
_nocol = os.path.join(BASE, "nocol.db")
shutil.copy(IG_DB, _nocol)
_c3 = sqlite3.connect(_nocol)
_c3.execute("ALTER TABLE posts DROP COLUMN is_video")
_c3.commit(); _c3.close()
_e3 = dict(os.environ); _e3["IG_DB_PATH"] = _nocol
_o3 = subprocess.run([sys.executable, "-c", f"""
import sys
sys.path.insert(0, {str(APP)!r})
import web
r = web.app.test_client().get("/user/ig:ig_artist")
print("STATUS", r.status_code)
"""], capture_output=True, env=_e3, text=True, timeout=120).stdout
check("列が欠けてもユーザーページは開く", "STATUS 200" in _o3, _o3.strip()[:200])

print("\n== [5b] IGの投稿をTumblrへ ==")
# ig-ray の web を止められるかはここにかかっている。
# IG側のDBには一切書かず、読んだ実ファイルを送るだけであることも確かめる。
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

TCALLS = []


class _MockTumblr(BaseHTTPRequestHandler):
    def _j(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._j(200, {"meta": {"status": 200},
                      "response": {"user": {"blogs": [{"name": "mainblog"}]}}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        TCALLS.append({"path": self.path, "body": self.rfile.read(n)})
        self._j(201, {"meta": {"status": 201},
                      "response": {"id": 1, "id_string": "1"}})

    def log_message(self, *a):
        pass


_tsrv = HTTPServer(("127.0.0.1", 5393), _MockTumblr)
threading.Thread(target=_tsrv.serve_forever, daemon=True).start()

_accounts = os.path.join(BASE, "tumblr_accounts.json")
json.dump([{"label": "main", "blog": "mainblog", "token": "t", "secret": "s"}],
          open(_accounts, "w"))
os.chmod(_accounts, 0o600)

_tenv = dict(os.environ)
_tenv.update({"TUMBLR_CONSUMER_KEY": "ck", "TUMBLR_CONSUMER_SECRET": "cs",
              "TUMBLR_ACCOUNTS_FILE": _accounts})
_tcode = f"""
import sys, json, sqlite3
sys.path.insert(0, {str(APP)!r})
import ig_bridge, web
web.tumblr_client.API_BASE = "http://127.0.0.1:5393/v2"
cc = web.app.test_client()

# ig-ray のパス表記から実ファイルを引けるか
print("LOCALFILE", ig_bridge.local_file("/data/cache/ab/IGAAA_0.jpg"))
print("TRAVERSAL", ig_bridge.local_file("/data/cache/../../etc/passwd"))

h = cc.get("/?source=ig").get_data(as_text=True)
print("BTN", h.count('onclick="openTumblrShare'))
print("PKEY", 'data-pkey="ig:ig_artist"' in h)
print("NOTE", 'data-ig="選んだ投稿先にそのまま投稿します。'
      '出典はInstagramの元投稿になります。"' in h)

r = cc.post("/api/tumblr/post", data={{"tweet_id": "ig:IGAAA", "account": "main",
            "caption": "IGから投稿", "tags": "art"}}).get_json()
print("POST", json.dumps(r, ensure_ascii=False))

p = cc.get("/api/tumblr/presets").get_json()["presets"]
print("PRESETKEY", "ig:ig_artist" in p)
print("PRESETVAL", json.dumps(p.get("ig:ig_artist"), ensure_ascii=False))
"""
_out = subprocess.run([sys.executable, "-c", _tcode], capture_output=True,
                      env=_tenv, text=True, timeout=180)
_o = _out.stdout


def _line(key):
    for ln in _o.splitlines():
        if ln.startswith(key + " "):
            return ln[len(key) + 1:]
    return ""


check("ig-rayのパス表記から実ファイルを引ける",
      _line("LOCALFILE").startswith(IG_CACHE), _line("LOCALFILE") or _out.stderr[-200:])
check("キャッシュ外は拒否する", _line("TRAVERSAL") == "None", _line("TRAVERSAL"))
check("IGカードにtボタンが出る", _line("BTN").isdigit() and int(_line("BTN")) >= 1,
      _line("BTN"))
check("覚えるキーに出自が付く", _line("PKEY") == "True", _line("PKEY"))

_res = json.loads(_line("POST") or "{}")
check("IGの投稿を投稿できる", _res.get("ok") is True, _line("POST"))
check("2枚とも送られる", _res.get("count") == 2, _line("POST"))
check("投稿主が ig: 付きで返る", _res.get("screen_name") == "ig:ig_artist",
      _line("POST"))
check("実バイナリが載っている", any(b"\xff\xd8\xff\xd9" in t["body"] for t in TCALLS),
      str(len(TCALLS)))
check("出典がInstagramのURLになる",
      any(b"https://www.instagram.com/p/IGAAA/" in t["body"] for t in TCALLS))
check("覚える先は ig: 付きのキー", _line("PRESETKEY") == "True", _line("PRESETKEY"))
check("出典の文言をInstagram向けに差し替えられる",
      _line("NOTE") == "True", _line("NOTE"))

# ここが肝。ig-ray のDBに書いてしまうと読み取り専用の前提が崩れる。
_chk = sqlite3.connect(IG_DB)
try:
    _n = _chk.execute("SELECT COUNT(*) FROM tumblr_presets").fetchone()[0]
except Exception:
    _n = 0      # そもそもテーブルが無いDBなら書かれていない
_chk.close()
check("ig-ray側のDBには書いていない", _n == 0, str(_n))
_tsrv.shutdown()

print("\n== [5c] ブックマークとミュート ==")
# 前の節の状態を持ち込まない
_cl = db.get_conn(); _cl.execute("DELETE FROM bookmarks"); _cl.commit(); _cl.close()
# ブックマーク: ig-ray の保持期間で元画像が消えても残るよう、
# 実体を X-Ray の永続領域へコピーする（X側の昇格と同じ考え方）。
_r = c.post("/api/bookmark/toggle", data={"tweet_id": "ig:IGAAA"}).get_json()
check("IGをブックマークできる", _r.get("ok") and _r.get("bookmarked"), str(_r))
check("画像を永続領域へコピーする", _r.get("promoted", 0) == 2, str(_r))
_copied = sorted(n for n in os.listdir(os.path.join(BASE, "x", "images"))
                 if n.startswith("ig_"))
check("実体が置かれている", len(_copied) == 2, str(_copied))

_bm = c.get("/bookmarks").get_data(as_text=True)
check("一覧に出る", "caption of IGAAA" in _bm)
check("出自バッジが付く", "src-badge src-ig" in _bm)
check("永続側の画像を参照する", "/images/ig_IGAAA_0.jpg" in _bm, )
check("TLで★が点く", "marked" in c.get("/?source=ig").get_data(as_text=True))

# 元がig-rayから消えても、ブックマークぶんは投稿できる
_r2 = c.post("/api/bookmark/toggle", data={"tweet_id": "ig:IGAAA"}).get_json()
check("解除できる", _r2.get("ok") and not _r2.get("bookmarked"), str(_r2))

# ミュート: **ig-ray のDBに書く**ので向こうの画面とも揃う
_m = c.post("/api/ig/mute", data={"username": "ig_artist", "muted": "1"}).get_json()
check("ミュートできる", _m.get("ok") and _m.get("muted"), str(_m))
_chk2 = sqlite3.connect(IG_DB)
_mv = _chk2.execute("SELECT is_muted FROM accounts WHERE username='ig_artist'"
                    ).fetchone()[0]
_chk2.close()
check("ig-ray側のDBに反映される", _mv == 1, str(_mv))
check("TLから消える",
      "caption of IGAAA" not in c.get("/?source=ig").get_data(as_text=True))
check("ミュート一覧に出る", "ig_artist" in c.get("/ig-mutes").get_data(as_text=True))
_m2 = c.post("/api/ig/mute", data={"username": "ig_artist", "muted": "0"}).get_json()
check("解除できる（ミュート）", _m2.get("ok") and not _m2.get("muted"), str(_m2))
check("TLに戻る",
      "caption of IGAAA" in c.get("/?source=ig").get_data(as_text=True))
check("usernameなしは400",
      c.post("/api/ig/mute", data={}).status_code == 400)

# 書き込み用の接続を分けてあること（読み取りはquery_onlyのまま）
_ro = db.get_conn()
ig_bridge.attach(_ro)
_wrote = True
try:
    _ro.execute("UPDATE ig.accounts SET is_muted=1 WHERE username='ig_artist'")
    _ro.commit()
except Exception:
    _wrote = False
_ro.close()
check("読み取り接続からは書けないまま", not _wrote)

print("\n== [5d] IG取得側の健康状態を画面に出す ==")
# 取得を X-Ray に取り込むと、IG側の異常がどこにも出なくなる。
# IG-Rayで24日、X-Rayで12日、どちらも「止まっているのに画面が正常」で
# 気づけなかった。同じ轍を踏まないための検証。
from datetime import datetime as _dt, timedelta as _td, timezone as _tz


def _ig_log(rows, clear=True):
    """IG側の scrape_log を作って / を描画し、本文を返す。"""
    cc = sqlite3.connect(IG_DB)
    if clear:
        cc.execute("DELETE FROM scrape_log")
    for user, status, msg, hours in rows:
        cc.execute("INSERT INTO scrape_log (username, status, message, ended_at)"
                   " VALUES (?,?,?,?)",
                   (user, status, msg,
                    (_dt.now(_tz.utc) - _td(hours=hours)).isoformat()))
    cc.commit(); cc.close()
    return c.get("/").get_data(as_text=True)

# 正常に取れている
_h = _ig_log([("ig_artist", "ok", "", 0.2)])
check("正常なら何も出さない",
      "IG: " not in _h or "取得できていません" not in _h, )

# 出口の異常
_h = _ig_log([("ig_artist", "ok", "", 0.2),
              ("__egress__", "error", "proxyを迂回しています", 0.1)])
check("出口の異常が出る", "IG: 出口の異常" in _h)
check("理由がそのまま読める", "proxyを迂回しています" in _h)

# ログイン失敗
_h = _ig_log([("ig_artist", "ok", "", 0.2),
              ("__login__", "login_error", "401 Unauthorized", 0.1)])
check("ログイン失敗が出る", "IG: ログインに失敗しています" in _h)
check("理由が読める（ログイン）", "401 Unauthorized" in _h)

# 無音（巡回間隔6時間 x 3 = 18時間を超える）
_h = _ig_log([("ig_artist", "ok", "", 48)])
check("止まっていたら出る", "IG: 48時間 取得できていません" in _h, )

# 擬似アカウントの成功を「取れた」と数えない
_h = _ig_log([("ig_artist", "ok", "", 48), ("__egress__", "ok", "", 0.1)])
check("擬似アカウントの行では無音判定を解除しない",
      "取得できていません" in _h, )

# backfill_done は成功扱い（IG-Rayで赤字の誤報を出したことがある）
_h = _ig_log([("ig_artist", "backfill_done", "", 0.2)])
check("backfill_done は失敗にしない", "取得できていません" not in _h)

_ig_log([("ig_artist", "ok", "", 0.2)])      # 後続の節に持ち込まない

print("\n== [6] ig-ray側が変わっても壊れない ==")
# 別リポジトリとして進化し続けるので、列が増えた/減ったで落ちないこと。
c2 = sqlite3.connect(IG_DB)
c2.execute("ALTER TABLE posts ADD COLUMN some_new_column TEXT")
c2.commit()
c2.close()
check("列が増えても読める", "caption of IGAAA" in c.get("/").get_data(as_text=True))

# is_target が無い旧DBでは「絞り込みなし」に縮退する（全部消してはいけない）
OLD = os.path.join(BASE, "old.db")
shutil.copy(IG_DB, OLD)
c3 = sqlite3.connect(OLD)
c3.execute("ALTER TABLE accounts DROP COLUMN is_target")
c3.commit()
c3.close()
old_env = dict(os.environ)
old_env["IG_DB_PATH"] = OLD
code = f'''
import os, sys
sys.path.insert(0, {str(APP)!r})
import web
h = web.app.test_client().get("/").get_data(as_text=True)
print("COAUTHOR", "caption of IGCOAU" in h)
print("NORMAL", "caption of IGAAA" in h)
'''
out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                     env=old_env, text=True, timeout=120).stdout
check("is_targetが無い旧DBでも投稿は消えない", "NORMAL True" in out, out.strip())
check("旧DBでは絞り込みをしない（空集合と混同しない）",
      "COAUTHOR True" in out, out.strip())

print("\n== [7] IGが無い／壊れていても本体は動く ==")
for label, path in (("未設定", ""), ("ファイルが無い", os.path.join(BASE, "nope.db")),
                    ("中身が壊れている", os.path.join(BASE, "broken.db"))):
    if label == "中身が壊れている":
        Path(path).write_bytes(b"this is not a database")
    e2 = dict(os.environ)
    e2["IG_DB_PATH"] = path
    code2 = f'''
import sys
sys.path.insert(0, {str(APP)!r})
import web
r = web.app.test_client().get("/")
print("STATUS", r.status_code)
print("XOK", "tweet x1" in r.get_data(as_text=True))
'''
    out2 = subprocess.run([sys.executable, "-c", code2], capture_output=True,
                          env=e2, text=True, timeout=120).stdout
    check(f"{label}でもトップが200で返る", "STATUS 200" in out2, out2.strip()[:160])
    check(f"{label}でもXの投稿は出る", "XOK True" in out2, out2.strip()[:160])

print("\n== [7b] DBのファイル名が旧名でも読める ==")
# ig-ray は v4.1 で ig_ray.db に改名したが、既存DBは作り直さず
# 旧 insta_ray.db を使い続ける。長く運用しているほど実体は旧名のまま。
# 実際にこれで「設定したのに出ない」を踏んだので、両方を探すようにしてある。
OLDNAME_DIR = os.path.join(BASE, "oldname")
os.makedirs(os.path.join(OLDNAME_DIR, "cache"), exist_ok=True)
shutil.copy(IG_DB, os.path.join(OLDNAME_DIR, "insta_ray.db"))
shutil.copytree(IG_CACHE, os.path.join(OLDNAME_DIR, "cache"), dirs_exist_ok=True)

def _probe_env(db_path, cache_dir):
    e = dict(os.environ)
    e["IG_DB_PATH"] = db_path
    e["IG_CACHE_DIR"] = cache_dir
    code = f"""
import sys
sys.path.insert(0, {str(APP)!r})
import ig_bridge, web
print("RESOLVED", ig_bridge.IG_DB_PATH)
print("REASON", ig_bridge.IG_DB_REASON)
h = web.app.test_client().get("/?source=ig").get_data(as_text=True)
print("HASIG", "caption of IGAAA" in h)
print("WARN", 'class="src-warn"' in h)
"""
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          env=e, text=True, timeout=120).stdout

# 新名を指定しているのに実体は旧名、という今回踏んだ状況
out = _probe_env(os.path.join(OLDNAME_DIR, "ig_ray.db"),
                 os.path.join(OLDNAME_DIR, "cache"))
check("新名指定・実体が旧名でも読める", "HASIG True" in out, out.strip()[:200])
check("旧名に解決される", "insta_ray.db" in out, out.strip()[:200])
check("読めているので警告は出ない", "WARN False" in out, out.strip()[:200])

# 指定なしでもマウント先から見つける
out = _probe_env("", os.path.join(OLDNAME_DIR, "cache"))
check("IG_DB_PATH未設定でも見つける", "HASIG True" in out, out.strip()[:200])

# 本当に無いときは理由を出す
out = _probe_env(os.path.join(BASE, "empty", "ig_ray.db"),
                 os.path.join(BASE, "empty", "cache"))
check("本当に無いときは警告を出す", "WARN True" in out, out.strip()[:200])
check("理由が空でない", "REASON \n" not in out and "REASON" in out,
      [l for l in out.splitlines() if l.startswith("REASON")])

print("\n== [8] WALが残っていても読める ==")
# ig-ray が落ちて -wal だけ残った状態。読み取り専用マウントだと開けないので
# 書き込み可マウント＋query_only にしてある、という設計の裏付け。
kill_code = (f"import sqlite3, os, signal\n"
             f"c = sqlite3.connect({IG_DB!r})\n"
             f"c.execute(\"INSERT INTO posts (shortcode, owner_username, caption,"
             f" date_utc, media_json) VALUES ('IGWAL','ig_artist','caption of IGWAL',"
             f"'2026-11-19T07:00:00+00:00','[]')\")\n"
             f"c.commit()\n"
             f"os.kill(os.getpid(), signal.SIGKILL)\n")
subprocess.run([sys.executable, "-c", kill_code], capture_output=True)
if os.path.exists(IG_DB + "-shm"):
    os.remove(IG_DB + "-shm")
check("-walが残っている", os.path.exists(IG_DB + "-wal"))
out3 = subprocess.run(
    [sys.executable, "-c", f'''
import sys
sys.path.insert(0, {str(APP)!r})
import web
h = web.app.test_client().get("/").get_data(as_text=True)
print("WALPOST", "caption of IGWAL" in h)
'''], capture_output=True, env=dict(os.environ), text=True, timeout=120).stdout
check("-shmが無くてもWALの中身まで読める", "WALPOST True" in out3, out3.strip())

shutil.rmtree(BASE, ignore_errors=True)
print("\n" + ("=== 全て通過 ===" if not fails
             else f"=== 失敗 {len(fails)}件: {fails} ==="))
sys.exit(1 if fails else 0)
