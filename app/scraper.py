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
import shutil
import sys
import hashlib
import urllib.request
from datetime import datetime, timedelta, timezone

from twscrape import API, gather
from twscrape.logger import set_log_level

from db import get_conn, init_db
from cache_utils import (
    CACHE_DIR,
    IMAGES_DIR,
    PERSIST_CATEGORIES,
    file_sha256,
    is_deleted,
    link_or_copy,
    should_persist,
)

set_log_level("WARNING")

ACCOUNTS_FILE = os.environ.get("TWITTER_ACCOUNTS_FILE", "/data/accounts.txt")
TWEETS_PER_USER = int(os.environ.get("TWEETS_PER_USER", "10"))
SAVE_IMAGES = os.environ.get("SAVE_IMAGES", "1") == "1"  # 画像ローカル保存の有効/無効
# twscrapeのXアカウントプールDB。デフォルトだとcwd(/app)に作られ、
# docker compose build のたびに消えてクッキー再登録が必要になるため /data に置く。
TWSCRAPE_DB = os.environ.get("TWSCRAPE_DB", "/data/accounts_pool.db")
# IMAGES_DIR(永続) / CACHE_DIR(表示用) / PERSIST_CATEGORIES は cache_utils で定義


def media_key(url: str) -> str:
    """クエリ(?format=&name=)を除いた正規化URL。リポストでも同一になる。"""
    return url.split("?")[0]


def lookup_media(conn, url: str):
    """既にDL済みの同一画像があればそのファイル名を返す（重複排除）"""
    row = conn.execute(
        "SELECT filename FROM media_index WHERE remote_url=?", (media_key(url),)
    ).fetchone()
    if not row:
        return None
    fn = row["filename"]
    for d in (IMAGES_DIR, CACHE_DIR):
        fp = os.path.join(d, fn)
        if os.path.exists(fp):
            return fp
    return None  # 実体が消えている（キャッシュ削除済み等）→ 再DL


