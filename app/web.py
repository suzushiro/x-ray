import json
import base64
from flask import Flask, render_template, request

from db import get_conn
from seed_accounts import ALL_CATEGORIES

app = Flask(__name__)


@app.route("/")
def index():
    category = request.args.get("category", "all")

    conn = get_conn()

    if category == "all":
        rows = conn.execute("""
            SELECT t.*, a.display_name, a.categories, a.profile_image_url
            FROM tweets t
            JOIN accounts a ON t.screen_name = a.screen_name
            ORDER BY t.created_at DESC
            LIMIT 200
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT t.*, a.display_name, a.categories, a.profile_image_url
            FROM tweets t
            JOIN accounts a ON t.screen_name = a.screen_name
            WHERE a.categories LIKE ?
            ORDER BY t.created_at DESC
            LIMIT 200
        """, (f'%"{category}"%',)).fetchall()

    # 各カテゴリの件数（タブのバッジ用）
    counts = {}
    for cat in ALL_CATEGORIES:
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

    conn.close()

    tweets = []
    for r in rows:
        d = dict(r)
        d["media"] = json.loads(d["media_json"] or "[]")
        d["videos"] = json.loads(d["video_json"] or "[]")
        d["categories_list"] = json.loads(d["categories"] or "[]")
        # media URLリストをbase64エンコード（HTML属性に安全に埋め込むため）
        d["media_b64"] = base64.b64encode(
            json.dumps(d["media"]).encode()
        ).decode() if d["media"] else ""
        tweets.append(d)

    return render_template(
        "index.html",
        tweets=tweets,
        categories=ALL_CATEGORIES,
        current_category=category,
        counts=counts,
        total_count=total_count,
        last_run=last_run["run_at"] if last_run else None,
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
