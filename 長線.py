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


OM_DROP = 0.08            # 單季營業利益率比去年同季掉 ≥ 8 個百分點 → 🟡（11/4 回測：之後 6 個月比對照組差約 10%、前後兩段都成立；但 4 成照樣贏大盤，只提醒去看原因，不當賣訊）


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
    """我的長期持股（股票追蹤〔長期持股〕區）：每週都列，燈號看公司生意，不看短期股價。
    🔴 營收連 2 個月年減（生意在變差）；🟡 最新一個月營收年減，或單季營業利益率比去年同季掉 ≥ 8 個百分點，
    或股價離高點跌 ≥ 20% 又比大盤多跌 ≥ 25%（留意、去查原因）；🟢 營收還在成長。
    燈號只看營收：回測「跌破 10 月線就賣」反而比較差，股價趨勢只列出來參考。"""
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
    try:
        rel = rel_strength([s for s in ids if not s.startswith("00")])
    except Exception as e:
        print(f"相對大盤失敗：{e}")
        rel = {}
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
        if x.neg2:
            light, why, o = "🔴", "營收連 2 個月年減，生意在變差，要不要減碼你自己判斷", 0
        elif pd.notna(x.yoy_last) and x.yoy_last < 0:
            light, why, o = "🟡", f"{last} 營收年減，再一個月也年減就會變 🔴，留意", 1
        else:
            light, why, o = "🟢", "營收還在成長，續抱", 2
        m = omc.get(sid)
        om_txt = ""
        if m:
            om_txt = (f"\n單季營業利益率（{m[0]}）：{m[1] * 100:.1f}%，" +
                      (f"去年同季 {m[2] * 100:.1f}%（{m[3] * 100:+.1f} 個百分點）" if m[2] > 0 else "去年同季虧損，不比"))
            if m[2] > 0 and m[3] <= -OM_DROP:
                om_txt += "\n⚠️ 利潤率大掉：獲利可能被成本、降價或擴廠折舊吃掉，去看財報或法說會找原因（一次性的就沒事）"
                if o == 2:
                    light, why, o = "🟡", "營收還在成長，但利潤率比去年同季大掉，留意", 1
        rv = rel.get(sid)
        if rv:
            om_txt += f"\n近 3 個月：{rv[1] * 100:+.0f}%，同期大盤 {rv[2] * 100:+.0f}%（{'落後大盤 ' + format(-(rv[1] - rv[2]) * 100, '.0f') + ' 個百分點' if rv[1] < rv[2] else '贏大盤'}）"
            if rv[3] <= REL_DD and rv[1] - rv[2] <= REL_GAP:
                om_txt += ("\n📉 跌深而且比大盤多跌很多：可能是公司自己出事。1.先查原因（法說會、重大訊息、同業營收）"
                           "→ 2.一次性的就續抱 → 3.找不到好理由就賣一半換富邦台50")
                if o == 2:
                    light, why, o = "🟡", "營收還在成長，但股價自己跌特別多，去查原因", 1
        h = hi.get(sid, np.nan)
        rows.append((o, f"\n{light} {x['name']}({sid})：{why}\n"
                        f"價格：{R.fmt_now(x.close)}（離一年高點 {pct(x.close / h - 1) if pd.notna(h) and h > 0 else '—'}；"
                        f"月 K {'在 10 月線上' if x.trend else '跌破 10 月線'}）\n"
                        f"營收：近 3 月年增 {p2(x.yoy3)}，{last} 年增 {p2(x.yoy_last)}\n"
                        f"EPS 近四季：{x.eps:.2f} 元｜ROE：{pct(x.roe)}｜營業利益率：{pct(x.om)}｜長線條件符合 {ok}/5{om_txt}"))
    rows.sort(key=lambda r: r[0])
    n = {k: sum(1 for o, _ in rows if o == k) for k in (0, 1, 2)}
    return [f"\n【我的長期持股】{len(ids)} 檔（🟢{n[2]} 🟡{n[1]} 🔴{n[0]}；看公司生意，不看短期股價）"] + [r for _, r in rows]


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


