"""
出自（X / IG / …）の抽象が効いているかの検査。

このスイートの目的はひとつ: **3つめの出自を足すときに web.py を
触らなくて済むこと**を機械で確かめる。

v54 までは「X用の分岐」と「IG用の分岐」が web.py に溜まる作りで、
IG分岐を持つルートが13本、IGの17列射影が3箇所に複製されていた。
その形に戻っていないことを、架空の第3の出自（Tumblr もどき）を
登録して一覧に出すことで示す。

使い方:
    DB_PATH=/tmp/s/data.db IMAGES_DIR=... CACHE_DIR=... \\
    IG_DB_PATH=/tmp/s/ig.db IG_CACHE_DIR=/tmp/s/igcache \\
    python sources_check.py
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

BASE = os.environ.get("SOURCES_CHECK_DIR") or tempfile.mkdtemp(prefix="srcchk")
os.makedirs(os.path.join(BASE, "images"), exist_ok=True)
os.makedirs(os.path.join(BASE, "cache"), exist_ok=True)
os.makedirs(os.path.join(BASE, "igcache"), exist_ok=True)
os.environ.setdefault("DB_PATH", os.path.join(BASE, "data.db"))
os.environ.setdefault("IMAGES_DIR", os.path.join(BASE, "images"))
os.environ.setdefault("CACHE_DIR", os.path.join(BASE, "cache"))
os.environ.setdefault("TWITTER_COOKIES_FILE", os.path.join(BASE, "cookies.txt"))
os.environ.setdefault("ACCOUNTS_JSON_PATH", os.path.join(BASE, "accounts.json"))
os.environ.setdefault("IG_DB_PATH", os.path.join(BASE, "ig_ray.db"))
os.environ.setdefault("IG_CACHE_DIR", os.path.join(BASE, "igcache"))

import db                                              # noqa: E402
import sources                                         # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("OK  " if ok else "NG  ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------------
print("== [1] 共通列の約束 ==")

NAMES = sources.COLUMN_NAMES
check("列名が重複していない", len(set(NAMES)) == len(NAMES))
check("TL_COLUMNS と COLUMN_NAMES が一致",
      [c.strip() for c in sources.TL_COLUMNS.replace("\n", " ").split(",")
       if c.strip()] == list(NAMES))

# POST_COLUMNS の各行は (共通名, X式, IG式) の3つ組。
# 行の形が崩れると projection() が黙って別の列を出す。
check("POST_COLUMNS が全行3つ組",
      all(len(r) == 3 for r in sources.POST_COLUMNS),
      str([r[0] for r in sources.POST_COLUMNS if len(r) != 3]))
check("式が空の行が無い",
      all(str(r[1]).strip() and str(r[2]).strip()
          for r in sources.POST_COLUMNS))

# 全ソースの射影が、同じ列名を同じ順に出すこと。
# UNION ALL は列名ではなく**位置**で対応するので、ここがずれると
# 「いいね数の所に返信数が出る」ような壊れ方をする。
for s in sources.all_sources():
    got = [p.split(" AS ")[-1].strip()
           for p in s.projection().split(",\n")]
    check(f"{s.name} の射影が約束どおり", got == list(NAMES),
          f"ずれ: {[(i, g, n) for i, (g, n) in enumerate(zip(got, NAMES)) if g != n]}")

# 上の検査は、射影を POST_COLUMNS から生成しているソース（X・IG）には
# 原理的に通る。並びを入れ替えても全体が揃って動くので実害が無く、
# 「並びのずれ」という不具合の種類そのものが消えている
# （POST_COLUMNS を入れ替えても全ページがバイト単位で変わらないことを実測）。
#
# 効かせたいのは **射影を自前で書くソース**。わざと1列落としたものを
# 作って、検査が気づくことを確かめる（でないとこの検査は空回り）。
class _Broken(sources.Source):
    name = "_broken"
    _col_index = 1

    def projection(self):
        # quoted_json を落とす。実際に起きた不具合と同じ形。
        return ",\n           ".join(
            f"NULL AS {n}" for n in sources.COLUMN_NAMES if n != "quoted_json")

    def from_sql(self):
        return "FROM tweets t JOIN accounts a ON t.screen_name = a.screen_name"


_b = _Broken()
_got = [p.split(" AS ")[-1].strip() for p in _b.projection().split(",\n")]
check("列を落とした射影は検知できる", _got != list(NAMES),
      f"落ちた列: {sorted(set(NAMES) - set(_got))}")
check("落ちた列を名指しできる", set(NAMES) - set(_got) == {"quoted_json"})

# tweets の列を足したのに約束へ入れ忘れると、その列を使う表示が黙って消える
# （実際に quoted_json がこれで落ちて引用RTが壊れた）。
db.init_db()
conn = db.get_conn()
tweet_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tweets)")}
# 共通列に出てこない tweets の列。保存専用の列はここに挙げて除外する。
STORAGE_ONLY = {"fetched_at"}
missing = tweet_cols - set(NAMES) - STORAGE_ONLY
check("tweetsの列が約束から漏れていない", not missing, f"漏れ: {sorted(missing)}")

# ---------------------------------------------------------------------
print("\n== [2] 登録 ==")

check("XとIGが登録されている",
      {s.name for s in sources.all_sources()} >= {"x", "ig"},
      str([s.name for s in sources.all_sources()]))
check("names() の先頭は all", sources.names()[0] == "all")

# 同じ名前で登録しても積み増さないこと。
# 最初は append で持っていて、テストが web を再importするたびに
# 同じソースが積まれ、**8重に登録されて同じ投稿が8件ずつ出た**。
before = len(sources.all_sources())
sources.register(sources.XSource())
sources.register(sources.XSource())
check("同名の再登録で増えない", len(sources.all_sources()) == before,
      f"{before} → {len(sources.all_sources())}")

# ---------------------------------------------------------------------
print("\n== [2b] どの出自も必要な口を埋めているか ==")

# 基底クラスの NotImplementedError 任せだと、埋め忘れは
# **その機能を使った瞬間に500**になって初めて分かる。
# 実際にこのセッションで、別の編集のついでに XSource から
# 5メソッドを消してしまい、ユーザーページとブックマークが500になった。
# 一覧で確かめる。
REQUIRED = [
    # (メソッド名, 無いと何が壊れるか)
    ("from_sql", "一覧・検索・ユーザーページ"),
    ("projection", "共通列（UNIONの位置対応）"),
    ("owns", "IDから出自を決める"),
    ("owner_where", "ユーザーページの絞り込み"),
    ("date_column", "期間の絞り込み"),
    ("text_where", "検索"),
    ("filter_sql", "カテゴリ・R18・メディア有無"),
    ("category_counts", "カテゴリ別件数"),
    ("owner_profile", "ユーザーページの見出し"),
    ("owner_stats", "ユーザーページの統計"),
    ("bookmark_payload", "ブックマーク"),
    ("post_media", "Tumblr投稿"),
    ("media_dirs", "画像の配信"),
]

for s_ in sources.all_sources():
    missing = []
    for name, what in REQUIRED:
        fn = getattr(type(s_), name, None)
        base = getattr(sources.Source, name, None)
        # 基底のまま（＝自分で実装していない）ものを探す。
        # 基底が中身を持つもの（filter_sql など既定で空を返す類）は
        # そのままでも動くので、NotImplementedError を投げる口だけ見る。
        if fn is None:
            missing.append(f"{name}（{what}）")
            continue
        if fn is base:
            try:
                src_text = fn.__doc__ or ""
            except Exception:
                src_text = ""
            import inspect
            try:
                body = inspect.getsource(fn)
            except Exception:
                body = ""
            if "NotImplementedError" in body:
                missing.append(f"{name}（{what}）")
    check(f"{s_.name} が必要な口を埋めている", not missing,
          f"未実装: {missing}")

# 逆向き: 埋め忘れたソースを作ると検知できること
class _Half(sources.Source):
    name = "_half"
    id_prefix = "half:"

    def from_sql(self):
        return "FROM tweets t JOIN accounts a ON t.screen_name = a.screen_name"


_h = _Half()
_miss = [n for n, _ in REQUIRED
         if getattr(type(_h), n, None) is getattr(sources.Source, n, None)
         and "NotImplementedError" in (
             __import__("inspect").getsource(getattr(sources.Source, n))
             if getattr(sources.Source, n, None) else "")]
check("埋め忘れを検知できる", len(_miss) >= 5, f"検知した未実装: {_miss}")

# ---------------------------------------------------------------------
print("\n== [3] IDから出自を引く ==")

check("ig: 付きはIG", sources.for_id("ig:ABC").name == "ig")
check("素の数字はX", sources.for_id("1234567890").name == "x")
check("空は None", sources.for_id("") is None)
check("未知の接頭辞でもXに落ちない",
      sources.for_id("zz:1") is None or sources.for_id("zz:1").name != "x",
      str(sources.for_id("zz:1")))

# ---------------------------------------------------------------------
print("\n== [4] 3つめの出自を足す（web.py を触らずに）==")

# ここが本題。架空の「Tumblr もどき」を1クラス書いて register するだけで
# 一覧・件数・ID解決に乗ることを確かめる。
# web.py には一切手を入れない。

TB_DB = os.path.join(BASE, "tb.db")
tb = sqlite3.connect(TB_DB)
tb.executescript("""
CREATE TABLE tb_posts (slug TEXT PRIMARY KEY, blog TEXT, body TEXT,
                       posted_at TEXT, notes INTEGER, photos TEXT);
