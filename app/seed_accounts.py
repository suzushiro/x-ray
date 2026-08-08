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

ACCOUNTS_FALLBACK = [
    ("nemoto_nagi", "根本凪", ["ギャル"]),
    ("minmin12344", "山谷花純", ["ギャル"]),
    ("sakurako144", "さくらこ", ["ギャル"]),
    ("ooo0914ooo", "さくらこ2", ["ギャル"]),
    ("barrel_0709", "Barrel", ["ギャル"]),
    ("CAOSnaSIO", "なつか", ["ギャル"]),
    ("AmaharaRuri", "天原瑠理", ["ギャル"]),
    ("R_Ap8_", "あまつまりな▽", ["ギャル"]),
    ("Igavehimanacorn", "暫(しばし)", ["ギャル"]),
    ("ksz_mican", "小鈴みかん", ["R18"]),
    ("tateno_saki1113", "立野沙紀@DRAW♡ME", ["ギャル"]),
    ("ohshiri2ki", "凛々奏", ["ギャル"]),
    ("gira_giragira", "社員食堂ギラギラ", ["ギャル"]),
    ("1035_magica", "とみこ", ["ギャル"]),
    ("hyoro1000", "葉月つばさ", ["ギャル"]),
    ("misaki_sawa_", "沢美沙樹", ["ギャル"]),
    ("Rin_Wada", "和田輪", ["ギャル"]),
    ("jazz_zzz_zz", "jazz", ["ギャル"]),
    ("merocha_rui", "青実るい", ["ギャル"]),
    ("y_u_ur_i", "ゆーり", ["ギャル"]),
    ("shiri_mochi02", "れいあ", ["R18"]),
    ("himitsukun2nd", "秘密君", ["R18"]),
    ("ko_kuramoto", "kuramoto", ["videogame"]),
    ("tereparu", "てれぱる", ["videogame"]),
    ("BNE_BNGM", "Bandai Namco Game Music", ["videogame"]),
    ("yoshivesmovie", "吉天堂", ["videogame", "gadget"]),
    ("yoshitend", "4.3(吉天堂)", ["videogame", "gadget"]),
    ("KX174A", "KXA", ["clubmusic", "artist"]),
    ("maticlog", "メチクロ", ["artist"]),
    ("ik_products", "HIROTO IKEUCHI", ["artist"]),
    ("hamusuko3", "ハ息子", ["artist", "ギャル"]),
    ("dnobori", "登大遊", ["developer"]),
    ("0x71ff", "0x71", ["developer"]),
    ("hikalium", "hikalium", ["developer"]),
    ("brave", "Brave", ["developer"]),
    ("kojima_hideo", "小島秀夫", ["artist", "videogame", "developer"]),
    ("ikuto_yamashita", "山下いくと", ["illustrator"]),
    ("akinosuzume_X", "秋野すずめ", ["illustrator", "ギャル"]),
    ("sayakamurata", "村田沙耶香", ["writer"]),
    ("hazuma", "東浩紀", ["writer"]),
    ("kyousuke_kyouko", "京すけ", ["illustrator"]),
    ("tsutomu_nihei", "弐瓶勉", ["illustrator"]),
    ("320_42", "水野しず", ["illustrator", "writer"]),
    ("katakura_seishi", "揉むげ", ["illustrator"]),
    ("motoyawata__bot", "本八幡Bot", ["news"]),
    ("yukiao", "青山裕企", ["photographer"]),
    ("comuram", "コムラマイ", ["photographer"]),
    ("akiraa_tanaka", "田中晃", ["developer", "gadget"]),
    ("megamouth_blog", "megamouthの葬列", ["writer"]),   
    ("sigure_official", "凛として時雨", ["artist"]),
    ("Yellock_jp", "YELLOCK", ["clubmusic", "artist"]),
    ("YASUKI_DJ", "YASUKI", ["clubmusic", "artist"]),
    ("nothing", "Nothing", ["gadget"]),
    ("yoshinokentarou", "吉野@連邦", ["videogame", "gadget"]),
    ("CyberpunkGame", "Cyberpunk 2077", ["videogame"]),
    ("sheying_haoting", "摄影师浩廷", ["photographer"]),
    ("BBS_nakano", "中野雅之", ["artist"]),
    ("Kuvshinov_Ilya", "イリヤ・クブシノブ", ["illustrator"]),
    ("ROCK_NEET_GIRL", "ろっくちゃん", ["ギャル"]),
    ("naoiinari", "いいなりなお", ["R18"]),
    ("monhannoero", "non", ["ギャル"]),
    ("anna0108yamada", "山田杏奈", ["ギャル"]),
]

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
    return ACCOUNTS_FALLBACK, ALL_CATEGORIES_FALLBACK


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
