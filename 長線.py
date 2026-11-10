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
import re
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
FIN_COLS = ["quarter", "stock_id", "market", "eps", "net_income", "equity", "op_revenue", "op_income", "ppe"]


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
        for col in ("op_revenue", "op_income", "ppe"):               # 營業收入、營業利益（年初累計）、非流動資產（期末，擴廠的近似指標）
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
    inc, bal, ppe = {}, {}, {}
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
        c_ppe = next((c for c in t.columns if c.startswith("不動產、廠房及設備")), None) or \
            next((c for c in t.columns if c.startswith("非流動資產")), None)   # 彙總表沒有廠房設備，用非流動資產代替
        for _, x in t.iterrows():
            sid = str(x["公司代號"]).strip()
            if R.is_stock_id(sid) and c_eq:
                bal[sid] = _num(x[c_eq])
                if c_ppe:
                    ppe[sid] = _num(x[c_ppe])
    rows = [{"quarter": q, "stock_id": s, "market": "TWSE" if market == "sii" else "TPEx", "eps": v[0],
             "net_income": v[1], "equity": bal.get(s, np.nan), "op_revenue": v[2], "op_income": v[3],
             "ppe": ppe.get(s, np.nan)} for s, v in inc.items()]
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
OM_MIN = 0.10              # 近四季營業利益率 ≥ 10%（10/30 回測：抱到營收轉弱才賣，平均 +58%→+67%、中位數 +18%→+27%，前後兩段都有效）
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
    for col in ("eps", "net_income", "op_revenue", "op_income"):
        if col not in f.columns:
            f[col] = np.nan
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
        rv, oi = float(g.loc[k - 3:k, "op_revenue_q"].sum()), float(g.loc[k - 3:k, "op_income_q"].sum())
        om = oi / rv if rv > 0 and g.loc[k - 3:k, "op_income"].notna().all() else np.nan
        out[sid] = (eps, ni / eq if pd.notna(eq) and eq > 0 else np.nan, g.loc[k, "quarter"], om)
    return out


def complete_months(rev):
    """只用「大部分公司都公布了」的月份：每月 10 號前陸續公布，月初那個月只有幾十家，不能當成最新月份。"""
    n = rev.groupby("month")["stock_id"].nunique()
    ok = n[n >= 0.8 * n.max()].index
    return rev[rev["month"] <= max(ok)] if len(ok) else rev


def screen():
    rev, fin, mon, px = load_all()
    rev = complete_months(rev)
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
        eps, roe, q, om = fx.get(sid, (np.nan, np.nan, "", np.nan))
        yy = YY[sid].dropna()
        rows.append({"stock_id": sid, "name": pnames.get(sid, names.get(sid, sid)), "close": c, "yoy3": yoy3,
                     "yoy_last": float(yy.iloc[-1]) if len(yy) else np.nan, "revhi": bool(revhi), "trend": bool(trend),
                     "eps": eps, "roe": roe, "om": om, "fin_q": q, "rev_month": last,
                     "neg2": bool(len(yy) >= 2 and yy.iloc[-1] < 0 and yy.iloc[-2] < 0)})
    df = pd.DataFrame(rows)
    ok = df[df["revhi"] & df["trend"] & (df["yoy3"] > YOY3_MIN) & (df["eps"] > 0) & (df["om"] >= OM_MIN)]
    return df, ok.sort_values("roe", ascending=False).head(TOP_N), last


def long_hold_alerts(rev, today):
    """長期持股（股票追蹤〔長期持股〕區）：營收連 2 個月年減才提醒，同一檔同一個營收月份只提醒一次。"""
    R.LONG_HOLDINGS.clear()
    R.load_holdings()
    ids = list(R.LONG_HOLDINGS)
    if not ids:
        return []
    rev = complete_months(rev)
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


def etf_line(sid):
    """ETF 的價格：用證交所 STOCK_DAY（個股日成交資訊）抓這個月和上個月，取最新收盤。"""
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
    name = {"006208": "富邦台50", "0050": "元大台灣50"}.get(sid, sid)
    if not rows:
        return f"\n⚪ {name}({sid})：ETF，這週抓不到價格"
    dt, px = rows[-1]
    return f"\n⚪ {name}({sid})：ETF，不看營收，照加碼計畫長期抱著\n價格：{R.fmt_now(px)}（{dt}）"


# 單季營業利益率：只列出來參考，不亮燈（11/5 回測：在過去一年大漲的強勢股，2023～26 掉 8 個百分點以上之後並沒有比較差）


def om_change(fin):
    """每檔最新一季：單季營業利益率、去年同季的、差幾個百分點（營運槓桿：營收小掉，獲利可能大掉）。"""
    f = fin.copy()
    f["y"] = f["quarter"].str[:4].astype(int)
    f["q"] = f["quarter"].str[-1].astype(int)
    f = f.sort_values(["stock_id", "y", "q"])
    for col in ("op_revenue", "op_income"):
        f[col + "_q"] = f[col] - f.groupby(["stock_id", "y"])[col].shift(1).fillna(0)
    f["om"] = np.where(f["op_revenue_q"] > 0, f["op_income_q"] / f["op_revenue_q"], np.nan)
    out = {}
    for sid, g in f.groupby("stock_id"):
        g = g.set_index(["y", "q"])
        y, q = g.index.max()
        if (y - 1, q) in g.index and pd.notna(g.loc[(y, q), "om"]) and pd.notna(g.loc[(y - 1, q), "om"]):
            a, b = float(g.loc[(y, q), "om"]), float(g.loc[(y - 1, q), "om"])
            out[sid] = (f"{y}Q{q}", a, b, a - b)
    return out


