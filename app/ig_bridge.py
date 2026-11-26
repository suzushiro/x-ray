"""
IG-Ray のアーカイブを X-Ray から読むための橋渡し。

X-Ray は IG のデータを**読むだけ**。取得（instaloader / cron / backfill）は
ig-ray 側に残っていて、このモジュールはそれが書いたDBを覗くだけの立場にある。

## なぜ読み取り専用マウントにしないのか

直感に反するが、ig-ray の data/ は**書き込み可**でマウントする必要がある。

SQLite の WAL モードでは、読むだけでも `-shm`（共有メモリ索引）が要る。
ig-ray が `-wal` を残したまま落ちて `-shm` が消えている状態だと、
読み取り専用マウントでは -shm を作れず **DBを開くことすらできない**。
X-Ray のタイムラインごと巻き添えで死ぬ。

`immutable=1` を付ければ開けるが、これは「このファイルは一生変化しない」と
SQLite に嘘をつく指定で、ig-ray が動き出した瞬間に古い値や壊れた読みを
返しうる。アーカイブ閲覧のために正しさを捨てる取引は割に合わない。

そこで**書き込み可でマウントし、`PRAGMA query_only = 1` で守る**。
この指定があると書き込みは `attempt to write a readonly database` で
確実に弾かれる。マウント権限ではなく SQLite 側で担保する形。

## スキーマの追従について

ig-ray は別リポジトリとして進化し続ける。こちらが知らない列が増えるのは
当然なので、**存在に依存する列は実行時に確認してから使う**。
ig-ray 側の変更でこちらが落ちるのが一番避けたい事故なので、
読めなければ「IGは出ない」に縮退して X-Ray 本体は動かす。
"""

import json
import os
import sqlite3

# ig-ray の DB とメディア置き場。未設定なら統合そのものを無効にする。
IG_DB_PATH = os.environ.get("IG_DB_PATH", "").strip()
IG_CACHE_DIR = os.environ.get("IG_CACHE_DIR", "").strip()

# ig-ray 側が巡回中だとロックを握っていることがあるので少し待つ
IG_BUSY_TIMEOUT_MS = int(os.environ.get("IG_BUSY_TIMEOUT_MS", "5000") or 5000)

# ATTACH したときのスキーマ名
SCHEMA = "ig"

# 投稿URLの組み立て。ig-ray 側と同じ形。
POST_URL_PREFIX = "https://www.instagram.com/p/"

# X-Ray の tweet_id と混ざらないようにする接頭辞。
# 表示だけでなく、書き込み系のルートが誤って IG を掴まないための目印でもある。
ID_PREFIX = "ig:"


def configured():
    """統合が有効に設定されているか（ファイルの実在は見ない）"""
    return bool(IG_DB_PATH)


def available():
    """実際に読める状態か。設定済みでもファイルが無ければ False。"""
    return bool(IG_DB_PATH) and os.path.exists(IG_DB_PATH)


def is_ig_id(tweet_id):
    return bool(tweet_id) and str(tweet_id).startswith(ID_PREFIX)


def shortcode_of(tweet_id):
    return str(tweet_id)[len(ID_PREFIX):] if is_ig_id(tweet_id) else None


def attach(conn):
    """
    既存のコネクションに ig-ray のDBを繋ぐ。

    **このコネクションは読み取り専用になる**（query_only を立てるため）。
    X-Ray 側に書き込むコネクションに対して呼んではいけない。

    成功したら True。失敗しても例外は投げない（IGが読めないだけで
    X-Ray のタイムラインは出したい）。
    """
    if not available():
        return False
    try:
        conn.execute(f"PRAGMA busy_timeout = {IG_BUSY_TIMEOUT_MS}")
        conn.execute("ATTACH DATABASE ? AS " + SCHEMA, (IG_DB_PATH,))
        # ATTACH の後に立てる。先に立てると -shm を作れず WAL が開けない。
        conn.execute("PRAGMA query_only = 1")
        return True
    except Exception as e:
        print(f"[!] IGアーカイブに接続できません: {type(e).__name__}: {e}")
        return False


def _columns(conn, table):
    try:
        return {r[1] for r in conn.execute(
            f"PRAGMA {SCHEMA}.table_info({table})")}
    except Exception:
        return set()


def hidden_owner_sql(conn, column="p.owner_username"):
    """
    「監視対象以外 ＋ ミュート」を除外する WHERE 断片。ig-ray 本体と同じ規則。

    共同投稿では owner_username が相手側になるため、監視対象で絞らないと
    登録していないアカウントの投稿が並ぶ。ig-ray 側で一度踏んだ問題なので
    こちらでも最初から絞る。

    **監視対象が0件のときは絞らない。** 空集合と「列が無い」を混同すると
    投稿が全部消えて障害に見える。
    """
    cols = _columns(conn, "accounts")
    clauses, params = [], []

    if "is_target" in cols:
        targets = [r[0] for r in conn.execute(
            f"SELECT username FROM {SCHEMA}.accounts "
            "WHERE COALESCE(is_target, 0) = 1")]
        if targets:
            clauses.append(f"{column} IN ({','.join('?' * len(targets))})")
            params.extend(sorted(targets))

    if "is_muted" in cols:
        muted = [r[0] for r in conn.execute(
            f"SELECT username FROM {SCHEMA}.accounts "
            "WHERE COALESCE(is_muted, 0) = 1")]
        if muted:
            clauses.append(f"{column} NOT IN ({','.join('?' * len(muted))})")
            params.extend(sorted(muted))

    return (" AND ".join(clauses), params)


