"""
IG-Ray から引き継ぐ一度だけの移行処理。

IG-Ray は撤収済み（RETIRE_IG_RAY.md）。ここにあるのは
「向こうに残っていたものをこちらへ持ってくる」処理で、
**一度だけ**走る。web.py のルートとは関係が無いので分けてある。
"""

from datetime import datetime, timezone

from db import get_conn
import ig_bridge

def import_ig_presets():
    """
    ig-ray 側に覚えてあったキャプション／タグを、**一度だけ** X-Ray に取り込む。

    以前は /api/tumblr/presets が毎回 ig-ray の DB を読んで穴埋めしていたが、
    その作りには2つ問題がある:
      - X-Ray 側で消したプリセットが、ig-ray 側に残っていると次回復活する
      - ig-ray の DB を外した日に、覚えていた内容がまとめて消える
    ig-ray はもうメンテしないので、取り込みは一度で終わらせ、
    以後は X-Ray 側の tumblr_presets だけを見る。

    取り込み済みの印は app_meta に残す。印があれば ig-ray の DB が
    繋がっていても二度と読まない（消したものが戻らないように）。
    """
    conn = get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_meta (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()
        done = conn.execute(
            "SELECT 1 FROM app_meta WHERE key='ig_presets_imported'").fetchone()
        if done:
            return 0

        if not ig_bridge.attach(conn):
            # IGが繋がっていない。印は付けない（繋いだ日に取り込めるように）。
            return 0

        rows = []
        try:
            rows = conn.execute(
                f"SELECT owner_username, caption, tags "
                f"FROM {ig_bridge.SCHEMA}.tumblr_presets").fetchall()
        except Exception:
            # 古い ig-ray にはこのテーブルが無い。取り込むものが無いのと同じ
            # なので、印を付けて以後は見ない。
            rows = []
    finally:
        conn.close()

    # attach したコネクションは query_only なので書き込み用に開き直す。
    conn = get_conn()
    try:
        n = 0
        for r in rows:
            key = ig_bridge.ID_PREFIX + r["owner_username"]
            cur = conn.execute("""
                INSERT INTO tumblr_presets (screen_name, caption, tags, updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT(screen_name) DO NOTHING
            """, (key, r["caption"] or "", r["tags"] or "",
                  datetime.now(timezone.utc).isoformat()))
            n += cur.rowcount or 0
        conn.execute("""
            INSERT INTO app_meta (key, value, updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value, updated_at=excluded.updated_at
        """, ("ig_presets_imported", str(n),
              datetime.now(timezone.utc).isoformat()))
        conn.commit()
        if n:
            print(f"[*] ig-ray のTumblrプリセットを {n} 件取り込みました")
        return n
    finally:
        conn.close()