def holdings_section(df, last, pct):
    """我的長期持股（股票追蹤〔長期持股〕區）：每週都列。燈號只用 11/5 回測過、兩段時期都有效的三條（每晚也會檢查、觸發馬上寄信）：
    📉 離高點跌 ≥ 20% 又 3 個月比大盤多跌 ≥ 25%；🐢 營收成長大減速（不用等股價）；🔊 近 20 天爆量又比大盤多跌 ≥ 15%。
    💰 股價隱含成長率比實際營收成長高 ≥ 35 個百分點（反推 DCF，11/7 回測）。
    任一條 → 🟡（照三步查原因）；都沒有 → 🟢 續抱。月營收年增、利潤率、10 月線只列出來參考。"""
    R.LONG_HOLDINGS.clear()
    R.load_holdings()
    ids = list(R.LONG_HOLDINGS)
    if not ids:
        return []
    allx = df.set_index("stock_id")
    p2 = lambda v: (">+999%（建案認列這種一次入帳的）" if pd.notna(v) and v > 9.99 else pct(v))
    try:
        px = R.load_table("prices", 400)
        hi = px.groupby("stock_id")["close"].max()
    except Exception:
        hi = pd.Series(dtype=float)
    try:
        with ldb() as c:
            omc = om_change(pd.read_sql("SELECT quarter, stock_id, op_revenue, op_income FROM fin WHERE stock_id IN (%s)"
                                        % ",".join("?" * len(ids)), c, params=ids, dtype={"stock_id": str}))
    except Exception as e:
        print(f"營業利益率變化失敗：{e}")
        omc = {}
    stk = [s for s in ids if not s.startswith("00")]
    try:
        rel = rel_strength(stk)
    except Exception as e:
        print(f"相對大盤失敗：{e}")
        rel = {}
    try:
        vol = vol_spike(stk)
    except Exception as e:
        print(f"爆量檢查失敗：{e}")
        vol = {}
    try:
        with ldb() as c:
            dec = rev_decel(pd.read_sql(f"SELECT month, stock_id, name, revenue, rev_ly FROM revenue WHERE stock_id IN ({','.join('?' * len(stk))})",
                                        c, params=stk, dtype={"stock_id": str}))
    except Exception as e:
        print(f"營收減速失敗：{e}")
        dec = {}
    try:
        val = valuation(stk)
    except Exception as e:
        print(f"估值失敗：{e}")
        val = {}
    rows = []
    for sid in ids:
        if sid.startswith("00"):                             # ETF：沒有營收，只列價格；照加碼計畫抱著
            rows.append((3, etf_line(sid)))
            continue
        if sid not in allx.index:
            rows.append((3, f"\n{sid}：這週沒有資料（可能成交量太小或營收還沒公布）"))
            continue
        x = allx.loc[sid]
        ok = int(bool(x.revhi)) + int(x.yoy3 > YOY3_MIN) + int(bool(x.trend)) + int(x.eps > 0) + int(pd.notna(x.om) and x.om >= OM_MIN)
        flags = []
        m = omc.get(sid)
        om_txt = ""
        if m:
            om_txt = (f"\n單季營業利益率（{m[0]}）：{m[1] * 100:.1f}%，" +
                      (f"去年同季 {m[2] * 100:.1f}%（{m[3] * 100:+.1f} 個百分點）" if m[2] > 0 else "去年同季虧損，不比"))
        rv = rel.get(sid)
        if rv:
            om_txt += f"\n近 3 個月：{rv[1] * 100:+.0f}%，同期大盤 {rv[2] * 100:+.0f}%（{'落後大盤 ' + format(-(rv[1] - rv[2]) * 100, '.0f') + ' 個百分點' if rv[1] < rv[2] else '贏大盤'}）"
            if rv[3] <= REL_DD and rv[1] - rv[2] <= REL_GAP:
                flags.append("📉 跌深而且比大盤多跌很多")
        dv = dec.get(sid)
        if dv:
            om_txt += f"\n營收成長速度：近 3 個月年增 {dv[1] * 100:+.0f}%（到 {dv[0]}），半年前 {dv[2] * 100:+.0f}%"
            if dv[2] - dv[1] >= DECEL and dv[2] <= DECEL_BASE:
                flags.append("🐢 營收成長大減速")
        vv = vol.get(sid)
        if vv:
            om_txt += f"\n近 20 天成交金額：平常的 {vv[1]:.1f} 倍（股價 {vv[2] * 100:+.0f}%，大盤 {vv[3] * 100:+.0f}%）"
            if vv[1] >= VOL_X and vv[2] - vv[3] <= VOL_GAP:
                flags.append("🔊 爆量下跌")
        va = val.get(sid)
        if va:
            om_txt += (f"\n估值（DCF）：約 {va[5]:.0f}，股價是估值的 {va[1] / va[5]:.1f} 倍；"
                       f"股價隱含每年成長 {va[6] * 100:+.0f}%，實際營收成長 {va[4] * 100:+.0f}%")
            if va[6] - va[4] >= VAL_GAP:
                flags.append("💰 估值太貴")
        if flags:
            light, why, o = "🟡", "、".join(flags) + "：照三步 1.查原因 → 2.一次性的就續抱 → 3.找不到好理由就賣一半換富邦台50", 1
        else:
            light, why, o = "🟢", "四條警示都沒有，續抱", 2
        h = hi.get(sid, np.nan)
        rows.append((o, f"\n{light} {x['name']}({sid})：{why}\n"
                        f"價格：{R.fmt_now(x.close)}（離一年高點 {pct(x.close / h - 1) if pd.notna(h) and h > 0 else '—'}；"
                        f"月 K {'在 10 月線上' if x.trend else '跌破 10 月線'}）\n"
                        f"營收：近 3 月年增 {p2(x.yoy3)}，{last} 年增 {p2(x.yoy_last)}\n"
                        f"EPS 近四季：{x.eps:.2f} 元｜ROE：{pct(x.roe)}｜營業利益率：{pct(x.om)}｜長線條件符合 {ok}/5{om_txt}"))
    rows.sort(key=lambda r: r[0])
    n = {k: sum(1 for o, _ in rows if o == k) for k in (0, 1, 2)}
    return [f"\n【我的長期持股】{len(ids)} 檔（🟢{n[2]} 🟡{n[1]}；🟡 = 📉相對大盤／🐢營收減速／🔊爆量下跌／💰估值太貴 任一條）"] + [r for _, r in rows]


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
             "選股：營收創 12 個月新高＋近 3 個月年增 > 20%＋月K站上 10 月線且往上＋EPS 近四季 > 0＋營業利益率 ≥ 10%，挑 ROE 最高的 10 檔。",
             "賣出：營收連 2 個月年減。"]
    try:
        parts += holdings_section(df, last, pct)
    except Exception as e:
        print(f"長期持股區塊失敗：{e}")
    parts.append(f"\n\n【本週名單】{len(top)} 檔（🆕＝新進榜；只當參考名單，不建議照著買）")
    for _, x in top.iterrows():
        parts.append(f"\n{'🆕 ' if x.stock_id not in held_ids else ''}{x['name']}({x.stock_id})\n"
                     f"目前價格：{R.fmt_now(x.close)}\n"
                     f"營收：近 3 月年增 {pct(x.yoy3)}，{last} 年增 {pct(x.yoy_last)}（創 12 個月新高）\n"
                     f"EPS 近四季：{x.eps:.2f} 元｜ROE：{pct(x.roe)}｜營業利益率：{pct(x.om)}（{x.fin_q}）")
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
    parts.append("\n回測（2019/12～2026/10，扣成本）：名單整體年化 +24.7%、最大回撤 −49%；同期 0050 含息年化 +29.1%、最大回撤 −34%。"
                 "名單沒有贏過 0050，只當作「最近哪些公司營收很強」的參考，不是投資建議。")
    body = "\n".join(parts) + "\n"
    print(body)
    if send_mail:
        R.send_email(f"{'⚠️' if lh else ''}長線成長股 {today[5:7]}/{today[8:]}｜新進榜{len(new)} 續抱{len(keeps)}"
                     + (f" 賣出{len(sells)}" if sells else "") + (f" 長期持股轉弱{len(lh)}" if lh else ""), body)


def company_refs(sid, name):
    """通知信附上的參考：公開資訊觀測站法說會連結＋鉅亨網最近和營收、法說有關的新聞標題。"""
    out = [f"   法說會資料：https://mopsov.twse.com.tw/mops/web/t100sb02_1（輸入代號 {sid} 查詢）",
           f"   公司重大訊息：https://mopsov.twse.com.tw/mops/web/t05st01（輸入代號 {sid} 查詢）"]
    try:
        r = R.requests.get("https://ess.api.cnyes.com/ess/api/v1/news/keyword", params={"q": sid, "limit": 30, "page": 1},
                           headers=UA, timeout=15)
        items = r.json().get("data", {}).get("items", [])
        keys = ("法說", "營收", "展望", "訂單", "新產品", "缺貨", "產能", "毛利", "庫存", "出貨", "客戶")
        n = 0
        for it in items:
            t = str(it.get("title", "")).replace("<mark>", "").replace("</mark>", "")
            if not any(k in t for k in keys):
                continue
            ts = datetime.fromtimestamp(int(it.get("publishAt", 0)), R.TZ)
            out.append(f"   ・{ts.month}/{ts.day} {t[:60]}")
            n += 1
            if n >= 5:
                break
    except Exception as e:
        print(f"{sid} 新聞抓不到：{e}")
    return out


