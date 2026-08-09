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
from datetime import datetime, timezone

from db import get_conn

IMAGES_DIR = os.environ.get("IMAGES_DIR", "/data/images")
CACHE_DIR = os.environ.get("CACHE_DIR", "/data/cache")
CACHE_RETENTION_DAYS = int(os.environ.get("CACHE_RETENTION_DAYS", "30"))

PERSIST_CATEGORIES = [
    c.strip()
    for c in os.environ.get(
        "PERSIST_CATEGORIES", ""
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


# ---------------------------------------------------------------- 手動削除

def is_deleted(conn, remote_url: str) -> bool:
    """その画像URLが手動削除済みか"""
    if not remote_url:
        return False
    row = conn.execute(
        "SELECT 1 FROM deleted_media WHERE remote_url=?", (remote_url,)
    ).fetchone()
    return row is not None


def deleted_urls(conn) -> set:
    """手動削除済みの画像URL一覧。表示側でまとめて弾くのに使う。"""
    try:
        return {r["remote_url"] for r in conn.execute(
            "SELECT remote_url FROM deleted_media")}
    except Exception:
        return set()


def _filename_refcount(conn, filename: str, skip_url: str) -> int:
    """
    そのファイル名を参照している他の画像URLの数を数える。

    dedupe（ハードリンク）で複数URLが同じ実体を指しうるので、
    参照が残っているうちは実ファイルを消してはいけない。
    """
    n = 0
    try:
        for r in conn.execute(
                "SELECT remote_url, filename FROM media_index WHERE filename=?",
                (filename,)):
            if r["remote_url"] != skip_url:
                n += 1
    except Exception:
        pass
    return n


def _paths_referencing(conn, filename: str, skip_tweet_id: str | None,
                       skip_index: int | None) -> int:
    """
    tweets / bookmarks の local_media_json 内で、そのファイル名を指している
    参照の数。media_index に載っていない古いデータの保険。
    """
    n = 0
    for table in ("tweets", "bookmarks"):
        try:
            rows = conn.execute(
                f"SELECT tweet_id, local_media_json FROM {table} "
                f"WHERE local_media_json LIKE ?", (f"%{filename}%",)).fetchall()
        except Exception:
            continue
        for r in rows:
            try:
                paths = json.loads(r["local_media_json"] or "[]")
            except Exception:
                continue
            for i, p in enumerate(paths):
                if not p or os.path.basename(p) != filename:
                    continue
                # 今まさに消そうとしている当人はカウントしない
                if (table == "tweets" and r["tweet_id"] == skip_tweet_id
                        and i == skip_index):
                    continue
                if table == "bookmarks" and r["tweet_id"] == skip_tweet_id:
                    continue
                n += 1
    return n


def delete_media(conn, tweet_id: str, index: int) -> dict:
    """
    ツイートの index 番目の画像を削除する。

      1. 実ファイルを消す（他から参照されていない場合のみ）
      2. deleted_media に墓標を残す（再表示・再DLの抑止）
      3. tweets / bookmarks の local_media_json の該当箇所を None にする

    戻り値: {ok, error?, remote_url?, file_removed, still_referenced}
    """
    row = conn.execute(
        "SELECT tweet_id, screen_name, media_json, local_media_json "
        "FROM tweets WHERE tweet_id=?", (tweet_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "ツイートが見つかりません"}

    try:
        media = json.loads(row["media_json"] or "[]")
    except Exception:
        media = []
    if index < 0 or index >= len(media):
        return {"ok": False, "error": "画像インデックスが範囲外です"}

    remote_url = media[index]
    try:
        local = json.loads(row["local_media_json"] or "[]")
    except Exception:
        local = []
    local_path = local[index] if index < len(local) else None

    file_removed = False
    still_referenced = False

    if local_path:
        filename = os.path.basename(local_path)
        refs = (_filename_refcount(conn, filename, remote_url)
                + _paths_referencing(conn, filename, tweet_id, index))
        if refs > 0:
            # 他のポストが同じ実体を参照している → ファイルは残す
            still_referenced = True
        else:
            for d in (IMAGES_DIR, CACHE_DIR):
                fp = os.path.join(d, filename)
                try:
                    if os.path.exists(fp):
                        os.remove(fp)
                        file_removed = True
                except OSError as e:
                    print(f"[!] 画像削除失敗 {fp}: {e}")

    # 墓標。これが再表示と再DLの両方を止める
    conn.execute(
        "INSERT OR REPLACE INTO deleted_media (remote_url, tweet_id, screen_name, deleted_at)"
        " VALUES (?,?,?,?)",
        (remote_url, tweet_id, row["screen_name"],
         datetime.now(timezone.utc).isoformat()))

    # media_index からも外す（次回DL時に参照されないように）
    try:
        conn.execute("DELETE FROM media_index WHERE remote_url=?", (remote_url,))
    except Exception:
        pass

    _null_local_path(conn, "tweets", tweet_id, index)
    _null_local_path(conn, "bookmarks", tweet_id, index)

    conn.commit()
    return {"ok": True, "remote_url": remote_url,
            "file_removed": file_removed, "still_referenced": still_referenced}


def _null_local_path(conn, table: str, tweet_id: str, index: int) -> None:
    """local_media_json の index 番目を None にする"""
    try:
        row = conn.execute(
            f"SELECT local_media_json FROM {table} WHERE tweet_id=?",
            (tweet_id,)).fetchone()
        if not row:
            return
        paths = json.loads(row["local_media_json"] or "[]")
        if index < len(paths):
            paths[index] = None
            conn.execute(
                f"UPDATE {table} SET local_media_json=? WHERE tweet_id=?",
                (json.dumps(paths), tweet_id))
    except Exception as e:
        print(f"[!] {table}.local_media_json 更新失敗 {tweet_id}[{index}]: {e}")


def delete_all_media(conn, tweet_id: str) -> dict:
    """ツイートの画像を全部削除する"""
    row = conn.execute(
        "SELECT media_json FROM tweets WHERE tweet_id=?", (tweet_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "ツイートが見つかりません"}
    try:
        media = json.loads(row["media_json"] or "[]")
    except Exception:
        media = []

    deleted, removed = 0, 0
    for i in range(len(media)):
        r = delete_media(conn, tweet_id, i)
        if r.get("ok"):
            deleted += 1
            if r.get("file_removed"):
                removed += 1
    return {"ok": True, "deleted": deleted, "file_removed": removed}
