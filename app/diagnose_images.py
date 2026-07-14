"""
画像リンク切れの原因切り分け用。
    docker exec -it x-ray-web    python diagnose_images.py
    docker exec -it x-ray-worker python diagnose_images.py
両方で実行して差分を見ると原因が特定できる。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_conn
import cache_utils as cu

print("=" * 60)
print("[環境変数]")
print(f"  IMAGES_DIR         = {cu.IMAGES_DIR}")
print(f"  CACHE_DIR          = {cu.CACHE_DIR}")
print(f"  PERSIST_CATEGORIES = {cu.PERSIST_CATEGORIES}")
print(f"  SAVE_IMAGES        = {os.environ.get('SAVE_IMAGES', '(未設定→1扱い)')}")
print(f"  whoami(uid/gid)    = {os.getuid()}/{os.getgid()}")

print("\n[ディレクトリ]")
for d in (cu.IMAGES_DIR, cu.CACHE_DIR):
    if not os.path.isdir(d):
        print(f"  {d}: ✗ 存在しない")
        continue
    st = os.stat(d)
    size, count = cu.dir_stats(d)
    print(f"  {d}: ✓ {count}ファイル / {size/1024/1024:.1f}MB "
          f"mode={oct(st.st_mode)[-3:]} owner={st.st_uid}:{st.st_gid} "
          f"read={os.access(d, os.R_OK)} write={os.access(d, os.W_OK)}")

print("\n[DBのローカルパス vs 実ファイル]")
conn = get_conn()
rows = conn.execute(
    "SELECT tweet_id, screen_name, local_media_json FROM tweets "
    "WHERE local_media_json IS NOT NULL AND local_media_json NOT IN ('', '[]') "
    "ORDER BY created_at DESC LIMIT 300"
).fetchall()
conn.close()

stats = {"images_ok": 0, "images_missing": 0, "cache_ok": 0, "cache_missing": 0, "null": 0}
missing_samples = []

for r in rows:
    try:
        paths = json.loads(r["local_media_json"] or "[]")
    except Exception:
        continue
    for p in paths:
        if not p:
            stats["null"] += 1
            continue
        real = cu.local_path_of(p)
        exists = bool(real and os.path.exists(real))
        key = ("images" if p.startswith("/images/") else "cache") + ("_ok" if exists else "_missing")
        stats[key] = stats.get(key, 0) + 1
        if not exists and len(missing_samples) < 10:
            missing_samples.append((r["screen_name"], p, real))

print(f"  /images/ 実在: {stats['images_ok']}  欠落: {stats['images_missing']}")
print(f"  /cache/  実在: {stats['cache_ok']}  欠落: {stats['cache_missing']}")
print(f"  null(リモートURLへフォールバック): {stats['null']}")

if missing_samples:
    print("\n[欠落サンプル] ← これが表示中のリンク切れ")
    for sn, p, real in missing_samples:
        print(f"  @{sn:20} {p}  (実パス: {real})")
else:
    print("\n  → DBが指すファイルは全部実在。原因はFlaskのルーティング/権限側。")

print("\n[ルート確認（webコンテナのみ有効）]")
try:
    import web
    routes = sorted(str(r) for r in web.app.url_map.iter_rules())
    for r in routes:
        if "cache" in r or "images" in r:
            print(f"  {r}")
    if not any("/cache/" in r for r in routes):
        print("  ✗ /cache/<path> ルートが無い → webコンテナが古いイメージのまま！")
except Exception as e:
    print(f"  (webモジュール未使用: {e})")
print("=" * 60)