DECEL = 0.30               # 近 3 個月營收年增率比半年前少 ≥ 30 個百分點（例：+50% → +15%）
DECEL_BASE = 2.0          # 半年前年增 > 200% 不比（去年基期太小，像建商交屋，比了沒意義；回測排除後兩段一樣成立）
# 11/6 回測（2019～2026，過去一年贏大盤 20% 以上的強勢股，像你的持股）：減速當下股價通常還在贏大盤（+4～7%），
# 之後半年中位數輸大盤 12～14%、約七成跑輸，兩段都成立。不等「股價也輸大盤 15%」：等的話平均已先多輸約 20 個百分點，
# 而且 74% 的減速根本等不到。原本的「月營收年減」在強勢股 2023～26 沒效，只列資訊不寄信。


def rev_decel(rev):
    """每檔：最新 3 個月營收年增率、半年前那 3 個月的，月份要連續才算。"""
    out = {}
    for sid, g in rev.sort_values("month").groupby("stock_id"):
        g = g[(g["revenue"] > 0) & (g["rev_ly"] > 0)].set_index("month")
        if g.empty:
            continue
        m = pd.Period(g.index[-1])
        now3 = [str(m - k) for k in range(3)]
        old3 = [str(m - 6 - k) for k in range(3)]
        if not all(k in g.index for k in now3 + old3):
            continue
        y = lambda ks: g.loc[ks, "revenue"].sum() / g.loc[ks, "rev_ly"].sum() - 1
        out[sid] = (str(m), y(now3), y(old3), g["name"].iloc[-1])
    return out


def hold_watch(send_mail=True):
    """每天晚上：抓最新一個月的營收（每家公司 1～10 號陸續公布）。持股營收成長大減速 → 馬上寄信（不等股價反應）。
    同一檔同一個營收月份只寄一次。"""
    init_ldb()
    R.LONG_HOLDINGS.clear()
    R.load_holdings()
    ids = [s for s in R.LONG_HOLDINGS if not s.startswith("00")]     # ETF 沒有營收
    if not ids:
        print("沒有長期持股")
        return
    now = R.now_tw()
    m = (pd.Period(now.strftime("%Y-%m")) - 1)                      # 這個月公布的是上個月的營收
    for mk in ("sii", "otc"):
        for ky in (False, True):
            try:
                n = lupsert("revenue", fetch_revenue(m.year, m.month, mk, ky=ky), REV_COLS)
                print(f"{m} {mk}{' KY' if ky else ''} 營收：{n} 家")
            except Exception as e:
                print(f"{m} {mk}{' KY' if ky else ''} 營收抓不到（可能還沒有公司公布）：{e}")
            time.sleep(3)
    with ldb() as c:
        c.execute("CREATE TABLE IF NOT EXISTS hold_watch (stock_id TEXT, month TEXT, level TEXT, date TEXT, "
                  "PRIMARY KEY(stock_id, month, level))")
        rev = pd.read_sql(f"SELECT month, stock_id, name, revenue, rev_ly FROM revenue WHERE stock_id IN ({','.join('?' * len(ids))})",
                          c, params=ids, dtype={"stock_id": str})
        done = set(map(tuple, c.execute("SELECT stock_id, month, level FROM hold_watch").fetchall()))
    dec = rev_decel(rev)
    try:
        rs = rel_strength(ids)
    except Exception as e:
        print(f"相對大盤失敗：{e}")
        rs = {}
    pct = lambda v: f"{v * 100:+.0f}%"
    hits = []
    for sid, (mm, y3, y3o, nm) in dec.items():
        rv = rs.get(sid)
        gap = rv[1] - rv[2] if rv else np.nan
        print(f"{nm}({sid})：近 3 月營收年增 {pct(y3)}（到 {mm}），半年前 {pct(y3o)}；近 3 月股價比大盤 {pct(gap) if pd.notna(gap) else '—'}")
        if y3o - y3 >= DECEL and y3o <= DECEL_BASE and (sid, mm, "減速") not in done:
            hits.append((sid, nm, mm, y3, y3o, rv))
    if not hits:
        print("營收減速：沒有持股觸發")
        return
    lines = ["你的長期持股出現「營收成長大減速」：", ""]
    for sid, nm, mm, y3, y3o, rv in hits:
        lines.append(f"🐢 {nm}({sid})：近 3 個月營收年增 {pct(y3)}（到 {mm}），半年前是 {pct(y3o)}，"
                     f"少了 {(y3o - y3) * 100:.0f} 個百分點" +
                     (f"；近 3 個月股價 {pct(rv[1])}，大盤 {pct(rv[2])}" if rv else ""))
        lines.append("成長股最怕的不是營收衰退，而是成長突然變慢：市場接下來常會降評價，股價通常還沒反應。照這三步：")
        lines += STEPS
        lines += company_refs(sid, nm) + [""]
    lines += ["回測（2019～2026，過去一年贏大盤 20% 以上的強勢股）：營收成長大減速時股價通常還在贏大盤，",
              "之後半年中位數輸大盤 12～14%，約七成跑輸；兩段時期都成立。但還是有三成之後贏大盤，所以先查原因再決定。"]
    body = "\n".join(lines)
    print(body)
    if send_mail and R.send_email(f"🐢 持股營收減速提醒：{'、'.join(h[1] for h in hits)}", body):
        with ldb() as c:
            c.executemany("INSERT OR IGNORE INTO hold_watch VALUES (?,?,?,?)",
                          [(h[0], h[2], "減速", now.strftime("%Y-%m-%d")) for h in hits])


STEPS = ["  1. 先查原因：法說會、重大訊息、同業和客戶的營收、新聞（下面有連結）",
         "  2. 找到一次性的好理由（大股東賣股、單一事件、一次性費用、客戶調庫存）→ 續抱",
         "  3. 找不到好理由 → 賣一半換成富邦台50(006208)；猜錯了剩下一半還跟得到，猜對了少虧一半",
         "  （回測：亮燈時不管同業有沒有一起跌，之後一樣容易繼續輸大盤，所以「整個族群都在跌」不算續抱的理由）"]


REL_DD = -0.20             # 離一年高點跌 ≥ 20%
REL_GAP = -0.25            # 而且近 3 個月比大盤多跌 ≥ 25 個百分點 → 可能是公司自己出事（11/4 回測：之後 6 個月中位數輸大盤 16%，
                           # 跟大盤一起跌的只輸 8%；前後兩段都成立。但 28% 照樣贏大盤，只提醒去查原因，不當賣訊）


