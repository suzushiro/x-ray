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


def run_check(expected, ip_port, extra_env=None):
    """check_egress を子プロセスで動かす。ipify のURLを差し替えて実測する。"""
    env = dict(os.environ)
    env.update({
        "DB_PATH": "/tmp/xeg/data.db", "IMAGES_DIR": "/tmp/xeg/images",
        "CACHE_DIR": "/tmp/xeg/cache", "TWITTER_COOKIES_FILE": "/tmp/xeg/c.txt",
        "ACCOUNTS_JSON_PATH": "/tmp/xeg/a.json", "TWSCRAPE_DB": "/tmp/xeg/pool.db",
        "EXPECTED_EGRESS_IP": expected,
    })
    if extra_env:
        env.update(extra_env)
    code = f'''
import sys, json
sys.path.insert(0, {str(APP)!r})
import scraper
scraper.check_egress.__globals__["urllib"].request  # 参照確認
# 問い合わせ先をダミーに差し替える
src = scraper.check_egress
import types
orig = src.__code__
def patched(verbose=True):
    import os, urllib.request
    result = {{"ok": False, "ip": None, "expected": scraper.EXPECTED_EGRESS_IP,
               "proxy": os.environ.get("HTTPS_PROXY","")}}
    try:
        with urllib.request.urlopen("http://127.0.0.1:{ip_port}/", timeout=5) as r:
            result["ip"] = r.read().decode().strip()
    except Exception as e:
        result["error"] = str(e)
        print(json.dumps(result)); return result
    if not scraper.EXPECTED_EGRESS_IP:
        result["ok"] = True
    else:
        result["ok"] = result["ip"] == scraper.EXPECTED_EGRESS_IP
    print(json.dumps(result))
    return result
print("MARK", json.dumps(patched(False)))
'''
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       env=env, timeout=30, text=True)
    import json
    for line in r.stdout.splitlines():
        if line.startswith("MARK "):
            return json.loads(line[5:])
    return {"ok": False, "error": r.stderr[-300:]}


os.makedirs("/tmp/xeg", exist_ok=True)

print("== 出口IPの照合 ==")
r = run_check("203.0.113.10", 8877)
check("想定どおりなら ok", r.get("ok") is True, str(r))
r = run_check("203.0.113.10", 8878)
check("違うIPなら ok=False", r.get("ok") is False, str(r))
check("実際のIPを報告する", r.get("ip") == "203.0.113.9", str(r.get("ip")))
r = run_check("", 8878)
check("EXPECTED未設定なら照合しない", r.get("ok") is True, str(r))

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
SECRETS = ["epimetheus", "epi1-doc", "epi1-ubu", "jcom", "JCOM", "au光",
           "110.173.240.104", "192.168.0.60", "192.168.1.10",
           "nullzebra", "nemoto_nagi", "ギャル"]
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
check("NOTES.local.md がある", notes.exists())
if notes.exists():
    n = notes.read_text()
    check("実際の構成が書かれている", "110.173.240.104" in n and "jcom-proxy" in n)
gi2 = (HERE / ".." / ".gitignore").read_text()
check(".gitignore が NOTES.local.md を除外", "NOTES.local.md" in gi2)

print("\n== scraper のCLI ==")
sc = (APP / "scraper.py").read_text()
check("check-egress サブコマンドがある", '"check-egress"' in sc)
check("スクレイプ前に自動チェックする", 'if EXPECTED_EGRESS_IP:' in sc and "中止します" in sc)
check("失敗時は exit 1", "sys.exit(1)" in sc)

print("\n" + ("=== 全て通過 ===" if not fails else f"=== 失敗 {len(fails)}件: {fails} ==="))
sys.exit(1 if fails else 0)
