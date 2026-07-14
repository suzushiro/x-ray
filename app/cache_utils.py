"""
画像キャッシュ / 永続保存の共通ユーティリティ。

ディレクトリ構成:
    /data/cache/   ... 表示用キャッシュ（全画像・削除OK）      URL: /cache/xxx
    /data/images/  ... 永続保存（PERSIST_CATEGORIES + ブックマーク）URL: /images/xxx

ファイル名は両ディレクトリで共通（screen_name_tweetid_idx.ext）のため、
「昇格」は単なる shutil.move + DBのパス prefix 書き換えで済む。
"""

import hashlib
import json
import os
import shutil
import time

from db import get_conn

IMAGES_DIR = os.environ.get("IMAGES_DIR", "/data/images")
CACHE_DIR = os.environ.get("CACHE_DIR", "/data/cache")
CACHE_RETENTION_DAYS = int(os.environ.get("CACHE_RETENTION_DAYS", "30"))

PERSIST_CATEGORIES = [
    c.strip()
    for c in os.environ.get(
        "PERSIST_CATEGORIES", "ギャル,illustrator,photographer,gadget"
    ).split(",")
    if c.strip()
]

CACHE_PREFIX = "/cache/"
PERSIST_PREFIX = "/images/"


def link_or_copy(src: str, dst: str) -> bool:
    """
    src の実データを dst で共有する（ハードリンク）。
    同一inodeなので追加のディスク消費はゼロ。
    別デバイス等でリンクできない場合はコピーにフォールバック。
    """
    if os.path.exists(dst):
        return True
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
        return True
    except OSError:
        try:
            shutil.copy2(src, dst)
            return True
        except Exception as e:
            print(f"[!] リンク/コピー失敗 {src} -> {dst}: {e}")
            return False


def file_sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def should_persist(categories) -> bool:
    """アカウントのカテゴリ一覧が永続対象かどうか"""
    if isinstance(categories, str):
        try:
            categories = json.loads(categories or "[]")
        except Exception:
            categories = []
    return any(c in PERSIST_CATEGORIES for c in (categories or []))


def local_path_of(url_path: str) -> str | None:
    """URLパス（/cache/xxx, /images/xxx）を実ファイルパスに変換"""
    if not url_path:
        return None
    if url_path.startswith(CACHE_PREFIX):
        return os.path.join(CACHE_DIR, os.path.basename(url_path))
    if url_path.startswith(PERSIST_PREFIX):
        return os.path.join(IMAGES_DIR, os.path.basename(url_path))
    return None


def resolve(url_path: str) -> str | None:
    """
    実際に存在する場所を返す（永続優先）。
    どちらにも無ければ None（呼び出し側でリモートURLにフォールバック）。
    """
    if not url_path:
        return None
    fn = os.path.basename(url_path)
    if os.path.exists(os.path.join(IMAGES_DIR, fn)):
        return PERSIST_PREFIX + fn
    if os.path.exists(os.path.join(CACHE_DIR, fn)):
        return CACHE_PREFIX + fn
    return None


def promote(url_paths: list) -> tuple[list, int]:
    """
    /cache/ にある画像を /images/ へ昇格（移動）する。
    戻り値: (書き換え後のパス配列, 昇格件数)
    既に /images/ のもの、None のものはそのまま。
    """
    os.makedirs(IMAGES_DIR, exist_ok=True)
    out = []
    moved = 0
    for p in url_paths or []:
        if not p or not p.startswith(CACHE_PREFIX):
            out.append(p)
            continue
        fn = os.path.basename(p)
        src = os.path.join(CACHE_DIR, fn)
        dst = os.path.join(IMAGES_DIR, fn)
        try:
            if os.path.exists(dst):
                # 既に永続にある（重複DL済み等）→ キャッシュ側を掃除
                if os.path.exists(src):
                    os.remove(src)
                out.append(PERSIST_PREFIX + fn)
                moved += 1
                continue
            if os.path.exists(src):
                shutil.move(src, dst)
                out.append(PERSIST_PREFIX + fn)
                moved += 1
                continue
        except Exception as e:
            print(f"[!] 昇格失敗 {fn}: {e}")
        # ファイルが無い（キャッシュ削除済み）→ Noneにしてリモートへフォールバック
        out.append(None)
    return out, moved


def promote_tweet(tweet_id: str, conn=None) -> int:
    """
    指定ツイートの画像をキャッシュ→永続へ昇格し、tweets/bookmarks 両方のパスを更新。
    戻り値: 昇格した画像枚数
    """
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        row = conn.execute(
            "SELECT local_media_json FROM tweets WHERE tweet_id=?", (tweet_id,)
        ).fetchone()
        if not row:
            return 0
        try:
            paths = json.loads(row["local_media_json"] or "[]")
        except Exception:
            return 0
        if not any(p and p.startswith(CACHE_PREFIX) for p in paths):
            return 0

        new_paths, moved = promote(paths)
        payload = json.dumps(new_paths, ensure_ascii=False)
        conn.execute(
            "UPDATE tweets SET local_media_json=? WHERE tweet_id=?",
            (payload, tweet_id),
        )
        conn.execute(
            "UPDATE bookmarks SET local_media_json=? WHERE tweet_id=?",
            (payload, tweet_id),
        )
        conn.commit()
        return moved
    finally:
        if own_conn:
            conn.close()