def rel_strength(ids):
    """每檔：近 3 個月（約 63 個交易日）漲跌、同期大盤、離一年高點。拆股、減資造成的跳動就不算。"""
    with sqlite3.connect(R.DB_PATH) as c:
        px = pd.read_sql(f"SELECT date, stock_id, stock_name, close FROM prices WHERE date >= ? AND stock_id IN ({','.join('?' * len(ids))})",
                         c, params=[(R.now_tw() - timedelta(days=400)).strftime("%Y-%m-%d")] + list(ids), dtype={"stock_id": str})
        tx = pd.read_sql("SELECT date, close FROM taiex ORDER BY date", c).set_index("date")["close"]
    out = {}
    for sid, g in px.sort_values("date").groupby("stock_id"):
        x = g.set_index("date")["close"].astype(float).dropna()
        if len(x) < 70 or ((x / x.shift(1)) < 0.5).any() or ((x / x.shift(1)) > 1.6).any():
            continue
        d0 = x.index[-64]
        t = tx[tx.index <= x.index[-1]]
        t0 = tx[tx.index <= d0]
        if t.empty or t0.empty:
            continue
        r3, m3 = x.iloc[-1] / x.loc[d0] - 1, t.iloc[-1] / t0.iloc[-1] - 1
        out[sid] = (g["stock_name"].iloc[-1], r3, m3, x.iloc[-1] / x.tail(250).max() - 1, x.index[-1])
    return out


def price_watch(send_mail=True):
    """每天晚上：持股股價跌深，而且跌得比大盤多很多 → 可能是公司自己出事，馬上提醒去查原因（同一檔一個月只寄一次）。"""
    R.LONG_HOLDINGS.clear()
    R.load_holdings()
    ids = [s for s in R.LONG_HOLDINGS if not s.startswith("00")]
    if not ids:
        return
    try:
        rs = rel_strength(ids)
    except Exception as e:
        print(f"相對大盤檢查失敗：{e}")
        return
    month = R.now_tw().strftime("%Y-%m")
    with ldb() as c:
        c.execute("CREATE TABLE IF NOT EXISTS hold_watch (stock_id TEXT, month TEXT, level TEXT, date TEXT, "
                  "PRIMARY KEY(stock_id, month, level))")
        done = set(map(tuple, c.execute("SELECT stock_id, month, level FROM hold_watch").fetchall()))
    pct = lambda v: f"{v * 100:+.0f}%"
    hits = [(s, v) for s, v in rs.items() if v[3] <= REL_DD and v[1] - v[2] <= REL_GAP and (s, month, "📉") not in done]
    for s, v in rs.items():
        print(f"{v[0]}({s})：近 3 月 {pct(v[1])}，大盤 {pct(v[2])}，差 {pct(v[1] - v[2])}，離一年高點 {pct(v[3])}")
    if not hits:
        print("相對大盤：沒有持股自己跌特別多")
        return
    lines = ["你的長期持股有股價「自己跌特別多」的情況（不是大盤一起跌）：", ""]
    for s, (nm, r3, m3, dd, d) in hits:
        lines.append(f"📉 {nm}({s})：近 3 個月 {pct(r3)}，同期大盤 {pct(m3)}，多跌 {pct(r3 - m3)}；離一年高點 {pct(dd)}（到 {d}）")
        lines.append("股價常比營收早幾個月反映壞消息。照這三步：")
        lines += STEPS
        lines += company_refs(s, nm) + [""]
    lines += ["回測（2019～2026，營收還在成長的股票）：從高點跌 20% 以上、又比大盤多跌 25% 以上的，之後 6 個月中位數輸大盤 16%，",
              "72% 跑輸；跟大盤一起跌的只輸 8%。但還是有 28% 之後贏大盤，所以這是「去查原因」的提醒，不是賣出訊號。"]
    body = "\n".join(lines)
    print(body)
    if send_mail and R.send_email(f"📉 持股相對大盤提醒：{'、'.join(v[0] for _, v in hits)}", body):
        with ldb() as c:
            c.executemany("INSERT OR IGNORE INTO hold_watch VALUES (?,?,?,?)",
                          [(s, month, "📉", R.now_tw().strftime("%Y-%m-%d")) for s, _ in hits])


VOL_X = 2.0                # 近 20 個交易日成交金額 ≥ 平常的 2 倍
VOL_GAP = -0.15            # 而且近 20 個交易日比大盤多跌 ≥ 15 個百分點
# 11/5 回測（2019～2026 強勢股，每天滾動判斷）：之後半年中位數輸大盤 10～14%，比強勢股平均多輸約 4～5 個百分點，
# 兩段都成立；跟其他兩條幾乎不重疊，能抓到它們漏掉的。


def vol_spike(ids):
    """每檔：近 20 個交易日成交金額 ÷ 之前平常 20 天的成交金額（之前最多 250 天、至少 100 天）、近 20 日漲跌、同期大盤。"""
    with sqlite3.connect(R.DB_PATH) as c:
        px = pd.read_sql(f"SELECT date, stock_id, stock_name, close, turnover FROM prices WHERE date >= ? AND stock_id IN ({','.join('?' * len(ids))})",
                         c, params=[(R.now_tw() - timedelta(days=420)).strftime("%Y-%m-%d")] + list(ids), dtype={"stock_id": str})
        tx = pd.read_sql("SELECT date, close FROM taiex ORDER BY date", c).set_index("date")["close"]
    out = {}
    for sid, g in px.sort_values("date").groupby("stock_id"):
        g = g.set_index("date")
        x, t = g["close"].astype(float), g["turnover"].astype(float)
        if len(g) < 121 or x.tail(21).isna().any() or ((x / x.shift(1)) < 0.5).any() or ((x / x.shift(1)) > 1.6).any():
            continue
        base = t.iloc[:-20].tail(250).mean() * 20
        if not base > 0:
            continue
        d0 = x.index[-21]
        t1, t0 = tx[tx.index <= x.index[-1]], tx[tx.index <= d0]
        if t1.empty or t0.empty:
            continue
        out[sid] = (g["stock_name"].iloc[-1], t.tail(20).sum() / base, x.iloc[-1] / x.loc[d0] - 1,
                    t1.iloc[-1] / t0.iloc[-1] - 1, x.index[-1])
    return out


def volume_watch(send_mail=True):
    """每天晚上：持股爆量下跌（近 20 天成交金額 ≥ 平常 2 倍，又比大盤多跌 ≥ 15%）→ 大量資金在賣，馬上寄信（同一檔一個月只寄一次）。"""
    R.LONG_HOLDINGS.clear()
    R.load_holdings()
    ids = [s for s in R.LONG_HOLDINGS if not s.startswith("00")]
    if not ids:
        return
    month = R.now_tw().strftime("%Y-%m")
    with ldb() as c:
        c.execute("CREATE TABLE IF NOT EXISTS hold_watch (stock_id TEXT, month TEXT, level TEXT, date TEXT, "
                  "PRIMARY KEY(stock_id, month, level))")
        done = set(map(tuple, c.execute("SELECT stock_id, month, level FROM hold_watch").fetchall()))
    vs = vol_spike(ids)
    pct = lambda v: f"{v * 100:+.0f}%"
    for s, v in vs.items():
        print(f"{v[0]}({s})：近 20 日成交金額是平常的 {v[1]:.1f} 倍，近 20 日 {pct(v[2])}，大盤 {pct(v[3])}")
    hits = [(s, v) for s, v in vs.items() if v[1] >= VOL_X and v[2] - v[3] <= VOL_GAP and (s, month, "🔊") not in done]
    if not hits:
        print("爆量下跌：沒有持股觸發")
        return
    lines = ["你的長期持股出現「爆量下跌」：成交量放大、股價又跌得比大盤多，代表有大量資金在賣：", ""]
    for s, (nm, ratio, r20, m20, d) in hits:
        lines.append(f"🔊 {nm}({s})：近 20 個交易日成交金額是平常的 {ratio:.1f} 倍；同期股價 {pct(r20)}，大盤 {pct(m20)}，多跌 {pct(r20 - m20)}（到 {d}）")
        lines.append("可能是法人、大股東在出貨（已經扣掉大盤的跌幅）。照這三步：")
        lines += STEPS
        lines += company_refs(s, nm) + [""]
    lines += ["回測（2019～2026，過去一年贏大盤 20% 以上的強勢股）：爆量又比大盤多跌 15% 以上的，之後半年中位數輸大盤 10～14%，",
              "兩段時期都成立。不是爆量一出現就要賣：照上面三步先查原因。"]
    body = "\n".join(lines)
    print(body)
    if send_mail and R.send_email(f"🔊 持股爆量下跌提醒：{'、'.join(v[0] for _, v in hits)}", body):
        with ldb() as c:
            c.executemany("INSERT OR IGNORE INTO hold_watch VALUES (?,?,?,?)",
                          [(s, month, "🔊", R.now_tw().strftime("%Y-%m-%d")) for s, _ in hits])


