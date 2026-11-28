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

print("\n== DB書き込みチェック ==")
check("関数がある", hasattr(scraper, "check_db_writable"))
check("書ける状態ならTrue", scraper.check_db_writable() is True)

# 書けない状態を作って検出できるか（root環境ではchmodが効かないので条件付き）
import stat as _stat
_p = os.environ["DB_PATH"]
_mode = os.stat(_p).st_mode
try:
    os.chmod(_p, 0o444)
    if not os.access(_p, os.W_OK):
        check("書けないとFalse", scraper.check_db_writable() is False)
    else:
        print("    (root実行のため書き込み不可を再現できず。スキップ)")
finally:
    os.chmod(_p, _mode)
check("戻した後はTrue", scraper.check_db_writable() is True)

print("\n== 多重起動防止 ==")
_df = (HERE / ".." / "Dockerfile.worker").read_text()
check("スクレイプに flock -n", "flock -n /tmp/scraper.lock" in _df)
check("キャッシュ削除にも flock -n", "flock -n /tmp/cache_cleanup.lock" in _df)
check("起動時の初回実行もロックを取る",
      "python db.py && flock -n /tmp/scraper.lock python scraper.py" in _df)
check("flock が入る util-linux を導入", "util-linux" in _df)

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

print("\n== 1件の失敗が全体を巻き込まないか ==")
# twscrape 0.21.0 から、取得失敗は None ではなく専用例外で飛んでくる。
# 監視対象1件の例外でループ全体が止まると、1垢の不調で全件が死ぬ。
# 将来の破壊的変更でここが崩れたら気づけるよう、実物の例外クラスで試す。
from twscrape import XClIdParseError, NoAccountError

conn = db.get_conn()
conn.execute("DELETE FROM accounts")
conn.execute("DELETE FROM scrape_log")
for _n in ("ng_parse", "ng_nopool", "ng_none"):
    conn.execute("INSERT INTO accounts (screen_name, display_name, categories)"
                 " VALUES (?,?,?)", (_n, _n, '["test"]'))
conn.commit(); conn.close()


class _FakeAPI:
    """Xに通信せずに、0.21.0が投げる例外だけを再現する。"""
    def __init__(self, *a, **k):
        self.pool = self

    async def accounts_info(self):
        return [{"username": "dummy"}]

    async def user_by_login(self, sn):
        if sn == "ng_parse":
            raise XClIdParseError("X web scripts not found")
        if sn == "ng_nopool":
            raise NoAccountError("no accounts available")
        return None          # 0.21.0: None は「Xにデータが無い」の意味

    def user_tweets(self, *a, **k):
        raise AssertionError("user_by_login で止まるはずなので到達しない")


_real_api = scraper.API
scraper.API = _FakeAPI
try:
    asyncio.run(scraper.scrape_all())
finally:
    scraper.API = _real_api

conn = db.get_conn()
# 擬似アカウント（巡回そのものの結果）は監視対象ではないので除く
_rows = dict((r[0], r[1]) for r in conn.execute(
    # scrape_log には実アカウントしか入らない（巡回・出口は run_log）
    "SELECT screen_name, message FROM scrape_log"
).fetchall())
conn.close()

check("例外が出ても3件すべて処理される", len(_rows) == 3, str(_rows))
check("例外の型名がログに残る",
      _rows.get("ng_parse", "").startswith("XClIdParseError"), str(_rows.get("ng_parse")))
check("プール枯渇も型名で分かる",
      _rows.get("ng_nopool", "").startswith("NoAccountError"), str(_rows.get("ng_nopool")))
check("Noneはデータ無しとして扱う",
      _rows.get("ng_none") == "user not found", str(_rows.get("ng_none")))

print("\n== 全滅が続いたら間隔を空ける ==")
# 2026-08 に X 側の変更で全87アカウントが落ち続けたとき、15分おきに
# 叩き続けていた。復旧しないうえに叩くほど心証が悪い。
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

