# -*- coding: utf-8 -*-
"""金像電型「低檔止跌」掃描：曾經大跌、現在剛好處在最近一年的最低點附近、已經不再創新低的股票。
法人（外資、投信）只當參考，列出自低點以來與近20日的累計買賣超，不當過濾條件。"""
import importlib, os, sys, argparse
import numpy as np, pandas as pd
R = importlib.import_module("主")

MIN_LOT_PRICE = float(os.getenv("LOW_MIN_PRICE", 100))   # 一張10萬以上（股價≥100）
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
        seg = g.iloc[hi_i:k + 1]
        trap = float(seg["turnover"].sum() / seg["volume"].sum()) if seg["volume"].sum() > 0 else np.nan   # 下跌這段的成交均價＝套牢區成本
        r = dict(trap=trap, high_full=dates[hi_i], low_full=dates[k], stock_id=sid, name=g["stock_name"].iloc[-1], close=last, low=lo, high=hi,
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
            sh = ig[ig["date"] >= dates[hi_i]]           # 自高點以來（法人資料只到 2026-06-08，更早的抓不到）
            vsh = float(vol.reindex(sh["date"]).fillna(0).sum())
            r["chip_pct"] = (sh["net"].sum() / vsh * 100) if vsh else np.nan
            r["chip_f"] = (sh["foreign_net"].fillna(0).sum() / vsh * 100) if vsh else np.nan
            r["chip_t"] = (sh["trust_net"].fillna(0).sum() / vsh * 100) if vsh else np.nan
            r["chip_days"] = len(sh); r["chip_full"] = bool(len(ig) and ig["date"].iloc[0] <= dates[hi_i])
            t10 = ig.tail(10)
            v10 = float(vol.reindex(t10["date"]).fillna(0).sum())
            r["chip10"] = (t10["net"].sum() / v10 * 100) if v10 else np.nan
            r["chip10_f"] = (t10["foreign_net"].fillna(0).sum() / v10 * 100) if v10 else np.nan
            r["chip10_t"] = (t10["trust_net"].fillna(0).sum() / v10 * 100) if v10 else np.nan
            since = ig[ig["date"] >= dates[k]]
            r["since_low_shares"] = float(since["net"].sum()) / 1000   # 張
            r["since_low_f"] = float(since["foreign_net"].fillna(0).sum()) / 1000
            r["since_low_t"] = float(since["trust_net"].fillna(0).sum()) / 1000
            r["since_days"] = len(since)
        rows.append(r)
    return pd.DataFrame(rows)

def chip_light(x):
    """低點有沒有法人撐著：看最近10個法人資料日，外資＋投信合計買賣超占成交量的 %"""
    c = x.get("chip10", np.nan)
    if pd.isna(c): return "⚪", "資料不足"
    if c >= 0: return "🟢", "法人撐著（近10日合計沒在賣）"
    if c >= -3: return "🟡", "法人小賣"
    return "🔴", "法人在賣、沒撐住"

def tick(p):
    """台股升降單位：把價格湊成實際能掛的價位"""
    if pd.isna(p): return p
    t = 0.01 if p < 10 else 0.05 if p < 50 else 0.1 if p < 100 else 0.5 if p < 500 else 1 if p < 1000 else 5
    return round(round(p / t) * t, 2)

def fmt(x):
    lt = chip_light(x)
    c, lo, hi = x["close"], x["low"], x["high"]
    trap = tick(x["trap"]) if pd.notna(x.get("trap", np.nan)) else np.nan
    s = f"{lt[0]} {x['name']}({x['stock_id']})｜收盤 {c:g}\n"
    s += f"   支撐（最低點）{lo:g}｜壓力（套牢區成交均價）{trap:g}｜前高 {hi:g}\n" if pd.notna(trap) else f"   支撐（最低點）{lo:g}｜前高 {hi:g}\n"
    if pd.notna(trap):
        rr = (trap / c - 1) / max(c / lo - 1, 0.005)
        s += f"   往下到支撐 {c - lo:.2f} 元（{(c/lo-1)*100:.1f}%）｜往上到壓力 {trap - c:.2f} 元（{(trap/c-1)*100:.1f}%）｜報酬風險比 {rr:.1f} 比 1\n".replace(".00 元", " 元")
    if pd.notna(x.get("chip10", np.nan)):
        s += f"   籌碼：近10日 外資 {x['chip10_f']:+.1f}%、投信 {x['chip10_t']:+.1f}%（占成交量）→ {lt[1]}"
        if not x.get("chip_full", True): s += "（法人資料只從 6/8 起）"
    else:
        s += "   籌碼：法人資料不足（上櫃股票還在累積）"
    s += f"\n   高點 {hi:g}（{x['high_date']}）→ 最低點 {lo:g}（{x['low_date']}），跌 {x['drop']:.0f}%"
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
        df["lt"] = [chip_light(r)[0] for _, r in df.iterrows()]
        df["_o"] = df["lt"].map({"🟢": 0, "🟡": 1, "⚪": 2, "🔴": 3})
        df = df.sort_values(["_o", "above"]).head(TOP_N)
        df.drop(columns=["_o"]).to_csv(os.path.join(R.OUTPUT_DIR, f"lowzone_{data_date}.csv"), index=False, encoding="utf-8-sig")
        cnt = {k: int((df["lt"] == k).sum()) for k in ["🟢", "🟡", "🔴", "⚪"]}
        body = (f"低檔止跌掃描｜資料日 {data_date}\n原本在高點、被賣下來，現在回到最近 6 個月最低點附近的股票：共 {len(df)} 檔（一張10萬以上）\n"
                f"低點籌碼：🟢法人撐著 {cnt['🟢']}｜🟡小賣 {cnt['🟡']}｜🔴沒撐住 {cnt['🔴']}｜⚪資料不足 {cnt['⚪']}\n\n━━━━━━━━━━━━\n"
                + "\n\n".join(fmt(x) for _, x in df.iterrows())
                + "\n\n━━━━━━━━━━━━\n【怎麼看】\n"
                  "條件：最近 120 個交易日內，從高點到最低點跌了 35% 以上；現在價格在最低點上方 10% 內（最低點可以是今天或剛過幾天）。\n"
                  "籌碼燈號（低點有沒有法人撐著）：看最近 10 個法人資料日，外資＋投信合計買賣超占成交量的 %。🟢 0% 以上＝法人在撐（有買或沒賣）；🟡 0% 到 -3%＝小賣；🔴 低於 -3%＝法人在賣、沒撐住。排序：🟢 在前，同燈號離最低點近的在前。「自高點以來」的數字當背景參考。報酬風險比＝（現價到套牢區）÷（現價到最低點）。\n"
                  "壓力（套牢區成交均價）：從高點跌到最低點這段的成交均價，代表套牢者的平均成本，反彈到這附近賣壓通常比較大；前高是更上面的壓力。價格已湊成實際可掛的價位。\n"
                  "注意：法人資料只有 2026-06-08 之後，高點早於這個日期的股票，籌碼只算到有資料的部分；上櫃股票法人資料還在累積。\n"
                  "我用歷史資料測過「自高點以來籌碼沒跑」，沒有看出後續報酬比較好（樣本只有 63 次）；低點有沒有法人撐著還沒驗證，所以都只當參考。不含產業面，僅供參考，不保證獲利。")
    print(body)
    if send_mail and not df.empty:
        R.send_email(f"低檔止跌掃描 {data_date[5:].replace('-','/')}｜{len(df)}檔（法人撐著{cnt['🟢']}）", body)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--no-email", action="store_true")
    run(not ap.parse_args().no_email)
