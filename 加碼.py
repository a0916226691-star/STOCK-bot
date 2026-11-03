"""大跌加碼器：大盤從高點跌到 10%、15%、20%、25%、30% 時各通知一次（分批加碼 0050 的參考）；
大盤漲太多又遇到外資大賣時，寄「風險升高」提醒（不是叫你賣，是提醒先不要在高點追加資金）。

每天早上收資料（主.py --collect-only）後呼叫 check()。有觸發才寄信，沒有就不打擾。
狀態存在 output/dip_state.json：同一波下跌，每個跌幅只通知一次；指數創新高後重新開始。

回測依據（2016/11～2026/11，0050 含息）：
  大盤從高點跌 10% 那天買 0050 → 1 年後平均 +45%，91% 賺錢
  跌 15% → 1 年後平均 +54%，100% 賺錢；跌 20% → 平均 +56%，100% 賺錢
  但買進後通常還會再跌：那一波最深 −10%～−32%
風險訊號（2019～2026）：指數比季線高 8% 以上、同時外資近 10 天賣超超過 811 億
  → 之後 3 個月內大跌 10% 的機率 47%（平常 19%）；照著賣反而年化少 10 個百分點以上，所以只提醒、不叫賣。
"""
import json
import os
from datetime import timedelta

import numpy as np
import pandas as pd

import importlib.util

_spec = importlib.util.spec_from_file_location("radar_main_dip", os.path.join(os.path.dirname(os.path.abspath(__file__)), "主.py"))
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

LEVELS = [0.10, 0.15, 0.20, 0.25, 0.30]
RISK_GAP = 0.08            # 指數比季線（60 日）高 8% 以上
RISK_FOREIGN = -811.5      # 外資（上市＋上櫃）近 10 天賣超金額（億）低於這個＝歷史上賣最多的兩成
RISK_COOLDOWN = 20         # 風險提醒至少隔 20 個交易日才再寄一次
STATS = {0.10: "1 年後平均 +45%，91% 賺錢", 0.15: "1 年後平均 +54%，100% 賺錢", 0.20: "1 年後平均 +56%，100% 賺錢",
         0.25: "次數太少，參考 20% 那一列", 0.30: "次數太少，參考 20% 那一列"}
STATE = os.path.join(R.OUTPUT_DIR, "dip_state.json")


def _roc_date(s):
    y, m, d = s.strip().split("/")
    return f"{int(y) + 1911}-{int(m):02d}-{int(d):02d}"


def update_taiex(months=2):
    """用證交所 FMTQIK（每日市場成交資訊）抓加權指數收盤，存進 taiex 表。第一次會補 24 個月。"""
    with R.db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS taiex (date TEXT PRIMARY KEY, close REAL)")
        n = c.execute("SELECT COUNT(*) FROM taiex").fetchone()[0]
    if n < 200:
        months = 24
    now = R.now_tw()
    rows = []
    for k in range(months):
        d = (now.replace(day=1) - pd.DateOffset(months=k)).strftime("%Y%m01")
        try:
            j = R.get_json("https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK", {"date": d, "response": "json"})
            f = j.get("fields") or []
            i_d, i_c = f.index("日期"), next(i for i, x in enumerate(f) if "加權" in x)
            for r in j.get("data") or []:
                v = R.safe_float(r[i_c])
                if pd.notna(v):
                    rows.append((_roc_date(r[i_d]), v))
        except Exception as e:
            print(f"加權指數 {d[:6]} 抓不到：{e}")
        R.time.sleep(2)
    with R.db() as c:
        c.executemany("INSERT OR REPLACE INTO taiex VALUES (?,?)", rows)
    print(f"加權指數：更新 {len(rows)} 天")


def foreign_10d():
    """外資（上市＋上櫃）近 10 個交易日買賣超金額（億元）：每檔買賣超股數 × 收盤價加總。"""
    ins = R.load_table("institutional", 30)
    px = R.load_table("prices", 30)
    if ins.empty or px.empty:
        return np.nan
    m = ins.merge(px[["date", "stock_id", "close"]], on=["date", "stock_id"])
    daily = (pd.to_numeric(m["foreign_net"], errors="coerce") * m["close"]).groupby(m["date"]).sum() / 1e8
    daily = daily.sort_index()
    return float(daily.tail(10).sum()) if len(daily) >= 10 else np.nan