DCF_R = 0.10               # 折現率（台股股東要求報酬）
DCF_GT = 0.02              # 永續成長率
VAL_GAP = 0.35             # 股價隱含成長率比實際營收成長率高 ≥ 35 個百分點 → 💰 太貴
# 11/7 回測（2019～2026，過去一年贏大盤 20% 以上的強勢股）：之後半年中位數輸大盤 12～18%、七成跑輸，
# 比其他強勢股多輸 7～8 個百分點，兩段都成立；跟另外三條警示幾乎不重疊（1,900 次只重疊 261 次）。


def dcf_value(e0, g, roe):
    """盈餘版 DCF：前 5 年每年成長 g，第 6～10 年線性降到 2%，之後永續 2%；
    每年能拿出來的現金 = 盈餘 ×（1 − 成長率 ÷ ROE），成長要靠再投資，ROE 越高要留的越少。"""
    roe = min(max(roe, 0.08), 0.40)
    v, e = 0.0, e0
    for t in range(1, 11):
        gt = g if t <= 5 else g + (DCF_GT - g) * (t - 5) / 5
        e *= 1 + gt
        v += e * (1 - min(max(gt / roe, 0), 0.9)) / (1 + DCF_R) ** t
    return v + e * (1 + DCF_GT) * (1 - DCF_GT / roe) / (DCF_R - DCF_GT) / (1 + DCF_R) ** 10


def implied_growth(price, e0, roe):
    """反推 DCF：現在股價等於「市場相信未來 5 年每年成長多少」。"""
    lo, hi = -0.3, 1.5
    if dcf_value(e0, hi, roe) < price:
        return hi
    if dcf_value(e0, lo, roe) > price:
        return lo
    for _ in range(50):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if dcf_value(e0, m, roe) < price else (lo, m)
    return (lo + hi) / 2


def ytd_eps(fin, sid):
    """今年已公布幾季的 EPS（年初累計）年化，和去年同期比的成長。回 (說明, 年化 EPS, 成長率或 None)；今年還沒有財報回 None。"""
    f = fin[fin["stock_id"] == sid].dropna(subset=["eps"]).sort_values("quarter")
    if f.empty:
        return None
    q = f["quarter"].iloc[-1]
    y, n = int(q[:4]), int(q[-1])
    if y < R.now_tw().year:
        return None
    e = float(f["eps"].iloc[-1])
    ly = f[f["quarter"] == f"{y - 1}Q{n}"]["eps"]
    ge = e / float(ly.iloc[0]) - 1 if len(ly) and float(ly.iloc[0]) > 0 and e > 0 else None
    return (f"今年前 {n} 季 EPS {e:.2f} 元年化成 {e * 4 / n:.2f} 元", e * 4 / n, ge)


def valuation(ids):
    """每檔：(名稱, 股價, 近四季 EPS, ROE, 估計成長率, DCF 估值, 隱含成長率)。估計成長率 = 近 1 年和近 3 年營收（近 12 個月合計）年成長平均，限制在 −10%～40%。"""
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    with ldb() as c:
        fin = pd.read_sql(f"SELECT * FROM fin WHERE stock_id IN ({q})", c, params=ids, dtype={"stock_id": str})
        tf = ttm_fin(fin)
        rv = pd.read_sql(f"SELECT month, stock_id, name, revenue FROM revenue WHERE stock_id IN ({q})", c, params=ids, dtype={"stock_id": str})
    with sqlite3.connect(R.DB_PATH) as c:
        px = pd.read_sql(f"SELECT date, stock_id, close FROM prices WHERE date >= ? AND stock_id IN ({q})", c,
                         params=[(R.now_tw() - timedelta(days=15)).strftime("%Y-%m-%d")] + list(ids), dtype={"stock_id": str})
    last = px.sort_values("date").groupby("stock_id")["close"].last()
    names = rv.sort_values("month").groupby("stock_id")["name"].last()
    ttm = rv.pivot_table(index="month", columns="stock_id", values="revenue").sort_index().rolling(12, min_periods=12).sum()
    out = {}
    for sid in ids:
        if sid not in tf or sid not in last.index or sid not in ttm.columns:
            continue
        eps, roe = tf[sid][0], tf[sid][1]
        t = ttm[sid].dropna()
        if not (eps > 0) or pd.isna(roe) or len(t) < 13:
            continue
        gs = [t.iloc[-1] / t.iloc[-13] - 1] + ([(t.iloc[-1] / t.iloc[-37]) ** (1 / 3) - 1] if len(t) >= 37 and t.iloc[-37] > 0 else [])
        g = min(max(float(np.mean(gs)), -0.10), 0.40)
        p = float(last[sid])
        out[sid] = (names.get(sid, sid), p, eps, roe, g, dcf_value(eps, g, roe), implied_growth(p, eps, roe), ytd_eps(fin, sid))
    return out


CNYES = "https://marketinfo.api.cnyes.com/mi/api/v1/financialIndicator/"
SCEN = {"悲觀": ("feLow", 0.05), "中等": ("feMedian", 0.15), "樂觀": ("feHigh", 0.25)}   # 用哪個法人 EPS、法人預估年度之後每年成長多少（再慢慢降到 2%）
SCEN_NOEST = {"悲觀": 0.5, "中等": 1.0, "樂觀": 1.5}                                   # 沒有法人預估：用過去實際成長 × 這個倍數（衰退中的公司倍數反過來）
EST_MAX_AGE = 180                                                                     # 法人預估超過半年沒更新就不用


def fetch_est(sid):
    """鉅亨網的 FactSet 綜合預估（外資＋本土券商一起算）：目標價、各年度 EPS（最高、最低、中位數、幾家）。抓不到回 None。"""
    h = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.cnyes.com/"}
    out = {}
    try:
        old = (R.now_tw() - timedelta(days=EST_MAX_AGE)).strftime("%Y-%m-%d")    # 太久沒更新的預估不用
        tp = requests.get(f"{CNYES}targetPrice/TWS:{sid}:STOCK", headers=h, timeout=20).json().get("data") or {}
        if tp.get("numEst") and str(tp.get("rateDate", "")) >= old:
            out["tp"] = tp
        ep = requests.get(f"{CNYES}estimateProfit/TWS:{sid}:STOCK", params={"type": "eps"}, headers=h, timeout=20).json().get("data") or []
        this_year = R.now_tw().year
        out["eps"] = {int(x["financialYear"]): x for x in ep
                      if x.get("numEst") and int(x["financialYear"]) > this_year - 1 and x.get("feMedian")
                      and str(x.get("rateDate", "")) >= old}
    except Exception as e:
        print(f"{sid} 法人預估抓不到：{e}")
        return None
    return out if (out.get("tp") or out.get("eps")) else None


