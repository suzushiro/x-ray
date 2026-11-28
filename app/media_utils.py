"""
ローカルに保存した画像の取り出し。

web.py と sources.py の両方から使う。web.py に置いたままだと
sources.py から参照できない（web.py が sources.py を import しているので
循環する）ので、小さな共有モジュールに分けてある。
"""

import json
import os

import cache_utils
from cache_utils import CACHE_DIR, IMAGES_DIR


def local_image_names(tweet_id, conn):
    """
    投稿に紐づくローカル保存済み画像のファイル名リストを返す。
    削除済み画像・ローカル未保存(リモートのみ)は除外する。
    （外部公開ページなので、手元にある実ファイルだけを対象にする）
    """
    row = conn.execute(
        "SELECT media_json, local_media_json FROM tweets WHERE tweet_id=?",
        (tweet_id,)).fetchone()
    if not row:
        # ブックマークにしか残っていない投稿（元ツイートが削除された等）も
        # 投稿できるようにフォールバックする。
        row = conn.execute(
            "SELECT media_json, local_media_json FROM bookmarks WHERE tweet_id=?",
            (tweet_id,)).fetchone()
    if not row:
        return []
    try:
        media = json.loads(row["media_json"] or "[]")
        local = json.loads(row["local_media_json"] or "[]")
    except Exception:
        return []
    deleted = cache_utils.deleted_urls(conn)
    names = []
    for i, remote in enumerate(media):
        if remote in deleted:
            continue
        lp = local[i] if i < len(local) else None
        if lp:
            names.append(os.path.basename(lp))
    return names


def local_image_paths(tweet_id, conn, indices=None):
    """
    投稿に紐づくローカル画像の実ファイルパスを返す。
    削除済み・ローカル未保存は除外する。

    indices（添字のリスト）を渡すとその添字だけに絞り、
    **渡された順序どおり**に返す。UI側が選んだ順に番号を振って見せているため、
    その順序をそのままTumblrへの添付順にする。
    """
    names = local_image_names(tweet_id, conn)
    if indices is not None:
        picked = []
        for i in indices:
            if 0 <= i < len(names) and names[i] not in picked:
                picked.append(names[i])
        names = picked
    paths = []
    for n in names:
        for d in (IMAGES_DIR, CACHE_DIR):
            fp = os.path.join(d, n)
            if os.path.exists(fp):
                paths.append(fp)
                break
    return paths