def check(send_mail=True):
    update_taiex()
    with R.db() as c:
        t = pd.read_sql("SELECT date, close FROM taiex ORDER BY date", c).set_index("date")["close"]
    if len(t) < 60:
        print("加權指數資料不夠，先不判斷")
        return
    last_d, last = t.index[-1], float(t.iloc[-1])
    peak_d, peak = t.idxmax(), float(t.max())
    dd = last / peak - 1
    gap = last / float(t.tail(60).mean()) - 1
    f10 = foreign_10d()
    try:
        st = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        st = {}
    if st.get("peak_date") != peak_d:                       # 創新高（或第一次跑）：重新開始這一波
        st = {"peak_date": peak_d, "alerted": [], "risk_date": st.get("risk_date", "")}
    print(f"加權指數 {last_d} {last:,.0f}，高點 {peak_d} {peak:,.0f}（{dd:+.1%}）｜季線乖離 {gap:+.1%}｜外資近 10 天 {f10:,.0f} 億")
    mails = []
    hit = [lv for lv in LEVELS if dd <= -lv and lv not in st["alerted"]]
    if hit:
        lv = max(hit)
        st["alerted"] = sorted(set(st["alerted"]) | set(hit))
        lines = [f"大盤從高點跌了 {-dd:.1%}，碰到「跌 {int(lv * 100)}%」這一檔加碼點。",
                 f"加權指數：{last_d} 收 {last:,.0f} 點（高點 {peak_d} {peak:,.0f} 點）",
                 "",
                 "過去 10 年（2016～2026），大盤第一次跌到這個幅度那天買 0050（含息）：",
                 "  跌 10%：1 年後平均 +45%，91% 賺錢",
                 "  跌 15%：1 年後平均 +54%，100% 賺錢",
                 "  跌 20%：1 年後平均 +56%，100% 賺錢",
                 "但買進後通常還會再跌一段，那一波最深跌到 −10%～−32%，所以建議分批：",
                 "每碰到下一個跌幅（15%、20%、25%、30%）再加碼一份，不要一次買完。",
                 "",
                 f"這一波還沒通知的加碼點：{'、'.join(f'{int(x * 100)}%' for x in LEVELS if x not in st['alerted']) or '已經全部通知過'}",
                 "大盤創新高之後會重新開始計算。",
                 "（過去 10 年是台股大多頭，未來不一定一樣；這只是歷史統計，不是投資建議。）"]
        mails.append((f"📉 大跌加碼點：大盤從高點跌 {int(lv * 100)}%（{last_d[5:7]}/{last_d[8:]}）", "\n".join(lines)))
    risk = pd.notna(f10) and gap >= RISK_GAP and f10 <= RISK_FOREIGN
    if risk:
        days_since = (t.index > st.get("risk_date", "")).sum() if st.get("risk_date") else 999
        if days_since >= RISK_COOLDOWN:
            st["risk_date"] = last_d
            lines = [f"大盤比季線高 {gap:.1%}（漲多），同時外資近 10 天賣超 {-f10:,.0f} 億。",
                     "",
                     "2019～2026 年出現這種組合時，之後 3 個月內大盤跌超過 10% 的機率是 47%（平常是 19%）。",
                     "不是叫你賣：回測照這個訊號賣掉，假警報太多，年化反而少 10 個百分點以上。",
                     "只是提醒：先不要在這時候大筆追加資金，留一點錢等加碼點的通知。",
                     "（只是歷史統計，不是投資建議。）"]
            mails.append((f"⚠️ 大盤風險升高提醒（{last_d[5:7]}/{last_d[8:]}）", "\n".join(lines)))
    try:
        json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
    except OSError as e:
        print(f"寫加碼器狀態失敗：{e}")
    for subj, body in mails:
        print("\n===== " + subj + " =====\n" + body)
        if send_mail:
            R.send_email(subj, body)
    if not mails:
        print("大跌加碼器：今天沒有要通知的")


if __name__ == "__main__":
    check(send_mail=False)