def protected_filenames() -> set:
    """
    ブックマーク由来で消してはいけないファイル名。
    通常は昇格済みで /images/ 側にあるはずだが、昇格漏れ対策の保険。
    """
    names = set()
    conn = get_conn()
    try:
        for r in conn.execute("SELECT local_media_json FROM bookmarks"):
            try:
                for p in json.loads(r["local_media_json"] or "[]"):
                    if p:
                        names.add(os.path.basename(p))
            except Exception:
                pass
    except Exception:
        pass
    finally:
        conn.close()
    return names


def sync_db_paths() -> int:
    """
    DB内の /cache/ パスを実ファイルの所在に合わせて修正する。
      - 永続に移動済み  → /images/xxx に書き換え
      - どこにも無い    → None（表示時にリモートURLへフォールバック）
    戻り値: 更新した行数
    """
    conn = get_conn()
    updated = 0
    try:
        rows = conn.execute(
            "SELECT tweet_id, local_media_json FROM tweets "
            "WHERE local_media_json LIKE '%/cache/%'"
        ).fetchall()
        for r in rows:
            try:
                paths = json.loads(r["local_media_json"] or "[]")
            except Exception:
                continue
            new_paths = [
                (resolve(p) if p and p.startswith(CACHE_PREFIX) else p) for p in paths
            ]
            if new_paths != paths:
                payload = json.dumps(new_paths, ensure_ascii=False)
                conn.execute(
                    "UPDATE tweets SET local_media_json=? WHERE tweet_id=?",
                    (payload, r["tweet_id"]),
                )
                conn.execute(
                    "UPDATE bookmarks SET local_media_json=? WHERE tweet_id=?",
                    (payload, r["tweet_id"]),
                )
                updated += 1
        conn.commit()
    finally:
        conn.close()
    return updated


def cleanup_cache(days: int | None = None, dry_run: bool = False) -> dict:
    """
    キャッシュを削除する。
      days=None → CACHE_RETENTION_DAYS（既定30日）より古いものを削除
      days=0    → 全削除
    ブックマーク参照中のファイルは保護する。
    削除後、DBのパスを同期して壊れたリンクを消す。
    """
    days = CACHE_RETENTION_DAYS if days is None else int(days)
    cutoff = time.time() - days * 86400 if days > 0 else None
    protected = protected_filenames()

    deleted = 0
    freed = 0
    kept = 0

    if os.path.isdir(CACHE_DIR):
        for fn in os.listdir(CACHE_DIR):
            fp = os.path.join(CACHE_DIR, fn)
            if not os.path.isfile(fp):
                continue
            try:
                st = os.stat(fp)
            except Exception:
                continue
            if fn in protected:
                kept += 1
                continue
            if cutoff is not None and st.st_mtime >= cutoff:
                kept += 1
                continue
            # ハードリンク共有中(nlink>1)のファイルは、消しても実データは解放されない
            real_freed = st.st_size if st.st_nlink <= 1 else 0
            if dry_run:
                deleted += 1
                freed += real_freed
                continue
            try:
                os.remove(fp)
                deleted += 1
                freed += real_freed
            except Exception as e:
                print(f"[!] キャッシュ削除失敗 {fn}: {e}")

    synced = 0
    if deleted and not dry_run:
        synced = sync_db_paths()

    return {
        "deleted": deleted,
        "freed": freed,
        "kept": kept,
        "synced": synced,
        "days": days,
        "dry_run": dry_run,
    }


def dir_stats(path: str, seen_inodes: set | None = None) -> tuple[int, int]:
    """
    (合計バイト数, ファイル数)
    ハードリンクで重複排除した実データ量を返すため、同一inodeは1回だけ数える。
    ファイル数は見た目通り（リンクも1件と数える）。
    """
    total = 0
    count = 0
    seen = seen_inodes if seen_inodes is not None else set()
    if os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    st = os.stat(os.path.join(root, f))
                    count += 1
                    if st.st_ino in seen:
                        continue  # ハードリンク済み → 実データは既に計上済み
                    seen.add(st.st_ino)
                    total += st.st_size
                except Exception:
                    pass
    return total, count


if __name__ == "__main__":
    import sys

    d = None
    if len(sys.argv) > 1:
        d = int(sys.argv[1])
    res = cleanup_cache(days=d)
    mb = res["freed"] / 1024 / 1024
    print(
        f"[+] キャッシュ削除: {res['deleted']}件 / {mb:.1f}MB 解放 "
        f"(保持 {res['kept']}件, DB同期 {res['synced']}行, 閾値 {res['days']}日)"
    )
