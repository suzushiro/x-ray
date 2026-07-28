"""検証専用のダミーデータ投入スクリプト（本番では使わない）。

全ルートのレンダリングを実際に通すため、以下を含むデータを作る:
  - 引用RTあり（画像あり / 画像なし）のツイート
  - ローカル保存画像あり / リモートのみ / 動画あり
  - 自己リプライ（スレッド）
  - ブックマーク（quoted_json コピー済み）
  - scrape_log に ok / error 両方
"""
import os
import sys

# dev/ から app/ のモジュールを読めるようにする
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import json
import os
from datetime import datetime, timedelta, timezone

from db import get_conn, init_db
import cache_utils

JST = timezone(timedelta(hours=9))
now = datetime.now(JST)


def iso(minutes_ago):
    return (now - timedelta(minutes=minutes_ago)).isoformat()


ACCOUNTS = [
    ("alice_art", "Alice / 絵かき", ["illustrator", "ギャル"], "1001",
     "https://pbs.twimg.com/profile_images/alice.jpg"),
    ("bob_photo", "Bob Photography", ["photographer"], "1002",
     "https://pbs.twimg.com/profile_images/bob.jpg"),
    ("carol_dev", "きゃろる", ["gadget"], "1003", None),
    ("dave_none", "Dave (未分類)", [], "1004", None),
]

QUOTED_WITH_MEDIA = {
    "tweet_id": "900001",
    "screen_name": "quoted_user",
    "display_name": "引用元アカウント",
    "profile_image_url": "https://pbs.twimg.com/profile_images/quoted.jpg",
    "content": "これは引用元の投稿です。画像が2枚ついています。\n改行も含む。",
    "url": "https://x.com/quoted_user/status/900001",
    "created_at": iso(600),
    "media": [
        "https://pbs.twimg.com/media/qqq1.jpg",
        "https://pbs.twimg.com/media/qqq2.jpg",
    ],
    "like_count": 1234,
    "retweet_count": 56,
}

QUOTED_TEXT_ONLY = {
    "tweet_id": "900002",
    "screen_name": "another_user",
    "display_name": "テキストのみ引用元",
    "profile_image_url": "",
    "content": "画像なしのテキストだけの引用元。",
    "url": "https://x.com/another_user/status/900002",
    "created_at": iso(700),
    "media": [],
    "like_count": 7,
    "retweet_count": 0,
}

QUOTED_SINGLE_MEDIA = dict(QUOTED_WITH_MEDIA,
                           tweet_id="900003",
                           media=["https://pbs.twimg.com/media/qqq3.jpg"],
                           content="画像1枚だけの引用元（single クラス確認用）")

# (tweet_id, screen_name, content, minutes_ago, media, local_media, video, reply_to, quoted)
TWEETS = [
    ("100001", "alice_art", "引用RT + 自前画像2枚のケース。#テスト", 5,
     ["https://pbs.twimg.com/media/aaa1.jpg", "https://pbs.twimg.com/media/aaa2.jpg"],
     ["aaa1.jpg", "aaa2.jpg"], None, None, QUOTED_WITH_MEDIA),
    ("100002", "alice_art", "上の投稿への自己リプライ（スレッド）。", 4,
     [], [], None, "100001", None),
    ("100003", "bob_photo", "テキストのみ引用元をRTしたケース。", 20,
     [], [], None, None, QUOTED_TEXT_ONLY),
    ("100004", "bob_photo", "引用元が画像1枚のケース。", 25,
     [], [], None, None, QUOTED_SINGLE_MEDIA),
    ("100005", "carol_dev", "動画つきの投稿。", 40,
     [], [],
     [{"thumb": "https://pbs.twimg.com/ext_tw_video_thumb/vvv1.jpg",
       "url": "https://video.twimg.com/vvv1.mp4"}], None, None),
    ("100006", "carol_dev", "リモート画像のみ（ローカル未保存）。", 60,
     ["https://pbs.twimg.com/media/ccc1.jpg"], [], None, None, None),
    ("100007", "dave_none", "画像も引用もない素のテキスト投稿。URL入り https://example.com", 90,
     [], [], None, None, None),
    ("100008", "dave_none", "検索用キーワード: カメラ レンズ 作例", 120,
     [], [], None, None, None),
]


