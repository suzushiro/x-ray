"""
投稿の「出自」（X / Instagram）を1枚の境界にまとめる。

## なぜこれがあるのか

IG を取り込んだとき、IG側だけが `ig_bridge` というモジュールを持ち、
X側のSQLは `web.py` に直書きのままだった。この非対称のせいで、機能を
足すたびに「X用の分岐」と「IG用の分岐」が web.py に溜まっていった。
v54 時点で IG 分岐を持つルートは13本、IGの17列射影は**3箇所に複製**され、
カテゴリ件数の集計はほぼ同形のGROUP BYが2本あった。

ここに置くのは「どのソースも同じ質問に答えられる」という約束だけ。
web.py は**ソースを並べてループする**ようになり、ソースごとの違いは
このファイルの中だけで閉じる。3つめのソース（Tumblr、Bluesky など）を
足すときは、ここに1クラス足せば一覧・検索・件数に乗る。

## 列の約束

`POST_COLUMNS` が唯一の正。1行が (共通列名, X側の式, IG側の式) で、
**共通名・X式・IG式が横に並ぶ**ので、列を足すときに片方だけ直して
しまう事故が起きない。以前は共通名の一覧（TL_COLUMNS）とX射影とIG射影が
離れた場所にあり、`quoted_json` が共通名から漏れて引用RTが黙って
壊れたことがある。

UNION ALL は列名ではなく**位置**で対応するので、並びがずれると
「いいね数の所に返信数が出る」ような壊れ方をする。射影は必ず
`projection()` から作ること。手書きしてはいけない。
"""

import json
import os
import shutil
from datetime import datetime, timezone

import ig_bridge


# (共通列名, X側の式, IG側の式)
#
# IG に無い概念（引用RT・動画の別配列など）は NULL を入れて列数と並びを
# 揃える。NULL を入れるのは手抜きではなく、UNION の位置対応を保つため。
POST_COLUMNS = [
    ("source",            "'x'",                 "'ig'"),
    ("tweet_id",          "t.tweet_id",          "'{id_prefix}' || p.shortcode"),
    ("screen_name",       "t.screen_name",       "p.owner_username"),
    ("display_name",      "a.display_name",      "a.full_name"),
    ("content",           "t.content",           "p.caption"),
    ("created_at",        "t.created_at",        "p.date_utc"),
    ("url",               "t.url",
     "'{post_url_prefix}' || p.shortcode || '/'"),
    ("like_count",        "t.like_count",        "p.likes"),
    # IGにリポストの概念は無い
    ("retweet_count",     "t.retweet_count",     "0"),
    ("reply_count",       "t.reply_count",       "p.comments"),
    ("media_json",        "t.media_json",        "p.media_json"),
    # IGの動画は media_json の中に入っている（別列を持たない）
    ("video_json",        "t.video_json",        "NULL"),
    # IGのローカル画像は attach_media() が後から解決する
    ("local_media_json",  "t.local_media_json",  "NULL"),
    ("categories",        "a.categories",        "a.categories_json"),
    ("profile_image_url", "a.profile_image_url", "a.profile_pic_local"),
    ("reply_to_tweet_id", "t.reply_to_tweet_id", "NULL"),
    # IGに引用RTの概念は無い
    ("quoted_json",       "t.quoted_json",       "NULL"),
]

COLUMN_NAMES = tuple(c[0] for c in POST_COLUMNS)

# UNION した結果を外側で並べ替えるときに使う列並び
TL_COLUMNS = ",\n    ".join(COLUMN_NAMES)

# 並び替えのキー。created_at をそのまま文字列比較してはいけない。
# X と IG で保存形式のオフセットが揃っておらず（+09:00 と +00:00 が混在しうる）、
# 素の文字列ソートだと時系列が狂う。strftime はオフセットを畳んで UTC に
# 正規化するので、これを通した値で並べる。
TL_ORDER = "strftime('%Y-%m-%dT%H:%M:%S', created_at)"


