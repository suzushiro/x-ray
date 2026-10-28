"""検証専用: proxy環境変数の効き方と check-egress の挙動を確認する。"""
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / ".." / "app"

fails = []


def check(label, cond, detail=""):
    print(f"{'OK ' if cond else 'NG '} {label}{('  ' + detail) if detail else ''}")
    if not cond:
        fails.append(label)


# ---- ダミーHTTPサーバ（出口IPを返すふりをする / proxyの当たりも記録する）
HITS = []


def fake_server(port, body):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(5)
    while True:
        try:
            c, _ = s.accept()
            data = c.recv(4096)
            HITS.append(data.split(b"\r\n")[0].decode("latin1"))
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                      % (len(body), body))
            c.close()
        except Exception:
            pass


threading.Thread(target=fake_server, args=(8877, b"203.0.113.10"), daemon=True).start()
threading.Thread(target=fake_server, args=(8878, b"203.0.113.9"), daemon=True).start()
time.sleep(0.5)


def run_check(expected, proxied_ip, direct_ip=None, direct_fail=False,
              proxy="http://127.0.0.1:8877"):
    """本物の check_egress を動かす。IP問い合わせだけ差し替えて判定を試す。

    以前はここで check_egress の中身を書き写していたが、それでは
    本体のロジックを一切テストできていなかったので実物を呼ぶ形にした。
    """
    env = dict(os.environ)
    env.update({
        "DB_PATH": "/tmp/xeg/data.db", "IMAGES_DIR": "/tmp/xeg/images",
        "CACHE_DIR": "/tmp/xeg/cache", "TWITTER_COOKIES_FILE": "/tmp/xeg/c.txt",
        "ACCOUNTS_JSON_PATH": "/tmp/xeg/a.json", "TWSCRAPE_DB": "/tmp/xeg/pool.db",
        "EXPECTED_EGRESS_IP": expected,
    })
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(k, None)
    if proxy:
        env["HTTPS_PROXY"] = proxy
    code = f'''
import sys, json
sys.path.insert(0, {str(APP)!r})
import scraper

def fake(use_proxy=True, timeout=15):
    if use_proxy:
        return {proxied_ip!r}, None
    if {direct_fail!r}:
        return None, "TimeoutError: dummy"
    return {direct_ip!r}, None

scraper._fetch_global_ip = fake
print("MARK", json.dumps(scraper.check_egress(False)))
'''
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       env=env, timeout=30, text=True)
    import json
    for line in r.stdout.splitlines():
        if line.startswith("MARK "):
            return json.loads(line[5:])
    return {"ok": False, "error": r.stderr[-400:]}


os.makedirs("/tmp/xeg", exist_ok=True)

print("== 出口IPの照合 ==")
r = run_check("203.0.113.10", "203.0.113.10")
check("想定どおりなら ok", r.get("ok") is True and r.get("verdict") == "match", str(r))

r = run_check("", "203.0.113.9")
check("EXPECTED未設定なら照合しない",
      r.get("ok") is True and r.get("verdict") == "unchecked", str(r))

r = run_check("203.0.113.10", None)
check("IPが取れなければ止める",
      r.get("ok") is False and r.get("verdict") == "unreachable", str(r))

print("\n== 想定と違ったときの切り分け ==")
# proxy経由と直接の出口が違う＝分離は効いている。回線のIPが変わっただけ。
r = run_check("203.0.113.10", "203.0.113.9", direct_ip="198.51.100.7")
check("proxyが効いていれば続行する",
      r.get("ok") is True and r.get("verdict") == "proxy_ip_changed", str(r))
check("実際のIPを報告する", r.get("ip") == "203.0.113.9", str(r.get("ip")))
check("直接の出口も報告する", r.get("direct_ip") == "198.51.100.7", str(r))

# proxy経由と直接が同じ＝迂回している。これは本物の漏れなので止める。
r = run_check("203.0.113.10", "198.51.100.7", direct_ip="198.51.100.7")
check("proxyを迂回していたら止める",
      r.get("ok") is False and r.get("verdict") == "proxy_bypassed", str(r))

