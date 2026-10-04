# -*- coding: utf-8 -*-
"""金像電型「低檔止跌」掃描：曾經大跌、現在剛好處在最近一年的最低點附近、已經不再創新低的股票。
法人（外資、投信）只當參考，列出自低點以來與近20日的累計買賣超，不當過濾條件。"""
import importlib, os, sys, argparse
import numpy as np, pandas as pd
R = importlib.import_module("主")

MIN_LOT_PRICE = float(os.getenv("LOW_MIN_PRICE", 300))   # 一張30萬以上
MIN_DROP   = float(os.getenv("LOW_MIN_DROP", 0.35))      # 高點到最低點至少跌 35%
NEAR_LOW   = float(os.getenv("LOW_NEAR", 0.10))          # 現價在最低點上方 12% 內
MIN_AGE    = int(os.getenv('LOW_MIN_AGE', 0))               # 最低點可以是今天（像金像電 705 當天就長下影線反彈）
WINDOW     = int(os.getenv('LOW_WINDOW', 120))           # 只看最近 120 個交易日（約 6 個月）
MIN_HIST   = 120
TOP_N = 40

def pick(px, inst, data_date):
    px = px.sort_values(["stock_id", "date"])
    liq = px.groupby("stock_id")["turnover"].apply(lambda s: s.tail(20).mean())
    inst = inst.copy(); inst["date"] = inst["date"].astype(str)
    inst["net"] = (inst["foreign_net"].fillna(0) + inst["trust_net"].fillna(0))
    ib = {s: g.sort_values("date") for s, g in inst.groupby("stock_id")}
    rows = []
    for sid, g in px.groupby("stock_id"):
        if not (len(sid) == 4 and sid.isdigit()) or len(g) < MIN_HIST: continue
        if str(g["date"].iloc[-1]) != data_date: continue
        g = g.tail(WINDOW)
        c = g["close"].to_numpy(float)
        if (np.abs(c[1:] / c[:-1] - 1) > 0.115).any(): continue   # 單日漲跌超過 11.5%＝可能是分割／減資，股價沒還原，跳過
        h = np.where(g["high"].notna(), g["high"], g["close"]).astype(float)
        l = np.where(g["low"].notna(), g["low"], g["close"]).astype(float)
        last = c[-1]
        if last < MIN_LOT_PRICE or liq.get(sid, 0) < R.MIN_DAILY_TURNOVER: continue
        k = int(np.argmin(l)); lo = l[k]                       # 最低點
        if len(c) - 1 - k < MIN_AGE: continue                  # 還在創新低
        hi_i = int(np.argmax(h[:k + 1])); hi = h[hi_i]         # 最低點之前的最高點
        if hi_i == k or lo > hi * (1 - MIN_DROP): continue     # 跌幅不夠
        if last > lo * (1 + NEAR_LOW): continue                # 已經離低點太遠
        dates = g["date"].astype(str).tolist()
        r = dict(stock_id=sid, name=g["stock_name"].iloc[-1], close=last, low=lo, high=hi,
                 drop=(1 - lo / hi) * 100, above=(last / lo - 1) * 100,
                 low_date=dates[k][5:], high_date=dates[hi_i][5:], age=len(c) - 1 - k,
                 rng5=(h[-5:].max() - l[-5:].min()) / last * 100)
        # 法人參考（外資、投信分開）
        ig = ib.get(sid)
        if ig is not None and len(ig) >= 20:
            vol = g.set_index(g["date"].astype(str))["volume"]
            def cum(col, sub): 
                v = float(vol.reindex(sub["date"]).fillna(0).sum()); return (sub[col].fillna(0).sum() / v * 100) if v else np.nan
            t20 = ig.tail(20); t20p = ig.iloc[-40:-20] if len(ig) >= 40 else None
            r.update(f20=cum("foreign_net", t20), t20=cum("trust_net", t20), n_inst=len(ig))
            r["f20_prev"] = cum("foreign_net", t20p) if t20p is not None else np.nan
            r["t20_prev"] = cum("trust_net", t20p) if t20p is not None else np.nan
            since = ig[ig["date"] >= dates[k]]
            r["since_low_shares"] = float(since["net"].sum()) / 1000   # 張
            r["since_low_f"] = float(since["foreign_net"].fillna(0).sum()) / 1000
            r["since_low_t"] = float(since["trust_net"].fillna(0).sum()) / 1000
            r["since_days"] = len(since)
        rows.append(r)
    return pd.DataFrame(rows)

def arrow(a, b):
    if pd.isna(a): return "資料不足"
    if pd.isna(b): return f"近20日 {a:+.1f}%"
    return f"近20日 {a:+.1f}%（前20日 {b:+.1f}%）" + ("↗" if a > b else "↘")

def fmt(x):
    s = (f"{x['name']}({x['stock_id']})｜收盤 {x['close']:g}｜離最低點 {x['above']:+.1f}%\n"
         f"   最低點 {x['low']:g}（{x['low_date']}，{x['age']} 個交易日前）｜之前高點 {x['high']:g}（{x['high_date']}）｜跌幅 {x['drop']:.0f}%｜近5日震幅 {x['rng5']:.1f}%")
    if pd.notna(x.get("f20", np.nan)):
        s += (f"\n   法人參考｜外資 {arrow(x['f20'], x['f20_prev'])}\n"
              f"             投信 {arrow(x['t20'], x['t20_prev'])}\n"
              f"             自最低點以來（{int(x['since_days'])} 個資料日）外資 {x['since_low_f']:+,.0f} 張、投信 {x['since_low_t']:+,.0f} 張")
    else:
        s += "\n   法人參考｜資料不足（上櫃股票法人資料還在累積）"
    return s

def run(send_mail=True):
    R.init_db()
    px = R.load_table("prices", 420)
    px["date"] = px["date"].astype(str)
    data_date = str(px["date"].max())
    inst = R.load_table("institutional", 420)
    df = pick(px, inst, data_date)
    print(f"資料日 {data_date}｜符合 {len(df)} 檔")
    if df.empty:
        body = "今天沒有符合的股票。"
    else:
        df = df.sort_values("above").head(TOP_N)
        df.to_csv(os.path.join(R.OUTPUT_DIR, f"lowzone_{data_date}.csv"), index=False, encoding="utf-8-sig")
        body = (f"低檔止跌掃描｜資料日 {data_date}\n曾經大跌、現在剛好在最近 6 個月（約 120 個交易日）最低點附近的股票：共 {len(df)} 檔（一張30萬以上，離最低點越近越前面）\n\n━━━━━━━━━━━━\n"
                + "\n\n".join(fmt(x) for _, x in df.iterrows())
                + "\n\n━━━━━━━━━━━━\n【怎麼看】\n條件：最近 120 個交易日的最低點之前的高點到最低點跌了 35% 以上；現在價格在最低點上方 10% 內（最低點可以是今天或剛過幾天）。\n"
                  "法人只是參考，沒有拿來篩選：看外資、投信近 20 日累計買賣超占成交量的 %，和前 20 日比是變多（↗）還是變少（↘）；以及自最低點以來累計買賣超幾張。\n"
                  "上市股票法人資料約 4 個月，上櫃股票還在累積。只看價格與籌碼，不含產業面，僅供參考，不保證獲利。")
    print(body)
    if send_mail and not df.empty:
        R.send_email(f"低檔止跌掃描 {data_date[5:].replace('-','/')}｜{len(df)}檔", body)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--no-email", action="store_true")
    run(not ap.parse_args().no_email)
