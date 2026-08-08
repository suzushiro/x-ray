#!/usr/bin/env python3
"""X のクッキー(auth_token / ct0)を垢ごとの永続ブラウザプロファイルから収集する。

F12 → ストレージ → コピー → タブ区切りで貼る、の手作業を置き換える。
ログイン操作そのものは人間がやる（パスワードはこのスクリプトに渡さない）。

使い方:
    # 初回。ログイン画面が出たら手でログインする。垢ごとに順番に開く
    python cookie_harvester.py --out ../data/cookies.txt

    # 2周目以降。プロファイルが生きているので無操作で取り直せる
    python cookie_harvester.py --refresh --push http://example-host:8501

    # 書き込まずに状態だけ見る（期限切れ検知）
    python cookie_harvester.py --check

アカウント一覧は accounts.txt（1行1ユーザー名）から読む。
無ければ既存の cookies.txt のユーザー名を流用する。
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
DEFAULT_ACCOUNTS = HERE / "accounts.txt"
DEFAULT_PROFILES = HERE / "profiles"

X_HOME = "https://x.com/home"
# ログインを促されている状態のURL
LOGIN_MARKERS = ("/login", "/i/flow/login", "/i/flow/signup", "/?logout")

STATUS_OK = "ok"
STATUS_LOGIN = "login_required"
STATUS_ERROR = "error"


# ---------------------------------------------------------------- 純ロジック
# （ブラウザに触らない部分。playwright 無しでもテストできるよう分離してある）

def load_accounts(accounts_path, cookies_path=None):
    """ユーザー名リストを取得。accounts.txt 優先、無ければ cookies.txt から拾う。"""
    p = Path(accounts_path)
    if p.exists():
        names = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line.lstrip("@"))
        if names:
            return names

    if cookies_path and Path(cookies_path).exists():
        return list(read_cookies_file(cookies_path).keys())

    return []


def read_cookies_file(path):
    """既存 cookies.txt を {username: cookie_str} で読む。壊れた行は無視。"""
    out = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        out[parts[0].strip()] = parts[1].strip()
    return out


def build_cookie_str(auth_token, ct0):
    return f"auth_token={auth_token}; ct0={ct0}"


def build_cookies_text(entries):
    """{username: cookie_str} を cookies.txt の中身にする。"""
    lines = ["# username\tauth_token=xxxx; ct0=yyyy"]
    for username, cookie_str in entries.items():
        lines.append(f"{username}\t{cookie_str}")
    return "\n".join(lines) + "\n"


def merge_entries(previous, harvested):
    """
    今回取れた分で上書きしつつ、取れなかった垢は前回の値を残す。

    これが無いと「7垢中3垢だけ成功」した時に残り4垢の行が消え、
    まだ生きているセッションまで巻き込んで落とすことになる。
    """
    merged = dict(previous)
    merged.update(harvested)
    kept = [u for u in previous if u not in harvested]
    return merged, kept


def push_cookies(base_url, cookies_text, timeout=20, merge=True):
    """web の /api/cookies/update に POST する。(ok, メッセージ) を返す。

    merge=True なら未取得の垢の行はサーバー側で保持される。
    """
    url = base_url.rstrip("/") + "/api/cookies/update"
    payload = {"cookies_text": cookies_text}
    if merge:
        payload["merge"] = "1"
    data = urlencode(payload).encode()
    req = Request(url, data=data, method="POST",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return True, f"HTTP {r.status} {r.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:
        body = ""
        if hasattr(e, "read"):
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
        return False, f"{type(e).__name__}: {e} {body}"


def fmt_expiry(expires):
    """cookie の expires(unix秒) を『あと N 日』表記にする。-1 はセッションcookie。"""
    if not expires or expires < 0:
        return "セッション"
    try:
        dt = datetime.fromtimestamp(expires, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "不明"
    days = (dt - datetime.now(timezone.utc)).days
    if days < 0:
        return "期限切れ"
    return f"あと{days}日"


# ---------------------------------------------------------------- ブラウザ側

def playwright_available():
    """playwright が入っているか。入っていなければ案内文を返す。"""
    try:
        import playwright.sync_api  # noqa: F401
        return True, ""
    except ImportError:
        return False, ("playwright が未インストールです。\n"
                       "    pip install -r requirements-harvester.txt\n"
                       "    playwright install chromium")


def _extract(context):
    """コンテキストの cookie jar から auth_token / ct0 / auth_token の期限を取る。"""
    auth = ct0 = None
    expires = None
    for c in context.cookies():
        domain = (c.get("domain") or "").lstrip(".")
        if domain not in ("x.com", "twitter.com"):
            continue
        if c["name"] == "auth_token" and c.get("value"):
            auth = c["value"]
            expires = c.get("expires")
        elif c["name"] == "ct0" and c.get("value"):
            ct0 = c["value"]
    return auth, ct0, expires


def harvest_one(username, profiles_dir, interactive=True, headless=False,
                login_timeout=300, settle=3.0):
    """
    1垢分。永続プロファイルでブラウザを開き、必要なら手動ログインを待つ。
    戻り値: dict(status, auth_token, ct0, expires, note)
    """
    from playwright.sync_api import sync_playwright

    profile = Path(profiles_dir) / username
    profile.mkdir(parents=True, exist_ok=True)

    result = {"status": STATUS_ERROR, "auth_token": None, "ct0": None,
              "expires": None, "note": ""}

    with sync_playwright() as p:
        ctx = None
        try:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=headless,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(X_HOME, wait_until="domcontentloaded", timeout=60000)
            time.sleep(settle)

            auth, ct0, expires = _extract(ctx)
            needs_login = not (auth and ct0) or any(m in page.url for m in LOGIN_MARKERS)

            if needs_login:
                if not interactive:
                    result.update(status=STATUS_LOGIN,
                                  note="セッション切れ。--refresh を外して手動ログインが必要")
                    return result

                print(f"    → ログインが必要。ブラウザで @{username} にログインしてください "
                      f"(最大{login_timeout}秒待機 / Ctrl+C で中断)")
                deadline = time.time() + login_timeout
                while time.time() < deadline:
                    time.sleep(2)
                    auth, ct0, expires = _extract(ctx)
                    if auth and ct0 and not any(m in page.url for m in LOGIN_MARKERS):
                        time.sleep(settle)  # ct0 が確定するまで少し待つ
                        auth, ct0, expires = _extract(ctx)
                        break
                else:
                    result.update(status=STATUS_LOGIN, note="ログイン待ちタイムアウト")
                    return result

            if auth and ct0:
                result.update(status=STATUS_OK, auth_token=auth, ct0=ct0, expires=expires)
            else:
                result.update(status=STATUS_LOGIN, note="auth_token/ct0 を取得できず")
        except KeyboardInterrupt:
            result.update(status=STATUS_ERROR, note="中断")
        except Exception as e:
            result.update(status=STATUS_ERROR, note=f"{type(e).__name__}: {e}")
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass

    return result


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="X のクッキーを永続プロファイルから収集して cookies.txt を更新する")
    ap.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS),
                    help="ユーザー名リスト (既定: tools/accounts.txt)")
    ap.add_argument("--profiles", default=str(DEFAULT_PROFILES),
                    help="プロファイル置き場 (既定: tools/profiles)")
    ap.add_argument("--out", help="書き出す cookies.txt のパス")
    ap.add_argument("--push", metavar="BASE_URL",
                    help="web の /api/cookies/update に POST (例: http://<host>:8501)")
    ap.add_argument("--refresh", action="store_true",
                    help="無操作モード。ログインが必要な垢はスキップして報告のみ")
    ap.add_argument("--check", action="store_true",
                    help="状態を見るだけ。書き込みも POST もしない")
    ap.add_argument("--only", metavar="USER", action="append",
                    help="特定の垢だけ処理 (複数可)")
    ap.add_argument("--headless", action="store_true",
                    help="ヘッドレスで実行 (Xに弾かれやすいので非推奨)")
    ap.add_argument("--login-timeout", type=int, default=300,
                    help="手動ログインの待ち時間(秒)")
    ap.add_argument("--no-merge", action="store_true",
                    help="POST時にサーバー側の既存行を残さず置き換える")
    ap.add_argument("--force", action="store_true",
                    help="一部の垢が取得できなくてもそのまま反映する")
    args = ap.parse_args(argv)

    if args.check:
        args.refresh = True  # 状態確認は無操作で

    ok, msg = playwright_available()
    if not ok:
        print(f"[!] {msg}")
        return 3

    accounts = load_accounts(args.accounts, args.out)
    if args.only:
        wanted = {u.lstrip("@") for u in args.only}
        accounts = [a for a in accounts if a in wanted] or list(wanted)
    if not accounts:
        print(f"[!] アカウントが見つかりません。{args.accounts} に1行1ユーザー名で書いてください。")
        return 2

    previous = read_cookies_file(args.out) if args.out else {}

    print(f"[*] 対象 {len(accounts)} 垢 / プロファイル: {args.profiles}")
    if args.refresh:
        print("[*] 無操作モード（ログインが必要な垢はスキップ）")

    harvested, results = {}, []
    for i, username in enumerate(accounts, 1):
        print(f"[{i}/{len(accounts)}] @{username}")
        r = harvest_one(username, args.profiles,
                        interactive=not args.refresh,
                        headless=args.headless,
                        login_timeout=args.login_timeout)
        results.append((username, r))
        if r["status"] == STATUS_OK:
            harvested[username] = build_cookie_str(r["auth_token"], r["ct0"])
            print(f"    OK  期限: {fmt_expiry(r.get('expires'))}")
        else:
            print(f"    NG  {r['status']}: {r.get('note', '')}")

    # ------- サマリ
    print("\n=== 状態 ===")
    need_login = []
    for username, r in results:
        if r["status"] == STATUS_OK:
            print(f"  OK           @{username}  ({fmt_expiry(r.get('expires'))})")
        else:
            mark = "セッション切れ" if r["status"] == STATUS_LOGIN else "エラー"
            print(f"  {mark}  @{username}  {r.get('note', '')}")
            need_login.append(username)

    print(f"\n取得 {len(harvested)}/{len(accounts)} 垢")
    if need_login:
        print("要ログイン: " + ", ".join("@" + u for u in need_login))
        print("  → python cookie_harvester.py " +
              " ".join(f"--only {u}" for u in need_login))

    if args.check:
        print("\n[*] --check のため書き込みなし")
        return 0 if not need_login else 1

    if not harvested:
        print("[!] 1垢も取得できなかったので何もしません")
        return 1

    merged, kept = merge_entries(previous, harvested)
    if kept:
        print(f"[*] 前回の値を維持: {', '.join('@' + u for u in kept)}")

    incomplete = len(harvested) < len(accounts)
    if incomplete and not kept and not args.force:
        print("[!] 一部の垢が取得できず、前回値も無いため中止しました。"
              "意図的なら --force を付けてください。")
        return 1

    text = build_cookies_text(merged)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            backup = out.with_suffix(out.suffix + ".bak")
            backup.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"[*] バックアップ: {backup}")
        out.write_text(text, encoding="utf-8")
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
        print(f"[+] 書き出し: {out} ({len(merged)}垢)")

    if args.push:
        ok, msg = push_cookies(args.push, text, merge=not args.no_merge)
        print(f"[{'+' if ok else '!'}] POST {args.push}: {msg}")
        if not ok:
            return 1

    if not args.out and not args.push:
        print("\n--- cookies.txt (--out / --push 未指定のため標準出力) ---")
        print(text, end="")

    return 0


if __name__ == "__main__":
    sys.exit(main())
