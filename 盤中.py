# -*- coding: utf-8 -*-
"""11:30 盤中信：檢查今天「可買」的股票還能不能買、你的持股盤中有沒有要跑。

資料：證交所盤中即時報價（mis.twse.com.tw）。法人資料要收盤後才公布，所以盤中只看股價和成交量。
用法：python 盤中.py            # 抓即時報價 → 寄信
      python 盤中.py --no-email # 只印出來
"""
import argparse
import importlib
import time
from datetime import datetime

import numpy as np
import pandas as pd
import requests

R = importlib.import_module("主")

MIS_INDEX = "https://mis.twse.com.tw/stock/index.jsp"
MIS_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

CHASE_PCT = 3.0            # 可買：今天已經漲超過此 % → 別追
WEAK_PCT = -3.0            # 可買／持股：今天跌超過此 %
HEAVY_VOL = 1.5            # 盤中防線：照目前速度，全天量會超過 5 日均量的此倍數
SHADOW_PCT = 3.0           # 盤中長上影：最高價到現價超過此 %
SHADOW_VOL = 2.0           # 盤中長上影：照目前速度，全天量超過 5 日均量的此倍數
BELOW_COST_PCT = R.BELOW_COST_PCT if hasattr(R, "BELOW_COST_PCT") else 3.0


def num(v):
    try:
        t = str(v).split("_")[0].replace(",", "").strip()
        return float(t) if t not in ("", "-") else np.nan
    except (TypeError, ValueError):
        return np.nan


def fetch_quotes(stocks):
    """stocks：{代號: 'tse' 或 'otc'}。回傳 {代號: dict(price, open, high, low, prev, vol_lots, date, name)}。"""
    if not stocks:
        return {}
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 Chrome/124.0", "Referer": MIS_INDEX})
    try:
        s.get(MIS_INDEX, timeout=15)
    except requests.RequestException as e:
        print(f"MIS 首頁連線失敗（繼續嘗試）：{e}")
    out, ids = {}, list(stocks.items())
    for i in range(0, len(ids), 40):                     # 一次最多查 40 檔
        chunk = ids[i:i + 40]
        ex_ch = "|".join(f"{mk}_{sid}.tw" for sid, mk in chunk)
        for attempt in range(3):
            try:
                r = s.get(MIS_API, params={"ex_ch": ex_ch, "json": "1", "delay": "0",
                                           "_": int(time.time() * 1000)}, timeout=15)
                r.raise_for_status()
                data = r.json().get("msgArray", [])
                break
            except Exception as e:
                print(f"即時報價第 {attempt + 1} 次失敗：{e}")
                data = []
                time.sleep(2)
        for it in data:
            sid = str(it.get("c", "")).strip()
            price = num(it.get("z"))
            if np.isnan(price):                          # 這一刻沒有成交：用最佳買價代替
                price = num(it.get("b"))
            out[sid] = dict(price=price, open=num(it.get("o")), high=num(it.get("h")), low=num(it.get("l")),
                            prev=num(it.get("y")), vol_lots=num(it.get("v")), date=str(it.get("d", "")),
                            name=str(it.get("n", "")).strip())
        time.sleep(1)
    return out


def day_fraction(now):
    """開盤到現在占全天交易時間（09:00～13:30）的比例，用來推估全天成交量。"""
    m = (now.hour * 60 + now.minute) - 9 * 60
    return min(max(m / 270, 1 / 270), 1.0)


def load_context():
    """從資料庫抓：最新一天的可買名單、每檔的市場別、前 5 日均量（張）、法人成本。"""
    R.init_db()
    with R.db() as conn:
        last = conn.execute("SELECT MAX(date) FROM picks").fetchone()[0]          # 早上那封信的資料日
        buys = pd.read_sql("SELECT stock_id, stock_name, close FROM picks WHERE date=? AND grp='C回補'",
                           conn, params=(last,), dtype={"stock_id": str}) if last else pd.DataFrame()
        waits = pd.read_sql("SELECT stock_id, stock_name, close, note FROM picks WHERE date=? AND grp='C等噴出'",
                            conn, params=(last,), dtype={"stock_id": str}) if last else pd.DataFrame()
    px = R.load_table("prices", 40)
    px = px.sort_values(["stock_id", "date"])
    market = px.groupby("stock_id")["market"].last().to_dict()
    vol5 = (px.groupby("stock_id")["volume"].apply(lambda s: pd.to_numeric(s, errors="coerce").tail(5).mean()) / 1000).to_dict()
    names = px.groupby("stock_id")["stock_name"].last().to_dict()
    cost = R.make_inst_cost(R.load_table("institutional", 30), R.load_table("prices", 30))
    cost = cost.set_index("stock_id")["inst_cost"].to_dict() if not cost.empty else {}
    return last, buys, waits, market, vol5, names, cost


def buy_status(q, vol5, frac):
    chg = (q["price"] / q["prev"] - 1) * 100
    proj = q["vol_lots"] / frac if pd.notna(q["vol_lots"]) else np.nan
    heavy = pd.notna(proj) and pd.notna(vol5) and vol5 > 0 and proj >= vol5 * HEAVY_VOL
    if chg >= CHASE_PCT:
        return chg, "🔴 漲太多，別追"
    if chg <= WEAK_PCT and heavy:
        return chg, "🔴 爆量走弱，先別買"
    if chg <= WEAK_PCT or (pd.notna(q["open"]) and q["price"] < q["open"] and heavy):
        return chg, "🟡 走弱，等收盤再說"
    return chg, "🟢 還可以買"