CREATE TABLE tb_blogs (blog TEXT PRIMARY KEY, title TEXT, tags TEXT);
INSERT INTO tb_blogs VALUES ('myblog','My Blog','["art"]');
INSERT INTO tb_posts VALUES
 ('p1','myblog','タンブラーの投稿','2026-11-10T00:00:00+00:00',7,'["https://x/1.jpg"]'),
 ('p2','myblog','ふたつめ','2026-11-11T00:00:00+00:00',3,'[]');
""")
tb.commit()
tb.close()


class TbSource(sources.Source):
    """検査用の架空の出自。実機では使わない。"""

    name = "tb"
    label = "TB"
    id_prefix = "tb:"
    post_url_prefix = "https://example.invalid/post/"
    _col_index = 2          # IGと同じ「2番目の式」の枠を借りる

    # POST_COLUMNS の IG 用の式は p.* を前提にしているので、
    # このソース用に式を差し替える。列名と並びは共通のまま。
    EXPR = {
        "source": "'tb'",
        "tweet_id": "'tb:' || b.slug",
        "screen_name": "b.blog",
        "display_name": "g.title",
        "content": "b.body",
        "created_at": "b.posted_at",
        "url": "'https://example.invalid/post/' || b.slug",
        "like_count": "b.notes",
        "retweet_count": "0",
        "reply_count": "0",
        "media_json": "b.photos",
        "video_json": "NULL",
        "local_media_json": "NULL",
        "categories": "g.tags",
        "profile_image_url": "NULL",
        "reply_to_tweet_id": "NULL",
        "quoted_json": "NULL",
    }

    def projection(self):
        return ",\n           ".join(
            f"{self.EXPR[n]} AS {n}" for n in sources.COLUMN_NAMES)

    def from_sql(self):
        return ("FROM tb.tb_posts b\n"
                "        LEFT JOIN tb.tb_blogs g ON b.blog = g.blog")

    def attach(self, conn):
        try:
            conn.execute("ATTACH DATABASE ? AS tb", (TB_DB,))
            conn.execute("PRAGMA query_only = 1")
            return True
        except Exception:
            return False

    def available(self, conn):
        try:
            return any(r[1] == "tb"
                       for r in conn.execute("PRAGMA database_list"))
        except Exception:
            return False

    def owner_where(self):
        return "b.blog = ?"

    def date_column(self):
        return "b.posted_at"

    def text_where(self, q):
        return "b.body LIKE ?", [f"%{q}%"]

    def filter_sql(self, conn, category="all", show_r18=False,
                   require_media=False):
        where, params = [], []
        if category != "all":
            where.append("g.tags LIKE ?")
            params.append(f'%"{category}"%')
        if require_media:
            where.append("b.photos IS NOT NULL AND b.photos != '[]'")
        return " AND ".join(where), params

    # --- 書き込み系 ---

    TB_MEDIA_DIR = os.path.join(BASE, "tbmedia")

    def media_dirs(self):
        return [self.TB_MEDIA_DIR]

    def bookmark_payload(self, ro, rw, post_id):
        slug = self.normalize_owner(post_id)
        row = ro.execute(
            "SELECT b.slug, b.blog, b.body, b.posted_at, b.photos, g.title,"
            " g.tags FROM tb.tb_posts b LEFT JOIN tb.tb_blogs g"
            " ON b.blog = g.blog WHERE b.slug = ?", (slug,)).fetchone()
        if not row:
            return None, 0
        photos = json.loads(row["photos"] or "[]")
        # 画像の実体をこちら側に確保する（本物のソースと同じ考え方）
        os.makedirs(self.TB_MEDIA_DIR, exist_ok=True)
        locals_out = []
        for i, _ in enumerate(photos):
            name = f"tb_{slug}_{i}.jpg"
            fp = os.path.join(self.TB_MEDIA_DIR, name)
            if not os.path.exists(fp):
                open(fp, "wb").write(b"\xff\xd8\xff\xd9")
            locals_out.append(f"/media/tb/{name}")
        return {
            "tweet_id": post_id, "screen_name": row["blog"],
            "display_name": row["title"] or row["blog"],
            "content": row["body"], "created_at": row["posted_at"],
            "url": self.post_url_prefix + slug,
            "media_json": json.dumps(photos),
            "local_media_json": json.dumps(locals_out),
            "video_json": None, "categories": row["tags"],
            "profile_image_url": None, "quoted_json": None,
        }, len(locals_out)

    def post_media(self, ro, rw, post_id, indices=None):
        slug = self.normalize_owner(post_id)
        paths = sorted(
            os.path.join(self.TB_MEDIA_DIR, f)
            for f in os.listdir(self.TB_MEDIA_DIR)
            if f.startswith(f"tb_{slug}_")) \
            if os.path.isdir(self.TB_MEDIA_DIR) else []
        if indices is not None:
            paths = [paths[i] for i in indices if 0 <= i < len(paths)]
        return paths, self.post_url_prefix + slug, "tb:" + slug

    def category_counts(self, conn):
        counts, total = {}, 0
        for r in conn.execute("""
            SELECT g.tags AS cats, COUNT(*) AS cnt
            FROM tb.tb_posts b LEFT JOIN tb.tb_blogs g ON b.blog = g.blog
            GROUP BY g.tags
        """):
            total += r["cnt"]
            for c in json.loads(r["cats"] or "[]"):
                counts[c] = counts.get(c, 0) + r["cnt"]
        return counts, total


sources.register(TbSource())
check("足したら names() に出る", "tb" in sources.names(),
      str(sources.names()))
check("tb: 付きのIDを引ける", sources.for_id("tb:p1").name == "tb")
check("射影が約束どおり",
      [p.split(" AS ")[-1].strip()
       for p in sources.by_name("tb").projection().split(",\n")]
      == list(sources.COLUMN_NAMES))

# 実際に読めるか。tb を ATTACH した接続で一覧を引く。
c2 = db.get_conn()
c2.execute("ATTACH DATABASE ? AS tb", (TB_DB,))
active = sources.active(c2, "all")
check("読める出自に tb が入る", "tb" in [s.name for s in active],
      str([s.name for s in active]))

sql, params = sources.union_timeline_sql(c2, active, "all", False, False)
rows = [dict(r) for r in c2.execute(sql, (*params, 50, 0)).fetchall()]
ids = [r["tweet_id"] for r in rows]
check("tbの投稿が一覧に混ざる", "tb:p1" in ids and "tb:p2" in ids, str(ids[:8]))
check("出自の列が tb になっている",
      all(r["source"] == "tb" for r in rows if str(r["tweet_id"]).startswith("tb:")))
check("時系列に混ざっている（降順）",
      [r["created_at"] for r in rows] == sorted(
          (r["created_at"] for r in rows), reverse=True))

# カテゴリ件数にも乗る。ここが漏れると「art 0件」なのに投稿が並ぶ。
counts, total = sources.category_counts_all(c2, ["art", "gadget"], "all")
check("カテゴリ件数に乗る", counts["art"] >= 2, str(counts))

# 絞り込みも効く
sql, params = sources.union_timeline_sql(c2, active, "all", False, True)
media_ids = [r["tweet_id"] for r in c2.execute(sql, (*params, 50, 0))]
check("メディア有りの絞り込みが効く",
      "tb:p1" in media_ids and "tb:p2" not in media_ids, str(media_ids[:8]))

# 1つだけに絞れる
only = sources.active(c2, "tb")
check("tb だけに絞れる", [s.name for s in only] == ["tb"])
sql, params = sources.union_timeline_sql(c2, only, "all", False, False)
only_ids = [r["tweet_id"] for r in c2.execute(sql, (*params, 50, 0))]
check("絞ると他の出自が出ない",
      only_ids and all(i.startswith("tb:") for i in only_ids), str(only_ids))

# 検索も通る
found = sources.by_name("tb").search(c2, "ふたつめ", limit=10)
check("検索できる", [r["tweet_id"] for r in found] == ["tb:p2"],
      str([r["tweet_id"] for r in found]))

# ユーザーページ用の取得も通る
owned = sources.by_name("tb").owner_rows(c2, "myblog", 10, 0)
check("投稿主で引ける", len(owned) == 2, str(len(owned)))

c2.close()

# ---------------------------------------------------------------------
print("\n== [5] 画面に出るか（実リクエスト経路で）==")

# ここまでは sources の関数を直接呼んでいた。本番の経路（Flask の
# ルート → db_ro() → timeline_rows）でも出ることを確かめる。
# db_ro() が `ig_bridge.attach()` を名指ししていた間は、出自を足しても
# 接続時に繋がらず画面には出なかった。
import web                                              # noqa: E402

cl = web.app.test_client()

check("?source=tb が受け付けられる", "tb" in web.SOURCES, str(web.SOURCES))

h = cl.get("/?source=tb").get_data(as_text=True)
check("トップに tb の投稿が出る", "タンブラーの投稿" in h,
      f"len={len(h)}")
check("tb のIDが出る", "tb:p1" in h)
check("tb だけに絞れている", "alice_art" not in h)

h_all = cl.get("/").get_data(as_text=True)
check("混合にも tb が混ざる", "タンブラーの投稿" in h_all)

h_x = cl.get("/?source=x").get_data(as_text=True)
check("X だけ選ぶと tb は出ない", "タンブラーの投稿" not in h_x)

hg = cl.get("/gallery?source=tb").get_data(as_text=True)
check("ギャラリーにも出る", "tb:p1" in hg, f"len={len(hg)}")

hs = cl.get("/search?q=ふたつめ").get_data(as_text=True)
check("検索にも出る", "ふたつめ" in hs, f"len={len(hs)}")

# ---------------------------------------------------------------------
print("\n== [6] 書き込み系も出自に委譲されているか ==")

# ブックマーク。bookmarks への INSERT は web.py の1箇所だけで、
# 「どこから引くか」「画像の実体をどう確保するか」は出自が持つ。
_r = cl.post("/api/bookmark/toggle", data={"tweet_id": "tb:p1"}).get_json()
check("tbの投稿をブックマークできる", _r and _r.get("ok") and _r.get("bookmarked"),
      str(_r))
check("画像の実体を確保した", _r.get("promoted") == 1, str(_r))

_bm = sqlite3.connect(os.environ["DB_PATH"])
_bm.row_factory = sqlite3.Row
_row = _bm.execute("SELECT * FROM bookmarks WHERE tweet_id='tb:p1'").fetchone()
check("本文が入っている", _row and _row["content"] == "タンブラーの投稿",
      str(dict(_row) if _row else None))
check("出典URLが入っている",
      _row and _row["url"] == "https://example.invalid/post/p1")
check("ローカル画像のパスが入っている",
      _row and "/media/tb/" in (_row["local_media_json"] or ""))
_bm.close()

_bh = cl.get("/bookmarks").get_data(as_text=True)
check("ブックマーク一覧に出る", "タンブラーの投稿" in _bh)

_r2 = cl.post("/api/bookmark/toggle", data={"tweet_id": "tb:p1"}).get_json()
check("外せる", _r2 and _r2.get("ok") and not _r2.get("bookmarked"), str(_r2))

# メディア配信。ルートを足さずに配れること。
cl.post("/api/bookmark/toggle", data={"tweet_id": "tb:p1"})
_m = cl.get("/media/tb/tb_p1_0.jpg")
check("/media/tb/ で配信できる",
      _m.status_code == 200 and _m.data.startswith(b"\xff\xd8"),
      f"status={_m.status_code}")
check("遡上は弾く",
      cl.get("/media/tb/../../data.db").status_code in (400, 404),
      str(cl.get("/media/tb/../../data.db").status_code))

# ミュートを持たない出自は断る（X と同じ扱い）
_mu = cl.post("/api/mute", data={"username": "tb:myblog"})
check("ミュートを持たない出自は断る", _mu.status_code == 400,
      str(_mu.status_code))

# 画像の手動削除は X-Ray 自身のデータだけ。外のアーカイブには書かない。
_dl = cl.post("/api/media/delete",
              data={"tweet_id": "tb:p1", "media_index": "0"})
check("外のアーカイブの画像は消させない", _dl.status_code >= 400,
      str(_dl.status_code))

# ---------------------------------------------------------------------
print("\n== [7] web.py に出自ごとの分岐が戻っていないか ==")

web_src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "web.py")).read()
# 17列の射影の直書き。ここに戻ると「列を1つ足すのに3箇所直す」状態に戻る。
check("IGの射影が web.py に無い",
      "p.shortcode AS tweet_id" not in web_src)
check("Xの射影が web.py に無い",
      "t.like_count, t.retweet_count" not in web_src)
# メディア解決をIGだけ名指ししていないか（3つめの出自で漏れる）
check("メディア解決を名指ししていない",
      "ig_bridge.attach_media" not in web_src)
# 接続も名指ししないこと。db_ro() が ig_bridge.attach() を直接呼んでいた間は
# 出自を足しても繋がらなかった（上の [5] がそれを検知する）。
check("読み取り接続で ig を名指ししていない",
      "g.ig_on = ig_bridge.attach" not in web_src)
# IG専用のブックマーク経路・ページ丸ごとのコピーが戻っていないこと
# 関数名を素で探すと**説明のコメントに書いた名前**を拾う（何度も踏んだ罠）。
# 定義があるかどうかを見る。
check("IG専用のブックマーク経路が無い", "def _bookmark_ig" not in web_src)
check("IG専用のユーザーページが無い", "def _ig_user_profile" not in web_src)
check("bookmarks への INSERT は1箇所",
      web_src.count("INSERT INTO bookmarks") == 1,
      str(web_src.count("INSERT INTO bookmarks")))

sources.reset()

print()
if fails:
    print(f"=== 失敗 {len(fails)}件: {fails} ===")
    sys.exit(1)
print("=== 全て通過 ===")