# proxy環境変数そのものが無い＝分離されていない。
r = run_check("203.0.113.10", "198.51.100.7", direct_ip="198.51.100.7", proxy="")
check("proxy未設定で不一致なら止める",
      r.get("ok") is False and r.get("verdict") == "proxy_bypassed", str(r))

# 直接の出口が取れない＝判定できない。安全側に倒して止める。
r = run_check("203.0.113.10", "203.0.113.9", direct_fail=True)
check("判定できなければ止める",
      r.get("ok") is False and r.get("verdict") == "undetermined", str(r))

print("\n== scrape_log への記録 ==")
def run_log(verdict_case):
    """log_egress が scrape_log に残すかを実DBで確かめる。"""
    import json, shutil
    shutil.rmtree("/tmp/xeglog", ignore_errors=True)
    os.makedirs("/tmp/xeglog", exist_ok=True)
    env = dict(os.environ)
    env.update({
        "DB_PATH": "/tmp/xeglog/data.db", "IMAGES_DIR": "/tmp/xeglog/images",
        "CACHE_DIR": "/tmp/xeglog/cache", "TWITTER_COOKIES_FILE": "/tmp/xeglog/c.txt",
        "ACCOUNTS_JSON_PATH": "/tmp/xeglog/a.json", "TWSCRAPE_DB": "/tmp/xeglog/pool.db",
        "EXPECTED_EGRESS_IP": "203.0.113.10",
    })
    code = f'''
import sys, json, sqlite3
sys.path.insert(0, {str(APP)!r})
import db, scraper
db.init_db()
scraper.log_egress({verdict_case!r})
c = sqlite3.connect("/tmp/xeglog/data.db")
rows = c.execute("SELECT screen_name, status, message FROM scrape_log").fetchall()
print("MARK", json.dumps(rows))
'''
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       env=env, timeout=60, text=True)
    for line in r.stdout.splitlines():
        if line.startswith("MARK "):
            return json.loads(line[5:])
    return [["ERR", r.stderr[-400:], ""]]

rows = run_log({"verdict": "match", "ok": True, "reason": "x"})
check("正常時は書かない", rows == [], str(rows))
rows = run_log({"verdict": "proxy_ip_changed", "ok": True, "reason": "IPが変わった"})
check("続行した警告は warn で残る",
      len(rows) == 1 and rows[0][0] == "__egress__" and rows[0][1] == "warn", str(rows))
rows = run_log({"verdict": "proxy_bypassed", "ok": False, "reason": "迂回"})
check("中止は error で残る",
      len(rows) == 1 and rows[0][1] == "error", str(rows))

print("\n== Web画面に出るか ==")
wb = (APP / "web.py").read_text()
check("web.py が出口ログを引く", "egress_alert" in wb)
check("アカウント一覧から擬似名を除く",
      "screen_name != ?" in wb and "EGRESS_LOG_NAME" in wb)
idx = (APP / "templates" / "index.html").read_text()
check("index.html が専用バナーを出す", "egress_alert" in idx)
check("中止と警告を描き分ける", "health-error" in idx)
st = (APP / "templates" / "_style.html").read_text()
check("health-error のCSSがある", ".health-error" in st)

print("\n== 実際に画面に出るか（レンダリング）==")
# 文字列grepだけでは足りない。マクロの with context 漏れを一度それで見逃したので、
# 本物の Flask にダミーDBを食わせて HTML を描かせて確かめる。
def render_index(egress_row):
    """egress_row=(status, message) を scrape_log に入れて / を描画する。"""
    import json, shutil
    shutil.rmtree("/tmp/xegweb", ignore_errors=True)
    os.makedirs("/tmp/xegweb", exist_ok=True)
    env = dict(os.environ)
    env.update({
        "DB_PATH": "/tmp/xegweb/data.db", "IMAGES_DIR": "/tmp/xegweb/images",
        "CACHE_DIR": "/tmp/xegweb/cache", "TWITTER_COOKIES_FILE": "/tmp/xegweb/c.txt",
        "ACCOUNTS_JSON_PATH": "/tmp/xegweb/a.json", "TWSCRAPE_DB": "/tmp/xegweb/pool.db",
    })
    ins = ""
    if egress_row:
        ins = (f'''
import sqlite3
from datetime import datetime, timezone
c = sqlite3.connect("/tmp/xegweb/data.db")
c.execute("INSERT INTO scrape_log (run_at, screen_name, status, message, new_tweets)"
          " VALUES (?,?,?,?,0)",
          (datetime.now(timezone.utc).isoformat(), "__egress__",
           {egress_row[0]!r}, {egress_row[1]!r}))
c.commit(); c.close()
''')
    code = f'''
import sys, subprocess, os
sys.path.insert(0, {str(HERE)!r})
sys.path.insert(0, {str(APP)!r})
subprocess.run([sys.executable, {str(HERE / "seed_dummy.py")!r}],
               check=True, capture_output=True)
{ins}
from web import app
html = app.test_client().get("/").get_data(as_text=True)
print("MARKLEN", len(html))
open("/tmp/xegweb/out.html", "w").write(html)
'''
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       env=env, timeout=180, text=True)
    if "MARKLEN" not in r.stdout:
        return "ERROR: " + r.stderr[-500:]
    return open("/tmp/xegweb/out.html").read()


