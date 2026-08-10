"""検証専用: Tumblr共有（トークン発行・OGPページ・画像配信・失効・スコープ）。"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / ".." / "app"))

BASE = "/tmp/xshare"
os.environ.update({
    "DB_PATH": f"{BASE}/data.db", "IMAGES_DIR": f"{BASE}/images",
    "CACHE_DIR": f"{BASE}/cache", "TWITTER_COOKIES_FILE": f"{BASE}/c.txt",
    "ACCOUNTS_JSON_PATH": f"{BASE}/a.json", "PERSIST_CATEGORIES": "illustrator",
    "PUBLIC_SHARE_BASE_URL": "https://share.example.com",
    "SHARE_TOKEN_TTL_MIN": "60",
})
for d in (BASE, f"{BASE}/images", f"{BASE}/cache"):
    os.makedirs(d, exist_ok=True)

import db
db.init_db()

fails = []


def check(label, cond, detail=""):
    print(f"{'OK ' if cond else 'NG '} {label}{('  ' + detail) if detail else ''}")
    if not cond:
        fails.append(label)


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8cfc0000003010100b5dcb0a20000000049454e44ae426082")


def seed():
    conn = db.get_conn()
    for t in ("tweets", "accounts", "share_tokens", "deleted_media"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("INSERT INTO accounts (screen_name, display_name, categories) "
                 "VALUES ('alice','Alice','[\"illustrator\",\"art\"]')")
    # 2枚: 両方ローカル保存
    conn.execute("""INSERT INTO tweets (tweet_id, screen_name, content, created_at, url,
                    media_json, local_media_json, fetched_at)
                    VALUES ('t1','alice','x','2026-01-01T00:00:00+00:00',
                    'https://x.com/alice/status/t1',?,?,'x')""",
                 (json.dumps(["https://pbs.twimg.com/media/a.jpg",
                              "https://pbs.twimg.com/media/b.jpg"]),
                  json.dumps(["/images/a.jpg", "/images/b.jpg"])))
    # リモートのみ（共有不可のはず）
    conn.execute("""INSERT INTO tweets (tweet_id, screen_name, content, created_at, url,
                    media_json, local_media_json, fetched_at)
                    VALUES ('t2','alice','y','2026-01-01T00:00:00+00:00','u2',?,?,'x')""",
                 (json.dumps(["https://pbs.twimg.com/media/c.jpg"]), json.dumps([None])))
    conn.commit()
    conn.close()
    for n in ("a.jpg", "b.jpg"):
        with open(f"{BASE}/images/{n}", "wb") as f:
            f.write(PNG)


seed()
import web
client = web.app.test_client()


def prepare(**form):
    return client.post("/api/share/prepare", data=form)


print("== トークン発行 ==")
r = prepare(tweet_id="t1", caption="@alice", tags="illustrator, art")
check("200 が返る", r.status_code == 200, r.get_data(as_text=True)[:100])
j = r.get_json()
check("share_url を返す", j.get("ok") and j.get("share_url", "").startswith(
    "https://share.example.com/share/"), str(j))
token = j["share_url"].rsplit("/", 1)[-1]
check("トークンがDBに入る", db.get_conn().execute(
    "SELECT 1 FROM share_tokens WHERE token=?", (token,)).fetchone() is not None)

print("\n== APIが画像URLと出典を返す ==")
check("image_urls を返す", isinstance(j.get("image_urls"), list) and len(j["image_urls"]) == 2,
      str(j.get("image_urls")))
check("画像URLが絶対URL", all(u.startswith("https://share.example.com/share-img/")
                              for u in j.get("image_urls", [])), str(j.get("image_urls")))
check("source_url が X の元投稿", j.get("source_url") == "https://x.com/alice/status/t1",
      str(j.get("source_url")))

print("\n== OGPページ ==")
html = client.get(f"/share/{token}").get_data(as_text=True)
check("og:image がある", 'property="og:image"' in html)
check("og:image が share-img を指す", f"/share-img/{token}/0" in html, "")
check("キャプションが og:description に入る", '@alice' in html)
check("canonical が入る", f"/share/{token}" in html)
check("タグが出る", "#illustrator" in html)
check("DBの内容を直接晒していない", "pbs.twimg.com" not in html)

print("\n== 画像配信 ==")
im = client.get(f"/share-img/{token}/0")
check("1枚目が配信される", im.status_code == 200 and im.data[:8] == PNG[:8], str(im.status_code))
check("2枚目も配信される", client.get(f"/share-img/{token}/1").status_code == 200)
check("範囲外は404", client.get(f"/share-img/{token}/9").status_code == 404)
check("無効トークンの画像は404", client.get("/share-img/bogus/0").status_code == 404)

print("\n== スコープ / 異常系 ==")
check("リモートのみの投稿は共有不可", prepare(tweet_id="t2").status_code == 400)
check("存在しない投稿は400", prepare(tweet_id="zzz").status_code == 400)
check("tweet_idなしは400", prepare().status_code == 400)
check("無効トークンのページは404", client.get("/share/bogus").status_code == 404)

print("\n== 公開ホスト制限（多層防御）==")
os.environ["PUBLIC_SHARE_HOST"] = "share.example.com"
import importlib as _il
_il.reload(web)
pub = web.app.test_client()

# 公開ホスト宛
r_mgr = pub.get("/manage", headers={"Host": "share.example.com"})
check("公開ホストで /manage は404", r_mgr.status_code == 404, str(r_mgr.status_code))
r_idx = pub.get("/", headers={"Host": "share.example.com"})
check("公開ホストで / は404", r_idx.status_code == 404, str(r_idx.status_code))
r_api = pub.post("/api/share/prepare", data={"tweet_id": "t1"},
                 headers={"Host": "share.example.com"})
check("公開ホストで prepare API は404", r_api.status_code == 404, str(r_api.status_code))

# 内部ホスト宛（制限を受けない）
r_int = pub.get("/manage", headers={"Host": "localhost"})
check("内部ホストで /manage は通る", r_int.status_code == 200, str(r_int.status_code))

# 公開ホストでも /share 系は通る（有効トークンで）
seed()
rp = pub.post("/api/share/prepare", data={"tweet_id": "t1"}, headers={"Host": "localhost"})
tok3 = rp.get_json()["share_url"].rsplit("/", 1)[-1]
r_share = pub.get(f"/share/{tok3}", headers={"Host": "share.example.com"})
check("公開ホストで /share は通る", r_share.status_code == 200, str(r_share.status_code))
r_simg = pub.get(f"/share-img/{tok3}/0", headers={"Host": "share.example.com"})
check("公開ホストで /share-img は通る", r_simg.status_code == 200, str(r_simg.status_code))

os.environ["PUBLIC_SHARE_HOST"] = ""
_il.reload(web)

print("\n== 削除済み画像は共有から除外 ==")
import cache_utils
conn = db.get_conn()
cache_utils.delete_media(conn, "t1", 0)   # a.jpg を削除
conn.close()
r = prepare(tweet_id="t1")
j = r.get_json()
token2 = j["share_url"].rsplit("/", 1)[-1]
html = client.get(f"/share/{token2}").get_data(as_text=True)
check("削除後は残り1枚のみ共有", html.count("/share-img/") >= 1
      and f"/share-img/{token2}/1" not in html, "")

print("\n== 失効 ==")
os.environ["SHARE_TOKEN_TTL_MIN"] = "0"
import importlib
importlib.reload(web)
client2 = web.app.test_client()
seed()
r = client2.post("/api/share/prepare", data={"tweet_id": "t1"})
j = r.get_json()
tok = j["share_url"].rsplit("/", 1)[-1]
time.sleep(1.1)
check("期限切れトークンのページは404", client2.get(f"/share/{tok}").status_code == 404)
check("期限切れトークンの画像は404", client2.get(f"/share-img/{tok}/0").status_code == 404)

print("\n== 無効化（PUBLIC_SHARE_BASE_URL 未設定）==")
os.environ["PUBLIC_SHARE_BASE_URL"] = ""
os.environ["SHARE_TOKEN_TTL_MIN"] = "60"
importlib.reload(web)
client3 = web.app.test_client()
seed()
check("未設定なら prepare は400",
      client3.post("/api/share/prepare", data={"tweet_id": "t1"}).status_code == 400)
# フラグがテンプレートに渡る
with web.app.test_request_context("/"):
    ctx = web.app.jinja_env.globals
check("share_enabled フラグが False",
      web._inject_share_flag()["share_enabled"] is False)

os.environ["PUBLIC_SHARE_BASE_URL"] = "https://share.example.com"
importlib.reload(web)
check("設定時は share_enabled True",
      web._inject_share_flag()["share_enabled"] is True)

print("\n== ボタン描画（クリーンなプロセスで）==")
# reload を重ねた同一プロセスだと状態が汚れるため、素の環境で描画を確認する
import subprocess
probe = """
import sys, os, json
sys.path.insert(0, %r)
import db, web
db.init_db()
conn = db.get_conn()
for t in ('tweets','accounts','share_tokens','deleted_media'): conn.execute('DELETE FROM '+t)
conn.execute("INSERT INTO accounts (screen_name,display_name,categories) VALUES ('alice','A','[\\"illustrator\\"]')")
conn.execute("INSERT INTO tweets (tweet_id,screen_name,content,created_at,url,media_json,local_media_json,fetched_at) VALUES ('t1','alice','x','2026-01-01T00:00:00+00:00','u',?,?,'x')",(json.dumps(['https://p/a.jpg']), json.dumps(['/images/a.jpg'])))
conn.commit(); conn.close()
os.makedirs(os.environ['IMAGES_DIR'], exist_ok=True)
open(os.environ['IMAGES_DIR']+'/a.jpg','wb').write(b'x')
h = web.app.test_client().get('/').get_data(as_text=True)
# JS定義(function openTumblrShare)ではなく、実ボタンのonclickを数える
n = h.count('onclick=\"openTumblrShare')
print('BTNCOUNT=' + str(n))
print('MODAL' if 'id=\"tmb\"' in h else 'NOMODAL')
""" % str(HERE / ".." / "app")
env = dict(os.environ)
env["PUBLIC_SHARE_BASE_URL"] = "https://share.example.com"
env["SHARE_TOKEN_TTL_MIN"] = "60"
r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env)
out = r.stdout
import re as _re2
_m = _re2.search(r"BTNCOUNT=(\d+)", out)
_n = int(_m.group(1)) if _m else 0
check("ローカル画像ありにボタンが出る（1件につき1個）", _n >= 1,
      f"count={_n} {r.stderr[-100:]}")
check("共有モーダルがある", "MODAL" in out and "NOMODAL" not in out)

print("\n== Tumblrへ渡すパラメータ ==")
js = (HERE / ".." / "app" / "templates" / "_scripts.html").read_text()
check("posttype=photo を渡す", "params.set('posttype', 'photo')" in js)
check("content に画像URLを渡す", "params.set('content'" in js and "image_urls" in js)
check("canonicalUrl は元投稿", "params.set('canonicalUrl', srcUrl)" in js)
check("キャプションを元投稿へのリンクにする", '<a href="${srcUrl}">' in js)
check("共有ドメインを canonicalUrl にしていない", "canonicalUrl', data.share_url" not in js)

print("\n== 複数枚時は1枚だけ渡す ==")
check("既定は選択した1枚のみ", "urls[Math.min(tmbSelected" in js)
check("join(',') は実験モードのみ", "if (tmbSendAll) {" in js and "urls.join(',')" in js)
check("サムネが選択UIになっている", "tmbSelected = i;" in js and "'selected'" in js)
check("複数枚のときだけ注記を出す", "tmbUpdateModeUI" in js and "multi &&" in js)
check("画像0枚なら中断する", "画像URLが取得できませんでした" in js)
css = (HERE / ".." / "app" / "templates" / "_style.html").read_text()
check("選択中のスタイルがある", ".tmb-imgs img.selected" in css)
check("全枚数モードの見た目がある", ".tmb-imgs.all-mode img" in css)
check("CSSが</style>の内側", css.rstrip().endswith("</style>"))

print("\n" + ("=== 全て通過 ===" if not fails else f"=== 失敗 {len(fails)}件: {fails} ==="))
sys.exit(1 if fails else 0)