def select_sql(conn, category="all", show_r18=False, require_media=False):
    """
    IG の投稿を **X-Ray の tweets JOIN accounts と同じ列並び**で返す SELECT。

    タイムラインで X の投稿と UNION ALL するためのもので、列の順序と名前は
    呼び出し側（web.py の X 側 SELECT）と1対1で対応させること。

    メディアはここでは組み立てない。`media_json` は ig-ray の dict 配列の
    ままで、ローカル画像は media_index にあるので、ページ分の行が確定して
    から `attach_media()` でまとめて解決する（1行ずつ引くと N+1 になる）。
    """
    hide_sql, hide_params = hidden_owner_sql(conn)
    clauses = [hide_sql] if hide_sql else []
    params = list(hide_params)

    # カテゴリの絞り込みは X-Ray 側と同じ規則にする。
    # ig-ray の categories_json も同じ「JSON配列の文字列」なので LIKE が効く。
    if category == "all":
        if not show_r18:
            clauses.append("COALESCE(a.categories_json,'') NOT LIKE '%\"R18\"%'")
    else:
        clauses.append("a.categories_json LIKE ?")
        params.append(f'%"{category}"%')

    if require_media:
        clauses.append("p.media_json IS NOT NULL AND p.media_json != '[]'")

    where = ("WHERE " + " AND ".join(clauses) + " ") if clauses else ""
    sql = f"""
        SELECT
            'ig'                                  AS source,
            '{ID_PREFIX}' || p.shortcode          AS tweet_id,
            p.owner_username                      AS screen_name,
            a.full_name                           AS display_name,
            p.caption                             AS content,
            p.date_utc                            AS created_at,
            '{POST_URL_PREFIX}' || p.shortcode || '/' AS url,
            p.likes                               AS like_count,
            0                                     AS retweet_count,
            p.comments                            AS reply_count,
            p.media_json                          AS media_json,
            NULL                                  AS video_json,
            NULL                                  AS local_media_json,
            a.categories_json                     AS categories,
            a.profile_pic_local                   AS profile_image_url,
            NULL                                  AS reply_to_tweet_id,
            -- IGに引用RTの概念は無い。列数と並びをX側に合わせるためのNULL。
            NULL                                  AS quoted_json
        FROM {SCHEMA}.posts p
        LEFT JOIN {SCHEMA}.accounts a ON p.owner_username = a.username
        {where}
    """
    return sql, params


def cache_url(local_path):
    """
    ig-ray の local_path を X-Ray が配信できるURLに直す。

    ig-ray 自身も `/cache/` で配っているが、**X-Ray の `/cache/` と衝突する**
    ので、こちらでは `/ig-cache/` に載せ替える。
    キャッシュ外を指すパスは配信しない（ディレクトリトラバーサル防止）。
    """
    if not local_path or not IG_CACHE_DIR:
        return None
    try:
        rel = os.path.relpath(local_path, IG_CACHE_DIR)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return "/ig-cache/" + rel.replace(os.sep, "/")


def attach_media(conn, rows):
    """
    IG の行に media / local_media を埋める。

    X-Ray の `format_tweet()` は「media＝リモートURLの配列」「local_media＝
    同じ添字のローカルパス配列」を前提にしているので、ig-ray の
    dict 配列（{index, image_url, is_video, video_url}）と media_index を
    その形に畳んでから渡す。

    rows は dict のリスト（source == 'ig' のものだけ処理する）。
    ページ分をまとめて1回のクエリで引く。
    """
    targets = [r for r in rows if r.get("source") == "ig"]
    if not targets:
        return rows

    codes = [shortcode_of(r["tweet_id"]) for r in targets]
    local_by_code = {}
    try:
        qs = ",".join("?" * len(codes))
        for r in conn.execute(
            f"SELECT shortcode, media_index, local_path FROM {SCHEMA}.media_index "
            f"WHERE shortcode IN ({qs})", codes
        ):
            if r["local_path"]:
                local_by_code.setdefault(r["shortcode"], {})[r["media_index"]] = \
                    r["local_path"]
    except Exception as e:
        print(f"[!] IGのメディア解決に失敗: {type(e).__name__}: {e}")

    for row in targets:
        code = shortcode_of(row["tweet_id"])
        try:
            media = json.loads(row.get("media_json") or "[]")
        except (TypeError, ValueError):
            media = []

        remotes, locals_ = [], []
        by_index = local_by_code.get(code, {})
        for i, m in enumerate(media):
            if not isinstance(m, dict):
                continue
            idx = m.get("index", i)
            remote = m.get("image_url")
            local = cache_url(by_index.get(idx))
            # ローカルが無くリモートURLも無いものは出しようがないので落とす。
            # ただしローカルがあるならリモートが失効していても表示できるので、
            # remote が空でもプレースホルダを置いて添字を合わせる。
            if not remote and not local:
                continue
            remotes.append(remote or "")
            locals_.append(local)

        row["media_json"] = json.dumps(remotes, ensure_ascii=False)
        row["local_media_json"] = json.dumps(locals_, ensure_ascii=False)

    return rows


def stats(conn):
    """ストレージ画面などに出す件数。読めなければ None。"""
    try:
        hide_sql, params = hidden_owner_sql(conn)
        where = f"WHERE {hide_sql} " if hide_sql else ""
        posts = conn.execute(
            f"SELECT COUNT(*) FROM {SCHEMA}.posts p {where}", params).fetchone()[0]
        owners = conn.execute(
            f"SELECT COUNT(DISTINCT p.owner_username) FROM {SCHEMA}.posts p {where}",
            params).fetchone()[0]
        return {"posts": posts, "accounts": owners}
    except Exception:
        return None