html = render_index(("error", "proxyを迂回しています（テスト用の文言）"))
check("中止バナーが出る", "🛑 取得停止（回線分離）" in html, html[:200])
check("理由がそのまま読める", "proxyを迂回しています（テスト用の文言）" in html)
check("強調スタイルが当たる", "health-error" in html)
check("擬似アカウント名を表に出さない", "@__egress__" not in html)

html = render_index(("warn", "回線側の出口IPが変わりました（テスト用の文言）"))
check("警告バナーが出る", "⚠️ 回線の出口IPが変わりました" in html, html[:200])
check("警告では取得停止と言わない", "🛑 取得停止（回線分離）" not in html)
check("擬似アカウント名を表に出さない（警告時）", "@__egress__" not in html)

html = render_index(None)
check("正常時はバナーを出さない",
      "取得停止（回線分離）" not in html and "回線の出口IPが変わりました" not in html)

print("\n== proxy環境変数が両ライブラリで効くか（実測）==")


def proxy_hit(lib, env):
    HITS.clear()
    e = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
        e.pop(k, None)
    e.update(env)
    if lib == "urllib":
        code = ('import urllib.request\n'
                'try: urllib.request.urlopen("https://pbs.twimg.com/media/x.jpg", timeout=3)\n'
                'except Exception: pass')
    else:
        code = ('import asyncio\n'
                'from curl_cffi.requests import AsyncSession\n'
                'async def m():\n'
                '    async with AsyncSession(impersonate="chrome", proxy=None) as s:\n'
                '        try: await s.get("https://x.com/", timeout=3)\n'
                '        except Exception: pass\n'
                'asyncio.run(m())')
    subprocess.run([sys.executable, "-c", code], capture_output=True, env=e, timeout=30)
    time.sleep(0.3)
    return list(HITS)


P = "http://127.0.0.1:8877"
h = proxy_hit("urllib", {"HTTPS_PROXY": P})
check("urllib（画像DL）が大文字を見る", any("CONNECT" in x for x in h), str(h[:1]))
h = proxy_hit("urllib", {"https_proxy": P})
check("urllib が小文字も見る", any("CONNECT" in x for x in h), str(h[:1]))
h = proxy_hit("curl", {"HTTPS_PROXY": P})
check("curl-cffi（twscrape）が大文字を見る", any("CONNECT" in x for x in h), str(h[:1]))
h = proxy_hit("curl", {"https_proxy": P})
check("curl-cffi が小文字も見る", any("CONNECT" in x for x in h), str(h[:1]))
h = proxy_hit("curl", {"HTTPS_PROXY": P, "NO_PROXY": "x.com"})
check("NO_PROXY で除外される", not any("CONNECT" in x for x in h), str(h[:1]))
h = proxy_hit("curl", {})
check("proxy未設定なら素通し（=漏れる）", not h, str(h[:1]))

print("\n== compose.yml（.env 参照になっているか）==")
import re as _re
compose = (HERE / ".." / "docker-compose.yml").read_text()
worker, _, web = compose.partition("  web:")
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "TWS_PROXY",
          "EXPECTED_EGRESS_IP"):
    check(f"worker に {k}", k in worker)