def hold_watch(send_mail=True):
    """每天晚上：抓最新一個月的營收（每家公司 1～10 號陸續公布），我的長期持股一公布就判斷，有問題馬上寄信。
    🟡 最新一個月營收年減（提醒留意）；🔴 連 2 個月年減（生意在變差）。同一檔同一個月同一個燈號只寄一次。"""
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
    rev["yoy"] = np.where(rev["rev_ly"] > 0, rev["revenue"] / rev["rev_ly"] - 1, np.nan)
    pct = lambda v: f"{v * 100:+.0f}%" if pd.notna(v) else "—"
    alerts = []
    for sid, g in rev.sort_values("month").groupby("stock_id"):
        g = g.dropna(subset=["yoy"])
        if g.empty or g.iloc[-1]["month"] != str(m):
            continue                                                # 這個月還沒公布
        y1 = g.iloc[-1]
        y2 = g.iloc[-2] if len(g) >= 2 and g.iloc[-2]["month"] == str(m - 1) else None
        if y1.yoy >= 0:
            continue
        level = "🔴" if y2 is not None and y2.yoy < 0 else "🟡"
        if (sid, str(m), level) in done:
            continue
        alerts.append((sid, y1["name"], level, y1, y2))
    if not alerts:
        print(f"持股營收：{m} 沒有新的問題")
        return
    lines = [f"你的長期持股有 {len(alerts)} 檔公布了 {m} 營收，出現年減：", ""]
    for sid, nm, level, y1, y2 in sorted(alerts, key=lambda a: a[2] != "🔴"):
        if level == "🔴":
            lines.append(f"🔴 {nm}({sid})：營收連 2 個月年減（{y2['month']} {pct(y2.yoy)}、{y1['month']} {pct(y1.yoy)}），"
                         "公司生意可能在變差，要不要減碼你自己判斷")
        else:
            lines.append(f"🟡 {nm}({sid})：{y1['month']} 營收年減 {pct(y1.yoy)}"
                         + (f"（上個月還是 {pct(y2.yoy)}）" if y2 is not None else "") + "，先留意，下個月也年減就會變 🔴")
        lines += company_refs(sid, nm) + [""]
    lines += ["2019～2026 年，成長股第一次連 2 月年減，56% 在 3 個月內轉回成長（換產品、客戶調庫存、基期高）；",
              "44% 沒轉回，之後 6 個月平均輸大盤 7%。所以先看法說會和新聞怎麼解釋，再決定。",
              "（營收是一個月公布一次的，股價短期漲跌不會觸發這個通知。只是提醒，不是投資建議。）"]
    body = "\n".join(lines)
    print(body)
    if send_mail and R.send_email(f"{'🔴' if any(a[2] == '🔴' for a in alerts) else '🟡'} 持股營收提醒：{'、'.join(a[1] for a in alerts)}", body):
        with ldb() as c:
            c.executemany("INSERT OR IGNORE INTO hold_watch VALUES (?,?,?,?)",
                          [(a[0], str(m), a[2], now.strftime("%Y-%m-%d")) for a in alerts])


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
        lines.append("  1. 先查原因：法說會、重大訊息、同業和客戶的營收、新聞（下面有連結）")
        lines.append("  2. 找到一次性的好理由（大股東賣股、單一事件、一次性費用）→ 續抱")
        lines.append("  3. 找不到好理由，或同業也在轉弱 → 賣一半換成富邦台50(006208)；猜錯了剩下一半還跟得到，猜對了少虧一半")
        lines += company_refs(s, nm) + [""]
    lines += ["回測（2019～2026，營收還在成長的股票）：從高點跌 20% 以上、又比大盤多跌 25% 以上的，之後 6 個月中位數輸大盤 16%，",
              "72% 跑輸；跟大盤一起跌的只輸 8%。但還是有 28% 之後贏大盤，所以這是「去查原因」的提醒，不是賣出訊號。"]
    body = "\n".join(lines)
    print(body)
    if send_mail and R.send_email(f"📉 持股相對大盤提醒：{'、'.join(v[0] for _, v in hits)}", body):
        with ldb() as c:
            c.executemany("INSERT OR IGNORE INTO hold_watch VALUES (?,?,?,?)",
                          [(s, month, "📉", R.now_tw().strftime("%Y-%m-%d")) for s, _ in hits])


def main():
    ap = argparse.ArgumentParser(description="長線成長股雷達")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--years", type=int, default=3, help="回補幾年（研究用可以填 7）")
    ap.add_argument("--hold-watch", action="store_true", help="每天晚上：持股一公布營收就檢查")
    a = ap.parse_args()
    if a.hold_watch:
        hold_watch(send_mail=not a.no_email)
        price_watch(send_mail=not a.no_email)
        return
    if a.backfill:
        backfill(years=max(a.years, 3), budget_min=270 if a.years > 3 else 70)
        return
    run(send_mail=not a.no_email)


if __name__ == "__main__":
    main()
