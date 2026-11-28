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
from datetime import datetime, timezone

# ig-ray の DB とメディア置き場。未設定なら統合そのものを無効にする。
_CONFIGURED_DB_PATH = os.environ.get("IG_DB_PATH", "").strip()
IG_CACHE_DIR = os.environ.get("IG_CACHE_DIR", "").strip()

# DBのファイル名の候補。ig-ray は v4.1 で ig_ray.db に改名したが、
# **既存DBを勝手に作り直さない**仕様で、新名が無く旧 insta_ray.db があれば
# そちらを使い続ける。つまり長く運用しているほど実体は旧名のままになる。
# 設定ミスではなく仕様なので、こちらが両方を見に行く。
_DB_NAMES = ("ig_ray.db", "insta_ray.db")


def _resolve_db_path(configured, cache_dir):
    """
    実際に読むDBのパスを決める。戻り値は (パス, 画面に出す理由)。

    パスが決まらなかったときだけ理由を返す。黙って無効になると
    「設定したのに出ない」の原因が画面から分からない。
    """
    if configured and os.path.isfile(configured):
        return configured, ""

    # 探す場所: 指定があればその隣、無ければキャッシュの親（＝マウント先）
    roots = []
    if configured:
        roots.append(configured if os.path.isdir(configured)
                     else os.path.dirname(configured))
    if cache_dir:
        roots.append(os.path.dirname(cache_dir.rstrip("/")))
    roots.append("/ig-data")

    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for name in _DB_NAMES:
            cand = os.path.join(root, name)
            if os.path.isfile(cand):
                if configured and cand != configured:
                    print(f"[*] IG: 指定の {configured} が無いので "
                          f"{cand} を使います")
                return cand, ""

    if configured:
        return "", (f"{configured} が見つかりません"
                    f"（{' / '.join(_DB_NAMES)} も探しましたが見当たりません）")
    return "", "IG_DB_PATH が未設定で、DBも自動で見つかりませんでした"


IG_DB_PATH, IG_DB_REASON = _resolve_db_path(_CONFIGURED_DB_PATH, IG_CACHE_DIR)

# attach が失敗したときの理由。画面の警告に出す。
IG_LAST_ERROR = ""

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
    """
    統合を使うつもりがあるか。

    IG_DB_PATH を書いたか、マウント先にDBが見つかったかのどちらか。
    「つもりはあるが読めていない」を画面で言えるよう、実在とは分けている。
    """
    return bool(IG_DB_PATH or _CONFIGURED_DB_PATH)


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
    global IG_LAST_ERROR
    if not available():
        IG_LAST_ERROR = IG_DB_REASON or "DBが見つかりません"
        return False
    try:
        conn.execute(f"PRAGMA busy_timeout = {IG_BUSY_TIMEOUT_MS}")
        conn.execute("ATTACH DATABASE ? AS " + SCHEMA, (IG_DB_PATH,))
        # ATTACH の後に立てる。先に立てると -shm を作れず WAL が開けない。
        conn.execute("PRAGMA query_only = 1")
        IG_LAST_ERROR = ""
        return True
    except Exception as e:
        IG_LAST_ERROR = f"{type(e).__name__}: {e}"
        print(f"[!] IGアーカイブに接続できません: {IG_LAST_ERROR}")
        return False


