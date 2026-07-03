import json
import base64
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request

JST = timezone(timedelta(hours=9))

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
            WHERE a.categories NOT LIKE '%"R18"%'
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
        d["media_b64"] = base64.b64encode(
            json.dumps(d["media"]).encode()
        ).decode() if d["media"] else ""
        try:
            dt_utc = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
            dt_jst = dt_utc.astimezone(JST)
            d["created_at_jst"] = dt_jst.strftime("%Y-%m-%d %H:%M")
            d["created_at_dt"] = dt_jst.strftime("%Y%m%d%H%M")
        except Exception:
            d["created_at_jst"] = d["created_at"][:16].replace("T", " ")
            d["created_at_dt"] = d["created_at"][:16].replace("-","").replace("T","").replace(":","")
        d["self_reply"] = None  # ぶら下がりリプライ（直前1件）
        tweets.append(d)

    # 自己リプライをグルーピング
    # tweet_idをキーにしたdict
    tweet_map = {t["tweet_id"]: t for t in tweets}
    # リプライとして使われるtweet_idのset（フィードから除外するため）
    reply_ids = set()

    for t in tweets:
        rid = t.get("reply_to_tweet_id")
        if rid and rid in tweet_map:
            parent = tweet_map[rid]
            # 同一アカウントの自己リプライのみぶら下げる
            if parent["screen_name"] == t["screen_name"]:
                # 既にself_replyがある場合は上書きしない（直前1件のみ）
                if parent["self_reply"] is None:
                    parent["self_reply"] = t
                    reply_ids.add(t["tweet_id"])

    # リプライとして使われてるものをフィードから除外
    tweet_items = [t for t in tweets if t["tweet_id"] not in reply_ids]

    return render_template(
        "index.html",
        tweets=tweet_items,
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
