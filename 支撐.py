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
REPLAY_DAYS = int(os.getenv('SW_REPLAY', 40))  # 第一次執行時，往回模擬幾個交易日
MAX_HOLD = 40       # 追蹤超過 40 個交易日還沒結果 → 逾期結案
COOLDOWN = 10       # 結案後 10 個交易日內不重複追蹤同一檔
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


def init_tables():
    with R.db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS sr_track(
            stock_id TEXT, first_date TEXT, stock_name TEXT, entry_px REAL, support REAL, resist REAL,
            last_date TEXT, last_px REAL, days INTEGER, status TEXT, end_date TEXT, end_px REAL,
            PRIMARY KEY(stock_id, first_date))""")
        c.execute("CREATE TABLE IF NOT EXISTS sr_meta(k TEXT PRIMARY KEY, v TEXT)")

def scan(groups, d, liq):
    """在 d 這一天收盤後，掃出符合『回檔到前低止跌盤整』的股票"""
    out = []
    for sid, g in groups.items():
        n = int(np.searchsorted(g["date"].to_numpy(), d, side="right"))
        if n < 40 or str(g["date"].iloc[n - 1]) != d: continue
        last = float(g["close"].iloc[n - 1])
        if last < MIN_LOT_PRICE or liq.get(sid, 0) < R.MIN_DAILY_TURNOVER: continue
        gg = g.iloc[:n]
        r = analyze(gg)
        if not r or r["kind"] != "B": continue
        r.update(stock_id=sid, name=g["stock_name"].iloc[n - 1], close=last,
                 l1d=str(gg["date"].iloc[r["i1"]])[5:], h1d=str(gg["date"].iloc[r["i2"]])[5:], l2d=str(gg["date"].iloc[r["k"]])[5:])
        out.append(r)
    return out

def step_day(conn, d, closes, matches, dates):
    """用 d 的收盤價更新追蹤中的股票，再把新符合的加進來"""
    cur = conn.execute("SELECT stock_id,first_date,support,resist,entry_px,days FROM sr_track WHERE status='追蹤中'")
    for sid, fd, L1, H1, e, days in cur.fetchall():
        px = closes.get(sid)
        if px is None: continue
        days += 1
        st = "追蹤中"
        if px > H1: st = "成功"
        elif px < L1 * (1 - BREAK_LO): st = "失敗"
        elif days >= MAX_HOLD: st = "逾期"
        conn.execute("UPDATE sr_track SET last_date=?,last_px=?,days=?,status=?,end_date=?,end_px=? WHERE stock_id=? AND first_date=?",
                     (d, px, days, st, d if st != "追蹤中" else None, px if st != "追蹤中" else None, sid, fd))
    new = []
    di = dates.index(d)
    cool = dates[max(0, di - COOLDOWN)]
    for r in matches:
        sid = r["stock_id"]
        busy = conn.execute("SELECT 1 FROM sr_track WHERE stock_id=? AND (status='追蹤中' OR end_date>=?)", (sid, cool)).fetchone()
        if busy: continue
        conn.execute("INSERT OR REPLACE INTO sr_track VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (sid, d, r["name"], r["close"], r["L1"], r["H1"], d, r["close"], 0, "追蹤中", None, None))
        new.append(r)
    return new

def pct(a, b): return (a / b - 1) * 100

def build_body(conn, data_date, new_today, ndays):
    tr = pd.read_sql("SELECT * FROM sr_track", conn)
    opn = tr[tr.status == "追蹤中"].sort_values("first_date", ascending=False)
    done = tr[tr.status != "追蹤中"]
    def line_open(x):
        return (f"{x['stock_name']}({x['stock_id']})｜進榜 {x['first_date'][5:]} 價 {x['entry_px']:g} → 現價 {x['last_px']:g}（{pct(x['last_px'], x['entry_px']):+.1f}%，第 {x['days']} 天）\n"
                f"   支撐區 {x['support']:g}（再跌破 {x['support']*(1-BREAK_LO):.4g} 收盤＝失敗）｜前高壓力區 {x['resist']:g}（還差 {pct(x['resist'], x['last_px']):.0f}%）")
    def line_done(x):
        icon = {"成功": "✅", "失敗": "❌", "逾期": "⏱"}[x["status"]]
        return (f"{icon}{x['stock_name']}({x['stock_id']})｜{x['first_date'][5:]} 進榜價 {x['entry_px']:g} → {x['end_date'][5:]} {x['end_px']:g}"
                f"（{pct(x['end_px'], x['entry_px']):+.1f}%，{x['status']}，{x['days']} 天）")
    def fmt_new(x):
        return (f"{x['name']}({x['stock_id']})｜收盤 {x['close']:g}\n"
                f"   支撐區 {x['L1']:g}（{x['l1d']}前一次波段低點）｜這次回檔低點 {x['L2']:g}（{x['l2d']}）\n"
                f"   前高壓力區 {x['H1']:g}（{x['h1d']}）｜還差 {pct(x['H1'], x['close']):.0f}%｜近5日震幅 {x['rng5']*100:.1f}%")
    recent = done[done.end_date >= str(sorted(tr["last_date"].unique())[-6] if tr["last_date"].nunique() > 6 else "")].sort_values("end_date", ascending=False)
    if len(done):
        ok = done[done.status == "成功"]; bad = done[done.status == "失敗"]
        rets = [pct(r.end_px, r.entry_px) for r in done.itertuples()]
        score = (f"累計結案 {len(done)} 檔：成功 {len(ok)}｜失敗 {len(bad)}｜逾期 {len(done)-len(ok)-len(bad)}\n"
                 f"   成功率 {len(ok)/len(done)*100:.0f}%｜平均報酬 {np.mean(rets):+.1f}%"
                 f"｜成功平均 {np.mean([pct(r.end_px, r.entry_px) for r in ok.itertuples()]) if len(ok) else 0:+.1f}%"
                 f"｜失敗平均 {np.mean([pct(r.end_px, r.entry_px) for r in bad.itertuples()]) if len(bad) else 0:+.1f}%")
    else:
        score = "還沒有結案的股票（要等股價突破前高或跌破支撐才算結案）"
    sep = "\n\n━━━━━━━━━━━━\n"
    return (f"支撐回檔掃描｜資料日 {data_date}（{ndays} 個交易日）\n追蹤中 {len(opn)} 檔｜今天新進 {len(new_today)} 檔｜一張30萬以上"
            + sep + f"🆕 今天新進榜 {len(new_today)} 檔（回到前低、止跌盤整）\n\n" + ("\n\n".join(fmt_new(x) for x in sorted(new_today, key=lambda r: r['close']/r['L1'])) or "（今天沒有新進）")
            + sep + f"📍 追蹤中 {len(opn)} 檔（尚未突破前高、也沒跌破支撐）\n\n" + ("\n\n".join(line_open(x) for _, x in opn.iterrows()) or "（沒有）")
            + sep + "🏁 最近結案\n\n" + ("\n".join(line_done(x) for _, x in recent.iterrows()) or "（最近沒有）")
            + sep + "📊 成績\n" + score
            + sep + "【怎麼看】\n支撐區＝前一次波段低點；壓力區＝前一次波段高點。\n"
              "進榜：前低→漲15%以上到前高→回到前低上方5%內→低點後築底3天以上→近5日震幅7%以內→離前高還有8%以上空間。\n"
              "結案：收盤站上前高＝成功；收盤跌破前低3%＝失敗；追蹤超過40個交易日沒結果＝逾期。\n"
              "第一次執行時，會用資料庫裡的歷史資料倒推模擬，所以成績一開始就有；之後每天自動追蹤。\n"
              "只看日K價格，不含產業與法人面；僅供參考。")

def run(send_mail=True, replay=None, reset=False):
    global REPLAY_DAYS
    if replay: REPLAY_DAYS = replay
    R.init_db(); init_tables()
    if reset:
        with R.db() as c:
            c.execute("DELETE FROM sr_track"); c.execute("DELETE FROM sr_meta")
        print("已清除舊的追蹤紀錄，重新倒推")
    px = R.load_table("prices", 150 + int(REPLAY_DAYS * 1.6)).sort_values(["stock_id", "date"])
    px = px[px["stock_id"].str.fullmatch(r"\d{4}")]
    dates = sorted(px["date"].astype(str).unique())
    data_date = dates[-1]
    px["date"] = px["date"].astype(str)
    groups = {sid: g.reset_index(drop=True) for sid, g in px.groupby("stock_id")}
    liq = px.groupby("stock_id")["turnover"].apply(lambda s: s.tail(20).mean())
    with R.db() as conn:
        row = conn.execute("SELECT v FROM sr_meta WHERE k='last'").fetchone()
        last = row[0] if row else None
        todo = [d for d in dates if (d > last)] if last else dates[-REPLAY_DAYS:]
        print(f"要處理的日期：{len(todo)} 天（{'接續' if last else '第一次，倒推模擬'}）")
        new_today = []
        for d in todo:
            closes = {sid: float(g.loc[g["date"] == d, "close"].iloc[0]) for sid, g in groups.items() if (g["date"] == d).any()}
            new = step_day(conn, d, closes, scan(groups, d, liq), dates)
            if d == data_date: new_today = new
            conn.execute("INSERT OR REPLACE INTO sr_meta VALUES ('last', ?)", (d,))
            conn.commit()
        body = build_body(conn, data_date, new_today, len(dates))
        pd.read_sql("SELECT * FROM sr_track ORDER BY first_date DESC", conn).to_csv(
            os.path.join(R.OUTPUT_DIR, "support_track.csv"), index=False, encoding="utf-8-sig")
    print(body)
    if send_mail:
        n_open = body.split("追蹤中 ")[1].split(" 檔")[0]
        R.send_email(f"支撐回檔掃描 {data_date[5:].replace('-','/')}｜新進{len(new_today)} 追蹤{n_open}", body)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--replay", type=int, default=0, help="第一次執行時往回模擬幾個交易日")
    ap.add_argument("--reset", action="store_true", help="清掉追蹤紀錄重算")
    a = ap.parse_args()
    run(not a.no_email, a.replay or None, a.reset)