class Source:
    """
    1つの出自。web.py はこの口だけを通して投稿を読む。

    サブクラスが埋めるもの:
      name / label       … 画面とクエリ文字列で使う識別子
      _col_index         … POST_COLUMNS の何番目の式を使うか（1=X, 2=IG）
      available(conn)    … いまこのリクエストで読めるか
      owns(post_id)      … そのIDがこのソースのものか
      from_sql()         … FROM 句
      filter_sql(...)    … カテゴリ・R18・メディア有無の絞り込み
      category_counts()  … カテゴリ別件数
    """

    name = ""
    label = ""
    id_prefix = ""
    post_url_prefix = ""
    _col_index = 1

    # ---- 列 ----------------------------------------------------------

    def projection(self):
        """
        SELECT句の列並び。POST_COLUMNS から組み立てるので、
        ソース間で並びがずれることが構造的に起きない。
        """
        parts = []
        for row in POST_COLUMNS:
            name, expr = row[0], row[self._col_index]
            expr = expr.format(id_prefix=self.id_prefix,
                               post_url_prefix=self.post_url_prefix)
            parts.append(f"{expr} AS {name}")
        return ",\n           ".join(parts)

    def select_sql(self, where_sql="", params=(), from_sql=None):
        """
        このソースの投稿を共通列で返す SELECT。where_sql は WHERE 以降。

        from_sql を渡すと FROM 句を差し替える（全文検索で索引テーブルを
        JOIN する場合など）。列の射影は差し替えられない — そこが
        ずれると UNION の位置対応が壊れるため。
        """
        where = f"WHERE {where_sql}" if where_sql else ""
        return f"""
        SELECT {self.projection()}
        {from_sql or self.from_sql()}
        {where}
        """, list(params)

    def ordered_rows(self, conn, where_sql="", params=(), limit=200,
                     offset=0, from_sql=None):
        """共通列で、新しい順に取得する。"""
        sql, p = self.select_sql(where_sql, params, from_sql)
        wrapped = (f"SELECT * FROM ( {sql} ) "
                   f"ORDER BY {TL_ORDER} DESC LIMIT ? OFFSET ?")
        return [dict(r) for r in conn.execute(
            wrapped, (*p, limit, offset)).fetchall()]

    # ---- サブクラスが埋める ------------------------------------------

    def from_sql(self):
        raise NotImplementedError

    def attach(self, conn):
        """
        読み取り用コネクションに自分のデータを繋ぐ。繋ぐものが無ければ True。

        以前は web.py の db_ro() が `ig_bridge.attach()` を直接呼んでいた。
        出自を足しても接続時に繋がらず、一覧に出ない状態になっていた
        （名指しが1箇所残っていた）。
        """
        return True

    def available(self, conn):
        return True

    def owns(self, post_id):
        return bool(post_id) and str(post_id).startswith(self.id_prefix)

    def visibility_sql(self, conn):
        """
        常に効く絞り込み（監視対象外・ミュートを伏せるなど）。
        一覧でも検索でもユーザーページでも効かせる必要があるので、
        カテゴリの絞り込みとは分けて持つ。
        """
        return "", []

    def filter_sql(self, conn, category="all", show_r18=False,
                   require_media=False):
        """(where_sql, params) を返す。絞り込み無しなら ("", [])。"""
        return "", []

    def text_where(self, q):
        """本文の部分一致。(where_sql, params)。"""
        raise NotImplementedError

    def search(self, conn, q, limit=200):
        """共通列で検索結果を返す。"""
        where, params = self.text_where(q)
        vis, vp = self.visibility_sql(conn)
        if vis:
            where = f"({where}) AND {vis}"
            params = list(params) + list(vp)
        return self.ordered_rows(conn, where, params, limit=limit)

    def owner_rows(self, conn, owner, limit, offset,
                   date_from=None, date_to=None):
        """1人ぶんの投稿。ユーザーページ用。"""
        where, params = [self.owner_where()], [owner]
        if date_from:
            where.append(f"{self.date_column()} >= ?")
            params.append(date_from)
        if date_to:
            where.append(f"{self.date_column()} <= ?")
            params.append(date_to + "T23:59:59")
        return self.ordered_rows(conn, " AND ".join(where), params,
                                 limit=limit, offset=offset)

    def category_counts(self, conn):
        """({カテゴリ: 件数}, 総件数) を返す。読めなければ ({}, 0)。"""
        return {}, 0

    def attach_media(self, conn, rows):
        """ページ分の行が確定してからローカル画像を解決する（N+1回避）。"""
        return rows

    # ---- 投稿主 ------------------------------------------------------

    def owner_profile(self, conn, owner):
        """
        投稿主のプロフィール。user.html に渡す dict、無ければ None。
        必要なキー: screen_name（URL用）/ handle（表示用）/ display_name /
        categories / categories_list / profile_image_url / source
        """
        raise NotImplementedError

    def owner_stats(self, conn, owner):
        """
        投稿主の統計。total / total_likes / total_rts / oldest / newest /
        with_media / with_video を埋める（取れないものは0やNone）。
        """
        raise NotImplementedError

    # ---- 書き込み・取り出し ------------------------------------------

    def bookmark_payload(self, ro, rw, post_id):
        """
        ブックマークに保存する1件を組み立てる。

        返すのは (列名→値 の dict, 確保した画像の枚数)。
        bookmarks への INSERT は呼び出し側が1箇所で行うので、
        ここでは**値を揃えることと、画像の実体を X-Ray 側に確保すること**
        だけをやる。見つからなければ (None, 0)。

        画像の実体をこちら側へ持ってくるのが肝。参照だけ持つと
        「ブックマークしたのに画像が消える」になる（元が消える／
        キャッシュの保持期間が切れる）。
        """
        raise NotImplementedError

    def post_media(self, ro, rw, post_id, indices=None):
        """
        Tumblr へ添付する実ファイルを返す。

        返すのは (ファイルパスの配列, 出典URL, 表示用の投稿主)。
        indices を渡されたら**その順序どおり**に返す
        （UIが選んだ順に番号を振って見せているため）。
        """
        raise NotImplementedError

    # ---- ミュート ----------------------------------------------------
    #
    # X にミュートは無い（カテゴリで出し分ける）。できる出自だけが名乗る。

    # 画像の手動削除。X-Ray 自身が持っているデータだけ消せる。
    # 外から ATTACH しているアーカイブに書くと向こうを壊す。
    supports_media_delete = False

    supports_mute = False

    def is_muted(self, conn, owner):
        return False

    def set_mute(self, owner, muted, reason=None):
        return False

    def muted_accounts(self, conn):
        return []

    # ---- メディアの配信 ----------------------------------------------

    def media_dirs(self):
        """
        `/media/<name>/<path>` で配信するディレクトリ（先に見つかった方を使う）。
        空なら配信しない。
        """
        return []

    def normalize_owner(self, owner):
        """画面やAPIから来た投稿主の表記を、このソース内部の形に直す。"""
        if self.id_prefix and str(owner).startswith(self.id_prefix):
            return str(owner)[len(self.id_prefix):]
        return owner

    # ---- 組み立て ----------------------------------------------------

    def timeline_sql(self, conn, category="all", show_r18=False,
                     require_media=False):
        where, params = self.filter_sql(conn, category, show_r18,
                                        require_media)
        return self.select_sql(where, params)

    def owner_where(self):
        raise NotImplementedError

    def date_column(self):
        raise NotImplementedError


