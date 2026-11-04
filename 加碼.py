"""大跌加碼器：大盤從近 2 年高點跌到 10%、15%、20%、25%、30% 時各通知一次（分批加碼 0050 的參考）；
大盤漲太多又遇到外資大賣時，寄「風險升高」提醒（不是叫你賣，是提醒先不要在高點追加資金）。

每天早上收資料（主.py --collect-only）後呼叫 check()。有觸發才寄信，沒有就不打擾。
狀態存在 output/dip_state.json：同一波下跌，每個跌幅只通知一次；指數創新高後重新開始。

回測依據（11/4 改用 1997～2026 加權指數，含 2000 網路泡沫、2008 金融海嘯；高點＝近 2 年最高）：
  跌 10% 那天買 → 1 年後平均 −2%（50% 賺錢）、2 年後平均 +15%（71% 賺錢）；之後平均還會再跌 18%，最多 62%
  1997～2015 年碰到時，1 年後多半是賠的；2016 年後的大多頭才幾乎都賺 → 只能當「2 年以上」的參考
  子彈分配：遞增（1/1.5/2/2.5/3 份）兩年後最差 −19%，一次全買最差 −30%
  「帶量破線」「在年線上下」測過沒有可靠的差別（樣本只有 17 波），所以不用來判斷
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
DEPLOY_DAYS = 125          # 第一次加碼通知後 6 個月（約 125 個交易日），剩下的子彈全部投入
BUDGET = 1_000_000         # 加碼預備金（使用者說 100 多萬，先用 100 萬算；要改就改這裡）
ETF = ("006208", "富邦台50")   # 加碼買哪一檔
PORTION = {0.10: 1, 0.15: 1.5, 0.20: 2, 0.25: 2.5, 0.30: 3}   # 加碼預備金分成 10 份，越跌買越多（剩下的留著）
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


def etf_price(sid=None):
    """ETF 最新收盤（證交所 STOCK_DAY，這個月和上個月）。抓不到回傳 (None, None)。"""
    sid = sid or ETF[0]
    rows = []
    now = R.now_tw()
    for k in (1, 0):
        d = (now.replace(day=1) - pd.DateOffset(months=k)).strftime("%Y%m01")
        try:
            j = R.get_json("https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY", {"stockNo": sid, "date": d, "response": "json"})
            f = j.get("fields") or []
            i_c = next(i for i, x in enumerate(f) if "收盤" in x)
            for r in j.get("data") or []:
                v = R.safe_float(r[i_c])
                if pd.notna(v):
                    rows.append((r[0], v))
        except Exception as e:
            print(f"{sid} 價格抓不到：{e}")
        R.time.sleep(1)
    return rows[-1] if rows else (None, None)


def lots(amount, price):
    """金額換成「幾張幾股」。"""
    if not price:
        return "（抓不到價格，請用現價自己換算）"
    sh = int(amount // price)
    z, o = divmod(sh, 1000)
    return " ".join(x for x in (f"{z} 張" if z else "", f"{o} 股（零股）" if o else "") if x) or "不到 1 股"


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
    if st["alerted"] and not st.get("start"):
        st["start"] = last_d                                # 這一波第一次通知的日子（6 個月期限從這天算）
    print(f"加權指數 {last_d} {last:,.0f}，高點 {peak_d} {peak:,.0f}（{dd:+.1%}）｜季線乖離 {gap:+.1%}｜外資近 10 天 {f10:,.0f} 億")
    mails = []
    hit = [lv for lv in LEVELS if dd <= -lv and lv not in st["alerted"]]
    if os.environ.get("DIP_RESEND") and not hit:                     # 手動重寄：用目前碰到的最深一檔再寄一次
        crossed = [lv for lv in LEVELS if dd <= -lv]
        if crossed:
            st["alerted"] = [x for x in st["alerted"] if x not in crossed]
            hit = crossed
    if hit:
        lv = max(hit)
        st["alerted"] = sorted(set(st["alerted"]) | set(hit))
        done = sum(PORTION[x] for x in st["alerted"])
        amt = sum(PORTION[x] for x in hit) * BUDGET / 10
        pdt, px = etf_price()
        nxt = [x for x in LEVELS if x not in st["alerted"]]
        lines = [f"👉 買 {ETF[1]}（{ETF[0]}） {lots(amt, px)}，大約 {amt / 1e4:,.0f} 萬"
                 + (f"（{pdt} 收盤 {R.fmt_now(px)} 元）" if px else ""),
                 "",
                 f"原因：大盤從高點 {peak:,.0f} 點跌到 {last:,.0f} 點（{dd:.1%}），碰到「跌 {int(lv * 100)}%」這一檔。",
                 f"這一波累計 {done * BUDGET / 10 / 1e4:,.0f} 萬，加碼預備金 {BUDGET / 1e4:,.0f} 萬還剩 {(10 - done) * BUDGET / 10 / 1e4:,.0f} 萬。",
                 (f"下一檔：大盤跌 {int(nxt[0] * 100)}%（約 {peak * (1 - nxt[0]):,.0f} 點）買 {PORTION[nxt[0]] * BUDGET / 10 / 1e4:,.0f} 萬。" if nxt else "這一波的加碼點都用完了。"),
                 f"期限：{(pd.Timestamp(st.get('start') or last_d) + pd.DateOffset(months=6)).strftime('%Y/%m/%d')} 前沒用完的，到時候會提醒你全部買進。",
                 "",
                 "（30 年統計：跌了分批買，抱 2 年以上平均約 +20%；1 年內可能還會再跌。只是參考，不是投資建議。）"]
        mails.append((f"📉 加碼：買 {ETF[1]} {lots(amt, px)}（大盤跌 {int(lv * 100)}%，{last_d[5:7]}/{last_d[8:]}）", "\n".join(lines)))
    if hit and not st.get("start"):
        st["start"] = last_d
    # 6 個月期限：第一次通知後約 125 個交易日，子彈還沒用完就提醒全部投入（30 年回測：2 年後平均 +17% → +20%，最差一樣 −19%）
    if st.get("start") and not st.get("deploy_sent") and (t.index > st["start"]).sum() >= DEPLOY_DAYS:
        used = sum(PORTION[x] for x in st["alerted"])
        if used < 10:
            st["deploy_sent"] = last_d
            rest = (10 - used) * BUDGET / 10
            pdt, px = etf_price()
            lines = [f"👉 買 {ETF[1]}（{ETF[0]}） {lots(rest, px)}，大約 {rest / 1e4:,.0f} 萬"
                     + (f"（{pdt} 收盤 {R.fmt_now(px)} 元）" if px else ""),
                     "",
                     f"原因：從 {st['start']} 第一次加碼到現在滿 6 個月，加碼預備金還剩 {rest / 1e4:,.0f} 萬，現在全部投入，不要再等更低的點。",
                     f"加權指數：{last_d} 收 {last:,.0f} 點（離高點 {dd:+.1%}）",
                     "（30 年統計：6 個月後把剩下的全買，比一直留著現金等，2 年後平均多 3 個百分點。只是參考，不是投資建議。）"]
            mails.append((f"💰 加碼：6 個月到了，買 {ETF[1]} {lots(rest, px)}（{last_d[5:7]}/{last_d[8:]}）", "\n".join(lines)))
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
