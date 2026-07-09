import json
import os
import base64
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory

JST = timezone(timedelta(hours=9))

from db import get_conn
import seed_accounts

app = Flask(__name__)


@app.template_filter("b64encode")
def b64encode_filter(s):
    """文字列/JSON文字列をbase64エンコードするJinjaフィルター"""
    if not isinstance(s, str):
        s = json.dumps(s)
    return base64.b64encode(s.encode()).decode()

COOKIES_FILE = os.environ.get("TWITTER_COOKIES_FILE", "/data/cookies.txt")
IMAGES_DIR = os.environ.get("IMAGES_DIR", "/data/images")
DB_PATH = os.environ.get("DB_PATH", "/data/data.db")
PER_PAGE = 100  # ページネーション件数


def get_categories():
    """最新のカテゴリ一覧を返す（accounts.jsonから動的取得）"""
    accounts, categories = seed_accounts._load_from_json()
    return categories


def get_bookmarked_ids(conn):
    """ブックマーク済みtweet_idのsetを返す"""
    try:
        rows = conn.execute("SELECT tweet_id FROM bookmarks").fetchall()
        return {r["tweet_id"] for r in rows}
    except Exception:
        return set()


def format_tweet(d, bookmarked_ids=None):
    """DBの行dict（tweets JOIN accounts想定）を表示用に整形する"""
    d["media"] = json.loads(d.get("media_json") or "[]")
    d["videos"] = json.loads(d.get("video_json") or "[]")
    d["local_media"] = json.loads(d.get("local_media_json") or "[]")
    d["categories_list"] = json.loads(d.get("categories") or "[]")

    # 表示用画像: ローカルがあればローカル優先、なければリモートURL
    display_imgs = []
    for i, remote in enumerate(d["media"]):
        local = d["local_media"][i] if i < len(d["local_media"]) else None
        display_imgs.append(local if local else remote)
    d["display_imgs"] = display_imgs

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

    d["is_bookmarked"] = bool(bookmarked_ids and d.get("tweet_id") in bookmarked_ids)
    d["self_reply"] = None
    return d


@app.route("/images/<path:filename>")
def serve_image(filename):
    """ローカル保存した画像を配信"""
    return send_from_directory(IMAGES_DIR, filename)


@app.route("/")
def index():
    category = request.args.get("category", "all")
    page = max(1, int(request.args.get("page", 1)))
    offset = (page - 1) * PER_PAGE

    conn = get_conn()
    bookmarked_ids = get_bookmarked_ids(conn)

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

    # 各カテゴリの件数（タブのバッジ用）
    categories_list = get_categories()
    counts = {}
    for cat in categories_list:
        c = conn.execute("""
            SELECT COUNT(*) as cnt FROM tweets t
            JOIN accounts a ON t.screen_name = a.screen_name
            WHERE a.categories LIKE ?
        """, (f'%"{cat}"%',)).fetchone()
        counts[cat] = c["cnt"]

    total_count = conn.execute("SELECT COUNT(*) as cnt FROM tweets").fetchone()["cnt"]

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
            SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as ok_count,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count
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

    conn.close()

    tweets = [format_tweet(dict(r), bookmarked_ids) for r in rows]

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

    return render_template(
        "index.html",
        tweets=tweet_items,
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
    conn = get_conn()
    db_accounts = {
        r["screen_name"]: dict(r)
        for r in conn.execute(
            "SELECT screen_name, last_scraped_at FROM accounts"
        ).fetchall()
    }
    conn.close()

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
    conn = get_conn()
    conn.execute("""
        INSERT INTO accounts (screen_name, display_name, categories)
        VALUES (?, ?, ?)
        ON CONFLICT(screen_name) DO UPDATE SET
            display_name=excluded.display_name,
            categories=excluded.categories
    """, (screen_name, display_name, json.dumps(categories, ensure_ascii=False)))
    conn.commit()
    conn.close()

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
    conn = get_conn()
    conn.execute("DELETE FROM accounts WHERE screen_name=?", (screen_name,))
    conn.execute("DELETE FROM tweets WHERE screen_name=?", (screen_name,))
    conn.commit()
    conn.close()

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

    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write("# username\tauth_token=xxxx; ct0=yyyy\n")
        for line in valid_lines:
            f.write(line + "\n")

    return jsonify({"ok": True, "count": len(valid_lines)})


@app.route("/api/bookmark/toggle", methods=["POST"])
def api_bookmark_toggle():
    """ブックマークのトグル。ツイート内容をコピー保存して永続化"""
    tweet_id = (request.form.get("tweet_id") or "").strip()
    if not tweet_id:
        return jsonify({"ok": False, "error": "tweet_idが必要です"}), 400

    conn = get_conn()
    existing = conn.execute(
        "SELECT tweet_id FROM bookmarks WHERE tweet_id=?", (tweet_id,)
    ).fetchone()

    if existing:
        conn.execute("DELETE FROM bookmarks WHERE tweet_id=?", (tweet_id,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "bookmarked": False})

    # ツイート本体を取得してコピー保存
    row = conn.execute("""
        SELECT t.*, a.display_name, a.categories, a.profile_image_url
        FROM tweets t JOIN accounts a ON t.screen_name = a.screen_name
        WHERE t.tweet_id = ?
    """, (tweet_id,)).fetchone()

    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "ツイートが見つかりません"}), 404

    d = dict(row)
    conn.execute("""
        INSERT INTO bookmarks
        (tweet_id, screen_name, display_name, content, created_at, url,
         media_json, local_media_json, video_json, categories, profile_image_url, bookmarked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        d["tweet_id"], d["screen_name"], d["display_name"], d["content"],
        d["created_at"], d["url"], d.get("media_json"), d.get("local_media_json"),
        d.get("video_json"), d.get("categories"), d.get("profile_image_url"),
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "bookmarked": True})


@app.route("/bookmarks")
def bookmarks():
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM bookmarks ORDER BY bookmarked_at DESC
    """).fetchall()
    conn.close()

    tweets = []
    for r in rows:
        d = dict(r)
        d["media"] = json.loads(d.get("media_json") or "[]")
        d["videos"] = json.loads(d.get("video_json") or "[]")
        d["local_media"] = json.loads(d.get("local_media_json") or "[]")
        d["categories_list"] = json.loads(d.get("categories") or "[]")
        display_imgs = []
        for i, remote in enumerate(d["media"]):
            local = d["local_media"][i] if i < len(d["local_media"]) else None
            display_imgs.append(local if local else remote)
        d["display_imgs"] = display_imgs
        d["media_b64"] = base64.b64encode(json.dumps(display_imgs).encode()).decode() if display_imgs else ""
        try:
            dt_jst = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00")).astimezone(JST)
            d["created_at_jst"] = dt_jst.strftime("%Y-%m-%d %H:%M")
            d["created_at_dt"] = dt_jst.strftime("%Y%m%d%H%M")
        except Exception:
            d["created_at_jst"] = (d.get("created_at") or "")[:16]
            d["created_at_dt"] = ""
        d["is_bookmarked"] = True
        d["self_reply"] = None
        tweets.append(d)

    return render_template("bookmarks.html", tweets=tweets, categories=get_categories())


