# -*- coding: utf-8 -*-
"""長線成長股雷達（跟短線完全分開）。

資料：月營收（公開資訊觀測站）、每季 EPS／淨利／權益（綜合損益表、資產負債表彙總），
      股價和月 K 直接讀短線的資料庫 output/tw_radar.db（只讀，不寫）。
自己的資料存在 output/longterm.db。

用法：python 長線.py --backfill      # 回補過去 3 年月營收、財報
      python 長線.py                 # 每週：更新資料、選股、寄信
      python 長線.py --no-email
"""
import argparse
import io
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

import 主 as R

LT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "longterm.db")
MOPS_HOSTS = ["https://mopsov.twse.com.tw"]      # mops.twse.com.tw 從 GitHub 連不到
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"}
REV_COLS = ["month", "stock_id", "name", "market", "revenue", "rev_ly", "yoy", "mom"]
FIN_COLS = ["quarter", "stock_id", "market", "eps", "net_income", "equity", "op_revenue", "op_income"]


@contextmanager
def ldb():
    conn = sqlite3.connect(LT_DB)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_ldb():
    os.makedirs(os.path.dirname(LT_DB), exist_ok=True)
    with ldb() as c:
        c.execute("CREATE TABLE IF NOT EXISTS revenue (month TEXT, stock_id TEXT, name TEXT, market TEXT, revenue REAL, "
                  "rev_ly REAL, yoy REAL, mom REAL, PRIMARY KEY(month, stock_id))")
        c.execute("CREATE TABLE IF NOT EXISTS fin (quarter TEXT, stock_id TEXT, market TEXT, eps REAL, net_income REAL, "
                  "equity REAL, PRIMARY KEY(quarter, stock_id))")
        have = {r[1] for r in c.execute("PRAGMA table_info(fin)")}
        for col in ("op_revenue", "op_income"):                      # 營業收入、營業利益（年初累計）
            if col not in have:
                c.execute(f"ALTER TABLE fin ADD COLUMN {col} REAL")


def lupsert(table, df, cols):
    if df is None or df.empty:
        return 0
    df = df[cols].replace({np.nan: None})
    with ldb() as c:
        c.executemany(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                      df.values.tolist())
    return len(df)


def _num(v):
    try:
        t = str(v).replace(",", "").strip()
        return float(t) if t not in ("", "-", "--", "nan", "None") else np.nan
    except ValueError:
        return np.nan


def _flat(df):
    """read_html 的多層欄位攤平成一層文字。"""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ["-".join(str(x) for x in col if "Unnamed" not in str(x)) for col in df.columns]
    df.columns = [str(c).replace(" ", "") for c in df.columns]
    return df


def _get(path, method="get", data=None):
    last = None
    for host in MOPS_HOSTS:
        for i in range(2):
            try:
                r = (requests.post if method == "post" else requests.get)(host + path, data=data, headers=UA, timeout=30)
                r.raise_for_status()
                if len(r.content) < 500:
                    raise ValueError(f"回應太短：{r.text[:100]}")
                return r
            except Exception as e:
                last = e
                time.sleep(3)
    raise last


# ───────────── 月營收 ─────────────
def fetch_revenue(year, month, market, ky=False):
    """market：sii（上市）或 otc（上櫃）。回傳該月全部公司營收。ky=True 抓外國公司（KY）那張表。"""
    roc = year - 1911
    r = _get(f"/nas/t21/{market}/t21sc03_{roc}_{month}_{1 if ky else 0}.html")
    r.encoding = "big5"
    tables = pd.read_html(io.StringIO(r.text))
    rows = []
    for t in tables:
        t = _flat(t)
        c_id = next((c for c in t.columns if c.endswith("公司代號")), None)
        c_rev = next((c for c in t.columns if "當月營收" in c), None)
        c_ly = next((c for c in t.columns if "去年當月營收" in c), None)
        c_yoy = next((c for c in t.columns if "去年同月增減" in c), None)
        c_mom = next((c for c in t.columns if "上月比較增減" in c), None)
        c_nm = next((c for c in t.columns if c.endswith("公司名稱")), None)
        if not (c_id and c_rev):
            continue
        for _, x in t.iterrows():
            sid = str(x[c_id]).strip()
            if not R.is_stock_id(sid):
                continue
            rows.append({"month": f"{year}-{month:02d}", "stock_id": sid, "name": str(x[c_nm]).strip() if c_nm else "",
                         "market": "TWSE" if market == "sii" else "TPEx", "revenue": _num(x[c_rev]),
                         "rev_ly": _num(x[c_ly]) if c_ly else np.nan, "yoy": _num(x[c_yoy]) if c_yoy else np.nan,
                         "mom": _num(x[c_mom]) if c_mom else np.nan})
    return pd.DataFrame(rows, columns=REV_COLS).drop_duplicates(["month", "stock_id"])


