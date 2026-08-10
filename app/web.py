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

# Tumblr共有ページを外部公開する際のベースURL（例: https://share.example.com）。
# 未設定なら共有機能は無効。X-Ray本体ではなく /share/<token> だけを
# リバースプロキシ等で外に出し、この値をそのドメインにする。
PUBLIC_SHARE_BASE_URL = os.environ.get("PUBLIC_SHARE_BASE_URL", "").strip().rstrip("/")
# 共有トークンの有効時間（分）。Tumblrが読み終わればもう不要なので短くてよい。
SHARE_TOKEN_TTL_MIN = int(os.environ.get("SHARE_TOKEN_TTL_MIN", "60") or 60)
# 外部公開に使うホスト名（例: share.example.com）。
# このホスト宛のリクエストは /share と /share-img 以外を 404 にする。
# cloudflared 側の ingress 制限に加えた二層目の防御。
PUBLIC_SHARE_HOST = os.environ.get("PUBLIC_SHARE_HOST", "").strip().lower()
if not PUBLIC_SHARE_HOST and PUBLIC_SHARE_BASE_URL:
    # ベースURLからホスト名を自動抽出
    from urllib.parse import urlparse as _urlparse
    PUBLIC_SHARE_HOST = (_urlparse(PUBLIC_SHARE_BASE_URL).hostname or "").lower()
from cache_utils import CACHE_DIR, IMAGES_DIR, PERSIST_CATEGORIES

app = Flask(__name__)


@app.before_request
def _restrict_public_host():
    """
    公開ホスト名で来たリクエストは共有系ルートだけ許可する。
    Tunnel経由でうっかり /manage 等が晒されるのを防ぐ最後の砦。
    """
    if not PUBLIC_SHARE_HOST:
        return None
    host = (request.host or "").split(":")[0].lower()
    if host != PUBLIC_SHARE_HOST:
        return None
    # このホストで許可するのは共有ページと共有画像のみ
    if request.path.startswith("/share/") or request.path.startswith("/share-img/"):
        return None
    from flask import abort
    abort(404)


@app.context_processor
def _inject_share_flag():
    # 全テンプレートで share_enabled を参照できるようにする
    return {"share_enabled": bool(PUBLIC_SHARE_BASE_URL)}


def db():
    """
    リクエスト単位でSQLiteコネクションを再利用し、teardownで必ず閉じる。
    途中で例外が出てもリークしない（WALロック残り対策）。
    """
    if "conn" not in g:
        g.conn = get_conn()
    return g.conn


@app.teardown_appcontext
def close_db(exc):
    conn = g.pop("conn", None)
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


def get_categories():
    """最新のカテゴリ一覧を返す（accounts.jsonから動的取得）"""
    accounts, categories = seed_accounts._load_from_json()
    return categories


_counts_cache = {"at": 0.0, "counts": None, "total": 0}
COUNTS_TTL = 60  # 秒


def get_category_counts(conn, categories_list):
    """
    カテゴリ別ツイート件数。
    以前はカテゴリ数ぶんCOUNT+LIKEをループしていた（categoriesはJSON文字列なので
    インデックスが効かず毎回フルスキャン×N回）。1クエリに畳んだうえで60秒キャッシュする。
    """
    now = time.time()
    if _counts_cache["counts"] is not None and now - _counts_cache["at"] < COUNTS_TTL:
        return _counts_cache["counts"], _counts_cache["total"]

    counts = {c: 0 for c in categories_list}
    total = 0
    # accounts.categories の組み合わせは高々アカウント数ぶんしか無いのでGROUP BYで畳める
    for r in conn.execute("""
        SELECT a.categories AS cats, COUNT(*) AS cnt
        FROM tweets t
        JOIN accounts a ON t.screen_name = a.screen_name
        GROUP BY a.categories
    """):
        total += r["cnt"]
        try:
            for c in json.loads(r["cats"] or "[]"):
                if c in counts:
                    counts[c] += r["cnt"]
        except Exception:
            pass

    _counts_cache.update({"at": now, "counts": counts, "total": total})
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
    # カテゴリをタグ用にbase64で渡す（| e がJSONを壊す既知問題の回避）
    d["categories_b64"] = base64.b64encode(
        json.dumps(d["categories_list"]).encode()).decode() if d["categories_list"] else ""

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