@app.route("/gallery")
def gallery():
    category = request.args.get("category", "all")
    show_r18 = request.args.get("r18", "0") == "1"
    page = max(1, int(request.args.get("page", 1)))
    offset = (page - 1) * PER_PAGE

    conn = get_conn()

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
    conn.close()

    has_next = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]

    # 画像を平坦化（1画像=1グリッドアイテム）
    gallery_items = []
    for r in rows:
        d = dict(r)
        media = json.loads(d.get("media_json") or "[]")
        local = json.loads(d.get("local_media_json") or "[]")
        for i, remote in enumerate(media):
            lp = local[i] if i < len(local) else None
            gallery_items.append({
                "img": lp if lp else remote,
                "screen_name": d["screen_name"],
                "display_name": d["display_name"],
                "url": d["url"],
                "tweet_id": d["tweet_id"],
            })

    return render_template(
        "gallery.html",
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
        conn = get_conn()
        bookmarked_ids = get_bookmarked_ids(conn)
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
        conn.close()
        results = [format_tweet(dict(r), bookmarked_ids) for r in rows]

    return render_template("search.html", results=results, query=q, categories=get_categories())


@app.route("/user/<screen_name>")
def user_profile(screen_name):
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    page = max(1, int(request.args.get("page", 1)))
    offset = (page - 1) * PER_PAGE

    conn = get_conn()
    bookmarked_ids = get_bookmarked_ids(conn)

    acc = conn.execute(
        "SELECT * FROM accounts WHERE screen_name = ?", (screen_name,)
    ).fetchone()
    if not acc:
        conn.close()
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

    # 統計
    stats = conn.execute("""
        SELECT COUNT(*) as total,
               SUM(like_count) as total_likes,
               MIN(created_at) as oldest,
               MAX(created_at) as newest
        FROM tweets WHERE screen_name = ?
    """, (screen_name,)).fetchone()
    conn.close()

    has_next = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]
    tweets = [format_tweet(dict(r), bookmarked_ids) for r in rows]

    return render_template(
        "user.html",
        account=acc,
        tweets=tweets,
        stats=dict(stats) if stats else {},
        date_from=date_from,
        date_to=date_to,
        page=page,
        has_next=has_next,
    )


def _dir_size(path):
    """ディレクトリの合計サイズと件数をベストエフォートで取得"""
    total = 0
    count = 0
    if os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                    count += 1
                except Exception:
                    pass
    return total, count


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

    img_size, img_count = _dir_size(IMAGES_DIR)

    # ディスク使用量
    try:
        du = shutil.disk_usage(os.path.dirname(DB_PATH) or "/")
        disk_total, disk_used, disk_free = du.total, du.used, du.free
    except Exception:
        disk_total = disk_used = disk_free = 0

    # 統計
    conn = get_conn()
    tweet_count = conn.execute("SELECT COUNT(*) c FROM tweets").fetchone()["c"]
    bookmark_count = conn.execute("SELECT COUNT(*) c FROM bookmarks").fetchone()["c"]
    conn.close()

    return render_template(
        "storage.html",
        db_size=_fmt_size(db_size),
        img_size=_fmt_size(img_size),
        img_count=img_count,
        total_size=_fmt_size(db_size + img_size),
        disk_total=_fmt_size(disk_total),
        disk_used=_fmt_size(disk_used),
        disk_free=_fmt_size(disk_free),
        disk_pct=round(disk_used / disk_total * 100, 1) if disk_total else 0,
        tweet_count=tweet_count,
        bookmark_count=bookmark_count,
    )


@app.route("/status")
def status():
    conn = get_conn()
    logs = conn.execute("""
        SELECT * FROM scrape_log ORDER BY run_at DESC LIMIT 50
    """).fetchall()
    accounts = conn.execute("""
        SELECT screen_name, display_name, last_scraped_at FROM accounts
        ORDER BY last_scraped_at DESC
    """).fetchall()
    conn.close()
    return render_template("status.html", logs=logs, accounts=accounts)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