class XSource(Source):
    """X（Twitter）。X-Ray 自身の tweets / accounts テーブル。"""

    supports_media_delete = True        # 自分のデータなので消せる
    name = "x"
    label = "X"
    id_prefix = ""          # XのツイートIDには接頭辞が付かない
    _col_index = 1

    def from_sql(self):
        return ("FROM tweets t\n"
                "        JOIN accounts a ON t.screen_name = a.screen_name")

    def owns(self, post_id):
        # 接頭辞が無いのが X。他のどのソースのものでもなければ X と見なす。
        # ここで「接頭辞が空だから常に True」にすると ig: のIDまで拾うので、
        # 判定は registry の for_id() が順番を保証する。
        return bool(post_id) and ":" not in str(post_id)

    def owner_where(self):
        return "t.screen_name = ?"

    def date_column(self):
        return "t.created_at"

    def filter_sql(self, conn, category="all", show_r18=False,
                   require_media=False):
        # カテゴリを選んでいるときは R18 で追加の絞り込みをしない（既存の挙動）。
        # 「all のときだけ R18 を伏せる」が元からの仕様で、タブで R18 を
        # 選べば出る。
        where, params = [], []
        if category == "all":
            if not show_r18:
                where.append("a.categories NOT LIKE '%\"R18\"%'")
        else:
            where.append("a.categories LIKE ?")
            params.append(f'%"{category}"%')
        if require_media:
            where.append("t.media_json IS NOT NULL AND t.media_json != '[]'")
        return " AND ".join(where), params

    def text_where(self, q):
        return "t.content LIKE ?", [f"%{q}%"]

    # 全文検索（FTS5）は使わない。
    #
    # 既定のトークナイザ（unicode61）は日本語の連なりを**ひとかたまりの
    # トークン**として扱うので、「猫」「写真」のような部分一致が
    # **1件も返らない**（実測）。本文が日本語主体の X-Ray では
    # 検索が機能していないのと同じだった。しかも例外を投げずに
    # 0件を返すので、LIKE へのフォールバックも働かなかった。
    #
    # trigram トークナイザは3文字以上なら引けるが、2文字以下は駄目で、
    # 索引は tweets 本体より大きい（23万行で本体25MBに対し30〜44MB）。
    # LIKE + LIMIT は同じ規模で 0.1ms、全件でも62msだったので、
    # 索引を持つ意味が無い。tweets_fts は v58 で撤去した。

    def owner_profile(self, conn, owner):
        acc = conn.execute("SELECT * FROM accounts WHERE screen_name = ?",
                           (owner,)).fetchone()
        if not acc:
            return None
        d = dict(acc)
        d["handle"] = d["screen_name"]
        d["categories_list"] = json.loads(d.get("categories") or "[]")
        d["source"] = self.name
        return d

    def owner_stats(self, conn, owner):
        st = dict(conn.execute("""
            SELECT COUNT(*) as total,
                   COALESCE(SUM(like_count), 0) as total_likes,
                   COALESCE(SUM(retweet_count), 0) as total_rts,
                   MIN(created_at) as oldest,
                   MAX(created_at) as newest
            FROM tweets WHERE screen_name = ?
        """, (owner,)).fetchone() or {})
        m = conn.execute("""
            SELECT
              COALESCE(SUM(CASE WHEN media_json IS NOT NULL
                                 AND media_json != '[]' THEN 1 ELSE 0 END), 0)
                AS with_media,
              COALESCE(SUM(CASE WHEN video_json IS NOT NULL
                                 AND video_json != '[]' THEN 1 ELSE 0 END), 0)
                AS with_video
            FROM tweets WHERE screen_name = ?
        """, (owner,)).fetchone()
        st["with_media"] = m["with_media"]
        st["with_video"] = m["with_video"]
        return st

    def bookmark_payload(self, ro, rw, post_id):
        import cache_utils
        row = rw.execute("""
            SELECT t.*, a.display_name, a.categories, a.profile_image_url
            FROM tweets t JOIN accounts a ON t.screen_name = a.screen_name
            WHERE t.tweet_id = ?
        """, (post_id,)).fetchone()
        if not row:
            return None, 0
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
            rw.execute("UPDATE tweets SET local_media_json=? WHERE tweet_id=?",
                       (d["local_media_json"], post_id))
        return {
            "tweet_id": d["tweet_id"], "screen_name": d["screen_name"],
            "display_name": d["display_name"], "content": d["content"],
            "created_at": d["created_at"], "url": d["url"],
            "media_json": d.get("media_json"),
            "local_media_json": d.get("local_media_json"),
            "video_json": d.get("video_json"),
            "categories": d.get("categories"),
            "profile_image_url": d.get("profile_image_url"),
            "quoted_json": d.get("quoted_json"),
        }, promoted

    def post_media(self, ro, rw, post_id, indices=None):
        import media_utils
        paths = media_utils.local_image_paths(post_id, rw, indices)
        row = rw.execute(
            "SELECT url, screen_name FROM tweets WHERE tweet_id=?",
            (post_id,)).fetchone() or rw.execute(
            "SELECT url, screen_name FROM bookmarks WHERE tweet_id=?",
            (post_id,)).fetchone()
        url = (row["url"] if row else "") or ""
        owner = (row["screen_name"] if row else "") or ""
        return paths, url, owner

    def media_dirs(self):
        from cache_utils import CACHE_DIR, IMAGES_DIR
        return [IMAGES_DIR, CACHE_DIR]

    def category_counts(self, conn):
        # accounts.categories の組み合わせは高々アカウント数ぶんしか無いので
        # GROUP BY で畳める。カテゴリ数ぶん COUNT+LIKE をループすると
        # （categories はJSON文字列なのでインデックスが効かず）毎回
        # フルスキャン×N回になる。
        counts, total = {}, 0
        for r in conn.execute("""
            SELECT a.categories AS cats, COUNT(*) AS cnt
            FROM tweets t
            JOIN accounts a ON t.screen_name = a.screen_name
            GROUP BY a.categories
        """):
            total += r["cnt"]
            for c in _json_list(r["cats"]):
                counts[c] = counts.get(c, 0) + r["cnt"]
        return counts, total


