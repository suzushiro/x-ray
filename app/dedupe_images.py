"""
保存済み画像の重複をハードリンクで排除する（リポスト・引用RT由来の同一画像など）。

仕組み:
  - /data/images と /data/cache の全ファイルを SHA-256 でグルーピング
  - 同一ハッシュのファイル群を「代表1ファイル」の実データにハードリンクで寄せる
  - DBのパスもファイル名も変えないので、表示・配信は一切影響なし
  - 代表は「永続(/data/images)にあるもの」を優先（キャッシュ削除で実体が消えないように）

使い方:
    docker exec -it x-ray-worker python dedupe_images.py --dry-run
    docker exec -it x-ray-worker python dedupe_images.py
"""

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cache_utils as cu
from db import get_conn

DRY_RUN = "--dry-run" in sys.argv


def fmt(n):
    for u in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def collect():
    """(sha256 -> [(path, stat), ...]) を作る。サイズが同じものだけハッシュ計算。"""
    by_size = defaultdict(list)
    for d in (cu.IMAGES_DIR, cu.CACHE_DIR):
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            fp = os.path.join(d, fn)
            if not os.path.isfile(fp):
                continue
            try:
                st = os.stat(fp)
            except Exception:
                continue
            by_size[st.st_size].append((fp, st))

    by_hash = defaultdict(list)
    for size, items in by_size.items():
        if len(items) < 2:
            continue  # サイズが唯一 → 重複なし。ハッシュ計算をスキップ
        for fp, st in items:
            try:
                by_hash[cu.file_sha256(fp)].append((fp, st))
            except Exception as e:
                print(f"[!] ハッシュ失敗 {fp}: {e}")
    return by_hash


def main():
    print(f"[*] スキャン: {cu.IMAGES_DIR}, {cu.CACHE_DIR}")
    before_p, cnt_p = cu.dir_stats(cu.IMAGES_DIR)
    before_c, cnt_c = cu.dir_stats(cu.CACHE_DIR)
    print(f"    永続 {cnt_p}件 / キャッシュ {cnt_c}件")

    by_hash = collect()
    dup_groups = {h: v for h, v in by_hash.items() if len(v) > 1}
    print(f"[*] 重複グループ: {len(dup_groups)}")

    linked = 0
    saved = 0
    skipped = 0

    for h, items in dup_groups.items():
        # 代表を選ぶ: 永続にあるもの優先、次に古いもの
        items.sort(key=lambda x: (0 if x[0].startswith(cu.IMAGES_DIR) else 1, x[1].st_mtime))
        canon_path, canon_st = items[0]

        for fp, st in items[1:]:
            if st.st_ino == canon_st.st_ino:
                skipped += 1  # 既に同一inode = リンク済み
                continue
            if DRY_RUN:
                linked += 1
                saved += st.st_size
                continue
            try:
                tmp = fp + ".dedupe-tmp"
                os.link(canon_path, tmp)
                os.replace(tmp, fp)  # アトミックに差し替え
                linked += 1
                saved += st.st_size
            except OSError as e:
                print(f"[!] リンク失敗 {os.path.basename(fp)}: {e}")
                if os.path.exists(fp + ".dedupe-tmp"):
                    os.remove(fp + ".dedupe-tmp")

    tag = "[DRY-RUN] " if DRY_RUN else ""
    print(f"\n{tag}ハードリンク化 : {linked} ファイル")
    print(f"{tag}リンク済みスキップ: {skipped} ファイル")
    print(f"{tag}解放される実データ: {fmt(saved)}")

    if not DRY_RUN and linked:
        after_p, _ = cu.dir_stats(cu.IMAGES_DIR)
        after_c, _ = cu.dir_stats(cu.CACHE_DIR)
        print(f"\n実データ量: {fmt(before_p + before_c)} → {fmt(after_p + after_c)}")
    if DRY_RUN:
        print("\n実行するには --dry-run を外してください。")


if __name__ == "__main__":
    main()
