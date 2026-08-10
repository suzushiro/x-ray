"""検証専用: 全ルートを Flask test client で叩いてステータスを確認する。"""
import os
import sys

# dev/ から app/ のモジュールを読めるようにする
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))


from web import app

GETS = [
    "/",
    "/?category=illustrator",
    "/?category=%E3%82%AE%E3%83%A3%E3%83%AB",
    "/?page=2",
    "/?page=abc",
    "/manage",
    "/bookmarks",
    "/bookmarks?page=1",
    "/gallery",
    "/gallery?category=photographer",
    "/search",
    "/search?q=カメラ",
    "/search?q=引用",
    "/user/alice_art",
    "/user/carol_dev",
    "/user/does_not_exist",
    "/storage",
    "/status",
    "/images/aaa1.jpg",
    "/images/nope.jpg",
    "/cache/nope.jpg",
]

# エンドポイントは request.form を読むので form data で送る
POSTS = [
    ("/api/bookmark/toggle", {"tweet_id": "100003"}),          # 追加
    ("/api/bookmark/toggle", {"tweet_id": "100003"}),          # 解除
    ("/api/bookmark/toggle", {}),                              # 400 期待
    ("/api/account/add", {"screen_name": "zed_test", "display_name": "Zed",
                          "categories": "gadget"}),
    ("/api/account/add", {"screen_name": "zed_test", "display_name": "Zed",
                          "categories": "gadget"}),            # 重複 -> 400
    ("/api/account/delete", {"screen_name": "zed_test"}),
    ("/api/cookies/update", {"cookies_text": "dummy_user\tauth_token=xxx; ct0=yyy"}),
    ("/api/cache/cleanup", {"days": "30"}),
    ("/api/cache/cleanup", {"days": "abc"}),                   # 400 期待
]

fails = []
client = app.test_client()

print("== GET ==")
for path in GETS:
    try:
        r = client.get(path)
        code = r.status_code
    except Exception as e:
        code = f"EXC {type(e).__name__}: {e}"
    ok = code in (200, 302, 304, 404)
    print(f"{'OK ' if ok else 'NG '} {code}  {path}")
    if not ok:
        fails.append((path, code))

print("\n== POST ==")
for path, payload in POSTS:
    try:
        r = client.post(path, data=payload)
        code = r.status_code
        body = r.get_data(as_text=True)[:120]
    except Exception as e:
        code, body = f"EXC {type(e).__name__}: {e}", ""
    ok = code in (200, 302, 400, 404)
    print(f"{'OK ' if ok else 'NG '} {code}  {path}  {body}")
    if not ok:
        fails.append((path, code))

# 引用カードが実際にHTMLに出ているかも確認（空データ素通り防止）
print("\n== レンダリング内容チェック ==")
checks = [
    ("/", "quoted-card", "トップに引用カード"),
    ("/", "引用元アカウント", "引用元の表示名"),
    ("/", "qqq1.jpg", "引用元画像のリモートURL"),
    ("/bookmarks", "quoted-card", "ブックマークの引用カード"),
    ("/user/alice_art", "quoted-card", "ユーザーページの引用カード"),
]
for path, needle, label in checks:
    html = client.get(path).get_data(as_text=True)
    hit = needle in html
    print(f"{'OK ' if hit else 'NG '} {label}: '{needle}' in {path}")
    if not hit:
        fails.append((path, f"missing {needle}"))

print("\n== テンプレートの閉じタグ漏れ ==")
# <style>/<script> の閉じタグ外にコードが漏れていないか（cat >> 追記ミスの検出）
for path in ("/"):
    html = client.get("/").get_data(as_text=True)
    style_after = html.split("</style>", 1)[1] if "</style>" in html else ""
    css_leak = ".tmb-go" in style_after or "border-radius:" in style_after[:2000]
    print(f"{'OK ' if not css_leak else 'NG '} </style>の外にCSSが漏れていない")
    if css_leak:
        fails.append("CSS leak")
    # style/script タグの開閉が1:1
    ok_tags = html.count("<style") == html.count("</style>")
    print(f"{'OK ' if ok_tags else 'NG '} styleタグの開閉が一致")
    if not ok_tags:
        fails.append("style tag mismatch")

print("\n" + ("=== 全て通過 ===" if not fails else f"=== 失敗 {len(fails)} 件: {fails} ==="))
sys.exit(1 if fails else 0)