class IgSource(Source):
    """
    Instagram。ig-ray のDBを ATTACH して読む。

    データの出入り（ATTACH・キャッシュのパス解決・ミュート・健康状態）は
    `ig_bridge` が持ち、ここは「どういうSQLで共通列にするか」だけを持つ。
    """

    name = "ig"
    label = "IG"
    _col_index = 2

    def __init__(self, bridge):
        self._b = bridge
        self.id_prefix = bridge.ID_PREFIX
        self.post_url_prefix = bridge.POST_URL_PREFIX

    @property
    def schema(self):
        return self._b.SCHEMA

    def from_sql(self):
        s = self.schema
        return (f"FROM {s}.posts p\n"
                f"        LEFT JOIN {s}.accounts a "
                f"ON p.owner_username = a.username")

    def attach(self, conn):
        return self._b.attach(conn)

    def available(self, conn):
        # この接続に実際に ATTACH できているかを見る。
        # リクエスト状態（flask の g）に頼らないので、テストや CLI から
        # 素の接続を渡しても同じ答えになる。
        # sqlite3.Connection には属性を付けられない（__dict__ が無い）ので、
        # 接続に印を付ける方式は取れない。
        return self._b.is_attached(conn)

    def owner_where(self):
        return "p.owner_username = ?"

    def date_column(self):
        return "p.date_utc"

    def visibility_sql(self, conn):
        # 監視対象外とミュートを伏せる。一覧・検索・ユーザーページの
        # どれでも効かせる必要がある。
        return self._b.hidden_owner_sql(conn)

    def text_where(self, q):
        return "p.caption LIKE ?", [f"%{q}%"]

    def filter_sql(self, conn, category="all", show_r18=False,
                   require_media=False):
        hide_sql, params = self.visibility_sql(conn)
        clauses = [hide_sql] if hide_sql else []
        params = list(params)

        # カテゴリの絞り込みは X 側と同じ規則にする。
        # ig-ray の categories_json も同じ「JSON配列の文字列」なので LIKE が効く。
        if category == "all":
            if not show_r18:
                clauses.append(
                    "COALESCE(a.categories_json,'') NOT LIKE '%\"R18\"%'")
        else:
            clauses.append("a.categories_json LIKE ?")
            params.append(f'%"{category}"%')

        if require_media:
            clauses.append("p.media_json IS NOT NULL AND p.media_json != '[]'")
        return " AND ".join(clauses), params

    def category_counts(self, conn):
        # 表示には出ているのに件数に入っていないと「photographer 0件」なのに
        # 写真が並ぶ、という食い違いになる。
        counts, total = {}, 0
        try:
            hide_sql, params = self._b.hidden_owner_sql(conn)
            where = f"WHERE {hide_sql} " if hide_sql else ""
            s = self.schema
            for r in conn.execute(f"""
                SELECT a.categories_json AS cats, COUNT(*) AS cnt
                FROM {s}.posts p
                LEFT JOIN {s}.accounts a ON p.owner_username = a.username
                {where}GROUP BY a.categories_json
            """, params):
                total += r["cnt"]
                for c in _json_list(r["cats"]):
                    counts[c] = counts.get(c, 0) + r["cnt"]
        except Exception as e:
            print(f"[!] IGの件数集計に失敗: {type(e).__name__}: {e}")
            return {}, 0
        return counts, total

    def attach_media(self, conn, rows):
        return self._b.attach_media(conn, rows)

    def media_dirs(self):
        return [self._b.IG_CACHE_DIR] if self._b.IG_CACHE_DIR else []

    def owner_profile(self, conn, owner):
        acc = conn.execute(
            f"SELECT username, full_name, categories_json, profile_pic_local "
            f"FROM {self.schema}.accounts WHERE username = ?",
            (owner,)).fetchone()
        if not acc:
            return None
        return {"screen_name": self.id_prefix + owner,   # URL用（ig: 付き）
                "handle": owner,                        # 画面に出す名前
                "display_name": acc["full_name"] or owner,
                "categories": acc["categories_json"],
                "categories_list": json.loads(acc["categories_json"] or "[]"),
                "profile_image_url": acc["profile_pic_local"],
                "source": self.name}

    def owner_stats(self, conn, owner):
        S = self.schema
        st = dict(conn.execute(f"""
            SELECT COUNT(*) AS total, COALESCE(SUM(likes), 0) AS total_likes,
                   0 AS total_rts, MIN(date_utc) AS oldest,
                   MAX(date_utc) AS newest
            FROM {S}.posts WHERE owner_username = ?
        """, (owner,)).fetchone() or {})
        st["with_media"] = conn.execute(f"""
            SELECT COALESCE(SUM(CASE WHEN media_json IS NOT NULL
                                      AND media_json != '[]' THEN 1 ELSE 0 END), 0)
                   AS n
            FROM {S}.posts WHERE owner_username = ?
        """, (owner,)).fetchone()["n"]
        # is_video のような列は ig-ray 側の都合で有無が変わる。
        # 統計が1つ欠けるだけでページごと落とさない。
        try:
            st["with_video"] = conn.execute(
                f"SELECT COALESCE(SUM(is_video), 0) AS n FROM {S}.posts "
                "WHERE owner_username = ?", (owner,)).fetchone()["n"]
        except Exception:
            st["with_video"] = 0
        return st

    def bookmark_payload(self, ro, rw, post_id):
        """
        IG の投稿を X-Ray のブックマークにコピーする。

        画像は **X-Ray 側の永続領域に実体をコピーする。**
        ig-ray のキャッシュは向こうの保持期間で消えるので、参照だけ持つと
        「ブックマークしたのに画像が消える」になる。X 側がキャッシュから
        永続へ昇格させているのと同じ考え方。
        コピー先は X-Ray の持ち物なので、読み取り専用の前提は崩れない。

        **ig-ray 側には書かない。**
        """
        from cache_utils import IMAGES_DIR
        if not self.available(ro):
            return None, 0
        code = self._b.shortcode_of(post_id)
        S = self.schema
        row = ro.execute(
            f"SELECT p.shortcode, p.owner_username, p.caption, p.date_utc,"
            f" p.likes, p.comments, p.media_json, a.full_name,"
            f" a.categories_json, a.profile_pic_local "
            f"FROM {S}.posts p LEFT JOIN {S}.accounts a"
            f" ON p.owner_username = a.username WHERE p.shortcode = ?",
            (code,)).fetchone()
        if not row:
            return None, 0

        try:
            media = json.loads(row["media_json"] or "[]")
        except (TypeError, ValueError):
            media = []
        remotes = [m.get("image_url") or "" for m in media
                   if isinstance(m, dict)]

        by_index = {}
        for r in ro.execute(
            f"SELECT media_index, local_path FROM {S}.media_index "
            f"WHERE shortcode=?", (code,)
        ):
            if r["local_path"]:
                by_index[r["media_index"]] = r["local_path"]

        locals_out, copied = [], 0
        os.makedirs(IMAGES_DIR, exist_ok=True)
        for i, m in enumerate(media):
            idx = m.get("index", i) if isinstance(m, dict) else i
            src = self._b.local_file(by_index.get(idx, ""))
            if not src:
                locals_out.append(None)
                continue
            ext = os.path.splitext(src)[1] or ".jpg"
            name = f"ig_{code}_{idx}{ext}"
            dst = os.path.join(IMAGES_DIR, name)
            try:
                if not os.path.exists(dst):
                    shutil.copyfile(src, dst)
                    copied += 1
                locals_out.append(f"/images/{name}")
            except Exception as e:
                print(f"[!] IG画像のコピーに失敗 {src}: {type(e).__name__}: {e}")
                locals_out.append(None)

        return {
            "tweet_id": post_id,
            "screen_name": row["owner_username"],
            "display_name": row["full_name"] or row["owner_username"],
            "content": row["caption"],
            "created_at": row["date_utc"],
            "url": f"{self._b.POST_URL_PREFIX}{code}/",
            "media_json": json.dumps(remotes, ensure_ascii=False),
            "local_media_json": json.dumps(locals_out, ensure_ascii=False),
            "video_json": None,
            "categories": row["categories_json"],
            "profile_image_url": row["profile_pic_local"],
            "quoted_json": None,
        }, copied

    def post_media(self, ro, rw, post_id, indices=None):
        """
        ig-ray 側の実体を優先し、無ければ X-Ray のブックマークに
        コピーしてある実体を使う。向こうの保持期間で元が消えても
        投稿できるようにするため。
        """
        from cache_utils import IMAGES_DIR
        if self.available(ro):
            info = self._b.post_info(ro, post_id, indices)
            if info and info["paths"]:
                return (info["paths"], info["source_url"],
                        self.id_prefix + info["owner"])

        row = rw.execute(
            "SELECT url, screen_name, local_media_json FROM bookmarks "
            "WHERE tweet_id=?", (post_id,)).fetchone()
        if not row:
            return [], "", ""
        try:
            locals_ = json.loads(row["local_media_json"] or "[]")
        except (TypeError, ValueError):
            locals_ = []
        order = [i for i, p in enumerate(locals_) if p]
        if indices is not None:
            order = [i for i in indices if i in order]
        paths = []
        for i in order:
            fp = os.path.join(IMAGES_DIR, os.path.basename(locals_[i]))
            if os.path.exists(fp) and fp not in paths:
                paths.append(fp)
        return (paths, row["url"] or "",
                self.id_prefix + (row["screen_name"] or ""))

    # --- ミュート。ig-ray の accounts.is_muted を直接書くので向こうの画面とも揃う ---

    supports_mute = True

    def is_muted(self, conn, owner):
        if not self.available(conn):
            return False
        r = conn.execute(
            f"SELECT COALESCE(is_muted,0) AS m FROM {self.schema}.accounts "
            "WHERE username=?", (owner,)).fetchone()
        return bool(r["m"]) if r else False

    def set_mute(self, owner, muted, reason=None):
        return self._b.set_mute(owner, muted, reason)

    def muted_accounts(self, conn):
        return self._b.muted_accounts(conn)

    def normalize_owner(self, owner):
        """ig: が付いていても受ける。"""
        return self._b.shortcode_of(owner) or owner