# ───────────── 季報：EPS、淨利、權益 ─────────────
def _fin_tables(api, market, year, season):
    data = {"encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1, "isQuery": "Y", "TYPEK": market,
            "year": year - 1911, "season": f"{season:02d}"}
    r = _get(f"/mops/web/{api}", "post", data)
    r.encoding = "utf-8"
    return [_flat(t) for t in pd.read_html(io.StringIO(r.text))]


def fetch_fin(year, season, market):
    q = f"{year}Q{season}"
    inc, bal = {}, {}
    for t in _fin_tables("ajax_t163sb04", market, year, season):          # 綜合損益表彙總
        if "公司代號" not in t.columns:
            continue
        c_eps = next((c for c in t.columns if "基本每股盈餘" in c), None)
        c_ni = next((c for c in t.columns if "歸屬於母公司業主" in c and "淨利" in c), None) or \
            next((c for c in t.columns if c.startswith("本期淨利") or c.startswith("本期稅後淨利")), None)
        c_rev = next((c for c in t.columns if c.startswith("營業收入")), None)
        c_oi = next((c for c in t.columns if c.startswith("營業利益")), None)
        for _, x in t.iterrows():
            sid = str(x["公司代號"]).strip()
            if R.is_stock_id(sid):
                inc[sid] = (_num(x[c_eps]) if c_eps else np.nan, _num(x[c_ni]) if c_ni else np.nan,
                            _num(x[c_rev]) if c_rev else np.nan, _num(x[c_oi]) if c_oi else np.nan)
    time.sleep(3)
    for t in _fin_tables("ajax_t163sb05", market, year, season):          # 資產負債表彙總
        if "公司代號" not in t.columns:
            continue
        c_eq = next((c for c in t.columns if "歸屬於母公司業主之權益" in c), None) or \
            next((c for c in t.columns if c.startswith("權益總") or c.startswith("權益合計")), None)
        for _, x in t.iterrows():
            sid = str(x["公司代號"]).strip()
            if R.is_stock_id(sid) and c_eq:
                bal[sid] = _num(x[c_eq])
    rows = [{"quarter": q, "stock_id": s, "market": "TWSE" if market == "sii" else "TPEx", "eps": v[0],
             "net_income": v[1], "equity": bal.get(s, np.nan), "op_revenue": v[2], "op_income": v[3]} for s, v in inc.items()]
    return pd.DataFrame(rows, columns=FIN_COLS)


LOG = []


