"""
30分おきに cron から実行されるスクレイプスクリプト。
twscrape で各監視対象アカウントの最新ツイートを取得し、SQLiteに保存する。

事前準備:
    docker exec -it x-ray-worker python scraper.py add-accounts
    （アカウント追加は accounts.txt を読み込んで一括登録）
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from twscrape import API, gather
from twscrape.logger import set_log_level

from db import get_conn, init_db

set_log_level("WARNING")

ACCOUNTS_FILE = os.environ.get("TWITTER_ACCOUNTS_FILE", "/data/accounts.txt")
TWEETS_PER_USER = int(os.environ.get("TWEETS_PER_USER", "10"))


async def add_accounts_via_cookies(api: API):
    """
    cookies.txt フォーマット (1行1アカウント、タブ区切り):
    username<TAB>auth_token=xxxx; ct0=yyyy

    ブラウザでXに手動ログイン後、開発者ツール(F12) > Application > Cookies で
    auth_token と ct0 の値をコピーして使う。
    パスワードログインを行わないため、bot判定によるブロックを受けにくい。
    """
    cookies_file = os.environ.get("TWITTER_COOKIES_FILE", "/data/cookies.txt")

    if not os.path.exists(cookies_file):
        print(f"[!] {cookies_file} が見つかりません。サンプルを作成します。")
        with open(cookies_file, "w") as f:
            f.write("# username\tauth_token=xxxx; ct0=yyyy\n")
        return

    with open(cookies_file) as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    success = 0
    for line in lines:
        parts = line.split("\t", 1)
        if len(parts) != 2:
            print(f"[!] フォーマット不正、スキップ: {line[:30]}...")
            continue
        username, cookie_str = parts
        try:
            await api.pool.add_account_cookies(username, cookie_str.strip())
            print(f"[+] クッキーでアカウント登録: {username}")
            success += 1
        except Exception as e:
            print(f"[!] 登録失敗 {username}: {e}")

    print(f"[+] {success}/{len(lines)} 件のアカウントをクッキー認証で登録しました")
    print("    パスワードログイン不要のため、即座にアクティブ状態になります")


async def add_accounts_from_file(api: API):
    """
    accounts.txt フォーマット (1行1アカウント、タブ区切り):
    username<TAB>password<TAB>email<TAB>email_password
    """
    if not os.path.exists(ACCOUNTS_FILE):
        print(f"[!] {ACCOUNTS_FILE} が見つかりません。サンプルを作成します。")
        with open(ACCOUNTS_FILE, "w") as f:
            f.write("# username\tpassword\temail\temail_password\n")
        return

    with open(ACCOUNTS_FILE) as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    accounts_added = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) != 4:
            print(f"[!] フォーマット不正、スキップ: {line}")
            continue
        username, password, email, email_password = parts
        try:
            await api.pool.add_account(username, password, email, email_password)
            print(f"[+] アカウント追加: {username}")
            accounts_added.append(username)
        except Exception as e:
            print(f"[!] アカウント追加失敗 {username}: {e}")

    # 1垢ずつ間隔を空けてログイン（連続ログインによるbot判定を避ける）
    print(f"[*] {len(accounts_added)}垢を間隔を空けてログインします")
    for i, username in enumerate(accounts_added):
        try:
            counter = await api.pool.login_all(usernames=[username])
            if counter.get("success", 0) > 0:
                print(f"[+] ログイン成功: {username}")
            else:
                print(f"[!] ログイン失敗: {username}")
        except Exception as e:
            print(f"[!] ログイン失敗 {username}: {e}")

        if i < len(accounts_added) - 1:
            wait_sec = 25
            print(f"    {wait_sec}秒待機中...")
            await asyncio.sleep(wait_sec)

    print("[+] ログイン処理完了")


def save_tweets(screen_name: str, tweets: list):
    conn = get_conn()
    cur = conn.cursor()
    new_count = 0

    for t in tweets:
        photo_urls = []
        video_items = []

        if t.media and t.media.photos:
            photo_urls = [p.url for p in t.media.photos]
        if t.media and t.media.videos:
            for v in t.media.videos:
                video_items.append({
                    "thumb": v.thumbnailUrl,
                    "url": t.url,
                })

        # 自己リプライ元のtweet_idを取得
        reply_to_id = None
        if t.inReplyToTweetId and t.inReplyToUser:
            # 同一アカウントへのリプライのみ記録
            if str(t.inReplyToUser.username).lower() == screen_name.lower():
                reply_to_id = str(t.inReplyToTweetId)

        cur.execute("""
        INSERT INTO tweets
        (tweet_id, screen_name, content, created_at, url,
         like_count, retweet_count, reply_count, media_json, video_json, reply_to_tweet_id, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tweet_id) DO UPDATE SET
            like_count=excluded.like_count,
            retweet_count=excluded.retweet_count,
            reply_count=excluded.reply_count
        """, (
            str(t.id),
            screen_name,
            t.rawContent,
            t.date.isoformat(),
            t.url,
            t.likeCount or 0,
            t.retweetCount or 0,
            t.replyCount or 0,
            json.dumps(photo_urls, ensure_ascii=False),
            json.dumps(video_items, ensure_ascii=False),
            reply_to_id,
            datetime.now(timezone.utc).isoformat(),
        ))
        if cur.rowcount and cur.lastrowid:
            new_count += 1

    conn.commit()
    conn.close()
    return new_count


def log_result(screen_name, status, message="", new_tweets=0):
    conn = get_conn()
    conn.execute("""
        INSERT INTO scrape_log (run_at, screen_name, status, message, new_tweets)
        VALUES (?, ?, ?, ?, ?)
    """, (datetime.now(timezone.utc).isoformat(), screen_name, status, message, new_tweets))
    conn.commit()
    conn.close()


def get_all_screen_names():
    conn = get_conn()
    rows = conn.execute("SELECT screen_name FROM accounts").fetchall()
    conn.close()
    return [r["screen_name"] for r in rows]


async def scrape_all():
    api = API()

    accounts = await api.pool.accounts_info()
    if not accounts:
        print("[!] twscrapeにXアカウントが登録されていません。")
        print("    先に: docker exec -it x-ray-worker python scraper.py add-accounts")
        return

    screen_names = get_all_screen_names()
    print(f"[*] {len(screen_names)}件の監視対象を取得開始")

    for screen_name in screen_names:
        try:
            user = await api.user_by_login(screen_name)
            if not user:
                log_result(screen_name, "error", "user not found")
                print(f"[!] {screen_name}: ユーザーが見つかりません")
                continue

            tweets = await gather(api.user_tweets(user.id, limit=TWEETS_PER_USER))
            new_count = save_tweets(screen_name, tweets)

            conn = get_conn()
            conn.execute(
                "UPDATE accounts SET user_id=?, profile_image_url=?, last_scraped_at=? WHERE screen_name=?",
                (str(user.id), user.profileImageUrl, datetime.now(timezone.utc).isoformat(), screen_name)
            )
            conn.commit()
            conn.close()

            log_result(screen_name, "ok", new_tweets=new_count)
            print(f"[+] {screen_name}: {len(tweets)}件取得 (新規{new_count}件)")

        except Exception as e:
            log_result(screen_name, "error", str(e))
            print(f"[!] {screen_name}: エラー {e}")

        await asyncio.sleep(2)  # レート制限緩和のためアカウント間で少し待つ

    print("[*] 全件取得完了")


async def main():
    init_db()
    api = API()

    if len(sys.argv) > 1 and sys.argv[1] == "add-accounts":
        await add_accounts_from_file(api)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "add-cookies":
        await add_accounts_via_cookies(api)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "relogin":
        await api.pool.login_all()
        print("[+] 再ログイン完了")
        return

    await scrape_all()


if __name__ == "__main__":
    asyncio.run(main())
