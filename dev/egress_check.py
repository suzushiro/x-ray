"""検証専用: proxy環境変数の効き方と check-egress の挙動を確認する。"""
import sqlite3
import re
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / ".." / "app"

# 環境ごとの語が無くても効く一般形の検査。
# RFC で文書用に予約されたアドレス（203.0.113.x / 192.0.2.x / 198.51.100.x）、
# ループバック、全アドレス、プライベート帯は見逃す。それ以外の
# IPv4 らしき文字列が公開ファイルに出ていたら実アドレスの疑い。
# 直前が "/" や数字・ドットのものは除く。"Chrome/124.0.0.0" のような
# バージョン表記を実IPと取り違えるため（実際に一度誤検知した）。
IP_RE = re.compile(r"(?<![\w./])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IP_ALLOW_RE = re.compile(
    r"^(?:203\.0\.113\.|192\.0\.2\.|198\.51\.100\.|127\.|0\.0\.0\.0$"
    r"|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)")


def looks_like_ip(s):
    """各オクテットが0-255か。SVGのパスデータ（3.176.765.84 など）を弾く。"""
    return all(p.isdigit() and int(p) <= 255 for p in s.split("."))
# 走査対象は「git が追跡するテキストファイル全部」。
#
# 以前はここに公開ファイル名を手で並べていた。そのリストに載っていない
# app/seed_accounts.py に監視対象62件がベタ書きされたまま、
# app/templates/manage.html と ig/dev/ に実アカウント名が残ったまま、
# そして**この走査スクリプト自身**が禁止語リストを抱えたまま通っていた。
# 名簿を手で保つ方式だと、新しいファイルは必ず検査の外から生まれる。
ROOT = (HERE / "..").resolve()
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "data", "ig-data",
             "node_modules", "profiles", ".idea", ".vscode"}
# secrets.local.example.txt は「こういう形の語を書く」という雛形なので
# プレースホルダが並ぶ。走査すると自分の例に引っかかるため対象外。
SKIP_NAMES = {"NOTES.md", "secrets.local.txt", "secrets.local.example.txt"}
SKIP_SUFFIX = {".db", ".db-wal", ".db-shm", ".png", ".jpg", ".jpeg", ".gif",
               ".webp", ".mp4", ".zip", ".pyc", ".ico", ".woff", ".woff2"}


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

print("\n== run_log への記録 ==")
def run_log(verdict_case):
    """log_egress が run_log に残すかを実DBで確かめる。

    v58 で scrape_log の擬似アカウント `__egress__` から
    run_log の kind='egress' に移した。アカウント名の列に
    アカウントでないものを入れるのをやめたため。
    """
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
rows = c.execute("SELECT kind, status, message FROM run_log").fetchall()
extra = c.execute("SELECT COUNT(*) FROM scrape_log").fetchone()[0]
print("SCRAPELOG", extra)
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
      len(rows) == 1 and rows[0][0] == "egress" and rows[0][1] == "warn",
      str(rows))
rows = run_log({"verdict": "proxy_bypassed", "ok": False, "reason": "迂回"})
check("中止は error で残る",
      len(rows) == 1 and rows[0][1] == "error", str(rows))

print("\n== Web画面に出るか ==")
wb = (APP / "web.py").read_text()
check("web.py が出口ログを引く", "egress_alert" in wb)
# 擬似名の除外は実装の書き方（!= / NOT LIKE / NOT GLOB）が変わるので、
# ソース文字列ではなく下の render_index で実際の画面を見て確かめる。
check("擬似名の除外条件が一箇所にまとまっている", "NOT_PSEUDO_SQL" in wb)
idx = (APP / "templates" / "index.html").read_text()
check("index.html が専用バナーを出す", "egress_alert" in idx)
check("中止と警告を描き分ける", "health-error" in idx)
st = (APP / "templates" / "_style.html").read_text()
check("health-error のCSSがある", ".health-error" in st)