def download_image(url: str, screen_name: str, tweet_id: str, idx: int,
                   persist: bool = False, conn=None) -> str | None:
    """
    画像をローカルに保存し、URLパス（/images/... or /cache/...）を返す。失敗時はNone。

    persist=True  → /data/images （永続）
    persist=False → /data/cache  （表示用キャッシュ）

    重複排除:
      同じ画像URL（リポスト/引用RT等）が既にDL済みなら再DLせず、
      ハードリンクを張って実データを共有する（追加ディスク消費ゼロ）。
    """
    if not SAVE_IMAGES:
        return None
    try:
        # 拡張子を推定
        base = url.split("?")[0]
        ext = "jpg"
        if "." in base.split("/")[-1]:
            ext = base.split(".")[-1][:4]
        if "format=png" in url:
            ext = "png"
        elif "format=jpg" in url or "format=jpeg" in url:
            ext = "jpg"

        filename = f"{screen_name}_{tweet_id}_{idx}.{ext}"
        persist_path = os.path.join(IMAGES_DIR, filename)
        cache_path = os.path.join(CACHE_DIR, filename)

        # 永続にあれば永続優先（キャッシュ側に残骸があれば掃除）
        if os.path.exists(persist_path):
            if os.path.exists(cache_path):
                try:
                    os.remove(cache_path)
                except Exception:
                    pass
            return f"/images/{filename}"

        if persist:
            os.makedirs(IMAGES_DIR, exist_ok=True)
            if os.path.exists(cache_path):
                shutil.move(cache_path, persist_path)  # 昇格
                return f"/images/{filename}"
            target, url_path = persist_path, f"/images/{filename}"
        else:
            os.makedirs(CACHE_DIR, exist_ok=True)
            if os.path.exists(cache_path):
                return f"/cache/{filename}"
            target, url_path = cache_path, f"/cache/{filename}"

        # --- 重複排除: 同一URLの実体が既にあればハードリンクで共有 ---
        own_conn = conn is None
        conn = conn or get_conn()
        try:
            existing = lookup_media(conn, url)
            if existing:
                if link_or_copy(existing, target):
                    print(f"    [=] 重複: {os.path.basename(existing)} → {filename} (リンク)")
                    return url_path

            # X画像は&name=origでオリジナルサイズ取得
            dl_url = url
            if "pbs.twimg.com" in url and "name=" not in url:
                sep = "&" if "?" in url else "?"
                dl_url = f"{url}{sep}name=orig"

            req = urllib.request.Request(dl_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            with open(target, "wb") as f:
                f.write(data)

            # 次回以降の重複判定用に登録
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO media_index"
                    "(remote_url, filename, persist, sha256, created_at) VALUES (?,?,?,?,?)",
                    (media_key(url), filename, 1 if persist else 0,
                     file_sha256(target), datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            except Exception as e:
                print(f"[!] media_index登録失敗: {e}")

            return url_path
        finally:
            if own_conn:
                conn.close()
    except Exception as e:
        print(f"[!] 画像DL失敗 {url[:50]}: {e}")
        return None


def merge_local_paths(conn, tweet_id: str, new_paths: list) -> list:
    """
    既存の local_media_json と新しい取得結果をインデックス単位でマージする。

    再スクレイプ時に画像DLが失敗すると new_paths に None が入るため、
    そのまま上書きすると保存済みアーカイブのパスを失ってしまう。
    新しい値が None の箇所だけ既存値を引き継ぐ（4枚中1枚だけ失敗、にも対応）。
    """
    row = conn.execute(
        "SELECT local_media_json FROM tweets WHERE tweet_id=?", (tweet_id,)
    ).fetchone()
    if not row:
        return new_paths
    try:
        old = json.loads(row["local_media_json"] or "[]")
    except Exception:
        return new_paths

    merged = []
    for i, p in enumerate(new_paths):
        if p:
            merged.append(p)
        elif i < len(old) and old[i]:
            merged.append(old[i])  # DL失敗 → 既存パスを維持
        else:
            merged.append(None)
    return merged


def get_account_categories(conn, screen_name: str) -> list:
    """accounts テーブルからカテゴリ一覧を取得"""
    row = conn.execute(
        "SELECT categories FROM accounts WHERE screen_name=?", (screen_name,)
    ).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["categories"] or "[]")
    except Exception:
        return []


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

    # アカウントのカテゴリで永続/キャッシュを判定
    cats = get_account_categories(conn, screen_name)
    persist = should_persist(cats)

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

        # 画像をローカル保存（オリジナルサイズ）
        # 永続カテゴリなら /data/images、それ以外は /data/cache
        local_paths = []
        for i, purl in enumerate(photo_urls):
            # 手動削除済みの画像は再取得しない（墓標が残っている限り永久に）
            if is_deleted(conn, purl):
                local_paths.append(None)
                continue
            lp = download_image(purl, screen_name, str(t.id), i + 1,
                                persist=persist, conn=conn)
            local_paths.append(lp)  # 失敗時はNoneが入る

        # DL失敗で既存のアーカイブパスを潰さないようマージ
        if photo_urls:
            local_paths = merge_local_paths(conn, str(t.id), local_paths)

        # 自己リプライ元のtweet_idを取得
        reply_to_id = None
        if t.inReplyToTweetId and t.inReplyToUser:
            if str(t.inReplyToUser.username).lower() == screen_name.lower():
                reply_to_id = str(t.inReplyToTweetId)

        # 引用RTの引用元をスナップショット保存（画像はリモートURLのまま＝容量節約）
        quoted_json = None
        if t.quotedTweet:
            q = t.quotedTweet
            try:
                q_photos = [p.url for p in q.media.photos] if (q.media and q.media.photos) else []
            except Exception:
                q_photos = []
            quoted = {
                "tweet_id": str(q.id),
                "screen_name": q.user.username if q.user else "",
                "display_name": q.user.displayname if q.user else "",
                "profile_image_url": q.user.profileImageUrl if q.user else "",
                "content": q.rawContent or "",
                "url": q.url,
                "created_at": q.date.isoformat() if q.date else "",
                "media": q_photos,
                "like_count": q.likeCount or 0,
                "retweet_count": q.retweetCount or 0,
            }
            quoted_json = json.dumps(quoted, ensure_ascii=False)

        cur.execute("""
        INSERT INTO tweets
        (tweet_id, screen_name, content, created_at, url,
         like_count, retweet_count, reply_count, media_json, video_json,
         local_media_json, reply_to_tweet_id, quoted_json, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tweet_id) DO UPDATE SET
            like_count=excluded.like_count,
            retweet_count=excluded.retweet_count,
            reply_count=excluded.reply_count,
            local_media_json=excluded.local_media_json,
            quoted_json=COALESCE(excluded.quoted_json, tweets.quoted_json)
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
            json.dumps(local_paths, ensure_ascii=False),
            reply_to_id,
            quoted_json,
            datetime.now(timezone.utc).isoformat(),
        ))
        is_new = cur.rowcount and cur.lastrowid
        if is_new:
            new_count += 1

        # FTS5に同期（重複を避けるため一旦削除して挿入）
        try:
            cur.execute("DELETE FROM tweets_fts WHERE tweet_id = ?", (str(t.id),))
            cur.execute("""
                INSERT INTO tweets_fts (tweet_id, content, screen_name, display_name)
                VALUES (?, ?, ?, ?)
            """, (str(t.id), t.rawContent or "", screen_name, ""))
        except Exception:
            pass  # FTS非対応環境では無視

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


SCRAPE_LOG_RETENTION_DAYS = int(os.environ.get("SCRAPE_LOG_RETENTION_DAYS", "7") or 7)


def prune_scrape_log(days: int | None = None) -> int:
    """
    古いスクレイプログを削除する。

    scrape_log は「15分 x アカウント数」で増えるため放置すると肥大化する。
    （85垢なら1日あたり約8000行）
    戻り値: 削除した行数。
    """
    days = SCRAPE_LOG_RETENTION_DAYS if days is None else days
    if days <= 0:
        return 0     # 0以下なら保持期限なし＝削除しない

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM scrape_log WHERE run_at < ?", (cutoff,))
        n = cur.rowcount or 0
        conn.commit()
        if n:
            print(f"[*] スクレイプログを{n}件削除（{days}日より古いもの）")
        return n
    except Exception as e:
        print(f"[!] スクレイプログの削除に失敗: {e}")
        return 0
    finally:
        conn.close()


def get_all_screen_names():
    conn = get_conn()
    rows = conn.execute("SELECT screen_name FROM accounts").fetchall()
    conn.close()
    return [r["screen_name"] for r in rows]


async def scrape_all():
    api = API(TWSCRAPE_DB)

    accounts = await api.pool.accounts_info()
    if not accounts:
        print("[!] twscrapeにXアカウントが登録されていません。")
        print("    先に: docker exec -it x-ray-worker python scraper.py add-accounts")
        return

    screen_names = get_all_screen_names()
    print(f"[*] {len(screen_names)}件の監視対象を取得開始")
    print(f"[*] 永続カテゴリ: {', '.join(PERSIST_CATEGORIES)} → {IMAGES_DIR}")
    print(f"[*] それ以外はキャッシュ → {CACHE_DIR}")

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

    prune_scrape_log()
    print("[*] 全件取得完了")


EXPECTED_EGRESS_IP = os.environ.get("EXPECTED_EGRESS_IP", "").strip()


def check_egress(verbose: bool = True) -> dict:
    """
    実際の出口グローバルIPを調べ、EXPECTED_EGRESS_IP と照合する。

    proxy環境変数の設定漏れは分離の抜け穴になる（Dockerホストが
    IPv6グローバルを持つため、素通しだとau側から出てしまう）。
    スクレイプ前に毎回これを通し、想定外の出口なら中断する。

    戻り値: {ok, ip, expected, proxy, error?}
    """
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "")
    result = {"ok": False, "ip": None, "expected": EXPECTED_EGRESS_IP, "proxy": proxy}

    # IPv4を強制して問い合わせる。IPv6で抜けている場合を検出するため。
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=15) as r:
                result["ip"] = r.read().decode("utf-8", "replace").strip()
                break
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"

    if not result["ip"]:
        if verbose:
            print(f"[!] 出口IPを取得できませんでした: {result.get('error', '')}")
        return result

    if not EXPECTED_EGRESS_IP:
        result["ok"] = True
        if verbose:
            print(f"[*] 出口IP: {result['ip']}（EXPECTED_EGRESS_IP 未設定のため照合なし）")
        return result

    result["ok"] = result["ip"] == EXPECTED_EGRESS_IP
    if verbose:
        if result["ok"]:
            print(f"[+] 出口IP: {result['ip']}（想定どおり / proxy={proxy or 'なし'}）")
        else:
            print(f"[!] 出口IPが想定と違います: {result['ip']} "
                  f"(想定 {EXPECTED_EGRESS_IP} / proxy={proxy or 'なし'})")
            print("[!] proxy環境変数の設定漏れ、またはIPv6経由で抜けている可能性があります。")
    return result


async def main():
    init_db()
    api = API(TWSCRAPE_DB)

    if len(sys.argv) > 1 and sys.argv[1] == "add-accounts":
        await add_accounts_from_file(api)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "add-cookies":
        await add_accounts_via_cookies(api)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "check-egress":
        res = check_egress()
        sys.exit(0 if res["ok"] else 1)

    if len(sys.argv) > 1 and sys.argv[1] == "relogin":
        await api.pool.login_all()
        print("[+] 再ログイン完了")
        return

    # 出口の確認。想定と違えばスクレイプしない（分離が崩れた状態で走らせない）
    if EXPECTED_EGRESS_IP:
        eg = check_egress()
        if not eg["ok"]:
            print("[!] 出口IPが想定と異なるためスクレイプを中止します。")
            print("[!] 意図的に無効化する場合は EXPECTED_EGRESS_IP を空にしてください。")
            sys.exit(1)

    await scrape_all()


if __name__ == "__main__":
    asyncio.run(main())