check("NO_PROXY に localhost", "localhost,127.0.0.1" in worker)
check("web にはproxyを入れない", "HTTPS_PROXY" not in web)

# 秘密の値がリポジトリに残っていないこと
tracked = ["docker-compose.yml", ".env.example", "README.md",
           "app/scraper.py", "app/web.py"]
ip_re = _re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ALLOWED = {"127.0.0.1", "0.0.0.0", "192.168.0.0"}
for f in tracked:
    fp = HERE / ".." / f
    if not fp.exists():
        continue
    found = {ip for ip in ip_re.findall(fp.read_text()) if ip not in ALLOWED}
    check(f"{f} に生IPが無い", not found, str(sorted(found)))

check("compose が ${PROXY_URL} を参照", "${PROXY_URL" in worker)
check("compose が ${EXPECTED_EGRESS_IP} を参照", "${EXPECTED_EGRESS_IP" in worker)

print("\n== .env / .gitignore ==")
gi = (HERE / ".." / ".gitignore")
check(".gitignore がある", gi.exists())
if gi.exists():
    g = gi.read_text()
    for pat in ("data/", ".env", "cookies.txt", "tools/profiles/"):
        check(f".gitignore に {pat}", pat in g)
    check(".env.example は除外しない", "!.env.example" in g)
ex = (HERE / ".." / ".env.example")
check(".env.example がある", ex.exists())
if ex.exists():
    e = ex.read_text()
    check(".env.example の値は空", not ip_re.findall(e), str(ip_re.findall(e)))
    for k in ("PROXY_URL", "EXPECTED_EGRESS_IP"):
        check(f".env.example に {k}", k in e)
check(".env 実体が同梱されていない", not (HERE / ".." / ".env").exists())

print("\n== 公開ファイルに環境固有の情報が無いか ==")
# 実ホスト名・実カテゴリ・実監視垢など、公開したくない語
# ホスト名・IP・実アカウント名など、公開したくない値。
# カテゴリ名（ギャル等）はタグ変換の設定例として公開ファイルに出てよいので含めない。
SECRETS = ["epimetheus", "epi1-doc", "epi1-ubu", "jcom", "JCOM", "au光",
           "110.173.240.104", "192.168.0.60", "192.168.1.10",
           "nullzebra", "nemoto_nagi", "suzushiro.io", "jpegmania"]
PUBLIC = ["README.md", ".env.example", "docker-compose.yml", ".gitignore",
          "app/scraper.py", "app/web.py", "app/db.py", "app/cache_utils.py",
          "tools/cookie_harvester.py", "tools/accounts.example.txt",
          "extension/common.js", "extension/options.html",
          "app/templates/share.html", "deploy/cloudflared-config.example.yml",
          "deploy/README.md", "docker-compose.yml"]
for f in PUBLIC:
    fp = HERE / ".." / f
    if not fp.exists():
        continue
    body = fp.read_text()
    hit = [w for w in SECRETS if w in body]
    check(f"{f}", not hit, str(hit))

print("\n== 作業メモ ==")
notes = HERE / ".." / "NOTES.local.md"
# NOTES.local.md は gitignore 済みで配布zipには含まれない。あるときだけ中身を見る。
if notes.exists():
    check("実際の構成が書かれている", "jcom-proxy" in notes.read_text())
else:
    print("    (NOTES.local.md は未配置。手元にだけ置く運用なので正常)")

gi2 = (HERE / ".." / ".gitignore").read_text()
check(".gitignore が NOTES.local.md を除外", "NOTES.local.md" in gi2)

print("\n== scraper のCLI ==")
sc = (APP / "scraper.py").read_text()
check("check-egress サブコマンドがある", '"check-egress"' in sc)
check("スクレイプ前に自動チェックする", 'if EXPECTED_EGRESS_IP:' in sc and "中止します" in sc)
check("失敗時は exit 1", "sys.exit(1)" in sc)
check("clear-egress-log サブコマンドがある", '"clear-egress-log"' in sc)
check("迂回時だけ止める文言", "分離が確認できないため" in sc)

print("\n" + ("=== 全て通過 ===" if not fails else f"=== 失敗 {len(fails)}件: {fails} ==="))
sys.exit(1 if fails else 0)