def _json_list(s):
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


# --- 登録 -------------------------------------------------------------
#
# ここに足したソースが、一覧・検索・カテゴリ件数に自動で乗る。
# web.py を触る必要は無い（dev/sources_check.py がそれを検査している）。

_REGISTRY = []


def register(source):
    """
    ソースを登録する。**同じ名前なら差し替える**（積み増さない）。

    最初は web.py の読み込み時に append していた。テストが環境変数を
    変えて web を再importするたびに同じソースが積まれ、8重に登録されて
    UNION ALL が8回重なり、同じ投稿が8件ずつ出た。
    足し算で持つと「何回読み込まれたか」に答えが依存する。
    """
    for i, s in enumerate(_REGISTRY):
        if s.name == source.name:
            _REGISTRY[i] = source
            return source
    _REGISTRY.append(source)
    return source


def reset():
    """登録を空にする（テスト用）。"""
    _REGISTRY.clear()


def all_sources():
    return tuple(_REGISTRY)


def names():
    """?source= で受け付ける値。"all" は全ソースの混合。"""
    return ("all",) + tuple(s.name for s in _REGISTRY)


def by_name(name):
    for s in _REGISTRY:
        if s.name == name:
            return s
    return None


def name_for_id(post_id, default="x"):
    """投稿IDから出自名。分からなければ default。"""
    s = for_id(post_id)
    return s.name if s else default


