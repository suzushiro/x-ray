"""検証専用: prune_scrape_log の動作と、scraper.py の関数到達性を確認する。

NameError: prune_scrape_log is not defined を取りこぼした反省から、
「呼んでいるのに定義がない」を機械的に検出する検査も入れてある。
"""
import ast
import builtins
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / ".." / "app"
sys.path.insert(0, str(APP))

BASE = "/tmp/xprune"
os.environ.update({
    "DB_PATH": f"{BASE}/data.db", "IMAGES_DIR": f"{BASE}/images",
    "CACHE_DIR": f"{BASE}/cache", "TWITTER_COOKIES_FILE": f"{BASE}/c.txt",
    "ACCOUNTS_JSON_PATH": f"{BASE}/a.json", "TWSCRAPE_DB": f"{BASE}/pool.db",
    "SCRAPE_LOG_RETENTION_DAYS": "7",
})
os.makedirs(BASE, exist_ok=True)

fails = []


def check(label, cond, detail=""):
    print(f"{'OK ' if cond else 'NG '} {label}{('  ' + detail) if detail else ''}")
    if not cond:
        fails.append(label)


print("== 静的検査: 呼んでいるのに定義がない関数 ==")
targets = ["scraper.py", "web.py", "db.py", "cache_utils.py", "seed_accounts.py"]
for name in targets:
    p = APP / name
    if not p.exists():
        continue
    tree = ast.parse(p.read_text())
    defined = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                defined.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
    missing = sorted({n.func.id for n in ast.walk(tree)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                      and n.func.id not in defined})
    check(f"{name}", not missing, str(missing))

import db
import scraper
db.init_db()


def seed_logs():
    conn = db.get_conn()
    conn.execute("DELETE FROM scrape_log")
    now = datetime.now(timezone.utc)
    for days_ago, n in ((0, 5), (3, 5), (6, 5), (8, 5), (30, 5)):
        for i in range(n):
            conn.execute(
                "INSERT INTO scrape_log (run_at, screen_name, status, message, new_tweets)"
                " VALUES (?,?,?,?,?)",
                ((now - timedelta(days=days_ago, minutes=i)).isoformat(),
                 f"u{i}", "ok", "", 1))
    conn.commit()
    total = conn.execute("SELECT count(*) c FROM scrape_log").fetchone()["c"]
    conn.close()
    return total


def count():
    conn = db.get_conn()
    n = conn.execute("SELECT count(*) c FROM scrape_log").fetchone()["c"]
    conn.close()
    return n


print("\n== prune_scrape_log ==")
check("関数が存在する", hasattr(scraper, "prune_scrape_log"))
total = seed_logs()
check("25件投入", total == 25, str(total))

removed = scraper.prune_scrape_log(days=7)
check("7日より古い10件が消える", removed == 10, f"{removed}件")
check("15件残る", count() == 15, f"{count()}件")

seed_logs()
removed = scraper.prune_scrape_log(days=0)
check("days=0 なら削除しない", removed == 0 and count() == 25, f"{removed}/{count()}")

seed_logs()
removed = scraper.prune_scrape_log(days=1)
check("days=1 なら直近1日だけ残る", count() == 5, f"{count()}件")

seed_logs()
removed = scraper.prune_scrape_log()
check("引数なしで環境変数(7日)を使う", count() == 15, f"{count()}件")

conn = db.get_conn(); conn.execute("DELETE FROM scrape_log"); conn.commit(); conn.close()
check("空でも落ちない", scraper.prune_scrape_log() == 0)

print("\n== 環境変数の読み取り ==")
check("SCRAPE_LOG_RETENTION_DAYS を読む", scraper.SCRAPE_LOG_RETENTION_DAYS == 7,
      str(scraper.SCRAPE_LOG_RETENTION_DAYS))

print("\n== scrape_all の末尾が最後まで到達するか ==")
# scrape_all を丸ごと動かすとXへ通信してしまうので、
# 監視対象0件の状態で走らせて末尾の prune_scrape_log まで到達するか見る
import asyncio
conn = db.get_conn(); conn.execute("DELETE FROM accounts"); conn.commit(); conn.close()
try:
    asyncio.run(scraper.scrape_all())
    check("scrape_all が例外なく完走する", True)
except NameError as e:
    check("scrape_all が例外なく完走する", False, f"NameError: {e}")
except Exception as e:
    # 通信やアカウントプール由来のエラーは想定内。NameErrorでなければ可
    check("scrape_all が NameError では落ちない", True, f"({type(e).__name__})")

print("\n" + ("=== 全て通過 ===" if not fails else f"=== 失敗 {len(fails)}件: {fails} ==="))
sys.exit(1 if fails else 0)