def hold_status(q, vol5, frac, icost):
    p, chg = q["price"], (q["price"] / q["prev"] - 1) * 100
    proj = q["vol_lots"] / frac if pd.notna(q["vol_lots"]) else np.nan
    vr = proj / vol5 if (pd.notna(proj) and pd.notna(vol5) and vol5 > 0) else np.nan
    hi, lo = q["high"], q["low"]
    if pd.notna(q["open"]) and p < q["open"] and chg <= WEAK_PCT and pd.notna(vr) and vr >= HEAVY_VOL:
        return chg, "🔴 爆量下殺，快跑"
    if pd.notna(icost) and p < icost * (1 - BELOW_COST_PCT / 100):
        return chg, "🔴 跌破法人成本，快跑"
    if (pd.notna(hi) and pd.notna(lo) and hi > lo and (hi - p) / p * 100 >= SHADOW_PCT
            and (p - lo) / (hi - lo) <= 0.5 and pd.notna(vr) and vr >= SHADOW_VOL):
        return chg, "🔴 拉高倒貨，快跑"
    if chg <= WEAK_PCT:
        return chg, "🟡 跌比較多，留意"
    return chg, "🟢 正常"


def block(name, sid, q, chg, status):
    return (f"股票代號：{name}({sid})\n"
            f"目前價格：{R.fmt_now(q['price'])}（今天 {chg:+.1f}%）\n"
            f"盤中狀況：{status}")


def run(send_mail=True, now=None, quotes=None):
    now = now or R.now_tw()
    last, buys, waits, market, vol5, names, cost = load_context()
    holdings = R.load_holdings()
    want = {sid: ("otc" if market.get(sid) == "TPEx" else "tse")
            for sid in list(buys["stock_id"] if not buys.empty else []) + list(waits["stock_id"] if not waits.empty else [])
            + list(holdings)}
    if not want:
        print("今天沒有可買、等噴出、持股要看，不寄盤中信。")
        return
    quotes = quotes if quotes is not None else fetch_quotes(want)
    today = now.strftime("%Y%m%d")
    live = {k: v for k, v in quotes.items() if v["date"] == today and pd.notna(v["price"]) and pd.notna(v["prev"])}
    if not live:
        print(f"今天（{today}）沒有盤中報價：休市，或證交所即時報價抓不到。不寄信。")
        return
    frac = day_fraction(now)
    parts = [f"盤中狀況：🟢 正常／可買　🟡 留意　🔴 快跑／別追（{now:%H:%M} 報價）"]

    parts.append(f"\n【今天可買】{len(buys)} 檔")
    if buys.empty:
        parts.append("今天沒有")
    for _, b in buys.iterrows():
        q = live.get(b["stock_id"])
        if q is None:
            parts.append(f"\n股票代號：{b['stock_name']}({b['stock_id']})\n目前價格：抓不到報價")
            continue
        chg, st = buy_status(q, vol5.get(b["stock_id"]), frac)
        parts.append("\n" + block(b["stock_name"], b["stock_id"], q, chg, st))

    if not waits.empty:
        turned, broke = [], []
        for _, w in waits.iterrows():
            q = live.get(w["stock_id"])
            if q is None:
                continue
            try:
                s4, s9, s19, bottom = (float(x) for x in str(w["note"]).split("|"))
            except ValueError:
                continue                                      # 舊格式的紀錄，略過
            p = q["price"]
            ma5, ma10, ma20 = (s4 + p) / 5, (s9 + p) / 10, (s19 + p) / 20   # 用目前價格算今天的均線
            chg = (p / q["prev"] - 1) * 100
            if p < bottom:
                broke.append(block(w["stock_name"], w["stock_id"], q, chg, f"🔴 跌破低點 {R.fmt_now(bottom)}，止跌失敗"))
            elif p > ma5 and p > ma10 and p < ma20:
                turned.append(block(w["stock_name"], w["stock_id"], q, chg,
                                    f"🟢 盤中站上 5 日線 {R.fmt_now(ma5)}、10 日線 {R.fmt_now(ma10)}（月線 {R.fmt_now(ma20)}），"
                                    f"收盤還在上面就是買點｜停損 {R.fmt_now(bottom)}"))
        parts.append(f"\n\n【止跌股盤中轉強】{len(turned)} 檔（從 {len(waits)} 檔 🟡 裡找）")
        parts.extend("\n" + x for x in turned)
        if broke:
            parts.append(f"\n\n【止跌股跌破低點】{len(broke)} 檔")
            parts.extend("\n" + x for x in broke)

    n_red = 0
    parts.append(f"\n\n【我的持股】{len(holdings)} 檔")
    if not holdings:
        parts.append("（股票追蹤清單是空的）")
    for sid, (cost_in, group) in holdings.items():
        q = live.get(sid)
        nm = names.get(sid, sid)
        if q is None:
            parts.append(f"\n股票代號：{nm}({sid})\n目前價格：抓不到報價")
            continue
        chg, st = hold_status(q, vol5.get(sid), frac, cost.get(sid, np.nan))
        n_red += st.startswith("🔴")
        parts.append("\n" + block(nm, sid, q, chg, st))
    body = "\n".join(parts) + "\n"
    print(body)
    if send_mail:
        subject = f"{'🚨' if n_red else ''}盤中 {now:%m/%d %H:%M}｜可買{len(buys)}" + (f" 持股快跑{n_red}" if n_red else "")
        R.send_email(subject, body)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="11:30 盤中信")
    ap.add_argument("--no-email", action="store_true")
    run(send_mail=not ap.parse_args().no_email)