def j(v):
    return json.dumps(v, ensure_ascii=False) if v else None


def main():
    init_db()
    conn = get_conn()
    cur = conn.cursor()

    for sn, dn, cats, uid, pic in ACCOUNTS:
        cur.execute("""
        INSERT INTO accounts (screen_name, display_name, categories, user_id,
                              profile_image_url, last_scraped_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(screen_name) DO UPDATE SET display_name=excluded.display_name
        """, (sn, dn, json.dumps(cats, ensure_ascii=False), uid, pic, iso(3)))

    for (tid, sn, content, mins, media, local, video, reply_to, quoted) in TWEETS:
        cur.execute("""
        INSERT OR REPLACE INTO tweets
        (tweet_id, screen_name, content, created_at, url,
         like_count, retweet_count, reply_count, media_json, video_json,
         local_media_json, reply_to_tweet_id, quoted_json, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (tid, sn, content, iso(mins), f"https://x.com/{sn}/status/{tid}",
              int(tid[-2:]) * 3, int(tid[-2:]), 2,
              j(media), j(video), j(local), reply_to, j(quoted), iso(1)))

        try:
            cur.execute(
                "INSERT INTO tweets_fts (tweet_id, content, screen_name, display_name) "
                "VALUES (?,?,?,?)", (tid, content, sn, sn))
        except Exception as e:
            print(f"[!] FTS insert skipped: {e}")

    # ブックマーク（引用RTありを1件、素のものを1件）
    for tid in ("100001", "100007"):
        row = cur.execute("SELECT * FROM tweets WHERE tweet_id=?", (tid,)).fetchone()
        acc = cur.execute("SELECT * FROM accounts WHERE screen_name=?",
                          (row["screen_name"],)).fetchone()
        cur.execute("""
        INSERT OR REPLACE INTO bookmarks
        (tweet_id, screen_name, display_name, content, created_at, url,
         media_json, local_media_json, video_json, categories,
         profile_image_url, quoted_json, bookmarked_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (row["tweet_id"], row["screen_name"], acc["display_name"], row["content"],
              row["created_at"], row["url"], row["media_json"], row["local_media_json"],
              row["video_json"], acc["categories"], acc["profile_image_url"],
              row["quoted_json"], iso(2)))

    # scrape_log: ok / error を混在（statusページの警告表示確認用）
    logs = [
        (iso(5), "alice_art", "ok", "", 2),
        (iso(5), "bob_photo", "ok", "", 0),
        (iso(5), "carol_dev", "error", "NoAccountError: no account available", 0),
        (iso(20), "carol_dev", "ok", "", 1),
        (iso(5), "dave_none", "error", "Couldn't get XClientTxId indices script", 0),
    ]
    for run_at, sn, status, msg, n in logs:
        cur.execute("INSERT INTO scrape_log (run_at, screen_name, status, message, new_tweets)"
                    " VALUES (?,?,?,?,?)", (run_at, sn, status, msg, n))

    cur.execute("INSERT OR REPLACE INTO media_index (remote_url, filename, persist, sha256,"
                " created_at) VALUES (?,?,?,?,?)",
                ("https://pbs.twimg.com/media/aaa1.jpg", "aaa1.jpg", 1, "deadbeef", iso(5)))

    conn.commit()
    conn.close()

    # ローカル画像ファイルの実体（1x1 PNG）も置いておく
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
        "de0000000c4944415408d763f8cfc0000003010100b5dcb0a20000000049454e44ae426082")
    for d in (cache_utils.IMAGES_DIR, cache_utils.CACHE_DIR):
        os.makedirs(d, exist_ok=True)
    for name in ("aaa1.jpg", "aaa2.jpg"):
        with open(os.path.join(cache_utils.IMAGES_DIR, name), "wb") as f:
            f.write(png)

    print("dummy data seeded.")


if __name__ == "__main__":
    main()
