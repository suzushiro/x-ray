"""検証専用: Tumblr直接投稿（OAuth1）。モックAPIサーバーに対して実HTTPで確認する。"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / ".." / "app"))

BASE = "/tmp/xtumblr"
os.environ.update({
    "DB_PATH": f"{BASE}/data.db", "IMAGES_DIR": f"{BASE}/images",
    "CACHE_DIR": f"{BASE}/cache", "TWITTER_COOKIES_FILE": f"{BASE}/c.txt",
    "ACCOUNTS_JSON_PATH": f"{BASE}/a.json", "PERSIST_CATEGORIES": "illustrator",
    "TUMBLR_ACCOUNTS_FILE": f"{BASE}/tumblr_accounts.json",
    "TUMBLR_CONSUMER_KEY": "ck_test", "TUMBLR_CONSUMER_SECRET": "cs_test",
})
for d in (BASE, f"{BASE}/images", f"{BASE}/cache"):
    os.makedirs(d, exist_ok=True)

fails = []


def check(label, cond, detail=""):
    print(f"{'OK ' if cond else 'NG '} {label}{('  ' + detail) if detail else ''}")
    if not cond:
        fails.append(label)


# ---- モックTumblr API -----------------------------------------------------
CALLS = []


class MockAPI(BaseHTTPRequestHandler):
    def _json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.endswith("/user/info"):
            CALLS.append({"path": self.path, "auth": self.headers.get("Authorization", ""),
                          "ua": self.headers.get("User-Agent", "")})
            return self._json(200, {"meta": {"status": 200},
                                    "response": {"user": {"blogs": [{"name": "mainblog"},
                                                                    {"name": "other"}]}}})
        self._json(404, {"meta": {"status": 404}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        rec = {"path": self.path, "auth": self.headers.get("Authorization", ""),
               "ua": self.headers.get("User-Agent", ""),
               "ctype": self.headers.get("Content-Type", ""), "body": body}
        CALLS.append(rec)
        if "/blog/failblog/post" in self.path:
            return self._json(400, {"meta": {"status": 400, "msg": "Bad Request"},
                                    "response": {"errors": ["画像が不正です"]}})
        self._json(201, {"meta": {"status": 201},
                         "response": {"id": 12345, "id_string": "12345"}})

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 5391), MockAPI)
threading.Thread(target=srv.serve_forever, daemon=True).start()

import tumblr_client
tumblr_client.API_BASE = "http://127.0.0.1:5391/v2"

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8cfc0000003010100b5dcb0a20000000049454e44ae426082")

for n in ("a.jpg", "b.jpg"):
    with open(f"{BASE}/images/{n}", "wb") as f:
        f.write(PNG)

print("== アカウント設定の読み書き ==")
check("未設定なら is_configured=False", not tumblr_client.is_configured())

tumblr_client.save_accounts([
    {"label": "main", "blog": "mainblog", "token": "t1", "secret": "s1"},
    {"label": "sub", "blog": "subblog", "token": "t2", "secret": "s2"},
])
check("2アカウント読める", len(tumblr_client.load_accounts()) == 2)
check("設定済みになる", tumblr_client.is_configured())
check("パーミッションが600",
      oct(os.stat(os.environ["TUMBLR_ACCOUNTS_FILE"]).st_mode)[-3:] == "600")

pub = tumblr_client.public_accounts()
check("公開用にトークンを含めない",
      all("token" not in a and "secret" not in a for a in pub), str(pub))
check("labelで引ける", tumblr_client.find_account("sub")["blog"] == "subblog")
check("blog名でも引ける", tumblr_client.find_account("mainblog")["label"] == "main")
check("未指定なら先頭", tumblr_client.find_account("")["label"] == "main")
check("不明なlabelはNone", tumblr_client.find_account("nope") is None)

print("\n== 認証ヘッダ ==")
CALLS.clear()
v = tumblr_client.verify(tumblr_client.find_account("main"))
check("verify成功", v.get("ok"), str(v))
check("ブログ一覧を返す", v.get("blogs") == ["mainblog", "other"], str(v.get("blogs")))
check("OAuth1署名が付く", CALLS and CALLS[0]["auth"].startswith("OAuth "), CALLS[0]["auth"][:40])
check("HMAC-SHA1", "HMAC-SHA1" in CALLS[0]["auth"])
check("User-Agentが一定", CALLS[0]["ua"] == tumblr_client.USER_AGENT, CALLS[0]["ua"])

print("\n== 投稿 ==")
CALLS.clear()
acc = tumblr_client.find_account("main")
r = tumblr_client.create_photo_post(
    acc, [f"{BASE}/images/a.jpg", f"{BASE}/images/b.jpg"],
    caption="<a href='https://x.com/a/status/1'>@alice</a>",
    tags=["art", "test"], state="draft", source_url="https://x.com/a/status/1")
check("投稿成功", r.get("ok"), str(r))
check("post_idを返す", r.get("id") == "12345", str(r.get("id")))
body = CALLS[0]["body"]
check("multipartで送る", "multipart/form-data" in CALLS[0]["ctype"], CALLS[0]["ctype"])
check("画像2枚が data[0]/data[1]", b'name="data[0]"' in body and b'name="data[1]"' in body)
check("画像の実バイナリが乗る", PNG[:8] in body)
check("type=photo", b"photo" in body)
check("stateが渡る", b"draft" in body)
check("tagsがカンマ区切り", b"art,test" in body)
check("source_urlが渡る", b"https://x.com/a/status/1" in body)
check("正しいブログに投稿", "/blog/mainblog/post" in CALLS[0]["path"], CALLS[0]["path"])

print("\n== 異常系 ==")
r = tumblr_client.create_photo_post(acc, [])
check("画像0枚は失敗", not r.get("ok"), str(r))
r = tumblr_client.create_photo_post(acc, [f"{BASE}/images/nope.jpg"])
check("存在しない画像は失敗", not r.get("ok") and "見つかりません" in r.get("error", ""), str(r))
r = tumblr_client.create_photo_post(acc, [f"{BASE}/images/a.jpg"], state="bogus")
check("不正なstateは失敗", not r.get("ok"), str(r))
bad = {"label": "x", "blog": "failblog", "token": "t", "secret": "s"}
r = tumblr_client.create_photo_post(bad, [f"{BASE}/images/a.jpg"])
check("APIエラーを拾う", not r.get("ok") and "400" in r.get("error", ""), str(r))

print("\n== web エンドポイント ==")
import db
db.init_db()
conn = db.get_conn()
conn.execute("INSERT OR REPLACE INTO accounts (screen_name,display_name,categories)"
             " VALUES ('alice','Alice','[\"illustrator\"]')")
conn.execute("""INSERT OR REPLACE INTO tweets (tweet_id,screen_name,content,created_at,url,
             media_json,local_media_json,fetched_at)
             VALUES ('t1','alice','x','2026-01-01T00:00:00+00:00',
             'https://x.com/alice/status/t1',?,?,'x')""",
             (json.dumps(["https://p/a.jpg", "https://p/b.jpg"]),
              json.dumps(["/images/a.jpg", "/images/b.jpg"])))
conn.commit()
conn.close()

import web
web.tumblr_client.API_BASE = "http://127.0.0.1:5391/v2"
c = web.app.test_client()

r = c.get("/api/tumblr/accounts").get_json()
check("アカウント一覧API", r.get("enabled") and len(r.get("accounts", [])) == 2, str(r))
check("一覧にトークンが無い",
      all("token" not in a for a in r.get("accounts", [])), str(r.get("accounts")))

CALLS.clear()
r = c.post("/api/tumblr/post", data={"tweet_id": "t1", "account": "sub",
                                     "caption": "@alice", "tags": "art",
                                     "state": "private"}).get_json()
check("投稿API成功", r.get("ok"), str(r))
check("枚数を返す", r.get("count") == 2, str(r))
check("指定アカウントに投稿", "/blog/subblog/post" in CALLS[0]["path"], CALLS[0]["path"])
check("キャプションが元投稿リンクになる",
      b"https://x.com/alice/status/t1" in CALLS[0]["body"])

CALLS.clear()
r = c.post("/api/tumblr/post", data={"tweet_id": "t1", "indices": "1"}).get_json()
check("indices指定で1枚だけ", r.get("count") == 1, str(r))
check("data[1]は送られない", b'name="data[1]"' not in CALLS[0]["body"])

check("tweet_idなしは400",
      c.post("/api/tumblr/post", data={}).status_code == 400)
check("不明アカウントは400",
      c.post("/api/tumblr/post", data={"tweet_id": "t1", "account": "zzz"}).status_code == 400)
check("不正stateは400",
      c.post("/api/tumblr/post", data={"tweet_id": "t1", "state": "bogus"}).status_code == 400)
check("存在しない投稿は400",
      c.post("/api/tumblr/post", data={"tweet_id": "zzz"}).status_code == 400)

print("\n== UI ==")
html = c.get("/").get_data(as_text=True)
check("投稿先セレクトがある", 'id="tmb-account"' in html)
check("下書きトグルがある", 'id="tmb-draft"' in html)
check("投稿ボタンになっている", 'onclick="tmbPost()"' in html)
check("トグルでstateを切り替える", "checked ? 'draft' : 'published'" in html)
check("トグルでボタン文言が変わる", "tmbDraftToggled" in html and "下書きに保存" in html)

print("\n== 下書き投稿 ==")
CALLS.clear()
r = c.post("/api/tumblr/post", data={"tweet_id": "t1", "state": "draft"}).get_json()
check("draftで投稿できる", r.get("ok") and r.get("state") == "draft", str(r))
check("draftが送られる", b"draft" in CALLS[0]["body"])
CALLS.clear()
r = c.post("/api/tumblr/post", data={"tweet_id": "t1", "state": "published"}).get_json()
check("publishedで投稿できる", r.get("ok") and r.get("state") == "published", str(r))
check("ボタンが描画される", html.count('onclick="openTumblrShare') == 1)

srv.shutdown()
print("\n" + ("=== 全て通過 ===" if not fails else f"=== 失敗 {len(fails)}件: {fails} ==="))
sys.exit(1 if fails else 0)
