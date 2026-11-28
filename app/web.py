import json
import os
import time
import re
import base64
import secrets
import shutil
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory

JST = timezone(timedelta(hours=9))

# スペースURLの検出パターン
SPACE_URL_RE = re.compile(r'https?://(?:twitter\.com|x\.com)/i/spaces/([A-Za-z0-9_-]+)')

from flask import g
from db import get_conn
import seed_accounts
import cache_utils
import tumblr_client
import ig_bridge
import sources        # 出自（X / IG）の登録と共通列の約束

# 出自ごとの違いは sources.py の中だけで閉じる。ここに X 用/IG 用の
# 分岐を増やさないこと（増やしたくなったら sources.Source に口を足す）。

# カテゴリ名 → Tumblrタグ の変換表。
# X-Ray内部のカテゴリ名をそのままタグにすると都合が悪いものを置き換える。
# JSONで上書きできる: TUMBLR_TAG_MAP='{"ギャル":"girl","R18":"nsfw"}'
# 空文字を指定するとそのカテゴリはタグから除外される。
_DEFAULT_TAG_MAP = {"ギャル": "girl"}
try:
    _raw_tag_map = os.environ.get("TUMBLR_TAG_MAP", "").strip()
    TUMBLR_TAG_MAP = json.loads(_raw_tag_map) if _raw_tag_map else dict(_DEFAULT_TAG_MAP)
    if not isinstance(TUMBLR_TAG_MAP, dict):
        raise ValueError("dictではありません")
except (ValueError, TypeError) as _e:
    print(f"[!] TUMBLR_TAG_MAP を解釈できません（既定値を使います）: {_e}")
    TUMBLR_TAG_MAP = dict(_DEFAULT_TAG_MAP)


def map_tags(categories):
    """カテゴリ名リストをTumblr用のタグリストに変換する。空になるものは落とす。"""
    out = []
    for c in categories or []:
        t = TUMBLR_TAG_MAP.get(c, c)
        if not t:
            continue          # 空文字指定＝タグにしない
        if t not in out:      # 変換で重複しうるので潰す
            out.append(t)
    return out


# これ以上の枚数を選んだら自動的に下書きにする。
# センシティブ寄りの投稿をまとめて公開すると凍結リスクが上がるため、
# 枚数が多いときは一旦下書きに置いて人の目を通す運用に寄せる。
# 0 以下にすると自動化しない。
try:
    TUMBLR_DRAFT_THRESHOLD = int(os.environ.get("TUMBLR_DRAFT_THRESHOLD", "3") or 3)
except ValueError:
    print("[!] TUMBLR_DRAFT_THRESHOLD が数値ではありません（既定の3を使います）")
    TUMBLR_DRAFT_THRESHOLD = 3

# 画面に出すバージョン。zipの採番と揃える（リリースのたびにここを上げる）。
# APP_VERSION 環境変数で上書きできるので、手元で検証中の版に印を付けたいときは
# compose の environment に書けばよい。
APP_VERSION = os.environ.get("APP_VERSION", "").strip() or "55"

# 外部公開はしない。
# Tumblr投稿をAPI方式に固定した時点（v35）で、一時公開URLを使う
# シェアツール方式は不要になった。それでも /share 系のルートと
# cloudflared のトンネルが残っていて、**もう存在しない機能のためだけに
# 外部公開面が開いている**状態だった。v54 で撤去。
from cache_utils import CACHE_DIR, IMAGES_DIR, PERSIST_CATEGORIES

app = Flask(__name__)


@app.context_processor
def _inject_flags():
    # 全テンプレートから参照できる値
    return {
        "tumblr_enabled": tumblr_client.is_configured(),
        "tumblr_draft_threshold": TUMBLR_DRAFT_THRESHOLD,
        "app_version": APP_VERSION,
        "ig_configured": ig_bridge.configured(),
        "ig_error": ig_bridge.IG_LAST_ERROR or ig_bridge.IG_DB_REASON,
    }


def db():
    """
    リクエスト単位でSQLiteコネクションを再利用し、teardownで必ず閉じる。
    途中で例外が出てもリークしない（WALロック残り対策）。
    """
    if "conn" not in g:
        g.conn = get_conn()
    return g.conn


def db_ro():
    """
    IGアーカイブを繋いだ**読み取り専用**コネクション。

    `db()` と分けてあるのは、外部DBの ATTACH が `PRAGMA query_only` を
    立てるため。書き込みのあるルートで使うと X-Ray 側の更新まで弾かれる。
    タイムラインやギャラリーのような読むだけの画面で使う。

    繋ぐ相手は sources の登録ぶん全部。以前はここで
    `ig_bridge.attach()` を名指ししていて、出自を足しても接続時に
    繋がらず一覧に出なかった。
    読めなかった出自は `sources.active()` が黙って外す。
    """
    if "conn_ro" not in g:
        conn = get_conn()
        attached = sources.attach_all(conn)
        g.ig_on = "ig" in attached
        g.conn_ro = conn
    return g.conn_ro


def ig_on():
    """このリクエストでIGアーカイブが読めているか"""
    return bool(g.get("ig_on"))


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


@app.teardown_appcontext
def close_db(exc):
    for key in ("conn", "conn_ro"):
        conn = g.pop(key, None)
        if conn is not None:
            conn.close()


def page_arg(name="page"):
    """?page=abc のような不正値で500にならないようにする"""
    try:
        return max(1, int(request.args.get(name, 1)))
    except (TypeError, ValueError):
        return 1


@app.template_filter("b64encode")
def b64encode_filter(s):
    """文字列/JSON文字列をbase64エンコードするJinjaフィルター"""
    if not isinstance(s, str):
        s = json.dumps(s)
    return base64.b64encode(s.encode()).decode()

COOKIES_FILE = os.environ.get("TWITTER_COOKIES_FILE", "/data/cookies.txt")
DB_PATH = os.environ.get("DB_PATH", "/data/data.db")
# IMAGES_DIR(永続) / CACHE_DIR(表示用) は cache_utils で定義
PER_PAGE = 100  # ページネーション件数

# scraper.py が出口（回線分離）の状態を scrape_log に残すときの擬似アカウント名。
# アカウント一覧には出さず、専用のバナーで扱う。
EGRESS_LOG_NAME = "__egress__"
RUN_LOG_NAME = "__run__"

# 擬似アカウントを個別に列挙すると、増えたときに漏れて
# 「__run__ が2回エラー」のような表示が出る。前後が __ の名前をまとめて除く。
# LIKE ではなく GLOB。LIKE だと _ がワイルドカード扱いで ESCAPE 指定が必要になり、
# そのエスケープ記述を一度書き損じてトップページを 500 にした。GLOB は _ がそのまま。
NOT_PSEUDO_SQL = "screen_name NOT GLOB '__*__'"


def get_categories():
    """最新のカテゴリ一覧を返す（accounts.jsonから動的取得）"""
    accounts, categories = seed_accounts._load_from_json()
    return categories


# 出自（all / x / ig）ごとに件数をキャッシュする
_counts_cache = {}
COUNTS_TTL = 60  # 秒


def get_category_counts(conn, categories_list, source="all"):
    """
    カテゴリ別の件数。読める出自ぶんを足し合わせる。

    集計そのものは各出自が持つ（sources.py）。以前はX用とIG用の
    ほぼ同形のGROUP BYがこの関数の中に2本並んでいて、出自が増えると
    3本目を足す形だった。

    表示には出ているのに件数に入っていないと「photographer 0件」なのに
    写真が並ぶ、という食い違いになるので、出自の取りこぼしは直接バグになる。

    キャッシュは出自ごとに分ける。混ぜると X だけ選んだときに
    混合の件数が出てしまう。
    """
    now = time.time()
    cached = _counts_cache.get(source)
    if cached and now - cached["at"] < COUNTS_TTL:
        return cached["counts"], cached["total"]

    counts, total = sources.category_counts_all(conn, categories_list, source)
    _counts_cache[source] = {"at": now, "counts": counts, "total": total}
    return counts, total


def get_bookmarked_ids(conn):
    """ブックマーク済みtweet_idのsetを返す"""
    try:
        rows = conn.execute("SELECT tweet_id FROM bookmarks").fetchall()
        return {r["tweet_id"] for r in rows}
    except Exception:
        return set()


