"""検証専用: /api/cookies/update の merge 挙動を実HTTPで確認する。"""
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / ".." / "app"))

BASE_DIR = "/tmp/xmerge"
os.environ["DB_PATH"] = f"{BASE_DIR}/data.db"
os.environ["IMAGES_DIR"] = f"{BASE_DIR}/images"
os.environ["CACHE_DIR"] = f"{BASE_DIR}/cache"
os.environ["TWITTER_COOKIES_FILE"] = f"{BASE_DIR}/cookies.txt"
os.environ["ACCOUNTS_JSON_PATH"] = f"{BASE_DIR}/accounts.json"
os.makedirs(BASE_DIR, exist_ok=True)
COOKIES = Path(os.environ["TWITTER_COOKIES_FILE"])

import db
db.init_db()
from web import app
from werkzeug.serving import make_server

srv = make_server("127.0.0.1", 5198, app)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(1)

fails = []


def check(label, cond, detail=""):
    print(f"{'OK ' if cond else 'NG '} {label}{('  ' + detail) if detail else ''}")
    if not cond:
        fails.append(label)


def post(text, merge=None):
    data = {"cookies_text": text}
    if merge is not None:
        data["merge"] = merge
    req = Request("http://127.0.0.1:5198/api/cookies/update",
                  data=urlencode(data).encode(), method="POST")
    try:
        with urlopen(req, timeout=10) as r:
            import json
            return r.status, json.load(r)
    except Exception as e:
        body = e.read().decode() if hasattr(e, "read") else str(e)
        return getattr(e, "code", 0), body


def read_users():
    if not COOKIES.exists():
        return {}
    out = {}
    for line in COOKIES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "\t" in line:
            u, v = line.split("\t", 1)
            out[u] = v
    return out


def line(u, tag):
    return f"{u}\tauth_token={tag}; ct0={tag}"


print("== 全垢投入（merge なし = 従来動作）==")
full = "\n".join(line(f"u{i}", f"v1_{i}") for i in range(1, 8))
st, res = post(full)
check("HTTP 200", st == 200, str(res))
check("7垢書かれた", len(read_users()) == 7, f"{len(read_users())}垢")

print("\n== 3垢だけ merge=1 で送る ==")
partial = "\n".join(line(f"u{i}", f"v2_{i}") for i in (1, 3, 5))
st, res = post(partial, merge="1")
users = read_users()
check("HTTP 200", st == 200, str(res))
check("7垢すべて残っている", len(users) == 7, f"{len(users)}垢")
check("送った3垢は更新された", users["u3"] == "auth_token=v2_3; ct0=v2_3", users.get("u3", ""))
check("残り4垢は前の値のまま", users["u4"] == "auth_token=v1_4; ct0=v1_4", users.get("u4", ""))
check("updated=3 を返す", res.get("updated") == 3, str(res))
check("kept=4 を返す", res.get("kept") == 4, str(res))
check("merged=true を返す", res.get("merged") is True, str(res))

print("\n== 同じ3垢を merge なしで送ると置き換わる（従来動作の維持）==")
st, res = post(partial)
users = read_users()
check("3垢に減る", len(users) == 3, f"{len(users)}垢")
check("merged=false", res.get("merged") is False, str(res))

print("\n== merge で新規垢を追加できる ==")
post(full)  # 7垢に戻す
st, res = post(line("u99", "new"), merge="1")
users = read_users()
check("8垢になる", len(users) == 8, f"{len(users)}垢")
check("新規垢が入る", users.get("u99") == "auth_token=new; ct0=new")
check("既存が壊れない", users.get("u1") == "auth_token=v1_1; ct0=v1_1")

print("\n== 並び順の維持 ==")
order = [l.split("\t")[0] for l in COOKIES.read_text().splitlines()
         if l.strip() and not l.startswith("#")]
check("既存の並び順を保つ", order[:7] == [f"u{i}" for i in range(1, 8)], str(order))
check("新規は末尾", order[-1] == "u99", str(order[-1]))

print("\n== 異常系 ==")
st, res = post("# コメントだけ", merge="1")
check("不正な中身は400", st == 400, str(res)[:60])
check("400時にファイルが壊れない", len(read_users()) == 8, f"{len(read_users())}垢")

st, res = post(line("u1", "v3"), merge="true")
check("merge=true も効く", res.get("merged") is True and len(read_users()) == 8, str(res))
st, res = post(line("u1", "v4"), merge="0")
check("merge=0 は置き換え", len(read_users()) == 1, f"{len(read_users())}垢")

print("\n== パーミッション ==")
mode = oct(COOKIES.stat().st_mode)[-3:]
check("cookies.txt は 600", mode == "600", mode)

srv.shutdown()
print("\n" + ("=== 全て通過 ===" if not fails else f"=== 失敗 {len(fails)}件: {fails} ==="))
sys.exit(1 if fails else 0)