def save_est(sid, est):
    """每週存一份，累積幾年後就能回測「法人預估準不準」。"""
    d = R.now_tw().strftime("%Y-%m-%d")
    rows = [(d, sid, "tp", None, est["tp"].get("feLow"), est["tp"].get("feMedian"), est["tp"].get("feHigh"), est["tp"].get("numEst"))] if est.get("tp") else []
    rows += [(d, sid, "eps", y, x.get("feLow"), x.get("feMedian"), x.get("feHigh"), x.get("numEst")) for y, x in est.get("eps", {}).items()]
    with ldb() as c:
        c.execute("CREATE TABLE IF NOT EXISTS analyst_est (date TEXT, stock_id TEXT, kind TEXT, year INTEGER, low REAL, median REAL, high REAL, n INTEGER, "
                  "PRIMARY KEY(date, stock_id, kind, year))")
        c.executemany("INSERT OR REPLACE INTO analyst_est VALUES (?,?,?,?,?,?,?,?)", rows)


def dcf_path(e0, eps_years, g_after, roe):
    """DCF：前面幾年用給定的 EPS，之後每年成長 g_after 到第 5 年，第 6～10 年線性降到 2%，再永續 2%（沒給 EPS 時跟 dcf_value 一樣）。現金 = 盈餘 ×（1 − 成長 ÷ ROE）。"""
    roe = min(max(roe, 0.08), 0.40)
    v, prev, e, n = 0.0, e0, e0, len(eps_years)
    for t in range(1, 11):
        if t <= n:
            e = eps_years[t - 1]
            gt = e / prev - 1 if prev > 0 else g_after
        else:
            gt = g_after if t <= 5 else g_after + (DCF_GT - g_after) * (t - max(5, n)) / (10 - max(5, n))
            e *= 1 + gt
        prev = e
        v += e * (1 - min(max(min(gt, 0.40) / roe, 0), 0.9)) / (1 + DCF_R) ** t   # 一年暴增（漲價、產能利用率）不需要等比例擴廠，再投資按最多 40% 成長算
    return v + e * (1 + DCF_GT) * (1 - DCF_GT / roe) / (DCF_R - DCF_GT) / (1 + DCF_R) ** 10


def scenarios(va, est):
    """三情境估值。有法人 EPS 預估：悲觀用最低、中等用中位數、樂觀用最高，預估年度之後每年成長 5%／15%／25%；
    沒有法人預估：用過去實際營收成長 × 0.5／1／1.5（上限 40%）。回 ({情境: 估值}, 說明, 法人預估年成長或 None)。"""
    nm, p, eps, roe, g, v, gi = va[:7]
    ys = sorted((est or {}).get("eps", {}))
    ys = [y for y in ys if y > R.now_tw().year]                       # 今年快結束了，從明年開始算
    if ys:
        out = {k: dcf_path(eps, [est["eps"][y][col] for y in ys], ga, roe) for k, (col, ga) in SCEN.items()}
        n = max(est["eps"][y]["numEst"] for y in ys)
        last = est["eps"][ys[-1]]["feMedian"]
        g_an = (last / eps) ** (1 / (ys[-1] - R.now_tw().year)) - 1 if eps > 0 and last > 0 else None
        return out, f"法人預估（FactSet 綜合 {n} 家）{ys[0]}～{ys[-1]} 年 EPS", g_an
    yt = va[7] if len(va) > 7 else None
    if yt:                                                             # 今年已公布的幾季 EPS 年化，當今年的 EPS
        lab, e1, ge = yt
        gg = min(max(ge if ge is not None else g, -0.10), 0.40)        # 先限制在 −10%～40%，再乘情境倍數，三個情境才分得開
        out = {k: dcf_path(eps, [e1], gg * (m if gg >= 0 else 2 - m), roe) for k, m in SCEN_NOEST.items()}
        why = f"EPS 年增 {ge * 100:+.0f}%" if ge is not None else f"去年同期虧損，改用營收成長 {g * 100:+.0f}%"
        return out, f"沒有法人預估：用{lab}（{why}）×0.5／1／1.5", None
    out = {k: dcf_value(eps, min(max(g * (m if g >= 0 else 2 - m), -0.10), 0.40), roe) for k, m in SCEN_NOEST.items()}
    return out, "沒有法人預估，用過去實際營收成長 ×0.5／1／1.5", None


def est_collect(send_mail=True):
    """週一到週五每晚：抓持股的法人預估（FactSet 綜合）存起來，週六的信裡比較這週有沒有調升、調降。不寄信。"""
    if R.now_tw().weekday() >= 5:
        print("週末不抓法人預估")
        return
    R.LONG_HOLDINGS.clear()
    R.load_holdings()
    for sid in [s for s in R.LONG_HOLDINGS if not s.startswith("00")]:
        est = fetch_est(sid)
        if est:
            save_est(sid, est)
            t = est.get("tp") or {}
            print(f"{sid} 法人預估：目標價中位數 {t.get('feMedian', '—')}（{t.get('numEst', 0)} 家），EPS 年度 {sorted(est.get('eps', {}))}")
        else:
            print(f"{sid} 沒有法人預估")
        time.sleep(1)


def est_week(sid):
    """這一週（7 天內最早一筆 vs 最新一筆）目標價中位數、明年 EPS 中位數的變化。"""
    try:
        with ldb() as c:
            d = pd.read_sql("SELECT date, kind, year, median, n FROM analyst_est WHERE stock_id = ? AND date >= ? ORDER BY date",
                            c, params=(sid, (R.now_tw() - timedelta(days=7)).strftime("%Y-%m-%d")))
    except Exception:
        return ""
    out = []
    tp = d[d.kind == "tp"]
    if len(tp) >= 2 and tp["median"].iloc[0] > 0 and tp["median"].iloc[-1] != tp["median"].iloc[0]:
        a, b = tp["median"].iloc[0], tp["median"].iloc[-1]
        out.append(f"目標價中位數 {a:,.0f} → {b:,.0f}（{(b / a - 1) * 100:+.1f}%）")
    ny = R.now_tw().year + 1
    ep = d[(d.kind == "eps") & (d.year == ny)]
    if len(ep) >= 2 and ep["median"].iloc[0] > 0 and ep["median"].iloc[-1] != ep["median"].iloc[0]:
        a, b = ep["median"].iloc[0], ep["median"].iloc[-1]
        out.append(f"{ny} 年 EPS 中位數 {a:.2f} → {b:.2f}（{(b / a - 1) * 100:+.1f}%）")
    return ("本週法人：" + "；".join(out)) if out else ("本週法人：沒有調整" if len(tp) or len(ep) else "")


