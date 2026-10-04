# -*- coding: utf-8 -*-
"""波段回測支撐掃描：找「前低→前高→回到前低附近止跌盤整」的股票。
A 已突破：回到前低後盤整，再漲過前高。 B 回檔中：現在正貼著前低支撐區止跌盤整。
只看價格（日K高低收），不看法人。"""
import importlib, os, sys, argparse
import numpy as np, pandas as pd
R = importlib.import_module("主")

SWING_PCT   = float(os.getenv("SW_PCT", 0.10))    # 反向走 10% 才算一個波段轉折
MIN_RISE    = float(os.getenv("SW_RISE", 0.15))   # 前低到前高至少漲 15%
NEAR_LO     = 0.05    # 回檔低點離前低上方 5% 內算「回到底部區」
BREAK_LO    = 0.03    # 收盤跌破前低 3% 以上算有效跌破
MIN_GAP     = 5       # 前高到這次回檔低點至少隔 5 天
CONSOL_RNG  = 0.07    # B：最近5日高低差占收盤 ≤7% 算盤整
MIN_BASE    = 3       # 回檔低點後至少 3 天（有築底動作）
MAX_B_FROM_HIGH = 0.08  # B：離前高至少還有 8% 空間
TOP_N = 25
MIN_LOT_PRICE = float(os.getenv('SW_MIN_PRICE', 300))  # 一張(1000股)至少30萬 → 股價≥300，上限不設

def zigzag(h, l, c, pct):
    """回傳轉折點 [(idx, 'L'/'H', price)]"""
    n = len(c); piv = []
    if n < 10: return piv
    hi_i = lo_i = 0; trend = 0
    for i in range(1, n):
        if h[i] > h[hi_i]: hi_i = i
        if l[i] < l[lo_i]: lo_i = i
        if trend >= 0 and l[i] <= h[hi_i] * (1 - pct) and hi_i < i and trend != 1 or (trend == 0 and l[i] <= h[hi_i]*(1-pct)):
            pass
        if trend == 0:
            if h[hi_i] >= l[lo_i] * (1 + pct) and hi_i > lo_i:
                piv.append((lo_i, 'L', l[lo_i])); trend = 1; lo_i = i; hi_i = hi_i
            elif h[hi_i] >= l[lo_i]*(1+pct) and lo_i > hi_i:
                pass
            if l[lo_i] <= h[hi_i]*(1-pct) and lo_i > hi_i:
                piv.append((hi_i, 'H', h[hi_i])); trend = -1; hi_i = i
        elif trend == 1:
            if h[i] >= h[hi_i]: hi_i = i
            if l[i] <= h[hi_i] * (1 - pct):
                piv.append((hi_i, 'H', h[hi_i])); trend = -1; lo_i = i
        else:
            if l[i] <= l[lo_i]: lo_i = i
            if h[i] >= l[lo_i] * (1 + pct):
                piv.append((lo_i, 'L', l[lo_i])); trend = 1; hi_i = i
    # 最後一個未確認的極值
    if trend == 1: piv.append((hi_i, 'H', h[hi_i]))
    elif trend == -1: piv.append((lo_i, 'L', l[lo_i]))
    return piv

def analyze(g):
    c = g["close"].to_numpy(float)
    h = np.where(g["high"].notna(), g["high"], g["close"]).astype(float)
    l = np.where(g["low"].notna(), g["low"], g["close"]).astype(float)
    n = len(c)
    piv = zigzag(h, l, c, SWING_PCT)
    best = None
    # 找 L1(低) → H1(高) → 之後回到 L1 附近
    for a in range(len(piv) - 1):
        i1, t1, L1 = piv[a]
        if t1 != 'L': continue
        # H1：L1 之後的最高轉折高點
        j = a + 1
        if piv[j][1] != 'H': continue
        i2, _, H1 = piv[j]
        if H1 < L1 * (1 + MIN_RISE): continue
        s = i2 + MIN_GAP
        if s >= n - 1: continue
        seg_l = l[s:]; seg_c = c[s:]
        # 沒有「有效跌破」：高點之後所有收盤 ≥ L1*(1-3%)
        if (c[i2:] < L1 * (1 - BREAK_LO)).any(): continue
        k = s + int(np.argmin(seg_l)); L2 = l[k]
        if L2 > L1 * (1 + NEAR_LO): continue          # 沒回到底部區
        if n - 1 - k < MIN_BASE: continue             # 還沒築底
        if k <= i2: continue
        after_c = c[k:]
        brk = np.where(c[k:] > H1)[0]
        last = c[-1]
        r = dict(L1=L1, H1=H1, L2=L2, i1=i1, i2=i2, k=k)
        if len(brk):
            bi = k + int(brk[0])
            if last >= H1 * 0.99:                      # 已突破且仍站在前高附近以上
                r.update(kind="A", brk_days=n-1-bi, brk_px=c[bi])
                best = r; continue
        else:
            rng5 = (h[-5:].max() - l[-5:].min()) / last
            if (last <= L1 * (1 + 0.08) and last >= L1 * (1 - BREAK_LO) and rng5 <= CONSOL_RNG
                    and last * (1 + MAX_B_FROM_HIGH) <= H1 and n - 1 - k <= 25):
                r.update(kind="B", rng5=rng5)
                best = r
    return best