def format_tweet(d, bookmarked_ids=None, deleted=None):
    """DBの行dict（tweets JOIN accounts想定）を表示用に整形する"""
    # 出自。タイムラインのSQLは列で持ってくるが、ブックマーク表のように
    # 列が無い経路もあるので tweet_id の接頭辞から補う。
    if not d.get("source"):
        d["source"] = "ig" if ig_bridge.is_ig_id(d.get("tweet_id")) else "x"
    d["media"] = json.loads(d.get("media_json") or "[]")
    d["videos"] = json.loads(d.get("video_json") or "[]")
    d["local_media"] = json.loads(d.get("local_media_json") or "[]")
    d["categories_list"] = json.loads(d.get("categories") or "[]")

    # 表示用画像: ローカルがあればローカル優先、なければリモートURL。
    # 手動削除済みのURLは除外し、何枚消したかを持たせる。
    deleted = deleted or set()
    display_imgs = []
    media_idx = []
    deleted_count = 0
    for i, remote in enumerate(d["media"]):
        if remote in deleted:
            deleted_count += 1
            continue
        local = d["local_media"][i] if i < len(d["local_media"]) else None
        display_imgs.append(local if local else remote)
        media_idx.append(i)     # 削除ボタンが送る元インデックス
    d["display_imgs"] = display_imgs
    d["media_idx"] = media_idx
    d["deleted_count"] = deleted_count

    # Tumblr共有用: ローカル保存済み画像が1枚でもあるか（削除済み除外後）
    d["has_local_media"] = any(
        (d["local_media"][i] if i < len(d["local_media"]) else None)
        for i, remote in enumerate(d["media"]) if remote not in deleted
    )
    # カテゴリをタグ用にbase64で渡す（| e がJSONを壊す既知問題の回避）。
    # 表示用のカテゴリ名ではなく、変換表を通したTumblrタグを渡す。
    # ensure_ascii は既定のまま（True）。日本語カテゴリを \uXXXX に逃がさないと
    # JS側の atob() が UTF-8 バイトを扱えず壊れる。
    _tags = map_tags(d["categories_list"])
    d["categories_b64"] = base64.b64encode(
        json.dumps(_tags).encode()).decode() if _tags else ""

    d["media_b64"] = base64.b64encode(
        json.dumps(display_imgs).encode()
    ).decode() if display_imgs else ""

    try:
        dt_utc = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
        dt_jst = dt_utc.astimezone(JST)
        d["created_at_jst"] = dt_jst.strftime("%Y-%m-%d %H:%M")
        d["created_at_dt"] = dt_jst.strftime("%Y%m%d%H%M")
    except Exception:
        d["created_at_jst"] = (d.get("created_at") or "")[:16].replace("T", " ")
        d["created_at_dt"] = (d.get("created_at") or "")[:16].replace("-", "").replace("T", "").replace(":", "")

    # スペースURLの検出（本文にspaces/URLが含まれていたら専用カード表示用の情報をセット）
    content = d.get("content") or ""
    m = SPACE_URL_RE.search(content)
    d["space"] = {"url": m.group(0), "id": m.group(1)} if m else None

    d["is_bookmarked"] = bool(bookmarked_ids and d.get("tweet_id") in bookmarked_ids)
    d["self_reply"] = None

    # 引用RTの引用元
    d["quoted"] = None
    if d.get("quoted_json"):
        try:
            q = json.loads(d["quoted_json"])
            # 引用元の投稿日時をJSTへ
            try:
                qdt = datetime.fromisoformat((q.get("created_at") or "").replace("Z", "+00:00"))
                q["created_at_jst"] = qdt.astimezone(JST).strftime("%Y-%m-%d %H:%M")
            except Exception:
                q["created_at_jst"] = ""
            d["quoted"] = q
        except Exception:
            d["quoted"] = None
    return d


@app.route("/ig-cache/<path:filename>")
def serve_ig_cache(filename):
    """
    IG-Ray 側のキャッシュ画像を配信する。

    ig-ray も自分では `/cache/` で配っているが、**X-Ray の `/cache/` と
    衝突する**ので別のパスに載せ替えてある。実体は ig-ray の data を
    マウントしたディレクトリで、こちらからは読むだけ。
    """
    if not ig_bridge.IG_CACHE_DIR:
        from flask import abort
        abort(404)
    return send_from_directory(ig_bridge.IG_CACHE_DIR, filename)


@app.route("/images/<path:filename>")
def serve_image(filename):
    """永続画像を配信。無ければキャッシュ側にフォールバック。"""
    if os.path.exists(os.path.join(IMAGES_DIR, filename)):
        return send_from_directory(IMAGES_DIR, filename)
    return send_from_directory(CACHE_DIR, filename)


@app.route("/cache/<path:filename>")
def serve_cache(filename):
    """
    表示用キャッシュ画像を配信。
    昇格済み（永続に移動済み）の場合は永続を優先して返す。
    """
    if os.path.exists(os.path.join(IMAGES_DIR, filename)):
        return send_from_directory(IMAGES_DIR, filename)
    return send_from_directory(CACHE_DIR, filename)


# 列の約束と並び替えキーは sources.py が持つ（唯一の正）。
# 以前はここに共通名の一覧とX射影が、ig_bridge にIG射影が、
# さらに /search とIGユーザーページにもIG射影の複製があった。
TL_COLUMNS = sources.TL_COLUMNS
TL_ORDER = sources.TL_ORDER


# タイムラインに出す出自。IGの取得は止まっているので混ぜるだけだと
# 新しいXの投稿に押し流されて何百ページも奥に行ってしまう。切り替えを用意する。
SOURCES = sources.names()


def source_arg():
    v = (request.args.get("source") or "all").strip().lower()
    return v if v in SOURCES else "all"


def timeline_rows(conn, category, limit, offset, show_r18=False,
                  require_media=False, source="all"):
    """
    登録されている出自を時系列に混ぜた1ページぶんの行を返す。

    source に出自名を渡せばそれだけに絞れる。読めない出自は黙って外れる。

    以前は「IGだけ」「混合」「Xだけ」の3分岐をここに書いていた。
    出自が増えるたびに分岐が増える形だったのを、sources の登録を
    並べるだけにした。混合が失敗したときに X 単独へ落とす保険は
    そのまま残す（ig-ray 側の事情でこちらを落とさないため）。
    """
    active = sources.active(conn, source)
    if not active:
        return []

    sql, params = sources.union_timeline_sql(
        conn, active, category, show_r18, require_media)
    try:
        return [dict(r) for r in conn.execute(
            sql, (*params, limit, offset)).fetchall()]
    except Exception as e:
        print(f"[!] タイムラインに失敗（{','.join(s.name for s in active)}）: "
              f"{type(e).__name__}: {e}")

    # 複数を混ぜて失敗したときだけ、最初の出自だけで出し直す。
    # 全部まとめて落とすと、片方の都合で画面が真っ白になる。
    if len(active) > 1:
        head = active[:1]
        sql, params = sources.union_timeline_sql(
            conn, head, category, show_r18, require_media)
        try:
            return [dict(r) for r in conn.execute(
                sql, (*params, limit, offset)).fetchall()]
        except Exception as e:
            print(f"[!] {head[0].name} 単独でも失敗: {type(e).__name__}: {e}")
    return []


# 取得が「黙って止まる」のを原因に関係なく見張るしきい値（時間）。
# 出口IPの判定で12日止まったとき、入れたのは「出口の異常をログに残す」で、
# 原因を1つ塞いだだけだった。想定していない死に方は必ず残るので、
# **最後に取れた時刻だけ**を見る見張りを別に置く。
# 15分巡回なので3時間＝12回ぶん空いたら異常とみなす。Xの一時的な不調で
# 鳴らない程度には鈍く、半日放置はしない程度には敏感な値として選んだ。
try:
    STALE_HOURS = float(os.environ.get("SCRAPE_STALE_HOURS", "3") or 3)
except ValueError:
    print("[!] SCRAPE_STALE_HOURS が数値ではありません（既定の3を使います）")
    STALE_HOURS = 3.0