def for_id(post_id):
    """
    投稿IDからソースを決める。接頭辞を持つソースを先に見る。
    X は接頭辞が無いので、最後の受け皿になる。
    """
    for s in _REGISTRY:
        if s.id_prefix and s.owns(post_id):
            return s
    for s in _REGISTRY:
        if not s.id_prefix and s.owns(post_id):
            return s
    return None


def active(conn, selected="all"):
    """
    このリクエストで実際に読むソース。

    selected が1つのソース名ならそれだけ（読めなければ空）。
    "all" なら読めるソース全部。
    """
    if selected and selected != "all":
        s = by_name(selected)
        return (s,) if s and s.available(conn) else ()
    return tuple(s for s in _REGISTRY if s.available(conn))


def union_timeline_sql(conn, sources, category="all", show_r18=False,
                       require_media=False):
    """
    複数ソースを時系列に混ぜる SQL。

    UNION の ORDER BY には式を書けない（SQLiteは結果列の名前か番号しか
    受け付けない）。外側のSELECTで包んでから並べ替える。
    """
    if not sources:
        return None, []
    parts, params = [], []
    for s in sources:
        sql, p = s.timeline_sql(conn, category, show_r18, require_media)
        parts.append(f"SELECT {TL_COLUMNS} FROM ( {sql} )")
        params += p
    body = "\n            UNION ALL\n            ".join(parts)
    return f"""
        SELECT * FROM (
            {body}
        )
        ORDER BY {TL_ORDER} DESC
        LIMIT ? OFFSET ?
    """, params