def run(send_mail=True):
    R.init_db()
    px = R.load_table("prices", 150)
    px = px.sort_values(["stock_id", "date"])
    data_date = str(px["date"].max())
    ndays = px["date"].nunique()
    liq = px.groupby("stock_id")["turnover"].apply(lambda s: s.tail(20).mean())
    rows = []
    for sid, g in px.groupby("stock_id"):
        if len(g) < 40 or not (len(sid) == 4 and sid.isdigit()): continue
        if str(g["date"].iloc[-1]) != data_date: continue
        last = float(g["close"].iloc[-1])
        if last < MIN_LOT_PRICE or liq.get(sid, 0) < R.MIN_DAILY_TURNOVER: continue
        r = analyze(g)
        if not r: continue
        r.update(stock_id=sid, name=g["stock_name"].iloc[-1], close=last,
                 vol_ratio=float(g["volume"].tail(5).mean() / max(g["volume"].tail(40).mean(), 1)),
                 l1d=str(g["date"].iloc[r["i1"]])[5:], h1d=str(g["date"].iloc[r["i2"]])[5:], l2d=str(g["date"].iloc[r["k"]])[5:])
        rows.append(r)
    df = pd.DataFrame(rows)
    print(f"資料日 {data_date}｜{ndays} 個交易日｜符合 {len(df)} 檔")
    if df.empty:
        body = "今天沒有符合的股票。"
    else:
        df["to_high"] = df["H1"] / df["close"] - 1
        df["to_low"] = df["close"] / df["L1"] - 1
        df["hold"] = df["L2"] / df["L1"] - 1
        B = df[df.kind == "B"].sort_values("to_low")
        df = B
        df.to_csv(os.path.join(R.OUTPUT_DIR, f"support_{data_date}.csv"), index=False, encoding="utf-8-sig")
        def fmt(x):
            return (f"{x['name']}({x['stock_id']})｜收盤 {x['close']:g}\n"
                    f"   支撐區 {x['L1']:g}（{x['l1d']}前一次波段低點）｜這次回檔低點 {x['L2']:g}（{x['l2d']}，{x['hold']*100:+.1f}%）\n"
                    f"   前高壓力區 {x['H1']:g}（{x['h1d']}）｜還差 {x['to_high']*100:.0f}%｜近5日震幅 {x['rng5']*100:.1f}%")
        body = (f"支撐回檔掃描｜資料日 {data_date}（{ndays} 個交易日）\n"
                f"股價回到前一次波段低點附近、沒有有效跌破、正在止跌盤整，之後有機會再挑戰前高：共 {len(B)} 檔（一張30萬以上，離支撐越近越前面）\n\n━━━━━━━━━━━━\n"
                + ("\n\n".join(fmt(x) for _, x in B.iterrows()) or "（沒有）")
                + "\n\n━━━━━━━━━━━━\n【怎麼看】\n支撐區＝前一次波段低點；壓力區＝前一次波段高點。\n"
                  "條件：前低→漲15%以上到前高→回到前低上方5%內→收盤沒有跌破前低3%以上→低點後至少築底3天→最近5日震幅7%以內→離前高還有8%以上空間。\n"
                  "失敗訊號：收盤跌破支撐區（前低−3%）。只看日K價格，不含產業與法人面；僅供參考。")
    print(body)
    if send_mail and not df.empty:
        R.send_email(f"支撐回檔掃描 {data_date[5:].replace('-','/')}｜{len(B)}檔", body)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--no-email", action="store_true")
    run(not ap.parse_args().no_email)