@app.route("/")
def index():
    category = request.args.get("category", "all")
    page = page_arg()
    offset = (page - 1) * PER_PAGE

    conn = db()
    bookmarked_ids = get_bookmarked_ids(conn)
    deleted_media = cache_utils.deleted_urls(conn)

    if category == "all":
        rows = conn.execute("""
            SELECT t.*, a.display_name, a.categories, a.profile_image_url
            FROM tweets t
            JOIN accounts a ON t.screen_name = a.screen_name
            WHERE a.categories NOT LIKE '%"R18"%'
            ORDER BY t.created_at DESC
            LIMIT ? OFFSET ?
        """, (PER_PAGE + 1, offset)).fetchall()
    else:
        rows = conn.execute("""
            SELECT t.*, a.display_name, a.categories, a.profile_image_url
            FROM tweets t
            JOIN accounts a ON t.screen_name = a.screen_name
            WHERE a.categories LIKE ?
            ORDER BY t.created_at DESC
            LIMIT ? OFFSET ?
        """, (f'%"{category}"%', PER_PAGE + 1, offset)).fetchall()

    # PER_PAGE+1件取得して、次ページがあるか判定
    has_next = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]

    tweets = [format_tweet(dict(r), bookmarked_ids, deleted_media) for r in rows]

    # 各カテゴリの件数（タブのバッジ用）
    categories_list = get_categories()
    counts, total_count = get_category_counts(conn, categories_list)

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
    error_accounts = conn.execute("""
        SELECT screen_name, COUNT(*) as err_count
        FROM scrape_log
        WHERE status = 'error'
          AND run_at > datetime('now', '-3 hours')
        GROUP BY screen_name
        HAVING err_count >= 2
        ORDER BY err_count DESC
        LIMIT 5
    """).fetchall()


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

    next_url = url_for("index", category=category, page=page + 1) if has_next else None
    if request.args.get("partial") == "1":
        return render_template("_feed.html", tweets=tweet_items, next_url=next_url)

    return render_template(
        "index.html",
        tweets=tweet_items,
        next_url=next_url,
        categories=categories_list,
        current_category=category,
        counts=counts,
        total_count=total_count,
        last_run_jst=last_run_jst,
        health=dict(health) if health else None,
        error_accounts=[dict(a) for a in error_accounts],
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


def _purge_expired_shares(conn):
    """期限切れの共有トークンを掃除する。"""
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("DELETE FROM share_tokens WHERE expires_at < ?", (now,))
        conn.commit()
    except Exception:
        pass


def _share_images_for(tweet_id, conn):
    """
    共有対象のローカル保存済み画像のファイル名リストを返す。
    削除済み画像・ローカル未保存(リモートのみ)は除外する。
    （外部公開ページなので、手元にある実ファイルだけを対象にする）
    """
    row = conn.execute(
        "SELECT media_json, local_media_json FROM tweets WHERE tweet_id=?",
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


@app.route("/api/share/prepare", methods=["POST"])
def api_share_prepare():
    """
    確認画面で確定した内容を受け取り、共有トークンを発行する。
    返す share_url を canonicalUrl にして Tumblr のシェアツールを開く。
    """
    if not PUBLIC_SHARE_BASE_URL:
        return jsonify({"ok": False,
                        "error": "共有機能が未設定です（PUBLIC_SHARE_BASE_URL）"}), 400

    tweet_id = (request.form.get("tweet_id") or "").strip()
    if not tweet_id:
        return jsonify({"ok": False, "error": "tweet_idが必要です"}), 400

    conn = db()
    _purge_expired_shares(conn)

    images = _share_images_for(tweet_id, conn)
    if not images:
        return jsonify({"ok": False,
                        "error": "共有できるローカル保存画像がありません"}), 400

    caption = (request.form.get("caption") or "").strip()
    tags_raw = (request.form.get("tags") or "").strip()
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    row = conn.execute("SELECT url FROM tweets WHERE tweet_id=?", (tweet_id,)).fetchone()
    source_url = row["url"] if row else ""

    payload = {
        "tweet_id": tweet_id,
        "images": images,
        "caption": caption,
        "tags": tags,
        "source_url": source_url,
    }

    token = secrets.token_urlsafe(16)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=SHARE_TOKEN_TTL_MIN)
    conn.execute(
        "INSERT INTO share_tokens (token, tweet_id, payload, created_at, expires_at)"
        " VALUES (?,?,?,?,?)",
        (token, tweet_id, json.dumps(payload, ensure_ascii=False),
         now.isoformat(), expires.isoformat()))
    conn.commit()

    share_url = f"{PUBLIC_SHARE_BASE_URL}/share/{token}"
    return jsonify({"ok": True, "share_url": share_url,
                    "expires_in_min": SHARE_TOKEN_TTL_MIN})


def _load_share(token, conn):
    row = conn.execute(
        "SELECT payload, expires_at FROM share_tokens WHERE token=?", (token,)).fetchone()
    if not row:
        return None
    if row["expires_at"] < datetime.now(timezone.utc).isoformat():
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


@app.route("/share/<token>")
def share_page(token):
    """
    Tumblr等が読むためのOGPページ。外部公開されるのはこのルートのみ。
    DBの中身は出さず、確定済みスナップショットの画像とキャプションだけ返す。
    """
    conn = db()
    data = _load_share(token, conn)
    if not data:
        return "リンクの有効期限が切れています。", 404

    base = PUBLIC_SHARE_BASE_URL or request.host_url.rstrip("/")
    img_urls = [f"{base}/share-img/{token}/{i}" for i in range(len(data["images"]))]
    og_image = img_urls[0] if img_urls else ""
    caption = data.get("caption") or ""
    tags = data.get("tags") or []
    title = caption or "X-Ray"

    return render_template("share.html",
                           og_image=og_image, img_urls=img_urls,
                           caption=caption, title=title, tags=tags,
                           source_url=data.get("source_url", ""),
                           canonical=f"{base}/share/{token}")


@app.route("/share-img/<token>/<int:idx>")
def share_image(token, idx):
    """共有トークン経由の画像配信。token が無効なら出さない。"""
    conn = db()
    data = _load_share(token, conn)
    if not data:
        return "not found", 404
    images = data.get("images", [])
    if idx < 0 or idx >= len(images):
        return "not found", 404
    filename = images[idx]
    if os.path.exists(os.path.join(IMAGES_DIR, filename)):
        return send_from_directory(IMAGES_DIR, filename)
    return send_from_directory(CACHE_DIR, filename)


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
    show_r18 = request.args.get("r18", "0") == "1"
    page = page_arg()
    offset = (page - 1) * PER_PAGE

    conn = db()

    where = ["t.media_json IS NOT NULL", "t.media_json != '[]'"]
    params = []
    if not show_r18:
        where.append("a.categories NOT LIKE '%\"R18\"%'")
    if category != "all":
        where.append("a.categories LIKE ?")
        params.append(f'%"{category}"%')

    where_sql = " AND ".join(where)
    params_full = params + [PER_PAGE + 1, offset]
    rows = conn.execute(f"""
        SELECT t.tweet_id, t.screen_name, t.media_json, t.local_media_json,
               t.url, t.created_at, a.display_name, a.categories
        FROM tweets t JOIN accounts a ON t.screen_name = a.screen_name
        WHERE {where_sql}
        ORDER BY t.created_at DESC
        LIMIT ? OFFSET ?
    """, params_full).fetchall()

    has_next = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]

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
            })

    next_url = (
        url_for("gallery", category=category, page=page + 1,
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
        results = [format_tweet(dict(r), bookmarked_ids, deleted_media) for r in rows]

    return render_template("search.html", results=results, query=q, categories=get_categories())


@app.route("/user/<screen_name>")
def user_profile(screen_name):
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    page = page_arg()
    offset = (page - 1) * PER_PAGE

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

    return render_template(
        "storage.html",
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
    return render_template("status.html", logs=logs, accounts=accounts)


if __name__ == "__main__":
    # Flaskの開発サーバーは本番非推奨。waitressがあればそちらを使う。
    try:
        from waitress import serve
        print("[*] waitress で起動: 0.0.0.0:5000")
        serve(app, host="0.0.0.0", port=5000, threads=8)
    except ImportError:
        print("[!] waitress未インストール。開発サーバーで起動します。")
        app.run(host="0.0.0.0", port=5000, debug=False)
