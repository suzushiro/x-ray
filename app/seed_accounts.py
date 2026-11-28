# Notion「Xアカウント」DBから取得した監視対象マスタ
# (screen_name, display_name, [categories])
#
# 注: 監視対象は data/accounts.json で管理されるようになりました。
#     このファイルの ACCOUNTS_FALLBACK は accounts.json が存在しない場合の
#     初期データ（フォールバック）としてのみ使われます。
#     通常の追加・編集は Web UI (/manage) または accounts.json を直接編集してください。

import os
import json

ACCOUNTS_JSON_PATH = os.environ.get("ACCOUNTS_JSON_PATH", "/data/accounts.json")

# 監視対象の実データは data/accounts.json（gitignore 済み）だけに置く。
#
# 以前はここに実アカウント名を62件ベタ書きしていた。accounts.json が
# 真のマスタになった後も残っていたため、公開リポジトリに監視対象一覧が
# そのまま載る状態だった。フォールバックは「空」にして、手元で初期データを
# 持たせたいときだけ data/accounts_fallback.json を置く運用にする。
FALLBACK_JSON_PATH = os.environ.get(
    "ACCOUNTS_FALLBACK_JSON",
    os.path.join(os.path.dirname(ACCOUNTS_JSON_PATH), "accounts_fallback.json"))


def _load_fallback_file():
    """手元に置いた初期データ（gitignore 済み）。無ければ空。"""
    try:
        with open(FALLBACK_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return [(a["screen_name"], a["display_name"], a["categories"])
                for a in data.get("accounts", [])]
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[!] {FALLBACK_JSON_PATH} の読み込み失敗: {e}")
        return []


ACCOUNTS_FALLBACK = []

ALL_CATEGORIES_FALLBACK = [
    "ギャル", "videogame", "clubmusic", "artist",
    "writer", "developer", "illustrator", "news", "photographer",
    "gadget", "R18",
]


def _load_from_json():
    """accounts.jsonから読み込む。なければフォールバックを返す。"""
    if os.path.exists(ACCOUNTS_JSON_PATH):
        try:
            with open(ACCOUNTS_JSON_PATH, encoding="utf-8") as f:
                data = json.load(f)
            accounts = [
                (a["screen_name"], a["display_name"], a["categories"])
                for a in data.get("accounts", [])
            ]
            categories = data.get("categories", ALL_CATEGORIES_FALLBACK)
            if accounts:
                return accounts, categories
        except Exception as e:
            print(f"[!] accounts.json読み込み失敗、フォールバックを使用: {e}")
    return (ACCOUNTS_FALLBACK or _load_fallback_file()), ALL_CATEGORIES_FALLBACK


def save_to_json(accounts, categories):
    """accounts と categories を accounts.json に保存"""
    data = {
        "categories": categories,
        "accounts": [
            {"screen_name": sn, "display_name": dn, "categories": cats}
            for sn, dn, cats in accounts
        ],
    }
    with open(ACCOUNTS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# モジュール読み込み時にJSONから展開
ACCOUNTS, ALL_CATEGORIES = _load_from_json()
