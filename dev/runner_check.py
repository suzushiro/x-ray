"""
ランナー（run_all.py）自身の検査。

**他の24スイートの合否はこれが判定する**ので、ここが間違っていると
「全部通っている」という嘘が出る。実際このセッション中に、手で書いた
判定ロジックで2回取り違えた:

  - 通過しているスイートの出力に「失敗」という語が含まれていて失敗扱い
    （`grep -q 失敗` で判定していた）
  - 依存が入っていないだけ（ModuleNotFoundError）を本物の失敗扱い

なので、わざと落ちる／依存が無い／途中で死ぬ／紛らわしい語を含む
スイートを作って、ランナーがそれぞれを正しく分類することを確かめる。
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_all                                           # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("OK  " if ok else "NG  ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------------
print("== [1] 合否の読み取り ==")

# 「全て通過」形式（dev/ 配下）
st, p, f, note = run_all.verdict("OK  a\nOK  b\n\n=== 全て通過 ===\n", 0)
check("全て通過を読める", (st, p, f) == ("ok", 2, 0), f"{st} {p} {f}")

# 「失敗 N件」形式
st, p, f, note = run_all.verdict(
    "OK  a\nNG  b\n=== 失敗 1件: ['b'] ===\n", 1)
check("失敗件数を読める", (st, f) == ("fail", 1), f"{st} {f}")

# 「PASS n / FAIL n」形式（ig/dev 配下）
st, p, f, note = run_all.verdict("PASS 12 / FAIL 0\n", 0)
check("PASS/FAIL 形式を読める", (st, p, f) == ("ok", 12, 0), f"{st} {p} {f}")
st, p, f, note = run_all.verdict("PASS 10 / FAIL 2\n", 1)
check("PASS/FAIL の失敗を読める", (st, f) == ("fail", 2), f"{st} {f}")

# ここが本番。**通過しているのに「失敗」という語を含む**出力。
# 検査項目の名前に「失敗」が入っているのはよくある
# （「取得に失敗したら止める」など）。語で判定すると落ちる。
tricky = (
    "OK  取得に失敗したら止める\n"
    "OK  ログイン失敗を検知する\n"
    "OK  失敗が続いたら待つ\n"
    "\n=== 全て通過 ===\n"
)
st, p, f, note = run_all.verdict(tricky, 0)
check("「失敗」を含む通過を取り違えない", st == "ok", f"{st} {note}")

# 逆に、項目名に「全て通過」が入っていても合否行を優先する
tricky2 = ("OK  「全て通過」と表示する\n"
           "NG  別の何か\n=== 失敗 1件: ['別の何か'] ===\n")
st, p, f, note = run_all.verdict(tricky2, 1)
check("合否行のほうを優先する", st == "fail", f"{st}")

# 依存欠けは失敗と分ける
dep = ("Traceback (most recent call last):\n"
       "  File \"x.py\", line 1, in <module>\n"
       "    import twscrape\n"
       "ModuleNotFoundError: No module named 'twscrape'\n")
st, p, f, note = run_all.verdict(dep, 1)
check("依存欠けを分けて返す", st == "deps", f"{st}")
check("足りないものを名指しする", note == "twscrape", note)

# 合否行が無いまま終わったら「不明」。黙って通過にしてはいけない。
st, p, f, note = run_all.verdict("OK  a\nOK  b\n", 1)
check("合否行が無ければ不明", st == "unknown", f"{st} {note}")
check("終了コードを添える", "1" in note, note)

# 合否行が「全て通過」でも終了コードが0でなければ通過にしない
st, p, f, note = run_all.verdict("=== 全て通過 ===\n", 1)
check("終了コードが0でなければ通過にしない", st != "ok", f"{st}")

# ---------------------------------------------------------------------
print("\n== [2] スイートの発見 ==")

found = run_all.discover([])
ids = [s["id"] for s in found]
check("x-ray側を拾う", "x-ray/route_check" in ids)
check("ig側を拾う", "ig/backfill_check" in ids)
check("JSのスイートも拾う", "x-ray/extension_check" in ids,
      str([i for i in ids if "extension" in i]))
# 同名のスイートが両方に居る。グループ名で区別できること。
check("同名でも区別できる",
      "x-ray/egress_check" in ids and "ig/egress_check" in ids)
# seed_dummy.py や run_all.py 自身は拾わない
check("補助スクリプトは拾わない",
      not any("seed_dummy" in i or "run_all" in i for i in ids), str(ids))
check("絞り込みが効く",
      [s["id"] for s in run_all.discover(["route"])] == ["x-ray/route_check"],
      str([s["id"] for s in run_all.discover(["route"])]))

# 新しいスイートを置いたら、手で登録しなくても拾われること。
# 手で並べる方式だと必ず漏れる（実際に .js の2本が長く走っていなかった）。
_tmp = os.path.join(HERE, "_zzz_probe_check.py")
open(_tmp, "w").write("print('=== 全て通過 ===')\n")
try:
    check("置くだけで拾われる",
          "x-ray/_zzz_probe_check" in [s["id"] for s in run_all.discover([])])
finally:
    os.remove(_tmp)

# ---------------------------------------------------------------------
print("\n== [3] 実際に走らせたときの分類 ==")

CASES = {
    "pass": "print('OK  a')\nprint('=== 全て通過 ===')\n",
    "fail": ("import sys\nprint('NG  b')\n"
             "print(\"=== 失敗 1件: ['b'] ===\")\nsys.exit(1)\n"),
    "deps": "import nonexistent_module_xyz\n",
    "crash": "raise SystemError('ばーん')\n",
    # 通過しているが紛らわしい語を含む
    "tricky": ("print('OK  取得に失敗したら止める')\n"
               "print('=== 全て通過 ===')\n"),
}
made = []
try:
    for key, body in CASES.items():
        fp = os.path.join(HERE, f"_zzz_{key}_check.py")
        open(fp, "w").write(body)
        made.append(fp)

    got = {}
    for key in CASES:
        suite = run_all.discover([f"_zzz_{key}_"])[0]
        got[key] = run_all.run_one(suite)

    check("通過を通過と見る", got["pass"]["state"] == "ok",
          got["pass"]["state"])
    check("失敗を失敗と見る", got["fail"]["state"] == "fail",
          got["fail"]["state"])
    check("失敗件数を拾う", got["fail"]["failed"] == 1,
          str(got["fail"]["failed"]))
    check("依存欠けを分ける", got["deps"]["state"] == "deps",
          got["deps"]["state"])
    check("足りないものを名指し",
          got["deps"]["note"] == "nonexistent_module_xyz",
          got["deps"]["note"])
    check("途中で死んだら不明", got["crash"]["state"] == "unknown",
          got["crash"]["state"])
    check("紛らわしい語でも通過", got["tricky"]["state"] == "ok",
          got["tricky"]["state"])

    # 終了コード: 失敗があれば1、依存欠けだけなら0
    r = subprocess.run([sys.executable, os.path.join(HERE, "run_all.py"),
                        "_zzz_fail_"], capture_output=True, text=True)
    check("失敗があれば終了コード1", r.returncode == 1, str(r.returncode))
    r = subprocess.run([sys.executable, os.path.join(HERE, "run_all.py"),
                        "_zzz_deps_"], capture_output=True, text=True)
    check("依存欠けだけなら終了コード0", r.returncode == 0, str(r.returncode))
    check("依存の入れ方を案内する", "pip install" in r.stdout,
          "案内なし" if "pip install" not in r.stdout else "")

    # --json が機械で読める形であること
    r = subprocess.run([sys.executable, os.path.join(HERE, "run_all.py"),
                        "--json", "_zzz_"], capture_output=True, text=True)
    try:
        data = json.loads(r.stdout)
        check("--json が読める", isinstance(data, list) and len(data) == 5,
              str(len(data) if isinstance(data, list) else type(data)))
        check("--json に出力本文を混ぜない",
              all("out" not in d for d in data))
    except json.JSONDecodeError as e:
        check("--json が読める", False, f"{e}: {r.stdout[:200]}")
finally:
    for fp in made:
        os.path.exists(fp) and os.remove(fp)

# ---------------------------------------------------------------------
print("\n== [4] フィクスチャの用意 ==")

# dev/ 配下のスイートは、環境変数とダミーデータをランナーが用意する前提。
# 用意を忘れると「no such table: tweets」という、変更とは無関係な
# 失敗に見える（実際にそれで route_check を落とした）。
base = tempfile.mkdtemp(prefix="runnerchk")
env = run_all.make_fixture(base)
for k in ("DB_PATH", "IMAGES_DIR", "CACHE_DIR", "TWITTER_COOKIES_FILE",
          "ACCOUNTS_JSON_PATH", "TWSCRAPE_DB"):
    check(f"{k} を渡す", k in env and env[k].startswith(base), env.get(k, ""))
check("IGは明示的に外す",
      "IG_DB_PATH" not in env and "IG_CACHE_DIR" not in env)
check("DBにダミーデータが入っている", os.path.exists(env["DB_PATH"]))

import sqlite3                                            # noqa: E402
_c = sqlite3.connect(env["DB_PATH"])
_n = _c.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
_a = _c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
_c.close()
check("tweets が入っている", _n > 0, str(_n))
check("accounts が入っている", _a > 0, str(_a))

# スイートごとに別のディレクトリになること（走らせる順番に依存しない）
base2 = tempfile.mkdtemp(prefix="runnerchk")
env2 = run_all.make_fixture(base2)
check("スイートごとに別の作業場", env["DB_PATH"] != env2["DB_PATH"])

print()
if fails:
    print(f"=== 失敗 {len(fails)}件: {fails} ===")
    sys.exit(1)
print("=== 全て通過 ===")
