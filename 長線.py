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
MOPS_HOSTS = ["https://mopsov.twse.com.tw", "https://mops.twse.com.tw"]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"}
REV_COLS = ["month", "stock_id", "name", "market", "revenue", "rev_ly", "yoy", "mom"]
FIN_COLS = ["quarter", "stock_id", "market", "eps", "net_income", "equity"]


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
                r = (requests.post if method == "post" else requests.get)(host + path, data=data, headers=UA, timeout=40)
                r.raise_for_status()
                if len(r.content) < 500:
                    raise ValueError(f"回應太短：{r.text[:100]}")
                return r
            except Exception as e:
                last = e
                time.sleep(3)
    raise last


# ───────────── 月營收 ─────────────
def fetch_revenue(year, month, market):
    """market：sii（上市）或 otc（上櫃）。回傳該月全部公司營收。"""
    roc = year - 1911
    r = _get(f"/nas/t21/{market}/t21sc03_{roc}_{month}_0.html")
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
        for _, x in t.iterrows():
            sid = str(x["公司代號"]).strip()
            if R.is_stock_id(sid):
                inc[sid] = (_num(x[c_eps]) if c_eps else np.nan, _num(x[c_ni]) if c_ni else np.nan)
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
             "net_income": v[1], "equity": bal.get(s, np.nan)} for s, v in inc.items()]
    return pd.DataFrame(rows, columns=FIN_COLS)


def backfill(years=3):
    init_ldb()
    now = R.now_tw()
    with ldb() as c:
        have_r = {(a, b) for a, b in c.execute("SELECT month, market FROM revenue GROUP BY month, market HAVING COUNT(*) > 100")}
        have_f = {(a, b) for a, b in c.execute("SELECT quarter, market FROM fin GROUP BY quarter, market HAVING COUNT(*) > 100")}
    start = (now - timedelta(days=int(365 * years) + 400)).replace(day=1)        # 多抓一年，才算得出年增率的歷史
    for m in pd.period_range(start.strftime("%Y-%m"), now.strftime("%Y-%m"), freq="M")[:-1]:
        for mk in ("sii", "otc"):
            key = (str(m), "TWSE" if mk == "sii" else "TPEx")
            if key in have_r:
                continue
            try:
                n = lupsert("revenue", fetch_revenue(m.year, m.month, mk), REV_COLS)
                print(f"月營收 {m} {mk}：{n} 家")
            except Exception as e:
                print(f"月營收 {m} {mk} 失敗：{e}")
            time.sleep(3)
    for y in range(start.year, now.year + 1):
        for s in (1, 2, 3, 4):
            if (y, s) > (now.year, (now.month - 1) // 3):
                break
            for mk in ("sii", "otc"):
                key = (f"{y}Q{s}", "TWSE" if mk == "sii" else "TPEx")
                if key in have_f:
                    continue
                try:
                    n = lupsert("fin", fetch_fin(y, s, mk), FIN_COLS)
                    print(f"財報 {y}Q{s} {mk}：{n} 家")
                except Exception as e:
                    print(f"財報 {y}Q{s} {mk} 失敗：{e}")
                time.sleep(4)
    with ldb() as c:
        print("月營收：", c.execute("SELECT market, COUNT(DISTINCT month), MIN(month), MAX(month), COUNT(*) FROM revenue GROUP BY market").fetchall())
        print("財報：", c.execute("SELECT market, COUNT(DISTINCT quarter), MIN(quarter), MAX(quarter), COUNT(*) FROM fin GROUP BY market").fetchall())


def main():
    ap = argparse.ArgumentParser(description="長線成長股雷達")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--no-email", action="store_true")
    a = ap.parse_args()
    if a.backfill:
        backfill()
        return
    print("選股與寄信還在開發中")


if __name__ == "__main__":
    main()
