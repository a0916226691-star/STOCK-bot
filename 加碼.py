"""大跌加碼器：大盤從近 2 年高點跌到 10%、15%、20%、25%、30% 時各通知一次（分批加碼 0050 的參考）；
大盤漲太多又遇到外資大賣時，寄「風險升高」提醒（不是叫你賣，是提醒先不要在高點追加資金）。

盤中每 30 分鐘 intraday()：碰到新檔位先寄「盤中觸及」；每晚 20:47（長線.py --hold-watch）用當天收盤 check() 確認，
盤中寄過的不重複寄；早上 7:10 收資料後也會再跑一次 check() 當備援。有觸發才寄信，沒有就不打擾。
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
    pre = [x for x in hit if x in st.get("intra", [])]              # 盤中已經寄過「盤中觸及」：收盤確認，不再重複寄
    if pre:
        st["alerted"] = sorted(set(st["alerted"]) | set(pre))
        hit = [x for x in hit if x not in pre]
        print(f"收盤確認：跌 {'、'.join(f'{int(x * 100)}%' for x in pre)} 盤中已通知過，不重複寄")
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


MIS_INDEX = "https://mis.twse.com.tw/stock/index.jsp"
MIS_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"


def mis_now():
    """證交所盤中即時：加權指數、006208 現價。回 (時間, 指數, ETF 價)；不在交易時間或抓不到回 None。"""
    s = R.requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 Chrome/124.0", "Referer": MIS_INDEX})
    try:
        s.get(MIS_INDEX, timeout=15)
        j = s.get(MIS_API, params={"ex_ch": f"tse_t00.tw|tse_{ETF[0]}.tw", "json": "1", "delay": "0"}, timeout=15).json()
    except Exception as e:
        print(f"盤中報價抓不到：{e}")
        return None
    q = {x.get("c"): x for x in j.get("msgArray") or []}
    num = lambda v: R.safe_float(v) if v not in (None, "-", "") else np.nan
    ix, et = q.get("t00", {}), q.get(ETF[0], {})
    v = num(ix.get("z"))
    if pd.isna(v):
        return None
    if ix.get("d") != R.now_tw().strftime("%Y%m%d"):
        print(f"報價日期 {ix.get('d')} 不是今天（休市？）")
        return None
    p = num(et.get("z"))
    if pd.isna(p):
        p = num((et.get("b") or "").split("_")[0])                 # 還沒成交就用最佳買價
    return ix.get("t", ""), float(v), (float(p) if pd.notna(p) else None)


def intraday(send_mail=True, sample=False):
    """盤中每 30 分鐘：大盤碰到新的加碼檔位就先寄「盤中觸及」（收盤還沒確定，要不要先買自己決定）。
    同一檔只寄一次；當天晚上用收盤價確認，盤中寄過的不再重複寄。sample=True 只寄一封範例信、不改狀態。"""
    with R.db() as c:
        t = pd.read_sql("SELECT date, close FROM taiex ORDER BY date", c).set_index("date")["close"]
    peak_d, peak = t.idxmax(), float(t.max())
    try:
        st = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        st = {}
    if st.get("peak_date") != peak_d:
        st = {"peak_date": peak_d, "alerted": [], "risk_date": st.get("risk_date", "")}
    now = mis_now()
    if now is None and not sample:
        print("不在交易時間或抓不到盤中報價，跳過")
        return
    tm, v, px = now if now else ("10:30（範例時間）", float(t.iloc[-1]), None)
    px_note = "盤中"
    if px is None:                                                   # 盤中抓不到 ETF 價（或範例信）：用上一個交易日收盤估張數
        pdt, px = etf_price()
        px_note = f"{pdt} 收盤，實際以下單時的價格為準" if px else ""
    dd = v / peak - 1
    sent = set(st["alerted"]) | set(st.get("intra", []))
    hit = [lv for lv in LEVELS if dd <= -lv and lv not in sent]
    print(f"盤中 {tm} 加權指數 {v:,.0f}（離高點 {dd:+.1%}）")
    if sample:
        nxt = [x for x in LEVELS if x not in sent]
        if not nxt:
            print("加碼點都用完了，沒有範例可寄")
            return
        hit = [nxt[0]]
    if not hit:
        print("盤中：沒有碰到新的加碼檔位")
        return
    lv = max(hit)
    amt = sum(PORTION[x] for x in hit) * BUDGET / 10
    done = sum(PORTION[x] for x in sent | set(hit))
    nxt = [x for x in LEVELS if x not in sent | set(hit)]
    if sample:
        v_show, dd_show = peak * (1 - lv), -lv
    else:
        v_show, dd_show = v, dd
    lines = (["【這是範例信，不是真的觸發】大盤真的碰到時，信會長這樣：", ""] if sample else []) + [
        f"👉 可以先買 {ETF[1]}（{ETF[0]}） {lots(amt, px)}，大約 {amt / 1e4:,.0f} 萬" + (f"（{px_note} {px:,.2f} 元）" if px else ""),
        "",
        f"原因：盤中 {tm} 大盤 {v_show:,.0f} 點，離高點 {peak:,.0f} 點 {dd_show:.1%}，碰到「跌 {int(lv * 100)}%」這一檔。",
        "⚠️ 這是盤中觸及，收盤還沒確定：收盤可能又拉回來。要不要現在就買，你自己決定；也可以等今晚的收盤確認。",
        "今晚 21:00 左右會用收盤價確認：收盤也跌破，就不會再寄同一檔的信；收盤拉回，這一檔先保留，之後收盤跌破也不會重複寄。",
        "買了就不用再買第二次。",
        "",
        f"這一波買完這檔累計 {done * BUDGET / 10 / 1e4:,.0f} 萬，加碼預備金 {BUDGET / 1e4:,.0f} 萬還剩 {(10 - done) * BUDGET / 10 / 1e4:,.0f} 萬。",
        (f"下一檔：大盤跌 {int(nxt[0] * 100)}%（約 {peak * (1 - nxt[0]):,.0f} 點）買 {PORTION[nxt[0]] * BUDGET / 10 / 1e4:,.0f} 萬。" if nxt else "這一波的加碼點都用完了。"),
        "",
        "（30 年統計：跌了分批買，抱 2 年以上平均約 +20%；1 年內可能還會再跌。只是參考，不是投資建議。）"]
    subj = f"{'【範例】' if sample else ''}⚡ 盤中觸及：大盤跌 {int(lv * 100)}%，可以先買 {ETF[1]} {lots(amt, px)}"
    body = "\n".join(lines)
    print("\n===== " + subj + " =====\n" + body)
    if sample:
        if send_mail:
            R.send_email(subj, body)
        return
    st["intra"] = sorted(set(st.get("intra", [])) | set(hit))
    if not st.get("start"):
        st["start"] = R.now_tw().strftime("%Y-%m-%d")
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
    if send_mail:
        R.send_email(subj, body)


if __name__ == "__main__":
    import sys
    if "--intraday" in sys.argv:
        intraday(send_mail="--no-email" not in sys.argv, sample="--sample" in sys.argv)
    else:
        check(send_mail=False)