def _utc_key(d):
    """表示用の行を時系列に並べるためのキー。

    created_at のオフセットが X と IG で揃っていないので、
    文字列のままでは比較できない（UTCに直してから比べる）。
    """
    try:
        t = datetime.fromisoformat(str(d.get("created_at") or "").replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


# IG側の見張り。巡回間隔（秒）の何倍で異常とみなすか。
try:
    IG_INTERVAL_SEC = float(os.environ.get("IG_RAY_INTERVAL", "21600") or 21600)
    IG_STALE_INTERVALS = float(os.environ.get("IG_RAY_STALE_INTERVALS", "3") or 3)
except ValueError:
    IG_INTERVAL_SEC, IG_STALE_INTERVALS = 21600.0, 3.0


def check_stale(conn):
    """
    最後に取得できてからの経過を見る。異常なら dict、正常なら None。

    成功ログが1件も無いときに黙って None を返してはいけない。
    scrape_log は7日で刈られるので、**7日以上止まると成功ログごと消えて
    「何も無い＝正常」に見えてしまう**。一番警告が要る状況で黙る。
    そこで成功ログが無いときは保存済みツイートの取得時刻で代用する。
    """
    if STALE_HOURS <= 0:
        return None

    row = conn.execute(
        "SELECT MAX(run_at) AS t FROM scrape_log "
        f"WHERE status = 'ok' AND {NOT_PSEUDO_SQL}").fetchone()
    last, basis = (row["t"] if row else None), "log"

    if not last:
        row = conn.execute("SELECT MAX(fetched_at) AS t FROM tweets").fetchone()
        last, basis = (row["t"] if row else None), "tweets"

    if not last:
        return None             # 1件も取ったことがない＝入れたばかり

    try:
        t = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None

    hours = (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    if hours <= STALE_HOURS:
        return None
    return {
        "hours": int(hours),
        "days": round(hours / 24, 1),
        "since": t.astimezone(JST).strftime("%Y-%m-%d %H:%M"),
        "limit_h": round(STALE_HOURS, 1),
        # ログから分かったのか、ツイートの取得時刻で代用したのか。
        # 代用のときは「ログが刈られるほど長く止まっている」の合図でもある。
        "basis": basis,
    }


@app.route("/")
def index():
    category = request.args.get("category", "all")
    source = source_arg()
    page = page_arg()
    offset = (page - 1) * PER_PAGE

    conn = db_ro()
    bookmarked_ids = get_bookmarked_ids(conn)
    deleted_media = cache_utils.deleted_urls(conn)

    rows = timeline_rows(conn, category, PER_PAGE + 1, offset, source=source)

    # PER_PAGE+1件取得して、次ページがあるか判定
    has_next = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]

    rows = sources.attach_media_all(conn, rows)
    tweets = [format_tweet(r, bookmarked_ids, deleted_media) for r in rows]

    # 各カテゴリの件数（タブのバッジ用）
    categories_list = get_categories()
    counts, total_count = get_category_counts(conn, categories_list, source)

    last_run = conn.execute("""
        SELECT run_at FROM scrape_log ORDER BY run_at DESC LIMIT 1
    """).fetchone()

    # 最終更新時刻をJSTに変換
    last_run_jst = None
    if last_run:
        try:
            dt_utc = datetime.fromisoformat(last_run["run_at"].replace("Z", "+00:00"))
            last_run_jst = dt_utc.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
        except Exception:
            last_run_jst = last_run["run_at"][:16].replace("T", " ") + " UTC"

    stale = check_stale(conn)
    # IG取得側の健康状態。取り込んだ以上、こちらが出さないと誰も気づけない。
    ig_health = (ig_bridge.health(conn, IG_INTERVAL_SEC, IG_STALE_INTERVALS)
                 if ig_on() else None)

    # ヘルス情報: 直近1時間のスクレイプログを集計
    health = conn.execute("""
        SELECT
            COUNT(*) as total,
            COALESCE(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), 0) as ok_count,
            COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0) as error_count
        FROM scrape_log
        WHERE run_at > datetime('now', '-1 hour')
    """).fetchone()

    # エラーが続いてるアカウントを抽出（直近3回全部errorのもの）
    # 擬似アカウント（__egress__ / __run__）はアカウント一覧からは除く。
    error_accounts = conn.execute(f"""
        SELECT screen_name, COUNT(*) as err_count
        FROM scrape_log
        WHERE status = 'error'
          AND {NOT_PSEUDO_SQL}
          AND run_at > datetime('now', '-3 hours')
        GROUP BY screen_name
        HAVING err_count >= 2
        ORDER BY err_count DESC
        LIMIT 5
    """).fetchall()

    # 出口（回線分離）の異常。ログファイルにしか出ないと気づけないので画面に出す。
    egress_alert = conn.execute("""
        SELECT status, message, run_at FROM scrape_log
        WHERE screen_name = ?
          AND run_at > datetime('now', '-12 hours')
        ORDER BY run_at DESC LIMIT 1
    """, (EGRESS_LOG_NAME,)).fetchone()


    tweets = [format_tweet(dict(r), bookmarked_ids, deleted_media) for r in rows]

    # 自己リプライをグルーピング
    tweet_map = {t["tweet_id"]: t for t in tweets}
    reply_ids = set()
    for t in tweets:
        rid = t.get("reply_to_tweet_id")
        if rid and rid in tweet_map:
            parent = tweet_map[rid]
            if parent["screen_name"] == t["screen_name"]:
                if parent["self_reply"] is None:
                    parent["self_reply"] = t
                    reply_ids.add(t["tweet_id"])
    tweet_items = [t for t in tweets if t["tweet_id"] not in reply_ids]

    next_url = url_for("index", category=category, page=page + 1,
                       source=source) if has_next else None
    if request.args.get("partial") == "1":
        return render_template("_feed.html", tweets=tweet_items, next_url=next_url)

    return render_template(
        "index.html",
        tweets=tweet_items,
        next_url=next_url,
        categories=categories_list,
        current_category=category,
        current_source=source,
        ig_available=ig_on(),
        counts=counts,
        total_count=total_count,
        last_run_jst=last_run_jst,
        health=dict(health) if health else None,
        error_accounts=[dict(a) for a in error_accounts],
        egress_alert=dict(egress_alert) if egress_alert else None,
        stale=stale,
        ig_health=ig_health,
        page=page,
        has_next=has_next,
    )


@app.route("/manage")
def manage():
    accounts, categories = seed_accounts._load_from_json()
    # DBから最終取得状況も取得
    conn = db()
    db_accounts = {
        r["screen_name"]: dict(r)
        for r in conn.execute(
            "SELECT screen_name, last_scraped_at FROM accounts"
        ).fetchall()
    }

    account_list = []
    for sn, dn, cats in accounts:
        account_list.append({
            "screen_name": sn,
            "display_name": dn,
            "categories": cats,
            "last_scraped_at": db_accounts.get(sn, {}).get("last_scraped_at"),
        })

    # クッキー登録済みアカウント数
    cookie_count = 0
    if os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE) as f:
            cookie_count = len([
                l for l in f
                if l.strip() and not l.startswith("#") and "\t" in l
            ])

    return render_template(
        "manage.html",
        accounts=account_list,
        all_categories=categories,
        cookie_count=cookie_count,
    )


@app.route("/api/account/add", methods=["POST"])
def api_account_add():
    screen_name = (request.form.get("screen_name") or "").strip().lstrip("@")
    display_name = (request.form.get("display_name") or "").strip()
    categories = request.form.getlist("categories")

    if not screen_name or not display_name or not categories:
        return jsonify({"ok": False, "error": "すべての項目を入力してください"}), 400

    accounts, all_cats = seed_accounts._load_from_json()

    # 重複チェック
    if any(a[0].lower() == screen_name.lower() for a in accounts):
        return jsonify({"ok": False, "error": f"@{screen_name} は既に登録済みです"}), 400

    accounts.append((screen_name, display_name, categories))
    seed_accounts.save_to_json(accounts, all_cats)

    # DBにも即反映
    conn = db()
    conn.execute("""
        INSERT INTO accounts (screen_name, display_name, categories)
        VALUES (?, ?, ?)
        ON CONFLICT(screen_name) DO UPDATE SET
            display_name=excluded.display_name,
            categories=excluded.categories
    """, (screen_name, display_name, json.dumps(categories, ensure_ascii=False)))
    conn.commit()

    return jsonify({"ok": True})


