"""
DBの行を画面に出す形へ整える層。

`web.py` から切り出してある。ルートの数が増えると、整形の都合と
ルーティングの都合が1つのファイルに混ざって読みにくくなるため。

ここは**表示のための変換だけ**を持つ。DBを引いたり、どの出自から
取るかを決めたりはしない（それは sources.py とルート側の仕事）。
"""

import base64
import json
import os
import re
from datetime import datetime, timezone, timedelta

import sources

JST = timezone(timedelta(hours=9))

# スペースURLの検出パターン
SPACE_URL_RE = re.compile(r'https?://(?:twitter\.com|x\.com)/i/spaces/([A-Za-z0-9_-]+)')

# カテゴリ名 → Tumblrタグ の変換表。
#
# import 時に1回だけ読む。web.py はこれを再輸出しているので、
# 環境変数を変えてから web だけ reload しても**古い値が残る**
# （presenters を先に reload する必要がある）。
# X-Ray内部のカテゴリ名をそのままタグにすると都合が悪いものを置き換える。
# JSONで上書きできる: TUMBLR_TAG_MAP='{"ギャル":"girl","R18":"nsfw"}'
# 空文字を指定するとそのカテゴリはタグから除外される。
_DEFAULT_TAG_MAP = {"ギャル": "girl"}
try:
    _raw_tag_map = os.environ.get("TUMBLR_TAG_MAP", "").strip()
    TUMBLR_TAG_MAP = json.loads(_raw_tag_map) if _raw_tag_map else dict(_DEFAULT_TAG_MAP)
    if not isinstance(TUMBLR_TAG_MAP, dict):
        raise ValueError("dictではありません")
except (ValueError, TypeError) as _e:
    print(f"[!] TUMBLR_TAG_MAP を解釈できません（既定値を使います）: {_e}")
    TUMBLR_TAG_MAP = dict(_DEFAULT_TAG_MAP)


def map_tags(categories):
    """カテゴリ名リストをTumblr用のタグリストに変換する。空になるものは落とす。"""
    out = []
    for c in categories or []:
        t = TUMBLR_TAG_MAP.get(c, c)
        if not t:
            continue          # 空文字指定＝タグにしない
        if t not in out:      # 変換で重複しうるので潰す
            out.append(t)
    return out



def format_tweet(d, bookmarked_ids=None, deleted=None):
    """DBの行dict（tweets JOIN accounts想定）を表示用に整形する"""
    # 出自。タイムラインのSQLは列で持ってくるが、ブックマーク表のように
    # 列が無い経路もあるので tweet_id の接頭辞から補う。
    if not d.get("source"):
        d["source"] = sources.name_for_id(d.get("tweet_id"))
    d["media"] = json.loads(d.get("media_json") or "[]")
    d["videos"] = json.loads(d.get("video_json") or "[]")
    d["local_media"] = json.loads(d.get("local_media_json") or "[]")
    d["categories_list"] = json.loads(d.get("categories") or "[]")

    # 表示用画像: ローカルがあればローカル優先、なければリモートURL。
    # 手動削除済みのURLは除外し、何枚消したかを持たせる。
    deleted = deleted or set()
    display_imgs = []
    media_idx = []
    deleted_count = 0
    for i, remote in enumerate(d["media"]):
        if remote in deleted:
            deleted_count += 1
            continue
        local = d["local_media"][i] if i < len(d["local_media"]) else None
        display_imgs.append(local if local else remote)
        media_idx.append(i)     # 削除ボタンが送る元インデックス
    d["display_imgs"] = display_imgs
    d["media_idx"] = media_idx
    d["deleted_count"] = deleted_count

    # Tumblr共有用: ローカル保存済み画像が1枚でもあるか（削除済み除外後）
    d["has_local_media"] = any(
        (d["local_media"][i] if i < len(d["local_media"]) else None)
        for i, remote in enumerate(d["media"]) if remote not in deleted
    )
    # カテゴリをタグ用にbase64で渡す（| e がJSONを壊す既知問題の回避）。
    # 表示用のカテゴリ名ではなく、変換表を通したTumblrタグを渡す。
    # ensure_ascii は既定のまま（True）。日本語カテゴリを \uXXXX に逃がさないと
    # JS側の atob() が UTF-8 バイトを扱えず壊れる。
    _tags = map_tags(d["categories_list"])
    d["categories_b64"] = base64.b64encode(
        json.dumps(_tags).encode()).decode() if _tags else ""

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

    # スペースURLの検出（本文にspaces/URLが含まれていたら専用カード表示用の情報をセット）
    content = d.get("content") or ""
    m = SPACE_URL_RE.search(content)
    d["space"] = {"url": m.group(0), "id": m.group(1)} if m else None

    d["is_bookmarked"] = bool(bookmarked_ids and d.get("tweet_id") in bookmarked_ids)
    d["self_reply"] = None

    # 引用RTの引用元
    d["quoted"] = None
    if d.get("quoted_json"):
        try:
            q = json.loads(d["quoted_json"])
            # 引用元の投稿日時をJSTへ
            try:
                qdt = datetime.fromisoformat((q.get("created_at") or "").replace("Z", "+00:00"))
                q["created_at_jst"] = qdt.astimezone(JST).strftime("%Y-%m-%d %H:%M")
            except Exception:
                q["created_at_jst"] = ""
            d["quoted"] = q
        except Exception:
            d["quoted"] = None
    return d


def utc_key(d):
    """表示用の行を時系列に並べるためのキー。

    created_at のオフセットが X と IG で揃っていないので、
    文字列のままでは比較できない（UTCに直してから比べる）。
    """
    try:
        t = datetime.fromisoformat(str(d.get("created_at") or "").replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