def is_attached(conn):
    """
    この接続に ig のDBが繋がっているか。

    `attach()` の戻り値を flask の g に持たせる方式だと、テストやCLIから
    素の接続を渡したときに答えが出せない。接続自体に聞く。
    sqlite3.Connection には属性を付けられない（__dict__ が無い）ので、
    接続に印を付ける方式も取れず、PRAGMA で見るのが素直。
    """
    try:
        return any(r[1] == SCHEMA for r in conn.execute("PRAGMA database_list"))
    except Exception:
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
    IG の投稿を **共通の列並び**で返す SELECT。

    列の射影と絞り込みは `sources.IgSource` が持つ。以前はこの関数が
    17列の射影を直書きしていて、同じ射影が /search と IGユーザーページにも
    複製されていた（計3箇所）。列を1つ足すと3箇所直す必要があり、
    実際に `quoted_json` を落として引用RTが壊れたことがある。

    互換のために残してある薄い入口。新しい呼び出しは
    `sources.by_name("ig").timeline_sql(...)` を使うこと。
    """
    import sources
    return sources.by_name("ig").timeline_sql(
        conn, category=category, show_r18=show_r18,
        require_media=require_media)


def cache_rel(local_path):
    """
    ig-ray が記録した local_path を、キャッシュ root からの相対パスに直す。

    **ig-ray は自分のコンテナ基準の絶対パスで保存している**（既定 `/data/cache/...`）。
    X-Ray 側は同じディレクトリを `/ig-data/cache` にマウントしているので、
    素直に relpath を取ると `../../data/cache/...` になって使えない。
    書いた側のパス表記に依存せず、キャッシュ root 以降だけを取り出す。

    戻り値は "ab/XXXX_0.jpg" のような相対パス。取れなければ None。
    """
    if not local_path or not IG_CACHE_DIR:
        return None

    # 1) そのまま相対が取れるならそれでよい（同じパス表記で動いている場合）
    try:
        rel = os.path.relpath(local_path, IG_CACHE_DIR)
        if not rel.startswith(".."):
            return rel.replace(os.sep, "/")
    except ValueError:
        pass

    # 2) パスの中の "cache/" 以降を拾う。ig-ray 側の root 名に依存しない。
    norm = str(local_path).replace("\\", "/")
    marker = "/cache/"
    idx = norm.rfind(marker)
    if idx >= 0:
        return norm[idx + len(marker):]

    # 3) 最後の手段。サブディレクトリ情報は失われるが何も出さないよりまし。
    return os.path.basename(norm) or None


def cache_url(local_path):
    """
    ig-ray の local_path を X-Ray が配信できるURLに直す。

    ig-ray 自身も `/cache/` で配っているが、**X-Ray の `/cache/` と衝突する**
    ので、こちらでは `/media/ig/` に載せ替える。
    実体が無いときは None を返し、呼び出し側はリモートURLに落ちる。
    """
    rel = cache_rel(local_path)
    if not rel:
        return None
    if ".." in rel.split("/"):
        return None          # ディレクトリトラバーサル防止
    if not os.path.exists(os.path.join(IG_CACHE_DIR, rel)):
        return None
    return "/media/ig/" + rel


def local_file(local_path):
    """
    Tumblr へ添付するための**実ファイルの絶対パス**。無ければ None。

    ファイル名だけ取り出すと開けない（ig-ray はサブディレクトリに置く）。
    """
    rel = cache_rel(local_path)
    if not rel or ".." in rel.split("/"):
        return None
    fp = os.path.join(IG_CACHE_DIR, rel)
    return fp if os.path.exists(fp) else None


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


def post_info(conn, tweet_id, indices=None):
    """
    Tumblr へ投稿するために必要なものを返す。

    戻り値: {"paths": [実ファイルの絶対パス...], "source_url": str,
             "owner": str} / 読めなければ None。

    indices を渡すと**その順序どおり**に絞る。UI が選んだ順に番号を振って
    見せているので、その順序をそのまま添付順にする。
    """
    code = shortcode_of(tweet_id)
    if not code:
        return None

    row = conn.execute(
        f"SELECT owner_username FROM {SCHEMA}.posts WHERE shortcode=?",
        (code,)).fetchone()
    if not row:
        row = conn.execute(
            f"SELECT owner_username FROM {SCHEMA}.bookmarks WHERE shortcode=?",
            (code,)).fetchone()
    if not row:
        return None

    locals_by_index = {}
    for r in conn.execute(
        f"SELECT media_index, local_path FROM {SCHEMA}.media_index "
        f"WHERE shortcode=? ORDER BY media_index", (code,)
    ):
        if r["local_path"]:
            locals_by_index[r["media_index"]] = r["local_path"]

    order = sorted(locals_by_index)
    if indices is not None:
        # タイムライン側の添字は「表示している並び」なので、
        # media_index の昇順に並べたものへの添字として解釈する。
        picked = []
        for i in indices:
            if 0 <= i < len(order) and order[i] not in picked:
                picked.append(order[i])
        order = picked

    paths = []
    for idx in order:
        fp = local_file(locals_by_index[idx])
        if fp and fp not in paths:
            paths.append(fp)

    return {"paths": paths,
            "source_url": f"{POST_URL_PREFIX}{code}/",
            "owner": row["owner_username"] or ""}


def write_conn():
    """
    **書き込み用**の接続。ミュートの切り替えだけに使う。

    読み取り用（attach + query_only）とは別にする。読む側を書き込み可に
    すると、表示経路のバグが ig-ray のデータを壊しうる。
    書く操作はごく少ないので、そのときだけ開いて閉じる。

    使い終わったら呼び出し側で close すること。読めなければ None。
    """
    if not available():
        return None
    try:
        conn = sqlite3.connect(IG_DB_PATH, timeout=IG_BUSY_TIMEOUT_MS / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {IG_BUSY_TIMEOUT_MS}")
        return conn
    except Exception as e:
        print(f"[!] IGアーカイブに書き込み接続できません: {type(e).__name__}: {e}")
        return None


def set_mute(username, muted, reason=None):
    """
    ig-ray の accounts.is_muted を切り替える。

    ig-ray 本体と同じ列を使うので、**向こうの画面とも揃う**。
    成功したら True。列が無い古いDBなら False。
    """
    conn = write_conn()
    if not conn:
        return False
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
        if "is_muted" not in cols:
            return False
        if "muted_at" in cols and "mute_reason" in cols:
            conn.execute(
                "UPDATE accounts SET is_muted=?, muted_at=?, mute_reason=? "
                "WHERE username=?",
                (1 if muted else 0,
                 datetime.now(timezone.utc).isoformat() if muted else None,
                 reason if muted else None, username))
        else:
            conn.execute("UPDATE accounts SET is_muted=? WHERE username=?",
                         (1 if muted else 0, username))
        conn.commit()
        return True
    except Exception as e:
        print(f"[!] IGのミュート切り替えに失敗: {type(e).__name__}: {e}")
        return False
    finally:
        conn.close()


def muted_accounts(conn):
    """
    ミュート中のアカウント一覧（表示用）。読み取り接続で引く。

    mute_reason / muted_at は ig-ray 側の版によって無いことがあるので、
    ある列だけを読む。無い列を名指しすると一覧ごと空になる。
    """
    cols = _columns(conn, "accounts")
    if "is_muted" not in cols:
        return []
    sel = ["username"]
    sel.append("full_name" if "full_name" in cols else "NULL AS full_name")
    sel.append("COALESCE(mute_reason,'') AS reason" if "mute_reason" in cols
               else "'' AS reason")
    sel.append("COALESCE(muted_at,'') AS muted_at" if "muted_at" in cols
               else "'' AS muted_at")
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT {', '.join(sel)} FROM {SCHEMA}.accounts "
            "WHERE COALESCE(is_muted, 0) = 1 ORDER BY username")]
    except Exception as e:
        print(f"[!] IGのミュート一覧を引けません: {type(e).__name__}: {e}")
        return []


# ig-ray が scrape_log に使う擬似アカウント名と、成功とみなすステータス。
# **ここは ig/app/db.py と揃える。** 向こうに新しいステータスが増えたら
# ここにも足さないと「成功しているのに止まって見える」になる。
LOG_EGRESS = "__egress__"
LOG_LOGIN = "__login__"
PSEUDO_ACCOUNTS = (LOG_EGRESS, LOG_LOGIN)
OK_STATUSES = ("ok", "ok_degraded", "backfill_done")

# **異常を「OK一覧に無いもの」で判定しない。**
# 引き算で判定すると、上流が新しい"正常"ステータスを足すたびに誤報になる。
# 実際 login_ok（ログイン失敗からの復帰を記録する行）を失敗として表示した。
# 追加するときは ig/app/ 側の log_scrape を grep して拾うこと。
BAD_LOGIN_STATUSES = ("login_error",)
BAD_EGRESS_STATUSES = ("egress_warn", "egress_error")


def health(conn, interval_sec=21600.0, stale_intervals=3.0):
    """
    IG取得側の健康状態。X-Ray の画面に出すためにまとめて返す。

    取得を X-Ray に取り込むと、**IG側の異常がどこにも表示されない**状態に
    なる。IG-Ray では 24日、X-Ray では 12日、どちらも「止まっているのに
    画面が正常と言い続けた」ことで気づけなかった。同じ轍を踏まないよう、
    出口・ログイン・無音の3つをまとめて出す。

    戻り値: {"egress": {...}|None, "login": {...}|None, "stale": {...}|None}
    どれも無ければ各値 None。読めなければ全体 None。
    """
    out = {"egress": None, "login": None, "stale": None}
    try:
        for key, name, bad in (("egress", LOG_EGRESS, BAD_EGRESS_STATUSES),
                               ("login", LOG_LOGIN, BAD_LOGIN_STATUSES)):
            r = conn.execute(
                f"SELECT status, message, ended_at FROM {SCHEMA}.scrape_log "
                "WHERE username = ? AND ended_at > datetime('now','-12 hours') "
                "ORDER BY ended_at DESC LIMIT 1", (name,)).fetchone()
            # 最新の1件だけを見る。復帰（login_ok）が最新なら何も出さない。
            if r and r["status"] in bad:
                out[key] = {"status": r["status"],
                            "message": (r["message"] or "")[:300],
                            "at": r["ended_at"]}

        # 無音の見張り。原因に関係なく「最後に取れた時刻」だけを見る。
        marks = ",".join("?" * len(OK_STATUSES))
        pseudo = ",".join("?" * len(PSEUDO_ACCOUNTS))
        r = conn.execute(
            f"SELECT MAX(ended_at) AS t FROM {SCHEMA}.scrape_log "
            f"WHERE status IN ({marks}) "
            f"  AND COALESCE(username,'') NOT IN ({pseudo})",
            tuple(OK_STATUSES) + PSEUDO_ACCOUNTS).fetchone()
        last = r["t"] if r else None
        if not last:
            # ログが刈られているときは投稿の取得時刻で代用する。
            # X-Ray 側で踏んだのと同じ穴（止まるほどログが消えて無言になる）。
            r = conn.execute(
                f"SELECT MAX(fetched_at) AS t FROM {SCHEMA}.posts").fetchone()
            last = r["t"] if r else None
        if last:
            t = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            hours = (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
            limit = max(interval_sec / 3600.0 * stale_intervals, 1.0)
            if hours > limit:
                out["stale"] = {"hours": int(hours), "limit_h": round(limit, 1),
                                "since": t.isoformat()}
    except Exception as e:
        print(f"[!] IGの状態を読めません: {type(e).__name__}: {e}")
        return None
    return out


def backfill_jobs(conn):
    """
    過去投稿のさかのぼり取得（バックフィル）の進み具合。読めなければ []。

    ig-ray のCLI（`backfill list`）しか見る手段がなく、「止まっているのに
    気づかない」をやりやすい。X-Ray の画面に出すために読むだけの一覧。

    列は ig-ray 側の版で増減するので、ある列だけを読む。無い列を
    名指しすると一覧ごと空になる（ミュート一覧で一度踏んだ）。
    """
    cols = _columns(conn, "backfill_jobs")
    if not cols or "username" not in cols:
        return []

    def col(name, default="NULL"):
        return name if name in cols else f"{default} AS {name}"

    sel = ", ".join([
        "username",
        col("status", "'?'"),
        col("posts_seen", "0"),
        col("posts_saved", "0"),
        col("media_saved", "0"),
        col("pages", "0"),
        col("oldest_date", "''"),
        col("message", "''"),
        col("updated_at", "''"),
        col("completed_at", "''"),
    ])
    order = "status, username" if "status" in cols else "username"
    try:
        cur = conn.execute(
            f"SELECT {sel} FROM {SCHEMA}.backfill_jobs ORDER BY {order}")
        rows = cur.fetchall()
    except sqlite3.Error:
        return []
    # dict(row) に頼らない。呼び出し側が row_factory を立てていないと
    # dict(tuple) が TypeError になり、それを握って**静かに空**を返す形に
    # なっていた（一覧が出ないのに原因が分からない、の典型）。
    # 列名は cursor.description から取れるので、接続の設定に依存させない。
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in rows]


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
