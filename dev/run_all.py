#!/usr/bin/env python3
"""
検査スイートをまとめて走らせる。

    python dev/run_all.py              # 全部
    python dev/run_all.py route sources   # 名前に含まれるものだけ
    python dev/run_all.py --list       # 一覧だけ出す
    python dev/run_all.py -v           # 落ちたスイートの出力も出す
    python dev/run_all.py --json       # 機械で読む形

## なぜこれがあるのか

スイートごとに走らせ方と合否の出し方が違い、**人間が目で判定していた**。

- `dev/` 配下は `DB_PATH` など8個の環境変数と `seed_dummy.py` を
  先に用意しないと落ちる。用意を忘れると
  「no such table: tweets」という、変更とは無関係な失敗に見える
- `ig/dev/` 配下は自己完結だが、合否の文言が `PASS n / FAIL n` で
  `dev/` 側の `=== 全て通過 ===` と違う
- スイート名を手で並べて回していたので、**追加したスイートが漏れた**
  （`.js` の2本は長い間一度も走っていなかった）
- 依存が入っていないだけの失敗（`ModuleNotFoundError`）と、本物の
  失敗が同じ見た目になる。コンテナを作り直した直後に取り違えた

どれも「判定を人間がやっている」ことが原因なので、ここに寄せる。

## スイートの作法

- ファイル名は `*_check.py` か `*_check.js`。置けば自動で拾われる
- 最後に `=== 全て通過 ===` か `=== 失敗 N件: [...] ===` を出す
  （`ig/dev/` 配下の `PASS n / FAIL n` 形式も読む）
- 成功なら終了コード0
- `dev/` 配下の Python スイートは、**環境とフィクスチャをこのランナーが
  用意する**前提でよい。1スイートにつき使い捨てのディレクトリを作って
  `seed_dummy.py` を流すので、スイート間で順序に依存しない
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
IG_DEV = os.path.join(ROOT, "ig", "dev")

# dev/ 配下の Python スイートが前提にしている環境。
# 以前は README を見ながら手で export していた。
FIXTURE_ENV = {
    "PERSIST_CATEGORIES": "art,gadget",
    "CACHE_RETENTION_DAYS": "30",
}


def discover(patterns):
    """スイートを探す。手で並べると追加したものが漏れる。"""
    found = []
    for d, group in ((HERE, "x-ray"), (IG_DEV, "ig")):
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not re.match(r".*_check\.(py|js)$", name):
                continue
            stem = name.rsplit(".", 1)[0]
            found.append({
                "group": group,
                # dev/egress_check.py と ig/dev/egress_check.py は同名なので
                # グループ名を付けて区別する
                "id": f"{group}/{stem}",
                "path": os.path.join(d, name),
                "lang": name.rsplit(".", 1)[1],
            })
    if patterns:
        found = [s for s in found
                 if any(p.lower() in s["id"].lower() for p in patterns)]
    return found


def make_fixture(base):
    """
    使い捨ての作業ディレクトリと、X側のダミーデータを用意する。

    スイートごとに作り直すので、走らせる順番に依存しない。
    （以前は共有の /tmp を使い回していて、片方が消したデータで
    もう片方が落ちることがあった。）
    """
    env = dict(os.environ)
    env.update(FIXTURE_ENV)
    for sub in ("images", "cache"):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    env.update({
        "DB_PATH": os.path.join(base, "data.db"),
        "IMAGES_DIR": os.path.join(base, "images"),
        "CACHE_DIR": os.path.join(base, "cache"),
        "TWITTER_COOKIES_FILE": os.path.join(base, "cookies.txt"),
        "ACCOUNTS_JSON_PATH": os.path.join(base, "accounts.json"),
        "TWSCRAPE_DB": os.path.join(base, "pool.db"),
        "SOURCES_CHECK_DIR": base,
    })
    # IGは各スイートが自分で用意する。ランナーが中途半端に
    # 指すと「読めているつもり」で食い違うので、明示的に外す。
    env.pop("IG_DB_PATH", None)
    env.pop("IG_CACHE_DIR", None)

    seed = os.path.join(HERE, "seed_dummy.py")
    if os.path.exists(seed):
        subprocess.run([sys.executable, seed], cwd=HERE, env=env,
                       capture_output=True, timeout=120)
    return env


# ``=== 失敗 3件: ['a', 'b'] ===`` / ``=== 全て通過 ===``
RE_JA_FAIL = re.compile(r"===\s*失敗\s*(\d+)\s*件")
RE_JA_OK = re.compile(r"===\s*全て通過\s*===")
# ``PASS 12 / FAIL 0``
RE_EN = re.compile(r"PASS\s+(\d+)\s*/\s*FAIL\s+(\d+)")
RE_MISSING_MOD = re.compile(r"No module named ['\"]([^'\"]+)['\"]")


def verdict(out, rc):
    """
    出力から合否を読む。2つの作法を1つに正規化する。

    戻り値: (状態, 通過数, 失敗数, 補足)
      状態は "ok" / "fail" / "deps" / "unknown"

    「落ちた」と「依存が無い」を分けるのが肝。コンテナを作り直した
    直後に `ModuleNotFoundError` を本物の失敗と取り違えた。
    """
    ok_n = len(re.findall(r"^OK\s", out, re.M))
    ng_n = len(re.findall(r"^NG\s", out, re.M))

    m = RE_EN.search(out)
    if m:
        p, f = int(m.group(1)), int(m.group(2))
        return ("ok" if f == 0 and rc == 0 else "fail"), p, f, ""

    m = RE_JA_FAIL.search(out)
    if m:
        return "fail", ok_n, int(m.group(1)), ""

    if RE_JA_OK.search(out) and rc == 0:
        return "ok", ok_n, 0, ""

    # 合否行が無い = 途中で死んでいる。依存欠けなら分けて報せる。
    mod = RE_MISSING_MOD.search(out)
    if mod:
        return "deps", ok_n, ng_n, mod.group(1)
    return "unknown", ok_n, ng_n, f"終了コード {rc}"


def run_one(suite, keep=False):
    base = tempfile.mkdtemp(prefix=f"xrchk_{suite['group']}_")
    try:
        if suite["lang"] == "js":
            node = shutil.which("node")
            if not node:
                return {"state": "deps", "passed": 0, "failed": 0,
                        "note": "node", "out": "", "secs": 0.0}
            cmd = [node, suite["path"]]
            env = dict(os.environ)
        else:
            cmd = [sys.executable, suite["path"]]
            env = (make_fixture(base) if suite["group"] == "x-ray"
                   else dict(os.environ))

        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=os.path.dirname(suite["path"]),
                               env=env, capture_output=True, text=True,
                               timeout=900)
            out = (r.stdout or "") + (r.stderr or "")
            rc = r.returncode
        except subprocess.TimeoutExpired:
            return {"state": "unknown", "passed": 0, "failed": 0,
                    "note": "15分で打ち切り", "out": "", "secs": 900.0}
        secs = time.time() - t0

        state, p, f, note = verdict(out, rc)
        return {"state": state, "passed": p, "failed": f, "note": note,
                "out": out, "secs": secs}
    finally:
        if not keep:
            shutil.rmtree(base, ignore_errors=True)


MARK = {"ok": "通過", "fail": "失敗", "deps": "依存欠け", "unknown": "不明"}


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("patterns", nargs="*", help="スイート名の部分一致で絞る")
    ap.add_argument("--list", action="store_true", help="一覧だけ出す")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="落ちたスイートの出力も出す")
    ap.add_argument("--json", action="store_true", help="機械で読む形で出す")
    ap.add_argument("--keep", action="store_true",
                    help="作業ディレクトリを消さない（調査用）")
    args = ap.parse_args()

    suites = discover(args.patterns)
    if not suites:
        print("該当するスイートがありません")
        return 1

    if args.list:
        for s in suites:
            print(f"  {s['id']}  ({s['lang']})")
        print(f"\n計 {len(suites)} 件")
        return 0

    results = []
    width = max(len(s["id"]) for s in suites)
    for s in suites:
        # 進捗は端末のときだけ。パイプやログに流すと制御文字が
        # そのまま残って結果の行にくっつく。
        if not args.json and sys.stdout.isatty():
            print(f"  … {s['id']}\033[K", end="\r", flush=True)
        r = run_one(s, keep=args.keep)
        r["id"] = s["id"]
        results.append(r)
        if not args.json:
            detail = (f"{r['passed']}件" if r["state"] == "ok"
                      else f"{r['failed']}件失敗" if r["state"] == "fail"
                      else r["note"])
            print(f"  {MARK[r['state']]:5} {s['id']:<{width}}  "
                  f"{detail:<12} {r['secs']:5.1f}秒")

    if args.json:
        print(json.dumps([{k: v for k, v in r.items() if k != "out"}
                          for r in results], ensure_ascii=False, indent=2))

    bad = [r for r in results if r["state"] in ("fail", "unknown")]
    deps = [r for r in results if r["state"] == "deps"]

    if args.verbose:
        for r in bad:
            print(f"\n{'=' * 60}\n{r['id']}\n{'=' * 60}")
            print("\n".join(
                l for l in r["out"].splitlines()
                if l.startswith("NG ") or "===" in l
                or "Error" in l or "line " in l)[:4000])

    if not args.json:
        n_ok = sum(1 for r in results if r["state"] == "ok")
        print()
        print(f"=== 通過 {n_ok} / 失敗 {len(bad)} / 依存欠け {len(deps)}"
              f"  （計 {len(results)} 件）===")
        if deps:
            mods = sorted({r["note"] for r in deps if r["note"]})
            print(f"    依存欠けは検査の失敗ではありません。足りないもの: "
                  f"{', '.join(mods)}")
            print(f"    入れ方: pip install --break-system-packages "
                  f"{' '.join(m.replace('_', '-') for m in mods)}")
        if bad:
            print(f"    落ちたスイート: {[r['id'] for r in bad]}")
            if not args.verbose:
                print("    中身を見る: python dev/run_all.py -v "
                      + " ".join(r["id"].split("/")[-1] for r in bad))

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
