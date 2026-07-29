"""検証専用: cookie_harvester のブラウザ非依存部分と、web への POST 疎通を確認する。"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / ".." / "app"))
sys.path.insert(0, str(HERE / ".." / "tools"))

import cookie_harvester as ch

fails = []


def check(label, cond, detail=""):
    print(f"{'OK ' if cond else 'NG '} {label}{('  ' + detail) if detail else ''}")
    if not cond:
        fails.append(label)


print("== フォーマット ==")
text = ch.build_cookies_text({"u1": "auth_token=aaa; ct0=bbb",
                              "u2": "auth_token=ccc; ct0=ddd"})
check("先頭がコメント行", text.splitlines()[0].startswith("#"))
check("タブ区切り", "\t" in text.splitlines()[1])
check("scraper のパースを通る",
      all(len(l.split("\t", 1)) == 2
          for l in text.splitlines() if l.strip() and not l.startswith("#")))
check("web の検証を通る",
      all("\t" in l and "auth_token" in l and "ct0" in l
          for l in text.splitlines() if l.strip() and not l.startswith("#")))

print("\n== 読み書きの往復 ==")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "cookies.txt"
    p.write_text(text, encoding="utf-8")
    back = ch.read_cookies_file(p)
    check("往復で一致", back == {"u1": "auth_token=aaa; ct0=bbb",
                                 "u2": "auth_token=ccc; ct0=ddd"}, str(back))

    p.write_text("# comment\n\nbroken_line_no_tab\nu9\tauth_token=x; ct0=y\n", encoding="utf-8")
    check("壊れた行を無視", ch.read_cookies_file(p) == {"u9": "auth_token=x; ct0=y"})

print("\n== マージ（部分失敗で既存を消さない）==")
prev = {f"u{i}": f"auth_token=old{i}; ct0=old{i}" for i in range(1, 8)}
new = {"u1": "auth_token=NEW1; ct0=NEW1", "u3": "auth_token=NEW3; ct0=NEW3"}
merged, kept = ch.merge_entries(prev, new)
check("7垢すべて残る", len(merged) == 7, f"{len(merged)}垢")
check("取得分は更新される", merged["u1"] == "auth_token=NEW1; ct0=NEW1")
check("未取得分は前回値を維持", merged["u5"] == "auth_token=old5; ct0=old5")
check("維持リストが正しい", set(kept) == {"u2", "u4", "u5", "u6", "u7"}, str(sorted(kept)))

print("\n== アカウント読み込み ==")
with tempfile.TemporaryDirectory() as d:
    a = Path(d) / "accounts.txt"
    a.write_text("# コメント\n\n@user1\nuser2\n", encoding="utf-8")
    check("@ を剥がす / 空行コメント無視", ch.load_accounts(a) == ["user1", "user2"])

    c = Path(d) / "cookies.txt"
    c.write_text("fallback_user\tauth_token=x; ct0=y\n", encoding="utf-8")
    check("accounts.txt 不在なら cookies.txt から",
          ch.load_accounts(Path(d) / "nope.txt", c) == ["fallback_user"])

print("\n== 期限表示 ==")
now = time.time()
check("セッションcookie", ch.fmt_expiry(-1) == "セッション")
check("未来の期限", ch.fmt_expiry(now + 86400 * 30).startswith("あと2"),
      ch.fmt_expiry(now + 86400 * 30))
check("過去の期限", ch.fmt_expiry(now - 86400) == "期限切れ")

print("\n== 実際の web に POST（/api/cookies/update 疎通）==")
os.environ.setdefault("DB_PATH", "/tmp/xharvest/data.db")
os.environ.setdefault("IMAGES_DIR", "/tmp/xharvest/images")
os.environ.setdefault("CACHE_DIR", "/tmp/xharvest/cache")
cookies_path = "/tmp/xharvest/cookies.txt"
os.environ["TWITTER_COOKIES_FILE"] = cookies_path
os.environ.setdefault("ACCOUNTS_JSON_PATH", "/tmp/xharvest/accounts.json")
os.makedirs("/tmp/xharvest", exist_ok=True)

import db
db.init_db()
from web import app
from werkzeug.serving import make_server

srv = make_server("127.0.0.1", 5199, app)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(1)

ok, msg = ch.push_cookies("http://127.0.0.1:5199", text)
check("POST 成功", ok, msg)
check("cookies.txt が実際に書かれた", Path(cookies_path).exists())
if Path(cookies_path).exists():
    written = ch.read_cookies_file(cookies_path)
    check("中身が往復して一致", written == {"u1": "auth_token=aaa; ct0=bbb",
                                            "u2": "auth_token=ccc; ct0=ddd"}, str(written))

ok2, msg2 = ch.push_cookies("http://127.0.0.1:5199", "# コメントだけ\n")
check("不正な中身は拒否される", not ok2, msg2)

ok3, msg3 = ch.push_cookies("http://127.0.0.1:59999", text)
check("接続不可を握り潰さない", not ok3, msg3[:60])
srv.shutdown()

print("\n== CLI ==")
import io
import contextlib
with tempfile.TemporaryDirectory() as d:
    a = Path(d) / "accounts.txt"
    a.write_text("solo_user\n", encoding="utf-8")
    has_pw, _ = ch.playwright_available()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ch.main(["--check", "--accounts", str(a), "--profiles", str(Path(d) / "prof")])
    out = buf.getvalue()
    if has_pw:
        check("--check は書き込まない", rc in (0, 1), f"rc={rc}")
    else:
        check("playwright 未導入なら即 rc=3 で打ち切る", rc == 3, f"rc={rc}")
        check("案内は1回だけ", out.lower().count("playwright install") == 1, out.strip()[:80])

    empty = Path(d) / "none.txt"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ch.main(["--accounts", str(empty)])
    check("アカウント0件は rc=2 (playwright有時)" if has_pw else "playwright無なら先に rc=3",
          rc == (2 if has_pw else 3), f"rc={rc}")

print("\n" + ("=== 全て通過 ===" if not fails else f"=== 失敗 {len(fails)}件: {fails} ==="))
sys.exit(1 if fails else 0)