def log(msg):
    print(msg)
    LOG.append(msg)
    with open(os.path.join(os.path.dirname(LT_DB), "lt_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(LOG[-300:]))


def backfill(years=3, budget_min=70, refresh_recent=False):
    init_ldb()
    now = R.now_tw()
    t0 = time.time()
    over = lambda: (time.time() - t0) / 60 > budget_min
    recent = {str(pd.Period(now.strftime("%Y-%m")) - k) for k in (1, 2)} if refresh_recent else set()
    with ldb() as c:
        have_r = {(a, b) for a, b in c.execute("SELECT month, market FROM revenue GROUP BY month, market HAVING COUNT(*) > 100")
                  if a not in recent}
        have_f = {(a, b) for a, b in c.execute("SELECT quarter, market FROM fin GROUP BY quarter, market "
                                                 "HAVING COUNT(*) > 100 AND COUNT(op_income) > 100")}
    start = (now - timedelta(days=int(365 * years) + 400)).replace(day=1)        # 多抓一年，才算得出年增率的歷史
    for m in pd.period_range(start.strftime("%Y-%m"), now.strftime("%Y-%m"), freq="M")[:-1]:
        for mk in ("sii", "otc"):
            key = (str(m), "TWSE" if mk == "sii" else "TPEx")
            if key in have_r:
                continue
            if over():
                log("時間到，先存檔，下次接著補"); break
            try:
                n = lupsert("revenue", fetch_revenue(m.year, m.month, mk), REV_COLS)
                log(f"月營收 {m} {mk}：{n} 家")
            except Exception as e:
                log(f"月營收 {m} {mk} 失敗：{e}")
            time.sleep(5)
    with ldb() as c:                                                     # KY 公司的營收在另一張表（_1）
        have_ky = {(a, b) for a, b in c.execute("SELECT month, market FROM revenue WHERE name LIKE '%KY%' GROUP BY month, market")
                   if a not in recent}
    for m in pd.period_range(start.strftime("%Y-%m"), now.strftime("%Y-%m"), freq="M")[:-1]:
        for mk in ("sii", "otc"):
            key = (str(m), "TWSE" if mk == "sii" else "TPEx")
            if key in have_ky or over():
                continue
            try:
                n = lupsert("revenue", fetch_revenue(m.year, m.month, mk, ky=True), REV_COLS)
                log(f"月營收（KY）{m} {mk}：{n} 家")
            except Exception as e:
                log(f"月營收（KY）{m} {mk} 失敗：{e}")
            time.sleep(3)
    for y in range(start.year, now.year + 1):
        for s in (1, 2, 3, 4):
            deadline = {1: f"{y}-05-16", 2: f"{y}-08-15", 3: f"{y}-11-15", 4: f"{y + 1}-04-01"}[s]
            if now.strftime("%Y-%m-%d") < deadline:                      # 還沒到公布期限
                break
            for mk in ("sii", "otc"):
                key = (f"{y}Q{s}", "TWSE" if mk == "sii" else "TPEx")
                if key in have_f:
                    continue
                if over():
                    log("時間到，先存檔，下次接著補"); break
                try:
                    n = lupsert("fin", fetch_fin(y, s, mk), FIN_COLS)
                    log(f"財報 {y}Q{s} {mk}：{n} 家")
                except Exception as e:
                    log(f"財報 {y}Q{s} {mk} 失敗：{e}")
                time.sleep(4)
    with ldb() as c:
        log("月營收：" + str(c.execute("SELECT market, COUNT(DISTINCT month), MIN(month), MAX(month), COUNT(*) FROM revenue GROUP BY market").fetchall()))
        log("財報：" + str(c.execute("SELECT market, COUNT(DISTINCT quarter), MIN(quarter), MAX(quarter), COUNT(*) FROM fin GROUP BY market").fetchall()))


# ───────────── 選股 ─────────────
TOP_N = 10                 # 每週列幾檔
YOY3_MIN = 0.20            # 近 3 個月營收年增率平均 > 20%
MIN_PRICE = 20
MIN_TURNOVER = 30_000_000  # 近 20 日平均成交金額
MAX_HOLD_MONTHS = 12       # 回測最多看 12 個月


def load_all():
    with ldb() as c:
        rev = pd.read_sql("SELECT month, stock_id, name, revenue, rev_ly FROM revenue", c, dtype={"stock_id": str})
        fin = pd.read_sql("SELECT * FROM fin", c, dtype={"stock_id": str})
    with sqlite3.connect(R.DB_PATH) as c:
        mon = pd.read_sql("SELECT month, stock_id, close FROM monthly", c, dtype={"stock_id": str})
        px = pd.read_sql("SELECT date, stock_id, stock_name, close, turnover FROM prices WHERE date >= ?", c,
                         params=((R.now_tw() - timedelta(days=45)).strftime("%Y-%m-%d"),), dtype={"stock_id": str})
    return rev, fin, mon, px


def ttm_fin(fin):
    """每檔最新一季的 近四季 EPS、ROE（年初累計數字換算成單季）。"""
    f = fin.copy()
    f["y"] = f["quarter"].str[:4].astype(int)
    f["q"] = f["quarter"].str[-1].astype(int)
    f = f.sort_values(["stock_id", "y", "q"])
    for col in ("eps", "net_income"):
        f[col + "_q"] = f[col] - f.groupby(["stock_id", "y"])[col].shift(1).fillna(0)
    f["idx"] = f["y"] * 4 + f["q"]
    out = {}
    for sid, g in f.groupby("stock_id"):
        g = g.set_index("idx")
        k = g.index.max()
        if not all(i in g.index for i in range(k - 3, k + 1)):
            continue
        eps = float(g.loc[k - 3:k, "eps_q"].sum())
        ni = float(g.loc[k - 3:k, "net_income_q"].sum())
        eq = g.loc[k, "equity"]
        out[sid] = (eps, ni / eq if pd.notna(eq) and eq > 0 else np.nan, g.loc[k, "quarter"])
    return out


def screen():
    rev, fin, mon, px = load_all()
    rev["yoy"] = np.where(rev["rev_ly"] > 0, rev["revenue"] / rev["rev_ly"] - 1, np.nan)
    RV = rev.pivot(index="month", columns="stock_id", values="revenue").sort_index()
    YY = rev.pivot(index="month", columns="stock_id", values="yoy").sort_index()
    names = rev.sort_values("month").groupby("stock_id")["name"].last().to_dict()
    last = RV.index.max()
    C = mon.pivot(index="month", columns="stock_id", values="close").sort_index()
    MA = C.rolling(10).mean()
    fx = ttm_fin(fin)
    px = px.sort_values("date")
    close = px.groupby("stock_id")["close"].last()
    to20 = px.groupby("stock_id")["turnover"].apply(lambda s: pd.to_numeric(s, errors="coerce").tail(20).mean())
    pnames = px.groupby("stock_id")["stock_name"].last().to_dict()
    rows = []
    for sid in RV.columns:
        if sid.startswith("00") or sid not in close.index or sid not in C.columns:
            continue
        c = float(close[sid])
        if c < MIN_PRICE or not to20.get(sid, 0) >= MIN_TURNOVER:
            continue
        r = RV[sid].dropna()
        if len(r) < 12 or r.index[-1] != last:
            continue
        yoy3 = float(YY[sid].loc[r.index[-3:]].mean())
        revhi = r.iloc[-1] >= r.iloc[-12:-1].max()
        ma = MA[sid].dropna()
        trend = len(ma) >= 2 and c > ma.iloc[-1] and ma.iloc[-1] > ma.iloc[-2]
        eps, roe, q = fx.get(sid, (np.nan, np.nan, ""))
        yy = YY[sid].dropna()
        rows.append({"stock_id": sid, "name": pnames.get(sid, names.get(sid, sid)), "close": c, "yoy3": yoy3,
                     "yoy_last": float(yy.iloc[-1]) if len(yy) else np.nan, "revhi": bool(revhi), "trend": bool(trend),
                     "eps": eps, "roe": roe, "fin_q": q, "rev_month": last,
                     "neg2": bool(len(yy) >= 2 and yy.iloc[-1] < 0 and yy.iloc[-2] < 0)})
    df = pd.DataFrame(rows)
    ok = df[df["revhi"] & df["trend"] & (df["yoy3"] > YOY3_MIN) & (df["eps"] > 0)]
    return df, ok.sort_values("roe", ascending=False).head(TOP_N), last


def long_hold_alerts(rev, today):
    """長期持股（股票追蹤〔長期持股〕區）：營收連 2 個月年減才提醒，同一檔同一個營收月份只提醒一次。"""
    R.LONG_HOLDINGS.clear()
    R.load_holdings()
    ids = list(R.LONG_HOLDINGS)
    if not ids:
        return []
    r = rev[rev["stock_id"].isin(ids)].copy()
    r["yoy"] = np.where(r["rev_ly"] > 0, r["revenue"] / r["rev_ly"] - 1, np.nan)
    with ldb() as c:
        c.execute("CREATE TABLE IF NOT EXISTS lh_alert (stock_id TEXT, month TEXT, date TEXT, PRIMARY KEY(stock_id, month))")
        done = set(map(tuple, c.execute("SELECT stock_id, month FROM lh_alert").fetchall()))
    last = rev["month"].max()
    prev = str(pd.Period(last) - 1)
    out = []
    for sid, g in r.sort_values("month").groupby("stock_id"):
        g = g.set_index("month")
        if last not in g.index or prev not in g.index:
            continue                                                     # 最新兩個月要都有資料
        y1, y2 = g.loc[last].copy(), g.loc[prev].copy()
        y1["month"], y2["month"] = last, prev
        if pd.isna(y1.yoy) or pd.isna(y2.yoy):
            continue
        if y1.yoy < 0 and y2.yoy < 0 and (sid, y1.month) not in done:
            out.append((sid, y1["name"], y2.month, y2.yoy, y1.month, y1.yoy))
    with ldb() as c:
        for a in out:
            c.execute("INSERT OR IGNORE INTO lh_alert VALUES (?,?,?)", (a[0], a[4], today))
    return out


def run(send_mail=True):
    init_ldb()
    with ldb() as c:
        c.execute("CREATE TABLE IF NOT EXISTS picks (date TEXT, stock_id TEXT, name TEXT, close REAL, status TEXT, "
                  "PRIMARY KEY(date, stock_id))")
    try:
        backfill(years=3, budget_min=40, refresh_recent=True)              # 補最新的月營收、財報
    except Exception as e:
        print(f"更新資料失敗（用現有資料選股）：{e}")
    today = R.now_tw().strftime("%Y-%m-%d")
    df, top, last = screen()
    if df.empty:
        print("沒有資料，結束")
        return
    allx = df.set_index("stock_id")
    with ldb() as c:
        held = pd.read_sql("SELECT stock_id, MIN(date) AS since, name, close FROM picks WHERE status='hold' "
                           "GROUP BY stock_id", c, dtype={"stock_id": str})
    sells, keeps = [], []
    for _, h in held.iterrows():
        x = allx.loc[h.stock_id] if h.stock_id in allx.index else None
        months = (pd.Period(today[:7]) - pd.Period(h.since[:7])).n
        if x is None:
            continue
        ret = x.close / h.close - 1
        if x.neg2:
            sells.append((h, x, ret, "營收連 2 個月年減"))
        elif months >= MAX_HOLD_MONTHS:
            sells.append((h, x, ret, f"已經追蹤 {months} 個月，重新評估"))
        else:
            keeps.append((h, x, ret))
    held_ids = set(held.stock_id)
    new = top[~top.stock_id.isin(held_ids)]
    with ldb() as c:
        for h, x, ret, why in sells:
            c.execute("UPDATE picks SET status='sold' WHERE stock_id=? AND status='hold'", (h.stock_id,))
        for _, x in new.iterrows():
            c.execute("INSERT OR REPLACE INTO picks VALUES (?,?,?,?, 'hold')", (today, x.stock_id, x["name"], x.close))
    pct = lambda v: f"{v * 100:+.0f}%" if pd.notna(v) else "—"
    parts = [f"長線成長股｜{today}（月營收到 {last}）",
             "選股：營收創 12 個月新高＋近 3 個月年增 > 20%＋月K站上 10 月線且往上＋EPS 近四季 > 0，挑 ROE 最高的 10 檔。",
             "賣出：營收連 2 個月年減。"]
    parts.append(f"\n【本週名單】{len(top)} 檔（🆕＝新進榜）")
    for _, x in top.iterrows():
        parts.append(f"\n{'🆕 ' if x.stock_id not in held_ids else ''}{x['name']}({x.stock_id})\n"
                     f"目前價格：{R.fmt_now(x.close)}\n"
                     f"營收：近 3 月年增 {pct(x.yoy3)}，{last} 年增 {pct(x.yoy_last)}（創 12 個月新高）\n"
                     f"EPS 近四季：{x.eps:.2f} 元｜ROE：{pct(x.roe)}（{x.fin_q}）")
    if sells:
        parts.append(f"\n\n【🔴 賣出】{len(sells)} 檔")
        for h, x, ret, why in sells:
            parts.append(f"\n{h['name']}({h.stock_id})：{why}\n進榜 {h.since} {R.fmt_now(h.close)} → 現在 {R.fmt_now(x.close)}（{pct(ret)}）")
    try:
        lh = long_hold_alerts(load_all()[0], today)
    except Exception as e:
        print(f"長期持股偵測失敗：{e}")
        lh = []
    if lh:
        parts.append(f"\n\n【⚠️ 長期持股轉弱】{len(lh)} 檔（營收連 2 個月年減，成長可能在轉弱，要不要減碼你自己判斷）")
        for sid, nm, m2, y2, m1, y1 in lh:
            parts.append(f"{nm}({sid})：{m2} 年增 {pct(y2)}、{m1} 年增 {pct(y1)}")
    if keeps:
        parts.append(f"\n\n【🟢 續抱】{len(keeps)} 檔（之前進榜、營收還沒轉弱）")
        for h, x, ret in keeps:
            parts.append(f"{h['name']}({h.stock_id})：進榜 {h.since} {R.fmt_now(h.close)} → {R.fmt_now(x.close)}（{pct(ret)}）")
    parts.append("\n回測（2024/8～2026/3 每月選一次）：平均 6 個月 +31%、中位數 +15%、61% 賺錢；"
                 "抱到營收連 2 月年減才賣，中位數 +25%。只有約 1 年半的資料，僅供參考，不是投資建議。")
    body = "\n".join(parts) + "\n"
    print(body)
    if send_mail:
        R.send_email(f"{'⚠️' if lh else ''}長線成長股 {today[5:7]}/{today[8:]}｜新進榜{len(new)} 續抱{len(keeps)}"
                     + (f" 賣出{len(sells)}" if sells else "") + (f" 長期持股轉弱{len(lh)}" if lh else ""), body)


def main():
    ap = argparse.ArgumentParser(description="長線成長股雷達")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--no-email", action="store_true")
    a = ap.parse_args()
    if a.backfill:
        backfill()
        return
    run(send_mail=not a.no_email)


if __name__ == "__main__":
    main()