print("\n== 実際に画面に出るか（レンダリング）==")
# 文字列grepだけでは足りない。マクロの with context 漏れを一度それで見逃したので、
# 本物の Flask にダミーDBを食わせて HTML を描かせて確かめる。
def render_index(egress_row, extra_rows=(), run_rows=()):
    """egress_row=(status, message) を scrape_log に入れて / を描画する。

    extra_rows=[(screen_name, status, message), ...] を渡すと
    scrape_log にもその行を入れる。
    run_rows=[(kind, status, message), ...] は run_log 側に入れる。
    """
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
    for _row in extra_rows:
        ins += (
            "\nimport sqlite3\n"
            "from datetime import datetime, timezone\n"
            "c = sqlite3.connect('/tmp/xegweb/data.db')\n"
            "c.execute('INSERT INTO scrape_log (run_at, screen_name, status,"
            " message, new_tweets) VALUES (?,?,?,?,0)',\n"
            "          (datetime.now(timezone.utc).isoformat(), "
            + repr(_row[0]) + ", " + repr(_row[1]) + ", " + repr(_row[2]) + "))\n"
            "c.commit(); c.close()\n"
        )
    for _r in run_rows:
        ins += (
            "\nimport sqlite3\n"
            "from datetime import datetime, timezone\n"
            "c = sqlite3.connect('/tmp/xegweb/data.db')\n"
            "c.execute('INSERT INTO run_log (run_at, kind, status, message)"
            " VALUES (?,?,?,?)',\n"
            "          (datetime.now(timezone.utc).isoformat(), "
            + repr(_r[0]) + ", " + repr(_r[1]) + ", " + repr(_r[2]) + "))\n"
            "c.commit(); c.close()\n"
        )
    if egress_row:
        ins += (f'''
import sqlite3
from datetime import datetime, timezone
c = sqlite3.connect("/tmp/xegweb/data.db")
c.execute("INSERT INTO run_log (run_at, kind, status, message)"
          " VALUES (?,?,?,?)",
          (datetime.now(timezone.utc).isoformat(), "egress",
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
check("出口の記録はアカウント欄に出ない", "__egress__" not in html)

html = render_index(("warn", "回線側の出口IPが変わりました（テスト用の文言）"))
check("警告バナーが出る", "⚠️ 回線の出口IPが変わりました" in html, html[:200])
check("警告では取得停止と言わない", "🛑 取得停止（回線分離）" not in html)
check("擬似アカウント名を表に出さない（警告時）", "@__egress__" not in html)

# 「エラーが続いているアカウント」の一覧に、アカウントでないものが
# 出てこないこと。
#
# v58 までは scrape_log の screen_name に `__run__` / `__egress__` を
# 相乗りさせていて、一覧から除く条件を全クエリに配る必要があった。
# その ESCAPE 指定を書き損じてトップページを500にしたことがある。
# いまは **scrape_log に実アカウントしか入らない**ので、
# 除外条件そのものが要らない。
#
# 一覧は「status='error' が直近3時間に2回以上」で拾うので、
# 本物のアカウントが拾われることを確かめる（条件を満たさない行を
# 入れると検査が空回りする。一度それをやった）。
html = render_index(None, extra_rows=[
    ("alice_art", "error", "本物のアカウントの失敗"),
    ("alice_art", "error", "本物のアカウントの失敗"),
])
check("本物のアカウントの失敗は出る", "alice_art" in html)

# 出口・巡回の記録は run_log にあり、アカウント一覧には現れない
html = render_index(None, run_rows=[
    ("run", "run_error", "全滅"),
    ("run", "run_error", "全滅"),
    ("egress", "error", "迂回"),
])
check("巡回の記録はアカウント一覧に出ない", "__run__" not in html)
check("出口の記録はアカウント一覧に出ない", "__egress__" not in html)

# scrape_log 側に擬似名が入っていないこと（移行後の状態）
_c = sqlite3.connect("/tmp/xegweb/data.db")
_pseudo = _c.execute(
    "SELECT COUNT(*) FROM scrape_log WHERE screen_name GLOB '__*__'"
).fetchone()[0]
_c.close()
check("scrape_log に擬似名が入らない", _pseudo == 0, str(_pseudo))

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

# 生IPの検査は後段の「公開ファイルに環境固有の情報が無いか」に一本化した。
# ここにも別の（弱い）正規表現で同じ検査があり、"Chrome/124.0.0.0" のような
# バージョン表記を実IPと取り違えていた。判定が2箇所にあると、片方だけ直して
# もう片方で落ちる。IP_RE / IP_ALLOW_RE / looks_like_ip を共通で使う。

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
    # .env.example は雛形なので、IPらしき値が1つでもあればおかしい
    _found = [ip for ip in IP_RE.findall(e) if looks_like_ip(ip)]
    check(".env.example の値は空", not _found, str(_found))
    for k in ("PROXY_URL", "EXPECTED_EGRESS_IP"):
        check(f".env.example に {k}", k in e)
check(".env 実体が同梱されていない", not (HERE / ".." / ".env").exists())

print("\n== 公開ファイルに環境固有の情報が無いか ==")
# 実ホスト名・実カテゴリ・実監視垢など、公開したくない語
# ホスト名・IP・実アカウント名など、公開したくない値。
# カテゴリ名（ギャル等）はタグ変換の設定例として公開ファイルに出てよいので含めない。
# 環境ごとの語（実ホスト名・実ISP名・実IP・実アカウント名）は
# dev/secrets.local.txt に1行1語で書く。gitignore 済み。
#
# 以前はこのリストを**このファイルに直接**書いていた。公開ファイルを
# 検査するスクリプト自身が、その禁止語を全部並べて公開していたことになる。
# （検査する側が漏らしていた。）リストは手元だけに置く。
SECRETS_FILE = HERE / "secrets.local.txt"
SECRETS = []
if SECRETS_FILE.exists():
    SECRETS = [l.strip() for l in SECRETS_FILE.read_text().splitlines()
               if l.strip() and not l.startswith("#")]


def public_files():
    out = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if set(rel.parts) & SKIP_DIRS:
            continue
        if p.name in SKIP_NAMES or p.suffix in SKIP_SUFFIX:
            continue
        # gitignore 済みのローカル専用ファイル（例は除く）
        if (".local." in p.name and ".local.example." not in p.name) \
                or p.name in (".env", "cookies.txt", "tumblr_accounts.json",
                              "accounts.txt", "accounts_fallback.json"):
            continue
        out.append(rel.as_posix())
    return out


PUBLIC = public_files()
check("走査対象が十分ある", len(PUBLIC) > 30, str(len(PUBLIC)))
check("走査スクリプト自身も対象", "dev/egress_check.py" in PUBLIC)
check("監視対象マスタも対象", "app/seed_accounts.py" in PUBLIC)
for f in PUBLIC:
    fp = ROOT / f
    try:
        body = fp.read_text()
    except (UnicodeDecodeError, OSError):
        continue
    hit = [w for w in SECRETS if w in body]
    # バージョン番号などに紛れるので、行に "version" を含む行は除く
    bad_ip = sorted({ip for line in body.splitlines()
                     if "version" not in line.lower()
                     for ip in IP_RE.findall(line)
                     if looks_like_ip(ip) and not IP_ALLOW_RE.match(ip)})
    check(f"{f}", not hit and not bad_ip, str(hit + bad_ip))

print("\n== 作業メモ ==")
notes = HERE / ".." / "NOTES.local.md"
# NOTES.local.md は gitignore 済みで配布zipには含まれない。あるときだけ中身を見る。
if notes.exists():
    # 中身の語を名指しすると、その語（実ISP名など）がこのファイルに
    # 残ってしまうので、書かれているかどうかだけ見る。
    check("中身が空でない", len(notes.read_text().strip()) > 100)
else:
    print("    (NOTES.local.md は未配置。手元にだけ置く運用なので正常)")

if SECRETS:
    print(f"    (環境ごとの禁止語 {len(SECRETS)}件を {SECRETS_FILE.name} から読んだ)")
else:
    print(f"    ({SECRETS_FILE.name} が無いので一般形の検査のみ。"
          f"手元では dev/secrets.local.example.txt を写して置く)")

gi2 = (HERE / ".." / ".gitignore").read_text()
check(".gitignore が NOTES.local.md を除外", "NOTES.local.md" in gi2)

print("\n== scraper のCLI ==")
sc = (APP / "scraper.py").read_text()
check("check-egress サブコマンドがある", '"check-egress"' in sc)
check("スクレイプ前に自動チェックする", 'if EXPECTED_EGRESS_IP:' in sc and "中止します" in sc)
check("失敗時は exit 1", "sys.exit(1)" in sc)
check("clear-egress-log サブコマンドがある", '"clear-egress-log"' in sc)
check("迂回時だけ止める文言", "分離が確認できないため" in sc)

# ---------------------------------------------------------------------
print("\n== IG向け設定の新旧どちらの名前でも読めるか ==")

# IG-Ray を畳んだので名前を IG_* に寄せたいが、既存の .env は IG_RAY_* で
# 書かれている。**書き換えを強制しない**こと（compose で両方受ける）。
_cmp = (HERE / ".." / "docker-compose.yml").read_text()
_short_ok, _missing = [], []
for _m in re.finditer(r"\$\{IG_RAY_([A-Z0-9_]+)", _cmp):
    _n = _m.group(1)
    if ("${IG_" + _n + ":-") in _cmp:
        _short_ok.append(_n)
    else:
        _missing.append(_n)
check("compose が短い名前も受ける", not _missing,
      f"受けていない: {sorted(set(_missing))}")
check("受けている数が妥当", len(set(_short_ok)) >= 20, str(len(set(_short_ok))))

# コンテナに渡す側の名前は変えない。変えると ig/app/ を直す必要が出る
# （あちらは無改変が原則）。
check("コンテナ内の変数名は IG_RAY_* のまま",
      "    IG_RAY_SLEEP_MIN:" in _cmp and "\n    IG_SLEEP_MIN:" not in _cmp)


def _ig_interval(**extra):
    """与えた環境で web.py が読み取った値を返す。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("IG_INTERVAL", "IG_RAY_INTERVAL",
                        "IG_STALE_INTERVALS", "IG_RAY_STALE_INTERVALS")}
    env.update({"DB_PATH": "/tmp/xegweb/data.db"})
    env.update(extra)
    code = (f"import sys; sys.path.insert(0, {str(APP)!r})\n"
            "import web; print('V', web.IG_INTERVAL_SEC, web.IG_STALE_INTERVALS)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=120, env=env)
    for line in r.stdout.splitlines():
        if line.startswith("V "):
            return line
    return "ERR " + r.stderr[-200:]


check("web が短い名前を読む",
      _ig_interval(IG_INTERVAL="111", IG_STALE_INTERVALS="4")
      == "V 111.0 4.0", _ig_interval(IG_INTERVAL="111",
                                     IG_STALE_INTERVALS="4"))
check("web が旧名も読む",
      _ig_interval(IG_RAY_INTERVAL="222").startswith("V 222.0"),
      _ig_interval(IG_RAY_INTERVAL="222"))
check("両方あれば新しい名前を優先",
      _ig_interval(IG_INTERVAL="111",
                   IG_RAY_INTERVAL="222").startswith("V 111.0"),
      _ig_interval(IG_INTERVAL="111", IG_RAY_INTERVAL="222"))
check("何も無ければ既定値",
      _ig_interval().startswith("V 21600.0 3.0"), _ig_interval())
check("数値でなければ既定値に落ちる",
      _ig_interval(IG_INTERVAL="あ").startswith("V 21600.0"),
      _ig_interval(IG_INTERVAL="あ"))

print("\n" + ("=== 全て通過 ===" if not fails else f"=== 失敗 {len(fails)}件: {fails} ==="))
sys.exit(1 if fails else 0)
