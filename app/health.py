"""
「黙って止まっている」の見張り。

取得が落ちても画面は普通に出てしまうので、最後に取れた時刻と
出口IPの状態を見て異常を知らせる。web.py から切り出してあるのは、
ルーティングと判定の都合を1つのファイルに混ぜないため。
"""

import os
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# 取得が「黙って止まる」のを原因に関係なく見張るしきい値（時間）。
# 出口IPの判定で12日止まったとき、入れたのは「出口の異常をログに残す」で、
# 原因を1つ塞いだだけだった。想定していない死に方は必ず残るので、
# **最後に取れた時刻だけ**を見る見張りを別に置く。
# 15分巡回なので3時間＝12回ぶん空いたら異常とみなす。Xの一時的な不調で
# 鳴らない程度には鈍く、半日放置はしない程度には敏感な値として選んだ。
try:
    STALE_HOURS = float(os.environ.get("SCRAPE_STALE_HOURS", "3") or 3)
except ValueError:
    print("[!] SCRAPE_STALE_HOURS が数値ではありません（既定の3を使います）")
    STALE_HOURS = 3.0


def check_stale(conn):
    """
    最後に取得できてからの経過を見る。異常なら dict、正常なら None。

    成功ログが1件も無いときに黙って None を返してはいけない。
    scrape_log は7日で刈られるので、**7日以上止まると成功ログごと消えて
    「何も無い＝正常」に見えてしまう**。一番警告が要る状況で黙る。
    そこで成功ログが無いときは保存済みツイートの取得時刻で代用する。
    """
    if STALE_HOURS <= 0:
        return None

    row = conn.execute(
        "SELECT MAX(run_at) AS t FROM scrape_log WHERE status = 'ok'"
    ).fetchone()
    last, basis = (row["t"] if row else None), "log"

    if not last:
        row = conn.execute("SELECT MAX(fetched_at) AS t FROM tweets").fetchone()
        last, basis = (row["t"] if row else None), "tweets"

    if not last:
        return None             # 1件も取ったことがない＝入れたばかり

    try:
        t = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None

    hours = (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    if hours <= STALE_HOURS:
        return None
    return {
        "hours": int(hours),
        "days": round(hours / 24, 1),
        "since": t.astimezone(JST).strftime("%Y-%m-%d %H:%M"),
        "limit_h": round(STALE_HOURS, 1),
        # ログから分かったのか、ツイートの取得時刻で代用したのか。
        # 代用のときは「ログが刈られるほど長く止まっている」の合図でもある。
        "basis": basis,
    }
