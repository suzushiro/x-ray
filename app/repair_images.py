"""
DBのローカル画像パスと実ファイルの不整合を修復する。

  1. 実在するファイルを探して正しいパス(/images/ or /cache/)に貼り直す
  2. どこにも無いものは、リモートURL(media_json)から現在のルールで再DL
  3. 再DLも失敗したら null にする（表示はX側のリモート画像へフォールバック）

使い方:
    docker exec -it x-ray-worker python repair_images.py --dry-run   # 確認だけ
    docker exec -it x-ray-worker python repair_images.py             # 実行
    docker exec -it x-ray-worker python repair_images.py --no-download  # 再DLせずnull化のみ
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import get_conn
import cache_utils as cu
from scraper import download_image

DRY_RUN = "--dry-run" in sys.argv
NO_DOWNLOAD = "--no-download" in sys.argv


def main():
    conn = get_conn()

    # アカウント→永続判定のマップ
    persist_map = {}
    for r in conn.execute("SELECT screen_name, categories FROM accounts"):
        persist_map[r["screen_name"]] = cu.should_persist(r["categories"])

    rows = conn.execute("""
        SELECT tweet_id, screen_name, media_json, local_media_json
        FROM tweets
        WHERE local_media_json IS NOT NULL
          AND local_media_json NOT IN ('', '[]')
    """).fetchall()

    n_rows = 0
    n_relink = 0
    n_redl = 0
    n_null = 0

    for r in rows:
        try:
            paths = json.loads(r["local_media_json"] or "[]")
            remotes = json.loads(r["media_json"] or "[]")
        except Exception:
            continue

        screen_name = r["screen_name"]
        tweet_id = r["tweet_id"]
        persist = persist_map.get(screen_name, False)
        new_paths = list(paths)
        changed = False

        for i, p in enumerate(paths):
            if not p:
                continue
            real = cu.local_path_of(p)
            if real and os.path.exists(real):
                continue  # 正常

            # (1) もう片方のディレクトリに実在しないか探す
            found = cu.resolve(p)
            if found:
                new_paths[i] = found
                changed = True
                n_relink += 1
                continue

            # (2) リモートURLから再DL
            remote = remotes[i] if i < len(remotes) else None
            if remote and not NO_DOWNLOAD and not DRY_RUN:
                lp = download_image(remote, screen_name, tweet_id, i + 1, persist=persist)
                if lp:
                    new_paths[i] = lp
                    changed = True
                    n_redl += 1
                    time.sleep(0.2)  # レート制限に配慮
                    continue

            # (3) 諦めて null（表示はリモートURLへフォールバック）
            if DRY_RUN and remote and not NO_DOWNLOAD:
                n_redl += 1
                changed = True
                continue
            new_paths[i] = None
            changed = True
            n_null += 1

        if changed:
            n_rows += 1
            if not DRY_RUN:
                payload = json.dumps(new_paths, ensure_ascii=False)
                conn.execute(
                    "UPDATE tweets SET local_media_json=? WHERE tweet_id=?",
                    (payload, tweet_id),
                )
                conn.execute(
                    "UPDATE bookmarks SET local_media_json=? WHERE tweet_id=?",
                    (payload, tweet_id),
                )

    if not DRY_RUN:
        conn.commit()
    conn.close()

    tag = "[DRY-RUN] " if DRY_RUN else ""
    print(f"{tag}修復対象ツイート : {n_rows} 件")
    print(f"{tag}  パス貼り直し   : {n_relink} 枚")
    print(f"{tag}  再ダウンロード : {n_redl} 枚")
    print(f"{tag}  null化(リモート): {n_null} 枚")
    if DRY_RUN:
        print("\n実行するには --dry-run を外してください。")


if __name__ == "__main__":
    main()