def attach_all(conn):
    """
    読み取り用コネクションに、登録されている出自を全部繋ぐ。
    繋げた出自の名前を返す。

    どれかが失敗しても他は繋ぐ。片方の都合で画面全体を落とさない。
    """
    ok = []
    for s in _REGISTRY:
        try:
            if s.attach(conn):
                ok.append(s.name)
        except Exception as e:
            print(f"[!] {s.name} に接続できません: {type(e).__name__}: {e}")
    return ok


def attach_media_all(conn, rows):
    """行の出自ごとに、そのソースのローカル画像解決を通す。"""
    for s in _REGISTRY:
        try:
            rows = s.attach_media(conn, rows)
        except Exception as e:
            print(f"[!] {s.name} のメディア解決に失敗: {type(e).__name__}: {e}")
    return rows


def category_counts_all(conn, categories_list, selected="all"):
    """カテゴリ別件数。読めるソースぶんを足し合わせる。"""
    counts = {c: 0 for c in categories_list}
    total = 0
    for s in active(conn, selected):
        got, n = s.category_counts(conn)
        total += n
        for c, v in got.items():
            if c in counts:
                counts[c] += v
    return counts, total


# 既定の登録。web.py ではなくここでやる。
# 読み込み側で register() を呼ぶ形にしていたら、テストが web を
# 再importするたびに積み増さった（上の register() の注記参照）。
register(XSource())
register(IgSource(ig_bridge))