def analyst_news(sid, name, days=7):
    """鉅亨網這幾天跟目標價、評等有關的新聞標題（升評、降評的原因）。回 [(日期, 標題, 連結)]。"""
    keys = ("目標價", "評等", "升評", "降評", "調升", "調降", "上修", "下修", "買進", "中立", "加碼", "減碼", "Factset", "外資", "投顧")
    since = R.now_tw().timestamp() - days * 86400
    out, seen = [], set()
    for q in (f"{name} 目標價", name):
        try:
            items = R.requests.get("https://ess.api.cnyes.com/ess/api/v1/news/keyword", params={"q": q, "limit": 30, "page": 1},
                                   headers=UA, timeout=15).json().get("data", {}).get("items", [])
        except Exception as e:
            print(f"{sid} 新聞抓不到：{e}")
            continue
        for it in items:
            t = str(it.get("title", "")).replace("<mark>", "").replace("</mark>", "")
            if it.get("newsId") in seen or (it.get("publishAt") or 0) < since or name not in t or not any(k in t for k in keys):
                continue
            seen.add(it.get("newsId"))
            d = pd.Timestamp(it["publishAt"], unit="s", tz="Asia/Taipei").strftime("%m/%d")
            out.append((d, t, f"https://news.cnyes.com/news/id/{it['newsId']}"))
        time.sleep(1)
    return sorted(out, reverse=True)[:6]


def valuation_report(send_mail=True):
    """每週六下午：持股估值週報。每檔：股價、DCF 估值、三情境、法人目標價（家數、調升／調降）、這週法人有沒有改預估、相關新聞（原因）。"""
    init_ldb()
    R.LONG_HOLDINGS.clear()
    R.load_holdings()
    ids = [s for s in R.LONG_HOLDINGS if not s.startswith("00")]
    va_all = valuation(ids)
    pct = lambda v: f"{v * 100:+.0f}%"
    today = R.now_tw().strftime("%Y-%m-%d")
    parts = [f"持股估值週報｜{today}",
             "估值是用假設算的（成長率、折現率 10%），只當參考；寄信的 💰 估值太貴警示才是回測過的。",
             "法人預估是 FactSet 綜合（外資＋本土券商一起算）；沒有法人預估的用今年已公布幾季的 EPS 年化。"]
    for sid in ids:
        va = va_all.get(sid)
        nm = va[0] if va else sid
        if not va:
            parts.append(f"\n【{nm}({sid})】近四季 EPS ≤ 0 或資料不夠，沒辦法估值")
            continue
        est = fetch_est(sid)
        if est:
            try:
                save_est(sid, est)
            except Exception as e:
                print(f"{sid} 法人預估存不進去：{e}")
        sc, how, g_an = scenarios(va, est)
        p = va[1]
        lines = [f"\n【{nm}({sid})】股價 {p:,.1f}" + ("　💰 估值太貴（隱含成長比實際高 ≥35 點）" if va[6] - va[4] >= VAL_GAP else ""),
                 "預估價格（三情境）：" + "｜".join(f"{k} {v:,.0f}（{v / p - 1:+.0%}）" for k, v in sc.items()),
                 f"　依據：{how}",
                 f"DCF 估值（用過去實際成長 {pct(va[4])}）：約 {va[5]:,.0f}；股價隱含每年成長 {pct(va[6])}"]
        if est and est.get("tp"):
            t = est["tp"]
            ud = f"，最近調升 {t.get('feUp', 0)} 家、調降 {t.get('feDown', 0)} 家" if t.get("feUp") is not None else ""
            lines.append(f"法人目標價：中位數 {t['feMedian']:,.0f}（{t['feMedian'] / p - 1:+.0%}；最低 {t['feLow']:,.0f}～最高 {t['feHigh']:,.0f}，"
                         f"{t['numEst']} 家{ud}，更新到 {t.get('rateDate', '')}）")
        else:
            lines.append("法人目標價：沒有（或超過半年沒更新）")
        if est and est.get("eps"):
            lines.append("法人 EPS 預估（中位數）：" + "、".join(f"{y} 年 {x['feMedian']:.2f}（{x['numEst']} 家）" for y, x in sorted(est["eps"].items())))
        wk = est_week(sid)
        lines.append(wk if wk else "本週法人：還沒有一週的紀錄可以比（週一到週五每晚會抓）")
        if g_an is not None:
            lines.append(f"課程判斷：股價隱含每年成長 {pct(va[6])}，法人預估每年成長 {pct(g_an)} → "
                         + ("法人預估比較高，潛在低估" if g_an > va[6] else "股價隱含比較高，潛在高估") + "（參考，未回測）")
        news = analyst_news(sid, nm)
        if news:
            lines.append("這週的相關新聞（升評、降評的原因看這裡）：")
            lines += [f"　{d} {t}\n　{u}" for d, t, u in news]
        else:
            lines.append("這週沒有跟目標價、評等有關的新聞")
        parts += lines
    parts.append("\n（只是參考，不是投資建議。）")
    body = "\n".join(parts)
    print(body)
    if send_mail:
        R.send_email(f"📊 持股估值週報｜{today}", body)


IR_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t100sb07_1"


def ir_latest(sid):
    """公開資訊觀測站：這家公司最近一場法說會。回 dict（日期、時間、地點、摘要、簡報、影音）或 None。"""
    d = {"encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1, "queryName": "co_id", "inpuType": "co_id", "TYPEK": "all", "co_id": sid}
    try:
        t = R.requests.post(IR_URL, data=d, headers=UA, timeout=30).text
    except Exception as e:
        print(f"{sid} 法說會抓不到：{e}")
        return None
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " | ", t))
    m = re.search(r"召開法人說明會日期：[ |]*(\d{3}/\d{2}/\d{2})[ |]*時間：[ |]*(\d+)[ |]*點[ |]*(\d+)", txt)
    if not m:
        return None
    y, mo, dd = m.group(1).split("/")
    date = f"{int(y) + 1911}-{mo}-{dd}"
    pick = lambda pat: (re.search(pat, txt).group(1).strip(" |") if re.search(pat, txt) else "")
    links = re.findall(r'(?:href|src)=["\']([^"\']+)', t)
    pdf = [("https://mopsov.twse.com.tw" + x) for x in links if x.endswith("M001.pdf")]
    video = [x for x in links if "irconference" in x or "youtu" in x]
    return {"date": date, "time": f"{int(m.group(2)):02d}:{int(m.group(3)):02d}",
            "place": pick(r"召開法人說明會地點：[ |]*([^|]+)"), "summary": pick(r"法人說明會擇要訊息：[ |]*([^|]+)"),
            "pdf": pdf[0] if pdf else "", "video": video[0] if video else ""}