conn = db.get_conn()
conn.execute("DELETE FROM run_log")
conn.commit(); conn.close()

check("失敗が無ければ待たない", scraper.backoff_active() is False)


def _put_run(status, minutes_ago, n=1):
    cn = db.get_conn()
    for _ in range(n):
        cn.execute("INSERT INTO run_log (run_at, kind, status, message)"
                   " VALUES (?,?,?,?)",
                   ((_dt.now(_tz.utc) - _td(minutes=minutes_ago)).isoformat(),
                    scraper.RUN_KIND, status, ""))
    cn.commit(); cn.close()


# 1回目の全滅 → 15分あける
_put_run("run_error", 5)
check("1回目は15分待つ", scraper.backoff_active() is True)
conn = db.get_conn(); conn.execute("DELETE FROM run_log"); conn.commit(); conn.close()
_put_run("run_error", 20)
check("15分経っていれば走る", scraper.backoff_active() is False)

# 3回続いたら 15*4=60分
conn = db.get_conn(); conn.execute("DELETE FROM run_log"); conn.commit(); conn.close()
_put_run("run_error", 30, n=3)
check("3回続いたら60分待つ", scraper.backoff_active() is True)

# 上限で頭打ち
conn = db.get_conn(); conn.execute("DELETE FROM run_log"); conn.commit(); conn.close()
_put_run("run_error", 10000, n=20)
check("上限を超えたら走る（頭打ち）", scraper.backoff_active() is False)

# 1件でも取れたら解除される
conn = db.get_conn(); conn.execute("DELETE FROM run_log"); conn.commit(); conn.close()
_put_run("run_error", 5, n=3)
scraper.log_run_result(1, 10)
check("1件でも取れたら解除", scraper.backoff_active() is False)

# 全滅したら記録される
conn = db.get_conn(); conn.execute("DELETE FROM run_log"); conn.commit(); conn.close()
scraper.log_run_result(0, 10)
conn = db.get_conn()
_r = conn.execute("SELECT status, message FROM run_log WHERE kind=?",
                  (scraper.RUN_KIND,)).fetchone()
conn.close()
check("全滅を記録する", _r and _r[0] == "run_error", str(_r))
check("件数が分かる", _r and "10件すべて失敗" in (_r[1] or ""), str(_r))

# 取得が1件も無い環境（監視対象ゼロ）では全滅扱いにしない
conn = db.get_conn(); conn.execute("DELETE FROM run_log"); conn.commit(); conn.close()
scraper.log_run_result(0, 0)
conn = db.get_conn()
_n = conn.execute("SELECT COUNT(*) FROM run_log WHERE kind=?",
                  (scraper.RUN_KIND,)).fetchone()[0]
conn.close()
check("監視対象ゼロなら記録しない", _n == 0, str(_n))

# 巡回の記録は scrape_log を汚さない。
# 以前は screen_name に `__run__` を入れていたので、アカウント単位の
# ログに「アカウントでないもの」が混ざっていた。
conn = db.get_conn()
conn.execute("DELETE FROM run_log"); conn.execute("DELETE FROM scrape_log")
conn.commit(); conn.close()
scraper.log_run_result(0, 10)
scraper.log_egress({"verdict": "proxy_bypassed", "ok": False, "reason": "迂回"})
conn = db.get_conn()
_sl = conn.execute("SELECT COUNT(*) FROM scrape_log").fetchone()[0]
_rl = conn.execute("SELECT kind, status FROM run_log ORDER BY id").fetchall()
conn.close()
check("scrape_log には入らない", _sl == 0, str(_sl))
check("run_log に2件入る", len(_rl) == 2, str([tuple(r) for r in _rl]))
check("巡回と出口を種別で分ける",
      {r[0] for r in _rl} == {"run", "egress"}, str([tuple(r) for r in _rl]))

print("\n" + ("=== 全て通過 ===" if not fails else f"=== 失敗 {len(fails)}件: {fails} ==="))
sys.exit(1 if fails else 0)