@app.route("/api/account/delete", methods=["POST"])
def api_account_delete():
    screen_name = (request.form.get("screen_name") or "").strip()
    if not screen_name:
        return jsonify({"ok": False, "error": "screen_nameが必要です"}), 400

    accounts, all_cats = seed_accounts._load_from_json()
    accounts = [a for a in accounts if a[0] != screen_name]
    seed_accounts.save_to_json(accounts, all_cats)

    # DBからも削除（ツイートも含めて）
    conn = db()
    conn.execute("DELETE FROM accounts WHERE screen_name=?", (screen_name,))
    conn.execute("DELETE FROM tweets WHERE screen_name=?", (screen_name,))
    conn.commit()

    return jsonify({"ok": True})


@app.route("/api/cookies/update", methods=["POST"])
def api_cookies_update():
    """クッキーテキストを受け取ってcookies.txtに保存"""
    cookies_text = request.form.get("cookies_text", "")
    if not cookies_text.strip():
        return jsonify({"ok": False, "error": "クッキー情報が空です"}), 400

    # バリデーション: 各行がタブ区切りでauth_token/ct0を含むか軽くチェック
    lines = [l.strip() for l in cookies_text.splitlines() if l.strip() and not l.startswith("#")]
    valid_lines = []
    for line in lines:
        if "\t" in line and "auth_token" in line and "ct0" in line:
            valid_lines.append(line)

    if not valid_lines:
        return jsonify({"ok": False, "error": "有効なクッキー行がありません（形式: username[TAB]auth_token=...; ct0=...）"}), 400

    # merge=1 なら既存行を保持したままユーザー名単位で上書きする。
    # 一部の垢しか取得できなかった時に、生きている残りの垢を巻き込んで消さないため。
    merge = (request.form.get("merge") or "").strip().lower() in ("1", "true", "on", "yes")

    entries = {}   # username -> 行
    order = []     # 既存の並びを保つ
    if merge and os.path.exists(COOKIES_FILE):
        try:
            with open(COOKIES_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "\t" not in line:
                        continue
                    username = line.split("\t", 1)[0].strip()
                    if username and username not in entries:
                        order.append(username)
                    entries[username] = line
        except OSError:
            pass

    kept_before = len(entries)
    updated = 0
    for line in valid_lines:
        username = line.split("\t", 1)[0].strip()
        if not username:
            continue
        if username not in entries:
            order.append(username)
        entries[username] = line
        updated += 1

    tmp = COOKIES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# username\tauth_token=xxxx; ct0=yyyy\n")
        for username in order:
            f.write(entries[username] + "\n")
    os.replace(tmp, COOKIES_FILE)
    try:
        os.chmod(COOKIES_FILE, 0o600)
    except OSError:
        pass

    return jsonify({
        "ok": True,
        "count": len(entries),      # ファイル内の総数
        "updated": updated,         # 今回書き換えた数
        "kept": max(0, len(entries) - updated),
        "merged": merge,
        "previous": kept_before,
    })


@app.route("/api/bookmark/toggle", methods=["POST"])
def api_bookmark_toggle():
    """ブックマークのトグル。ツイート内容をコピー保存して永続化"""
    tweet_id = (request.form.get("tweet_id") or "").strip()
    if not tweet_id:
        return jsonify({"ok": False, "error": "tweet_idが必要です"}), 400

    conn = db()
    existing = conn.execute(
        "SELECT tweet_id FROM bookmarks WHERE tweet_id=?", (tweet_id,)
    ).fetchone()

    if existing:
        conn.execute("DELETE FROM bookmarks WHERE tweet_id=?", (tweet_id,))
        conn.commit()
        return jsonify({"ok": True, "bookmarked": False})

    # IG の投稿は ig-ray のDBから内容をコピーする。**向こうには書かない。**
    if ig_bridge.is_ig_id(tweet_id):
        return _bookmark_ig(conn, tweet_id)

    # ツイート本体を取得してコピー保存
    row = conn.execute("""
        SELECT t.*, a.display_name, a.categories, a.profile_image_url
        FROM tweets t JOIN accounts a ON t.screen_name = a.screen_name
        WHERE t.tweet_id = ?
    """, (tweet_id,)).fetchone()

    if not row:
        return jsonify({"ok": False, "error": "ポストが見つかりません"}), 404

    d = dict(row)

    # キャッシュ → 永続へ昇格（ブックマークした画像は消えないようにする）
    promoted = 0
    try:
        local_paths = json.loads(d.get("local_media_json") or "[]")
    except Exception:
        local_paths = []
    if any(p and p.startswith("/cache/") for p in local_paths):
        local_paths, promoted = cache_utils.promote(local_paths)
        d["local_media_json"] = json.dumps(local_paths, ensure_ascii=False)
        conn.execute(
            "UPDATE tweets SET local_media_json=? WHERE tweet_id=?",
            (d["local_media_json"], tweet_id),
        )

    conn.execute("""
        INSERT INTO bookmarks
        (tweet_id, screen_name, display_name, content, created_at, url,
         media_json, local_media_json, video_json, categories, profile_image_url,
         quoted_json, bookmarked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        d["tweet_id"], d["screen_name"], d["display_name"], d["content"],
        d["created_at"], d["url"], d.get("media_json"), d.get("local_media_json"),
        d.get("video_json"), d.get("categories"), d.get("profile_image_url"),
        d.get("quoted_json"),
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    return jsonify({"ok": True, "bookmarked": True, "promoted": promoted})


def _bookmarked_ig_paths(tweet_id, indices=None):
    """
    X-Ray にブックマークしてある IG 投稿から、添付できる実ファイルを返す。

    ig-ray 側の保持期間で元が消えても、ブックマーク時に実体を
    X-Ray の永続領域へコピーしてあるので投稿できる。
    戻り値: (paths, source_url, screen_name)
    """
    row = db().execute(
        "SELECT url, screen_name, local_media_json FROM bookmarks "
        "WHERE tweet_id=?", (tweet_id,)).fetchone()
    if not row:
        return [], "", ""
    try:
        locals_ = json.loads(row["local_media_json"] or "[]")
    except (TypeError, ValueError):
        locals_ = []
    order = [i for i, p in enumerate(locals_) if p]
    if indices is not None:
        order = [i for i in indices if i in order]
    paths = []
    for i in order:
        fp = os.path.join(IMAGES_DIR, os.path.basename(locals_[i]))
        if os.path.exists(fp) and fp not in paths:
            paths.append(fp)
    return (paths, row["url"] or "",
            ig_bridge.ID_PREFIX + (row["screen_name"] or ""))


def _bookmark_ig(conn, tweet_id):
    """
    IG の投稿を X-Ray のブックマークにコピーする。

    画像は **X-Ray 側の永続領域に実体をコピーする。**
    ig-ray のキャッシュは向こうの保持期間で消えるので、参照だけ持つと
    「ブックマークしたのに画像が消える」になる。X 側のブックマークが
    キャッシュから永続へ昇格させているのと同じ考え方。
    コピー先は X-Ray の持ち物なので、読み取り専用の前提は崩れない。
    """
    ro = db_ro()
    if not ig_on():
        return jsonify({"ok": False, "error": "IGアーカイブを読めません"}), 400

    code = ig_bridge.shortcode_of(tweet_id)
    S = ig_bridge.SCHEMA
    row = ro.execute(
        f"SELECT p.shortcode, p.owner_username, p.caption, p.date_utc, p.likes,"
        f" p.comments, p.media_json, a.full_name, a.categories_json,"
        f" a.profile_pic_local "
        f"FROM {S}.posts p LEFT JOIN {S}.accounts a"
        f" ON p.owner_username = a.username WHERE p.shortcode = ?",
        (code,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "投稿が見つかりません"}), 404

    try:
        media = json.loads(row["media_json"] or "[]")
    except (TypeError, ValueError):
        media = []
    remotes = [m.get("image_url") or "" for m in media if isinstance(m, dict)]

    # ローカル実体を X-Ray の永続領域へコピー
    locals_out, copied = [], 0
    by_index = {}
    for r in ro.execute(
        f"SELECT media_index, local_path FROM {S}.media_index WHERE shortcode=?",
        (code,)
    ):
        if r["local_path"]:
            by_index[r["media_index"]] = r["local_path"]

    os.makedirs(IMAGES_DIR, exist_ok=True)
    for i, m in enumerate(media):
        idx = m.get("index", i) if isinstance(m, dict) else i
        src = ig_bridge.local_file(by_index.get(idx, ""))
        if not src:
            locals_out.append(None)
            continue
        ext = os.path.splitext(src)[1] or ".jpg"
        name = f"ig_{code}_{idx}{ext}"
        dst = os.path.join(IMAGES_DIR, name)
        try:
            if not os.path.exists(dst):
                shutil.copyfile(src, dst)
                copied += 1
            locals_out.append(f"/images/{name}")
        except Exception as e:
            print(f"[!] IG画像のコピーに失敗 {src}: {type(e).__name__}: {e}")
            locals_out.append(None)

    conn.execute("""
        INSERT INTO bookmarks
        (tweet_id, screen_name, display_name, content, created_at, url,
         media_json, local_media_json, video_json, categories,
         profile_image_url, quoted_json, bookmarked_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (tweet_id, row["owner_username"],
          row["full_name"] or row["owner_username"], row["caption"],
          row["date_utc"], f"{ig_bridge.POST_URL_PREFIX}{code}/",
          json.dumps(remotes, ensure_ascii=False),
          json.dumps(locals_out, ensure_ascii=False),
          None, row["categories_json"], row["profile_pic_local"], None,
          datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return jsonify({"ok": True, "bookmarked": True, "promoted": copied})


def _local_image_names(tweet_id, conn):
    """
    投稿に紐づくローカル保存済み画像のファイル名リストを返す。
    削除済み画像・ローカル未保存(リモートのみ)は除外する。
    （外部公開ページなので、手元にある実ファイルだけを対象にする）
    """
    row = conn.execute(
        "SELECT media_json, local_media_json FROM tweets WHERE tweet_id=?",
        (tweet_id,)).fetchone()
    if not row:
        # ブックマークにしか残っていない投稿（元ツイートが削除された等）も
        # 投稿できるようにフォールバックする。
        row = conn.execute(
            "SELECT media_json, local_media_json FROM bookmarks WHERE tweet_id=?",
            (tweet_id,)).fetchone()
    if not row:
        return []
    try:
        media = json.loads(row["media_json"] or "[]")
        local = json.loads(row["local_media_json"] or "[]")
    except Exception:
        return []
    deleted = cache_utils.deleted_urls(conn)
    names = []
    for i, remote in enumerate(media):
        if remote in deleted:
            continue
        lp = local[i] if i < len(local) else None
        if lp:
            names.append(os.path.basename(lp))
    return names


def _local_image_paths(tweet_id, conn, indices=None):
    """
    投稿に紐づくローカル画像の実ファイルパスを返す。
    削除済み・ローカル未保存は除外する。

    indices（添字のリスト）を渡すとその添字だけに絞り、
    **渡された順序どおり**に返す。UI側が選んだ順に番号を振って見せているため、
    その順序をそのままTumblrへの添付順にする。
    """
    names = _local_image_names(tweet_id, conn)
    if indices is not None:
        picked = []
        for i in indices:
            if 0 <= i < len(names) and names[i] not in picked:
                picked.append(names[i])
        names = picked
    paths = []
    for n in names:
        for d in (IMAGES_DIR, CACHE_DIR):
            fp = os.path.join(d, n)
            if os.path.exists(fp):
                paths.append(fp)
                break
    return paths


@app.route("/api/tumblr/accounts")
def api_tumblr_accounts():
    """投稿先の一覧。トークンは返さない。"""
    return jsonify({"ok": True,
                    "enabled": tumblr_client.is_configured(),
                    "accounts": tumblr_client.public_accounts()})


@app.route("/api/ig/mute", methods=["POST"])
def api_ig_mute():
    """
    IG の投稿主をミュートする／解除する。

    **ig-ray の accounts.is_muted を直接書く**ので、向こうの画面とも揃う。
    書き込みは読み取りとは別の接続で行う（読む側は query_only のまま）。
    """
    username = (request.form.get("username") or "").strip().lstrip("@")
    username = ig_bridge.shortcode_of(username) or username   # ig: が付いていても受ける
    if not username:
        return jsonify({"ok": False, "error": "usernameが必要です"}), 400
    if not ig_bridge.available():
        return jsonify({"ok": False, "error": "IGアーカイブを読めません"}), 400

    raw = (request.form.get("muted") or "").strip().lower()
    if raw in ("1", "true", "on", "yes"):
        target = True
    elif raw in ("0", "false", "off", "no"):
        target = False
    else:
        # 明示されていなければ今の状態を反転する
        ro = db_ro()
        cur = False
        if ig_on():
            r = ro.execute(
                f"SELECT COALESCE(is_muted,0) AS m FROM {ig_bridge.SCHEMA}.accounts "
                "WHERE username=?", (username,)).fetchone()
            cur = bool(r["m"]) if r else False
        target = not cur

    if not ig_bridge.set_mute(username, target,
                              request.form.get("reason") or None):
        return jsonify({"ok": False, "error": "ミュートを変更できませんでした"}), 500
    return jsonify({"ok": True, "muted": target, "username": username})


@app.route("/ig-mutes")
def ig_mutes():
    """ミュート中のIGアカウント一覧。解除もここから。"""
    conn = db_ro()
    accounts = ig_bridge.muted_accounts(conn) if ig_on() else []
    return render_template("ig_mutes.html", accounts=accounts,
                           ig_available=ig_on())


@app.route("/api/tumblr/presets")
def api_tumblr_presets():
    """
    Xの投稿主ごとに覚えてあるキャプション／タグを、まとめて1回で返す。
    画面を開いたときに読み込んでおき、tボタンを押した時点では通信しない
    （モーダルを開いてから値が後出しで書き換わるのを避けるため）。
    """
    conn = db()
    rows = conn.execute(
        "SELECT screen_name, caption, tags FROM tumblr_presets").fetchall()
    presets = {r["screen_name"]: {"caption": r["caption"], "tags": r["tags"]}
               for r in rows}
    # ig-ray 側のプリセットは起動時に import_ig_presets() で取り込み済み。
    # ここで ig-ray を読まないのは、X-Ray で消したものが復活しないようにするため。
    return jsonify({"ok": True, "presets": presets})


@app.route("/api/tumblr/preset/delete", methods=["POST"])
def api_tumblr_preset_delete():
    """覚えている内容を捨てて、次回から既定値（ハンドル名＋カテゴリ）に戻す。"""
    screen_name = (request.form.get("screen_name") or "").strip().lstrip("@")
    if not screen_name:
        return jsonify({"ok": False, "error": "screen_nameが必要です"}), 400
    conn = db()
    conn.execute("DELETE FROM tumblr_presets WHERE screen_name=?", (screen_name,))
    conn.commit()
    return jsonify({"ok": True, "screen_name": screen_name})


def _save_tumblr_preset(conn, screen_name, caption, tags):
    """投稿が通ったときだけ呼ぶ。失敗した内容は覚えない。"""
    if not screen_name:
        return
    conn.execute("""
        INSERT INTO tumblr_presets (screen_name, caption, tags, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(screen_name) DO UPDATE SET
            caption=excluded.caption, tags=excluded.tags,
            updated_at=excluded.updated_at
    """, (screen_name, caption, tags, datetime.now(timezone.utc).isoformat()))
    conn.commit()


@app.route("/api/tumblr/post", methods=["POST"])
def api_tumblr_post():
    """
    Tumblr に直接投稿する。画像はローカルファイルをそのままアップロードするので、
    外部公開（トンネル）は不要。
    """
    if not tumblr_client.is_configured():
        return jsonify({"ok": False,
                        "error": "Tumblr投稿が未設定です（キーまたはアカウント）"}), 400

    tweet_id = (request.form.get("tweet_id") or "").strip()
    if not tweet_id:
        return jsonify({"ok": False, "error": "tweet_idが必要です"}), 400

    # IGの投稿も投稿できる。IG側のDBには一切書かず、読んだ画像を送るだけ。
    is_ig = ig_bridge.is_ig_id(tweet_id)

    account = tumblr_client.find_account((request.form.get("account") or "").strip())
    if not account:
        return jsonify({"ok": False, "error": "投稿先アカウントが見つかりません"}), 400

    state = (request.form.get("state") or "published").strip()
    if state not in tumblr_client.VALID_STATES:
        return jsonify({"ok": False, "error": f"stateが不正です: {state}"}), 400

    # 添付する画像の添字（未指定なら全部）
    raw_idx = (request.form.get("indices") or "").strip()
    indices = None
    if raw_idx:
        try:
            # 重複は潰しつつ、渡された順序は保つ（添付順になる）
            seen = set()
            indices = []
            for x in raw_idx.split(","):
                x = x.strip()
                if x == "":
                    continue
                n = int(x)
                if n not in seen:
                    seen.add(n)
                    indices.append(n)
        except ValueError:
            return jsonify({"ok": False, "error": "indicesが不正です"}), 400
        if not indices:
            return jsonify({"ok": False, "error": "画像が選択されていません"}), 400

    if is_ig:
        # IG は読み取り専用の接続で引く（query_only が立っている）
        ig_conn = db_ro()
        if not ig_on():
            return jsonify({"ok": False,
                            "error": "IGアーカイブを読めません"}), 400
        info = ig_bridge.post_info(ig_conn, tweet_id, indices)
        if info and info["paths"]:
            paths, source_url = info["paths"], info["source_url"]
            screen_name = ig_bridge.ID_PREFIX + info["owner"]
        else:
            # ig-ray 側から消えていても、X-Ray にブックマークしてあれば
            # 画像の実体はこちらにコピーしてあるので投稿できる。
            paths, source_url, screen_name = _bookmarked_ig_paths(
                tweet_id, indices)
            if not paths:
                return jsonify(
                    {"ok": False,
                     "error": "ローカルに保存された画像がありません"}), 400
    else:
        conn = db()
        paths = _local_image_paths(tweet_id, conn, indices)
        if not paths:
            return jsonify({"ok": False,
                            "error": "投稿できるローカル画像がありません"}), 400
        row = conn.execute("SELECT url, screen_name FROM tweets WHERE tweet_id=?",
                           (tweet_id,)).fetchone()
        if not row:
            row = conn.execute(
                "SELECT url, screen_name FROM bookmarks WHERE tweet_id=?",
                (tweet_id,)).fetchone()
        source_url = row["url"] if row else ""
        # 覚えておくキーは投稿主。クライアントの申告ではなくDBから引く。
        screen_name = (row["screen_name"] if row else "") or ""

    raw_caption = (request.form.get("caption") or "").strip()
    raw_tags = (request.form.get("tags") or "").strip()

    # リンクは投稿ごとに違うので、保存するのは本文（raw_caption）だけにして
    # ここで毎回その投稿のURLから巻き直す。
    caption = raw_caption
    if caption and source_url:
        caption = f'<a href="{source_url}">{caption}</a>'
    tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

    res = tumblr_client.create_photo_post(
        account, paths, caption=caption, tags=tags, state=state, source_url=source_url)
    if not res.get("ok"):
        return jsonify(res), 502

    # 投稿が通ってから覚える。失敗した内容を残すと次回それが出てきて紛らわしい。
    # **保存先は常に X-Ray 側のDB。** IG の投稿でも ig-ray のDBには書かない
    # （読み取り専用を崩さないため）。キーは ig: 付きなので衝突しない。
    try:
        _save_tumblr_preset(db(), screen_name, raw_caption, raw_tags)
    except Exception as e:
        # 覚えられなくても投稿自体は成功している。ここで500にはしない。
        print(f"[!] Tumblrプリセットの保存に失敗: {e}")

    res["label"] = account["label"]
    res["count"] = len(paths)
    res["screen_name"] = screen_name
    res["saved_caption"] = raw_caption
    res["saved_tags"] = raw_tags
    return jsonify(res)


@app.route("/api/media/delete", methods=["POST"])
def api_media_delete():
    """
    画像を削除する。サーバー上の実ファイルも消える（復元不可）。

    tweet_id + index  → その1枚
    tweet_id + all=1  → そのポストの画像を全部
    """
    tweet_id = (request.form.get("tweet_id") or "").strip()
    if not tweet_id:
        return jsonify({"ok": False, "error": "tweet_idが必要です"}), 400

    if ig_bridge.is_ig_id(tweet_id):
        # IGアーカイブは読むだけ。ここから先はXのテーブルを前提にしているので
        # 通すとデータを壊す。画面側でもボタンを出していないが、
        # 直接叩かれても拒否する。
        return jsonify({"ok": False,
                        "error": "IGの投稿はX-Rayからは変更できません"}), 400

    conn = db()
    delete_all = (request.form.get("all") or "").strip().lower() in ("1", "true", "on", "yes")

    if delete_all:
        res = cache_utils.delete_all_media(conn, tweet_id)
        if not res.get("ok"):
            return jsonify(res), 404
        return jsonify(res)

    raw = (request.form.get("index") or "").strip()
    try:
        index = int(raw)
    except ValueError:
        return jsonify({"ok": False, "error": "indexが不正です"}), 400

    res = cache_utils.delete_media(conn, tweet_id, index)
    if not res.get("ok"):
        code = 404 if "見つかりません" in res.get("error", "") else 400
        return jsonify(res), code
    return jsonify(res)


@app.route("/bookmarks")
def bookmarks():
    conn = db()
    rows = conn.execute("""
        SELECT * FROM bookmarks ORDER BY bookmarked_at DESC
    """).fetchall()

    deleted = cache_utils.deleted_urls(conn)
    tweets = []
    for r in rows:
        d = dict(r)
        # 出自。この画面は format_tweet を通さず自前で組み立てているので
        # ここでも補う（バッジとリンク先の出し分けに要る）。
        d["source"] = "ig" if ig_bridge.is_ig_id(d.get("tweet_id")) else "x"
        d["media"] = json.loads(d.get("media_json") or "[]")
        d["videos"] = json.loads(d.get("video_json") or "[]")
        d["local_media"] = json.loads(d.get("local_media_json") or "[]")
        d["categories_list"] = json.loads(d.get("categories") or "[]")
        display_imgs = []
        media_idx = []
        deleted_count = 0
        for i, remote in enumerate(d["media"]):
            if remote in deleted:
                deleted_count += 1
                continue
            local = d["local_media"][i] if i < len(d["local_media"]) else None
            display_imgs.append(local if local else remote)
            media_idx.append(i)
        d["display_imgs"] = display_imgs
        d["media_idx"] = media_idx
        d["deleted_count"] = deleted_count
        d["media_b64"] = base64.b64encode(json.dumps(display_imgs).encode()).decode() if display_imgs else ""

        # Tumblr投稿ボタン用。トップページ(format_tweet)と同じ情報を持たせる。
        d["has_local_media"] = any(
            (d["local_media"][i] if i < len(d["local_media"]) else None)
            for i, remote in enumerate(d["media"]) if remote not in deleted
        )
        _tags = map_tags(d["categories_list"])
        d["categories_b64"] = base64.b64encode(
            json.dumps(_tags).encode()).decode() if _tags else ""

        try:
            dt_jst = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00")).astimezone(JST)
            d["created_at_jst"] = dt_jst.strftime("%Y-%m-%d %H:%M")
            d["created_at_dt"] = dt_jst.strftime("%Y%m%d%H%M")
        except Exception:
            d["created_at_jst"] = (d.get("created_at") or "")[:16]
            d["created_at_dt"] = ""
        m = SPACE_URL_RE.search(d.get("content") or "")
        d["space"] = {"url": m.group(0), "id": m.group(1)} if m else None
        d["is_bookmarked"] = True
        d["self_reply"] = None
        tweets.append(d)

    return render_template("bookmarks.html", tweets=tweets, categories=get_categories())


@app.route("/gallery")
def gallery():
    category = request.args.get("category", "all")
    source = source_arg()
    show_r18 = request.args.get("r18", "0") == "1"
    page = page_arg()
    offset = (page - 1) * PER_PAGE

    conn = db_ro()

    rows = timeline_rows(conn, category, PER_PAGE + 1, offset,
                         show_r18=show_r18, require_media=True, source=source)

    has_next = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]
    rows = sources.attach_media_all(conn, rows)

    # 画像を平坦化（1画像=1グリッドアイテム）
    deleted = cache_utils.deleted_urls(conn)
    gallery_items = []
    for r in rows:
        d = dict(r)
        media = json.loads(d.get("media_json") or "[]")
        local = json.loads(d.get("local_media_json") or "[]")
        for i, remote in enumerate(media):
            if remote in deleted:
                continue
            lp = local[i] if i < len(local) else None
            gallery_items.append({
                "media_index": i,
                "img": lp if lp else remote,
                "screen_name": d["screen_name"],
                "display_name": d["display_name"],
                "url": d["url"],
                "tweet_id": d["tweet_id"],
                "idx": i + 1,                      # 何枚目か（ファイル名用）
                "created_at": d["created_at"],
                "source": d.get("source", "x"),
            })

    next_url = (
        url_for("gallery", category=category, page=page + 1, source=source,
                **({"r18": 1} if show_r18 else {})) if has_next else None
    )
    if request.args.get("partial") == "1":
        return render_template("_gallery_items.html", items=gallery_items, next_url=next_url)

    return render_template(
        "gallery.html",
        next_url=next_url,
        items=gallery_items,
        categories=get_categories(),
        current_category=category,
        current_source=source,
        ig_available=ig_on(),
        show_r18=show_r18,
        page=page,
        has_next=has_next,
    )


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    results = []
    if q:
        conn = db()
        bookmarked_ids = get_bookmarked_ids(conn)
        deleted_media = cache_utils.deleted_urls(conn)
        try:
            # FTS5で検索
            rows = conn.execute("""
                SELECT t.*, a.display_name, a.categories, a.profile_image_url
                FROM tweets_fts f
                JOIN tweets t ON t.tweet_id = f.tweet_id
                JOIN accounts a ON t.screen_name = a.screen_name
                WHERE tweets_fts MATCH ?
                ORDER BY t.created_at DESC
                LIMIT 200
            """, (q,)).fetchall()
        except Exception:
            # FTS非対応時はLIKE検索にフォールバック
            rows = conn.execute("""
                SELECT t.*, a.display_name, a.categories, a.profile_image_url
                FROM tweets t JOIN accounts a ON t.screen_name = a.screen_name
                WHERE t.content LIKE ?
                ORDER BY t.created_at DESC LIMIT 200
            """, (f"%{q}%",)).fetchall()
        results = [format_tweet(dict(r) | {"source": "x"},
                                bookmarked_ids, deleted_media) for r in rows]

        # IG も同じ検索語で引いて混ぜる。全文検索の索引は X 側にしか
        # 無いので IG は本文のLIKE。どちらも共通列で返るので、
        # 並べ替えは取得後にまとめて行える。
        #
        # 以前はここに IG の17列射影が直書きされていた（ig_bridge と
        # ユーザーページにも同じものがあり計3箇所）。sources が持つ。
        ro = db_ro()
        for src in sources.active(ro, "all"):
            if src.name == "x":
                continue        # X は上で FTS 込みで引いている
            try:
                rows = src.search(ro, q, limit=200)
                rows = src.attach_media(ro, rows)
                results += [format_tweet(r, bookmarked_ids, deleted_media)
                            for r in rows]
                results.sort(key=_utc_key, reverse=True)
                results = results[:200]
            except Exception as e:
                print(f"[!] {src.name} の検索に失敗（他の出自で続行）: "
                      f"{type(e).__name__}: {e}")

    return render_template("search.html", results=results, query=q, categories=get_categories())


def _ig_user_profile(screen_name, date_from, date_to, page, offset):
    """IG の投稿主のページ。X 用と同じ user.html を使う。"""
    conn = db_ro()
    if not ig_on():
        return "IGアーカイブを読めません", 404
    owner = ig_bridge.shortcode_of(screen_name)      # ig: を外すだけ
    S = ig_bridge.SCHEMA

    acc = conn.execute(
        f"SELECT username, full_name, categories_json, profile_pic_local "
        f"FROM {S}.accounts WHERE username = ?", (owner,)).fetchone()
    if not acc:
        return "アカウントが見つかりません", 404
    acc = {"screen_name": screen_name,      # URL用（ig: 付き）
           "handle": owner,                 # 画面に出す名前
           "display_name": acc["full_name"] or owner,
           "categories": acc["categories_json"],
           "profile_image_url": acc["profile_pic_local"],
           "categories_list": json.loads(acc["categories_json"] or "[]"),
           "source": "ig"}

    # 投稿の取得は出自に委譲する。以前はここに IG の17列射影が
    # 直書きされていた（ig_bridge と /search にも同じものがあり計3箇所）。
    src = sources.by_name("ig")
    rows = src.owner_rows(conn, owner, PER_PAGE + 1, offset,
                          date_from=date_from, date_to=date_to)

    stats = dict(conn.execute(f"""
        SELECT COUNT(*) AS total, COALESCE(SUM(likes), 0) AS total_likes,
               0 AS total_rts, MIN(date_utc) AS oldest, MAX(date_utc) AS newest
        FROM {S}.posts WHERE owner_username = ?
    """, (owner,)).fetchone() or {})
    # is_video のような列は ig-ray 側の都合で有無が変わる。
    # 統計が1つ欠けるだけでページごと落とさない。
    stats["with_media"] = conn.execute(f"""
        SELECT COALESCE(SUM(CASE WHEN media_json IS NOT NULL
                                  AND media_json != '[]' THEN 1 ELSE 0 END), 0) AS n
        FROM {S}.posts WHERE owner_username = ?
    """, (owner,)).fetchone()["n"]
    try:
        stats["with_video"] = conn.execute(
            f"SELECT COALESCE(SUM(is_video), 0) AS n FROM {S}.posts "
            "WHERE owner_username = ?", (owner,)).fetchone()["n"]
    except Exception:
        stats["with_video"] = 0
    stats["per_day"] = None
    try:
        if stats.get("oldest") and stats.get("newest") and stats.get("total"):
            o = datetime.fromisoformat(stats["oldest"].replace("Z", "+00:00"))
            n = datetime.fromisoformat(stats["newest"].replace("Z", "+00:00"))
            stats["per_day"] = round(stats["total"] / max(1, (n - o).days), 1)
            stats["oldest_jst"] = o.astimezone(JST).strftime("%Y-%m-%d")
            stats["newest_jst"] = n.astimezone(JST).strftime("%Y-%m-%d")
    except Exception:
        pass

    has_next = len(rows) > PER_PAGE
    rows = sources.attach_media_all(conn, rows[:PER_PAGE])
    tweets = [format_tweet(r, get_bookmarked_ids(conn),
                           cache_utils.deleted_urls(conn)) for r in rows]
    next_url = (url_for("user_profile", screen_name=screen_name, page=page + 1,
                        **{"from": date_from, "to": date_to}) if has_next else None)
    if request.args.get("partial") == "1":
        return render_template("_feed.html", tweets=tweets, next_url=next_url)
    return render_template("user.html", account=acc, tweets=tweets, stats=stats,
                           date_from=date_from, date_to=date_to, page=page,
                           has_next=has_next, next_url=next_url)


@app.route("/user/<screen_name>")
def user_profile(screen_name):
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    page = page_arg()
    offset = (page - 1) * PER_PAGE

    if ig_bridge.is_ig_id(screen_name):
        return _ig_user_profile(screen_name, date_from, date_to, page, offset)

    conn = db()
    bookmarked_ids = get_bookmarked_ids(conn)
    deleted_media = cache_utils.deleted_urls(conn)

    acc = conn.execute(
        "SELECT * FROM accounts WHERE screen_name = ?", (screen_name,)
    ).fetchone()
    if not acc:
        return "アカウントが見つかりません", 404
    acc = dict(acc)
    acc["categories_list"] = json.loads(acc.get("categories") or "[]")

    where = ["t.screen_name = ?"]
    params = [screen_name]
    if date_from:
        where.append("t.created_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("t.created_at <= ?")
        params.append(date_to + "T23:59:59")
    where_sql = " AND ".join(where)

    rows = conn.execute(f"""
        SELECT t.*, a.display_name, a.categories, a.profile_image_url
        FROM tweets t JOIN accounts a ON t.screen_name = a.screen_name
        WHERE {where_sql}
        ORDER BY t.created_at DESC
        LIMIT ? OFFSET ?
    """, params + [PER_PAGE + 1, offset]).fetchall()

    # 統計（画像数・動画数・期間・1日あたり投稿数も）
    stats = dict(conn.execute("""
        SELECT COUNT(*) as total,
               COALESCE(SUM(like_count), 0) as total_likes,
               COALESCE(SUM(retweet_count), 0) as total_rts,
               MIN(created_at) as oldest,
               MAX(created_at) as newest
        FROM tweets WHERE screen_name = ?
    """, (screen_name,)).fetchone() or {})

    media_stat = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN media_json IS NOT NULL AND media_json != '[]' THEN 1 ELSE 0 END), 0) as with_media,
            COALESCE(SUM(CASE WHEN video_json IS NOT NULL AND video_json != '[]' THEN 1 ELSE 0 END), 0) as with_video
        FROM tweets WHERE screen_name = ?
    """, (screen_name,)).fetchone()
    stats["with_media"] = media_stat["with_media"]
    stats["with_video"] = media_stat["with_video"]

    # 1日あたり投稿数（保存期間ベース、ベストエフォート）
    stats["per_day"] = None
    try:
        if stats.get("oldest") and stats.get("newest") and stats.get("total"):
            o = datetime.fromisoformat(stats["oldest"].replace("Z", "+00:00"))
            n = datetime.fromisoformat(stats["newest"].replace("Z", "+00:00"))
            days = max(1, (n - o).days)
            stats["per_day"] = round(stats["total"] / days, 1)
            stats["oldest_jst"] = o.astimezone(JST).strftime("%Y-%m-%d")
            stats["newest_jst"] = n.astimezone(JST).strftime("%Y-%m-%d")
    except Exception:
        pass

    has_next = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]
    tweets = [format_tweet(dict(r), bookmarked_ids, deleted_media) for r in rows]

    next_url = (
        url_for("user_profile", screen_name=screen_name, page=page + 1,
                **{"from": date_from, "to": date_to}) if has_next else None
    )
    if request.args.get("partial") == "1":
        return render_template("_feed.html", tweets=tweets, next_url=next_url)

    return render_template(
        "user.html",
        account=acc,
        tweets=tweets,
        stats=stats,
        date_from=date_from,
        date_to=date_to,
        page=page,
        has_next=has_next,
        next_url=next_url,
    )


def _dir_size(path):
    """ディレクトリの合計サイズと件数をベストエフォートで取得"""
    return cache_utils.dir_stats(path)


def _fmt_size(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


@app.route("/storage")
def storage():
    # DB本体サイズ
    db_size = 0
    for suffix in ["", "-wal", "-shm"]:
        p = DB_PATH + suffix
        if os.path.exists(p):
            db_size += os.path.getsize(p)

    # inodeを共有カウントし、ハードリンク重複を二重に数えない。
    # 永続を先に数えるので cache_size は「キャッシュを消せば実際に空く容量」になる。
    seen = set()
    persist_size, persist_count = cache_utils.dir_stats(IMAGES_DIR, seen)
    cache_size, cache_count = cache_utils.dir_stats(CACHE_DIR, seen)
    img_size = persist_size + cache_size
    img_count = persist_count + cache_count

    # キャッシュのうち保持期間を超えている分（削除見込み）
    stale = cache_utils.cleanup_cache(dry_run=True)

    # ディスク使用量
    try:
        du = shutil.disk_usage(os.path.dirname(DB_PATH) or "/")
        disk_total, disk_used, disk_free = du.total, du.used, du.free
    except Exception:
        disk_total = disk_used = disk_free = 0

    # 統計
    conn = db()
    tweet_count = conn.execute("SELECT COUNT(*) c FROM tweets").fetchone()["c"]
    bookmark_count = conn.execute("SELECT COUNT(*) c FROM bookmarks").fetchone()["c"]

    # IGアーカイブの内訳。**容量は ig-ray 側の持ち物**なので、
    # X-Ray の「消せば空く容量」とは混ぜずに別枠で出す。
    ig_info = None
    ro = db_ro()
    if ig_on():
        st = ig_bridge.stats(ro)
        if st:
            ig_size, ig_count = cache_utils.dir_stats(ig_bridge.IG_CACHE_DIR, set())
            ig_info = {"posts": st["posts"], "accounts": st["accounts"],
                       "size": _fmt_size(ig_size), "count": ig_count,
                       "path": ig_bridge.IG_CACHE_DIR}

    return render_template(
        "storage.html",
        ig=ig_info,
        db_size=_fmt_size(db_size),
        img_size=_fmt_size(img_size),
        img_count=img_count,
        persist_size=_fmt_size(persist_size),
        persist_count=persist_count,
        cache_size=_fmt_size(cache_size),
        cache_count=cache_count,
        stale_count=stale["deleted"],
        stale_size=_fmt_size(stale["freed"]),
        retention_days=cache_utils.CACHE_RETENTION_DAYS,
        persist_categories=PERSIST_CATEGORIES,
        total_size=_fmt_size(db_size + img_size),
        disk_total=_fmt_size(disk_total),
        disk_used=_fmt_size(disk_used),
        disk_free=_fmt_size(disk_free),
        disk_pct=round(disk_used / disk_total * 100, 1) if disk_total else 0,
        tweet_count=tweet_count,
        bookmark_count=bookmark_count,
    )


@app.route("/api/cache/cleanup", methods=["POST"])
def api_cache_cleanup():
    """
    キャッシュ手動削除。
      days 未指定 → CACHE_RETENTION_DAYS（既定30日）より古いものを削除
      days=0      → 全削除
    ブックマーク参照中の画像は保護され、削除後にDBのパスも同期される。
    """
    raw = request.form.get("days")
    try:
        days = None if raw in (None, "") else max(0, int(raw))
    except ValueError:
        return jsonify({"ok": False, "error": "daysが不正です"}), 400

    res = cache_utils.cleanup_cache(days=days)
    return jsonify({
        "ok": True,
        "deleted": res["deleted"],
        "kept": res["kept"],
        "freed": _fmt_size(res["freed"]),
        "synced": res["synced"],
        "days": res["days"],
    })


@app.route("/status")
def status():
    conn = db()
    logs = conn.execute("""
        SELECT * FROM scrape_log ORDER BY run_at DESC LIMIT 50
    """).fetchall()
    accounts = conn.execute("""
        SELECT screen_name, display_name, last_scraped_at FROM accounts
        ORDER BY last_scraped_at DESC
    """).fetchall()

    # IGの過去投稿さかのぼり（バックフィル）の進み具合。
    # ig-ray の CLI でしか見えなかったので、止まっても気づけなかった。
    ro = db_ro()
    ig_backfill = ig_bridge.backfill_jobs(ro) if ig_on() else []

    return render_template("status.html", logs=logs, accounts=accounts,
                           ig_backfill=ig_backfill)


if __name__ == "__main__":
    # ig-ray から引き継ぐプリセットの取り込み。失敗しても画面は出したいので
    # ここで握る（取り込めなければ印が付かず、次の起動でまた試す）。
    try:
        import_ig_presets()
    except Exception as e:
        print(f"[!] Tumblrプリセットの取り込みに失敗: {type(e).__name__}: {e}")

    # Flaskの開発サーバーは本番非推奨。waitressがあればそちらを使う。
    try:
        from waitress import serve
        print("[*] waitress で起動: 0.0.0.0:5000")
        serve(app, host="0.0.0.0", port=5000, threads=8)
    except ImportError:
        print("[!] waitress未インストール。開発サーバーで起動します。")
        app.run(host="0.0.0.0", port=5000, debug=False)