def ir_watch(send_mail=True):
    """每天晚上：持股有新的法說會公告、或法說會影音上傳了 → 當晚寄信（附簡報、影音連結，可以丟給 Claude 分析）。"""
    R.LONG_HOLDINGS.clear()
    R.load_holdings()
    ids = [s for s in R.LONG_HOLDINGS if not s.startswith("00")]
    with ldb() as c:
        c.execute("CREATE TABLE IF NOT EXISTS ir_seen (stock_id TEXT PRIMARY KEY, date TEXT, video TEXT)")
        seen = {r[0]: (r[1], r[2]) for r in c.execute("SELECT stock_id, date, video FROM ir_seen")}
        names = dict(c.execute("SELECT stock_id, name FROM revenue GROUP BY stock_id HAVING month = MAX(month)").fetchall())
    news, upd = [], []
    for sid in ids:
        ir = ir_latest(sid)
        time.sleep(2)
        if not ir:
            continue
        nm = names.get(sid, sid)
        old = seen.get(sid)
        print(f"{nm}({sid}) 最近法說會：{ir['date']} {ir['time']} {ir['place']}；影音 {'有' if ir['video'] else '沒有'}")
        if old is None:                                   # 第一次記錄：只存起來，不寄（避免一次寄一堆舊的）
            upd.append((sid, ir["date"], ir["video"]))
            continue
        if ir["date"] != old[0]:
            news.append(("新的法說會", nm, sid, ir))
        elif ir["video"] and ir["video"] != old[1]:
            news.append(("法說會影音上傳了", nm, sid, ir))
        upd.append((sid, ir["date"], ir["video"]))
    with ldb() as c:
        c.executemany("INSERT OR REPLACE INTO ir_seen VALUES (?,?,?)", upd)
    try:                                                      # 排進收集佇列：ir.yml 每晚抓簡報、轉逐字稿，Claude 每晚分析
        import 法說
        for kind, nm, sid, ir in news:
            法說.enqueue({"sid": sid, "name": nm, "date": ir["date"], "kind": "法說", "pdf": ir["pdf"], "video": ir["video"],
                        "summary": ir["summary"], "place": ir["place"]})
    except Exception as e:
        print(f"法說會排進佇列失敗：{e}")
    if not news:
        print("法說會：沒有新的")
        return
    lines = []
    for kind, nm, sid, ir in news:
        lines += [f"📅 {nm}({sid})：{kind}",
                  f"   日期：{ir['date']} {ir['time']}　地點：{ir['place']}",
                  f"   摘要：{ir['summary']}"]
        if ir["pdf"]:
            lines.append(f"   簡報：{ir['pdf']}")
        if ir["video"]:
            lines.append(f"   影音：{ir['video']}")
        lines.append("")
    lines += ["系統會自動抓簡報、把影音轉成逐字稿，Claude 會分析「公司說的 vs 財報實際」有沒有出入，分析好另外寄信給你。"]
    body = "\n".join(lines)
    print(body)
    if send_mail:
        R.send_email(f"📅 持股法說會：{'、'.join(f'{x[1]}（{x[0]}）' for x in news)}", body)


def value_watch(send_mail=True):
    """每天晚上：持股股價隱含的成長率，比公司實際營收成長高太多（≥ 35 個百分點）→ 太貴，寄信（同一檔一個月一次）。"""
    R.LONG_HOLDINGS.clear()
    R.load_holdings()
    ids = [s for s in R.LONG_HOLDINGS if not s.startswith("00")]
    month = R.now_tw().strftime("%Y-%m")
    with ldb() as c:
        c.execute("CREATE TABLE IF NOT EXISTS hold_watch (stock_id TEXT, month TEXT, level TEXT, date TEXT, "
                  "PRIMARY KEY(stock_id, month, level))")
        done = set(map(tuple, c.execute("SELECT stock_id, month, level FROM hold_watch").fetchall()))
    va = valuation(ids)
    pct = lambda v: f"{v * 100:+.0f}%"
    for s, v in va.items():
        print(f"{v[0]}({s})：股價 {v[1]:.1f}，DCF 估值 {v[5]:.1f}（股價是估值的 {v[1] / v[5]:.2f} 倍）；"
              f"股價隱含每年成長 {pct(v[6])}，實際營收成長 {pct(v[4])}")
    hits = [(s, v) for s, v in va.items() if v[6] - v[4] >= VAL_GAP and (s, month, "💰") not in done]
    if not hits:
        print("估值：沒有持股太貴")
        return
    lines = ["你的長期持股股價太貴：市場預期的成長，遠高於公司實際的成長：", ""]
    for s, (nm, p, eps, roe, g, v, gi, *_) in hits:
        lines.append(f"💰 {nm}({s})：股價 {p:.1f}，DCF 估值約 {v:.0f}（股價是估值的 {p / v:.1f} 倍）")
        lines.append(f"   現在的股價等於相信「未來 5 年每年成長 {gi * 100:.0f}%」，但近 1～3 年營收實際每年只成長 {g * 100:.0f}%；"
                     f"（近四季 EPS {eps:.2f}、ROE {roe * 100:.0f}%、折現率 10%）")
        lines.append("照這三步（第 1 步改成查「成長撐不撐得住」）：")
        lines += ["  1. 查有沒有理由相信未來成長會比過去快很多：新產品、新客戶、在手訂單、法說會展望",
                  "  2. 有具體的理由（訂單、擴產、客戶已確認）→ 續抱，之後盯營收有沒有跟上",
                  "  3. 只是題材、說不出數字 → 賣一半換成富邦台50(006208)；猜錯了剩下一半還跟得到，猜對了少虧一半"]
        lines += company_refs(s, nm) + [""]
    lines += ["回測（2019～2026，過去一年贏大盤 20% 以上的強勢股）：股價隱含成長比實際營收成長高 35 個百分點以上的，",
              "之後半年中位數輸大盤 12～18%、七成跑輸，兩段時期都成立。估值只是假設（未來成長、折現率），太貴的股票也可能續漲一陣子，所以先查再決定。"]
    body = "\n".join(lines)
    print(body)
    if send_mail and R.send_email(f"💰 持股估值太貴提醒：{'、'.join(v[0] for _, v in hits)}", body):
        with ldb() as c:
            c.executemany("INSERT OR IGNORE INTO hold_watch VALUES (?,?,?,?)",
                          [(s, month, "💰", R.now_tw().strftime("%Y-%m-%d")) for s, _ in hits])


def refresh_today():
    """晚上檢查前先把「今天」的收盤價、成交金額、加權指數收進來（不然要等隔天早上 7:10 才有，警示會晚一天）。"""
    R.init_db()
    q = R.collect_quotes(R.load_table("prices", 120))
    if not q.empty:
        R.upsert("prices", q, R.QUOTE_COLS)
        print(f"今天行情：{len(q)} 檔（{q['date'].max()}）")
    import importlib.util as iu
    sp = iu.spec_from_file_location("radar_dip", os.path.join(os.path.dirname(os.path.abspath(__file__)), "加碼.py"))
    d = iu.module_from_spec(sp)
    sp.loader.exec_module(d)
    d.update_taiex(months=1)
    return d


def main():
    ap = argparse.ArgumentParser(description="長線成長股雷達")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--years", type=int, default=3, help="回補幾年（研究用可以填 7）")
    ap.add_argument("--valuation", action="store_true", help="每週六下午：持股估值週報")
    ap.add_argument("--hold-watch", action="store_true", help="每天晚上：持股營收減速、相對大盤、爆量下跌、估值太貴檢查")
    a = ap.parse_args()
    if a.valuation:
        valuation_report(send_mail=not a.no_email)
        return
    if a.hold_watch:
        dip = None
        try:
            dip = refresh_today()
        except Exception as e:
            print(f"今天行情收不到（用資料庫裡最新的）：{e}")
        if dip is not None:
            try:                                                   # 大跌加碼器：當天晚上就用收盤價確認（盤中寄過的不重複寄）
                dip.check(send_mail=not a.no_email)
            except Exception as e:
                print(f"大跌加碼器失敗：{e}")
        for f in (hold_watch, price_watch, volume_watch, value_watch, est_collect, ir_watch):        # 四條都每晚檢查，觸發就馬上寄信
            try:
                f(send_mail=not a.no_email)
            except Exception as e:
                print(f"{f.__name__} 失敗：{e}")
        return
    if a.backfill:
        backfill(years=max(a.years, 3), budget_min=270 if a.years > 3 else 70)
        return
    run(send_mail=not a.no_email)


if __name__ == "__main__":
    main()
