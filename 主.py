# -*- coding: utf-8 -*-
"""
台股每日收資料（2026/11/15 起短線雷達全部拿掉，只留下長線系統用得到的部分）

每天（main.yml，週一～五 07:10）：python 主.py --collect-only
  上市上櫃股價、三大法人、融資融券、集保千張大戶（每週）→ output/tw_radar.db
  更新月 K、把舊資料存進 output/history/、清理太舊的每日資料，最後跑 加碼.py 大跌加碼器。
其他程式（長線.py、加碼.py、法說.py、歷史.py）會 import 這支，用裡面的工具：抓行情、資料庫、讀持股檔、寄信。
寄信用環境變數：GMAIL_USER、GMAIL_APP_PASSWORD、RECIPIENT_EMAIL
"""
import argparse
import io
import os
import re
import smtplib
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import numpy as np
import pandas as pd
import requests

# ───────────────────────── 設定 ─────────────────────────
TZ = timezone(timedelta(hours=8))
OUTPUT_DIR = os.getenv("RADAR_OUTPUT_DIR", "output")
DB_PATH = os.path.join(OUTPUT_DIR, "tw_radar.db")
HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/124.0", "Referer": "https://www.twse.com.tw/"}

# 持股檔「股票追蹤」：[長期持股] 區每行「代號,名稱」，長線.py 每晚檢查、週六週報列出
HOLDINGS_FILES = [os.getenv("RADAR_HOLDINGS")] if os.getenv("RADAR_HOLDINGS") else [
    "股票追蹤.csv", "股票追蹤.txt", "股票追蹤", "holdings.csv"]
HOLDINGS_FILE = HOLDINGS_FILES[0]

# 融資融券餘額（張）
MARGIN_COLS = ["date", "stock_id", "margin_bal", "short_bal", "market"]
# 千張大戶（集保「股權分散表」每週公布）
BIG_HOLDER_LEVEL = 15      # 持股分級 15 ＝ 1,000,001 股（1000 張）以上
HOLDER_COLS = ["date", "stock_id", "big_ratio", "big_people"]

QUOTE_COLS = ["date", "stock_id", "stock_name", "market", "close", "volume", "turnover", "high", "low", "open"]
INST_COLS = ["date", "stock_id", "foreign_net", "trust_net", "dealer_prop", "dealer_hedge", "dealer_total", "market"]
EXPECTED_COLUMNS = {   # 表 → {欄位: 型別}，用來把舊資料庫補齊
    "prices": {c: ("REAL" if c in ("close", "turnover", "high", "low", "open") else "INTEGER" if c == "volume" else "TEXT")
               for c in QUOTE_COLS},
    "institutional": {c: ("TEXT" if c in ("date", "stock_id", "market") else "INTEGER") for c in INST_COLS},
}


# ───────────────────────── 小工具 ─────────────────────────


def now_tw():
    return datetime.now(TZ)


def safe_float(v):
    try:
        t = str(v).strip().replace(",", "").replace("＋", "+").replace("－", "-")
        if t in ("", "--", "---", "nan", "None"):
            return np.nan
        return float(t)
    except (ValueError, TypeError):
        return np.nan


def safe_int(v, default=0):
    n = safe_float(v)
    return default if pd.isna(n) else int(n)


def normalize_stock_id(v):
    t = str(v).strip()
    if t.endswith(".0"):
        t = t[:-2]
    return t.zfill(4) if t.isdigit() and len(t) < 4 else t


def is_stock_id(sid):
    return sid.isdigit() and len(sid) == 4


def pick(it, *names):
    for n in names:
        v = it.get(n)
        if v not in (None, ""):
            return v
    return None


def fmt_date(d):          # 20260930 -> 2026-09-30
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def parse_date(s):        # 支援民國 1150930／20260930／2026-09-30
    if s is None:
        return None
    s = str(s).strip().replace("/", "").replace("-", "")
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}"
    if len(s) == 8 and s.isdigit():
        return fmt_date(s)
    return None


def guess_last_close_date():
    """沒有日期資訊時，推測最近一個已收盤的平日（14:30 前算前一天）。"""
    n = now_tw()
    d = n.date()
    if n.hour * 60 + n.minute < 14 * 60 + 30:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def recent_trading_days(n=10):
    """從今天往回數 n 個平日（YYYYMMDD）。國定假日靠 API 回應判斷。"""
    d, out = now_tw(), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return out


def weekdays_back(calendar_days):
    now = now_tw()
    days = [now - timedelta(days=i) for i in range(calendar_days)]
    return [d.strftime("%Y%m%d") for d in days if d.weekday() < 5]


def get_json(url, params=None, timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            txt = r.text.strip()
            if not txt or txt.startswith("<"):
                raise ValueError("空回應或 HTML（可能被擋或網站維護）")
            return r.json()
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def find_table(j, need):
    """在 TWSE 回應中找出同時含有 need 欄位的表，回傳 (fields, rows)；支援 tables／fieldsN+dataN／fields+data。"""
    cands = [(t.get("fields", []), t.get("data", [])) for t in (j.get("tables") or [])]
    for k, v in j.items():
        if k.startswith("fields") and isinstance(v, list):
            cands.append((v, j.get("data" + k[len("fields"):], [])))
    for fields, rows in cands:
        if rows and all(n in fields for n in need):
            return fields, rows
    return None, None


# ───────────────────────── 資料庫 ─────────────────────────
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS prices (date TEXT, stock_id TEXT, stock_name TEXT, market TEXT, "
                     "close REAL, volume INTEGER, turnover REAL, high REAL, low REAL, PRIMARY KEY(date, stock_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS monthly (month TEXT, stock_id TEXT, market TEXT, open REAL, high REAL, "
                     "low REAL, close REAL, volume REAL, PRIMARY KEY(month, stock_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS institutional (date TEXT, stock_id TEXT, foreign_net INTEGER, "
                     "trust_net INTEGER, dealer_prop INTEGER, dealer_hedge INTEGER, dealer_total INTEGER, "
                     "market TEXT, PRIMARY KEY(date, stock_id, market))")
        # 舊版資料庫的表可能缺欄位（CREATE IF NOT EXISTS 不會補），這裡自動補上
        for table, cols in EXPECTED_COLUMNS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                    print(f"資料庫升級：{table} 補上欄位 {name}")
        # 融資融券餘額（每日；單位：張）
        conn.execute("CREATE TABLE IF NOT EXISTS margin (date TEXT, stock_id TEXT, margin_bal REAL, "
                     "short_bal REAL, market TEXT, PRIMARY KEY(date, stock_id, market))")
        # 集保千張大戶持股（每週一筆）
        conn.execute("CREATE TABLE IF NOT EXISTS holders (date TEXT, stock_id TEXT, big_ratio REAL, "
                     "big_people INTEGER, PRIMARY KEY(date, stock_id))")


def _py(v):
    """numpy／NaN → sqlite 能吃的 Python 值。"""
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, (np.bool_, bool)):
        return int(v)
    if isinstance(v, (np.integer, int)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return None if np.isnan(v) else float(v)
    return v


def upsert(table, df, cols):
    """INSERT OR REPLACE（主鍵相同就覆蓋），可重複執行。"""
    if df is None or df.empty:
        return 0
    rows = [tuple(_py(v) for v in r) for r in df[cols].itertuples(index=False, name=None)]
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    with db() as conn:
        conn.executemany(sql, rows)
    return len(rows)


def load_table(table, days):
    cutoff = (now_tw() - timedelta(days=days)).strftime("%Y-%m-%d")
    date_col = "date"
    try:
        with db() as conn:
            df = pd.read_sql(f"SELECT * FROM {table} WHERE {date_col} >= ?", conn, params=(cutoff,))
    except Exception as e:
        print(f"讀取 {table} 失敗：{e}")
        return pd.DataFrame()
    if "stock_id" in df.columns:
        df["stock_id"] = df["stock_id"].astype(str)
    return df


# ───────────────────────── 抓資料：行情 ─────────────────────────
def fetch_twse_quotes(d):
    """上市每日收盤行情（指定日期 d=YYYYMMDD）。休市日回傳空表。"""
    j = get_json("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                 {"response": "json", "date": d, "type": "ALLBUT0999"})
    if j.get("stat") != "OK":
        return pd.DataFrame(columns=QUOTE_COLS)
    fields, raw = find_table(j, ["證券代號", "收盤價", "成交股數"])
    if not fields:
        print("MI_INDEX 找不到「每日收盤行情」表，API 格式可能改了；回應欄位：",
              [t.get("fields") for t in (j.get("tables") or [])][:3])
        return pd.DataFrame(columns=QUOTE_COLS)
    date = fmt_date(d)
    rows = []
    for r in raw:
        it = dict(zip(fields, r))
        sid = normalize_stock_id(it.get("證券代號", ""))
        close = safe_float(it.get("收盤價"))
        if not is_stock_id(sid) or pd.isna(close) or close <= 0:
            continue
        rows.append({"date": date, "stock_id": sid, "stock_name": str(it.get("證券名稱", "")).strip(),
                     "market": "TWSE", "close": close, "volume": safe_float(it.get("成交股數")),
                     "turnover": safe_float(it.get("成交金額")),
                     "high": safe_float(it.get("最高價")), "low": safe_float(it.get("最低價")),
                     "open": safe_float(it.get("開盤價"))})
    return pd.DataFrame(rows, columns=QUOTE_COLS)


def fetch_twse_openapi():
    """備援：openapi 沒有日期欄位，只能推測日期，所以呼叫端要再用 looks_stale 檢查。"""
    data = get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    if not isinstance(data, list) or not data:
        return pd.DataFrame(columns=QUOTE_COLS)
    date = guess_last_close_date()
    rows = []
    for it in data:
        sid = normalize_stock_id(it.get("Code", ""))
        close = safe_float(it.get("ClosingPrice"))
        if not is_stock_id(sid) or pd.isna(close) or close <= 0:
            continue
        rows.append({"date": date, "stock_id": sid, "stock_name": str(it.get("Name", "")).strip(),
                     "market": "TWSE", "close": close, "volume": safe_float(it.get("TradeVolume")),
                     "turnover": safe_float(it.get("TradeValue")),
                     "high": safe_float(it.get("HighestPrice")), "low": safe_float(it.get("LowestPrice")),
                     "open": safe_float(it.get("OpeningPrice"))})
    return pd.DataFrame(rows, columns=QUOTE_COLS)


def fetch_tpex_quotes():
    data = get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
    if not isinstance(data, list) or not data:
        return pd.DataFrame(columns=QUOTE_COLS)
    rows = []
    for it in data:
        sid = normalize_stock_id(pick(it, "SecuritiesCompanyCode", "Code") or "")
        close = safe_float(pick(it, "Close", "ClosingPrice"))
        if not is_stock_id(sid) or pd.isna(close) or close <= 0:
            continue
        rows.append({"date": parse_date(pick(it, "Date")) or guess_last_close_date(), "stock_id": sid,
                     "stock_name": str(pick(it, "CompanyName", "Name") or "").strip(), "market": "TPEx",
                     "close": close,
                     "volume": safe_float(pick(it, "TradingShares", "TradeVolume", "Volume")),
                     "turnover": safe_float(pick(it, "TransactionAmount", "TradeValue", "Amount")),
                     "open": safe_float(pick(it, "Open", "OpeningPrice")),
                     "high": safe_float(pick(it, "High", "HighestPrice")),
                     "low": safe_float(pick(it, "Low", "LowestPrice"))})
    df = pd.DataFrame(rows, columns=QUOTE_COLS)
    if not df.empty and df["volume"].isna().all():
        print("⚠ TPEx 成交量欄位對不上（全是空值），量比會失效。API 欄位：", list(data[0].keys()))
    return df


def looks_stale(new, hist):
    """新抓到的收盤價與前一個交易日幾乎全同 → 多半是休市或資料尚未更新。"""
    if new.empty or hist is None or hist.empty:
        return False
    new_date, market = new["date"].iloc[0], new["market"].iloc[0]
    h = hist[(hist["market"] == market) & (hist["date"] < new_date)]
    if h.empty:
        return False
    last = h[h["date"] == h["date"].max()].drop_duplicates("stock_id").set_index("stock_id")["close"]
    cur = new.drop_duplicates("stock_id").set_index("stock_id")["close"]
    common = cur.index.intersection(last.index)
    if len(common) < 100:
        return False
    return bool((cur[common].values == last[common].values).mean() > 0.98)


def collect_quotes(hist_price):
    tw = pd.DataFrame(columns=QUOTE_COLS)
    for d in recent_trading_days(10):
        try:
            tw = fetch_twse_quotes(d)
        except Exception as e:
            print(f"TWSE MI_INDEX {d} 失敗：{e}")
            break                      # 網路／被擋就不用往前一天一天試了
        if not tw.empty:
            print(f"TWSE 行情 {d}：{len(tw)} 檔")
            break
        time.sleep(1)
    if tw.empty:
        print("MI_INDEX 沒資料，改用 openapi 備援…")
        try:
            tw = fetch_twse_openapi()
            if looks_stale(tw, hist_price):
                print("openapi 收盤價與前一交易日幾乎全同，判定休市／未更新，略過")
                tw = pd.DataFrame(columns=QUOTE_COLS)
            else:
                print(f"TWSE openapi：{len(tw)} 檔（日期推測為 {tw['date'].iloc[0] if not tw.empty else '-'}）")
        except Exception as e:
            print(f"openapi 也失敗：{e}")
    tp = pd.DataFrame(columns=QUOTE_COLS)
    try:
        tp = fetch_tpex_quotes()
        print(f"TPEx 行情：{len(tp)} 檔" + (f"（{tp['date'].iloc[0]}）" if not tp.empty else ""))
    except Exception as e:
        print(f"TPEx 行情失敗：{e}")
    frames = [x for x in (tw, tp) if not x.empty]
    if not frames:
        return pd.DataFrame(columns=QUOTE_COLS)
    return pd.concat(frames, ignore_index=True).drop_duplicates(["date", "stock_id"], keep="last")


# ───────────────────────── 抓資料：法人 ─────────────────────────
def fetch_twse_inst(d):
    j = get_json("https://www.twse.com.tw/rwd/zh/fund/T86",
                 {"response": "json", "date": d, "selectType": "ALLBUT0999"}, timeout=20)
    if j.get("stat") != "OK":
        return pd.DataFrame(columns=INST_COLS)
    fields, raw = find_table(j, ["證券代號"])
    if not fields:
        return pd.DataFrame(columns=INST_COLS)

    def col(pred):
        return next((f for f in fields if pred(f)), None)

    c_for = col(lambda f: f.startswith("外陸資買賣超") or f.startswith("外資買賣超"))
    c_tru = col(lambda f: f == "投信買賣超股數")
    c_tot = col(lambda f: f == "自營商買賣超股數")
    c_prop = col(lambda f: f.startswith("自營商買賣超") and "自行買賣" in f)
    c_hedge = col(lambda f: f.startswith("自營商買賣超") and "避險" in f)
    if not c_for or not c_tru:
        print("T86 找不到外資／投信買賣超欄位，API 格式可能改了；欄位：", fields)
        return pd.DataFrame(columns=INST_COLS)
    date = fmt_date(d)
    rows = []
    for r in raw:
        it = dict(zip(fields, r))
        sid = normalize_stock_id(it.get("證券代號", ""))
        if not is_stock_id(sid):
            continue
        rows.append({"date": date, "stock_id": sid, "foreign_net": safe_int(it.get(c_for)),
                     "trust_net": safe_int(it.get(c_tru)),
                     "dealer_prop": safe_int(it.get(c_prop)) if c_prop else 0,
                     "dealer_hedge": safe_int(it.get(c_hedge)) if c_hedge else 0,
                     "dealer_total": safe_int(it.get(c_tot)) if c_tot else 0, "market": "TWSE"})
    return pd.DataFrame(rows, columns=INST_COLS)


def fetch_tpex_inst():
    data = get_json("https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading")
    if not isinstance(data, list) or not data:
        return pd.DataFrame(columns=INST_COLS)
    keys = list(data[0].keys())
    diff = [k for k in keys if "Difference" in k]
    f_keys = [k for k in diff if "Foreign" in k]
    f_key = next((k for k in f_keys if "Exclud" in k), f_keys[0] if f_keys else None)
    t_key = next((k for k in diff if "InvestmentTrust" in k), None)
    if not f_key or not t_key:
        print("⚠ TPEx 法人欄位對不上，這次略過櫃買法人。API 欄位：", keys)
        return pd.DataFrame(columns=INST_COLS)
    # 自營商欄位（名稱我沒辦法事先確認，所以用關鍵字偵測；對不上就當 0，不影響其他功能）
    d_keys = [k for k in diff if "Dealer" in k and "Foreign" not in k]
    prop_k = next((k for k in d_keys if "Proprietary" in k or "Self" in k), None)
    hedge_k = next((k for k in d_keys if "Hedg" in k), None)
    tot_k = next((k for k in d_keys if k not in (prop_k, hedge_k)), None)
    print(f"TPEx 法人欄位對應：外資={f_key}｜投信={t_key}")
    print(f"TPEx 自營商欄位對應：自行買賣={prop_k}｜避險={hedge_k}｜合計={tot_k}｜（所有含 Dealer 的欄位：{d_keys}）")
    if not prop_k and not tot_k:
        print("⚠ TPEx 自營商欄位偵測不到，上櫃自營商先當 0。請把上面「所有含 Dealer 的欄位」貼給我。")
    date = parse_date(pick(data[0], "Date")) or guess_last_close_date()
    rows = []
    for it in data:
        sid = normalize_stock_id(pick(it, "SecuritiesCompanyCode", "Code") or "")
        if not is_stock_id(sid):
            continue
        hedge = safe_int(it.get(hedge_k)) if hedge_k else 0
        total = safe_int(it.get(tot_k)) if tot_k else None
        prop = safe_int(it.get(prop_k)) if prop_k else ((total - hedge) if total is not None else 0)
        rows.append({"date": date, "stock_id": sid, "foreign_net": safe_int(it.get(f_key)),
                     "trust_net": safe_int(it.get(t_key)), "dealer_prop": prop, "dealer_hedge": hedge,
                     "dealer_total": total if total is not None else prop + hedge, "market": "TPEx"})
    return pd.DataFrame(rows, columns=INST_COLS)


# ───────────────────────── 抓資料：融資融券（散戶借錢買股的程度） ─────────────────────────
def fetch_twse_margin(d):
    """上市融資融券餘額（張）。TWSE 這張表的『今日餘額』欄位名稱重複，所以用位置判斷：第一個＝融資、第二個＝融券。"""
    empty = pd.DataFrame(columns=MARGIN_COLS)
    j = get_json("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
                 {"response": "json", "date": d, "selectType": "STOCK"}, timeout=20)
    if j.get("stat") != "OK":
        return empty
    fields, raw, id_name = None, None, None
    for id_name in ("代號", "股票代號", "證券代號"):          # 11/15 發現證交所欄位名稱是「代號」，舊程式只認另外兩個，上市融資一直沒收到
        fields, raw = find_table(j, [id_name])
        if fields:
            break
    if not fields:
        print("MI_MARGN 找不到資料表，API 格式可能改了；回應欄位：", [k for k in j.keys()])
        return empty
    bal = [i for i, f in enumerate(fields) if str(f).endswith("今日餘額")]
    if len(bal) < 2:
        print("MI_MARGN 找不到融資／融券今日餘額欄位；欄位：", fields)
        return empty
    i_id, i_m, i_s = fields.index(id_name), bal[0], bal[1]
    date, rows = fmt_date(d), []
    for r in raw:
        sid = normalize_stock_id(str(r[i_id]))
        if is_stock_id(sid):
            rows.append({"date": date, "stock_id": sid, "margin_bal": safe_int(r[i_m]),
                         "short_bal": safe_int(r[i_s]), "market": "TWSE"})
    return pd.DataFrame(rows, columns=MARGIN_COLS)


def fetch_tpex_margin():
    """上櫃融資融券。我沒辦法事先確認 API 名稱與欄位，所以逐一嘗試、用關鍵字偵測；失敗就略過（上櫃股不套用融資條件）。"""
    empty = pd.DataFrame(columns=MARGIN_COLS)
    tried, data, used = [], None, None
    for name in ("tpex_mainboard_margin_balance", "tpex_margin_balance", "tpex_mainboard_margin_trading"):
        tried.append(name)
        try:
            data = get_json(f"https://www.tpex.org.tw/openapi/v1/{name}", retries=1)
        except Exception:
            continue
        if isinstance(data, list) and data:
            used = name
            break
    if not used:
        print(f"⚠ TPEx 融資融券：試過 {tried} 都抓不到，上櫃股先不套用融資條件（上市不受影響）")
        return empty
    keys = list(data[0].keys())
    low = {k: k.lower() for k in keys}
    bal = [k for k in keys if ("balance" in low[k] or "餘額" in k) and not any(w in low[k] for w in ("prev", "前日"))]
    m_k = next((k for k in bal if "margin" in low[k] or "融資" in k), None)
    s_k = next((k for k in bal if "short" in low[k] or "融券" in k), None)
    print(f"TPEx 融資融券欄位對應（{used}）：融資餘額={m_k}｜融券餘額={s_k}")
    if not m_k:
        print("⚠ TPEx 融資餘額欄位對不上，略過。API 欄位：", keys)
        return empty
    date = parse_date(pick(data[0], "Date")) or guess_last_close_date()
    rows = []
    for it in data:
        sid = normalize_stock_id(pick(it, "SecuritiesCompanyCode", "Code") or "")
        if is_stock_id(sid):
            rows.append({"date": date, "stock_id": sid, "margin_bal": safe_int(it.get(m_k)),
                         "short_bal": safe_int(it.get(s_k)) if s_k else 0, "market": "TPEx"})
    return pd.DataFrame(rows, columns=MARGIN_COLS)


def collect_margin():
    """抓最近還沒存過的幾天上市融資餘額（最多 6 天，讓 5 日增減馬上算得出來），再加上上櫃最新一天。"""
    frames = []
    try:
        with db() as conn:
            have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM margin WHERE market='TWSE'")}
        got = 0
        for d in recent_trading_days(10):
            if got >= 6:
                break
            if fmt_date(d) in have:
                continue
            m = fetch_twse_margin(d)
            if not m.empty:
                frames.append(m)
                got += 1
            time.sleep(1.5)
        print(f"TWSE 融資融券：新增 {got} 天")
    except Exception as e:
        print(f"TWSE 融資融券失敗：{e}")
    try:
        tp = fetch_tpex_margin()
        if not tp.empty:
            print(f"TPEx 融資融券 {tp['date'].iloc[0]}：{len(tp)} 檔")
            frames.append(tp)
    except Exception as e:
        print(f"TPEx 融資融券失敗：{e}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=MARGIN_COLS)


# ───────────────────────── 抓資料：千張大戶（集保，每週） ─────────────────────────
def fetch_tdcc_holders():
    """集保「股權分散表」開放資料：每檔股票各持股分級的人數、股數、佔比。只取千張以上那一級。"""
    r = requests.get("https://opendata.tdcc.com.tw/getOD.ashx?id=1-5", headers=HEADERS, timeout=90)
    r.raise_for_status()
    try:
        text = r.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = r.content.decode("cp950", errors="replace")
    df = pd.read_csv(io.StringIO(text), dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    find = lambda kw: next((c for c in df.columns if kw in c), None)
    c_date, c_id, c_lv, c_ratio, c_ppl = find("日期"), find("代號"), find("分級"), find("比例"), find("人數")
    if not (c_date and c_id and c_lv and c_ratio):
        print("⚠ 集保資料欄位對不上，這次略過千張大戶。欄位：", list(df.columns))
        return pd.DataFrame(columns=HOLDER_COLS)
    big = df[pd.to_numeric(df[c_lv], errors="coerce") == BIG_HOLDER_LEVEL]
    out = pd.DataFrame({"date": big[c_date].map(parse_date), "stock_id": big[c_id].map(normalize_stock_id),
                        "big_ratio": pd.to_numeric(big[c_ratio], errors="coerce"),
                        "big_people": pd.to_numeric(big[c_ppl], errors="coerce") if c_ppl else np.nan})
    out = out[out["stock_id"].map(is_stock_id) & out["date"].notna() & out["big_ratio"].notna()]
    if out.empty:
        print("⚠ 集保資料裡找不到持股分級", BIG_HOLDER_LEVEL, "的資料，這次略過千張大戶。分級值：",
              sorted(df[c_lv].dropna().unique())[:20])
        return pd.DataFrame(columns=HOLDER_COLS)
    return out[HOLDER_COLS].reset_index(drop=True)


def collect_holders():
    try:
        h = fetch_tdcc_holders()
    except Exception as e:
        print(f"集保千張大戶失敗：{e}")
        return pd.DataFrame(columns=HOLDER_COLS)
    if not h.empty:
        print(f"集保千張大戶：{len(h)} 檔（資料日 {h['date'].iloc[0]}，持股分級 {BIG_HOLDER_LEVEL}）"
              f"｜佔比中位數 {h['big_ratio'].median():.1f}%")
    return h


def collect_inst():
    frames = []
    for d in recent_trading_days(8):
        try:
            tw = fetch_twse_inst(d)
        except Exception as e:
            print(f"TWSE 法人 {d} 失敗：{e}")
            break
        if not tw.empty:
            print(f"TWSE 法人 {d}：{len(tw)} 檔")
            frames.append(tw)
            break
        time.sleep(1)
    try:
        tp = fetch_tpex_inst()
        if not tp.empty:
            print(f"TPEx 法人 {tp['date'].iloc[0]}：{len(tp)} 檔")
            frames.append(tp)
    except Exception as e:
        print(f"TPEx 法人失敗：{e}")
    return frames


def backfill(days, prices_only=False):
    """回補上市（TWSE）歷史行情與法人。TPEx 的歷史資料沒有穩定的公開 API，只能每天累積。"""
    with db() as conn:
        # 已有最高/最低價的日期才算「已補過」（舊資料沒有高低價，會重抓一次補上）
        have_p = {r[0] for r in conn.execute("SELECT DISTINCT date FROM prices WHERE market='TWSE' AND high IS NOT NULL "
                                             "AND open IS NOT NULL")}
        have_i = {r[0] for r in conn.execute("SELECT DISTINCT date FROM institutional WHERE market='TWSE'")}
        have_m = {r[0] for r in conn.execute("SELECT DISTINCT date FROM margin WHERE market='TWSE'")}
    for d in weekdays_back(days):
        iso = fmt_date(d)
        if iso not in have_p:
            try:
                n = upsert("prices", fetch_twse_quotes(d), QUOTE_COLS)
                print(f"回補行情 {iso}：{n} 檔" if n else f"{iso} 無行情（休市？）")
            except Exception as e:
                print(f"回補行情 {iso} 失敗：{e}")
            time.sleep(2)
        if prices_only:
            continue
        if iso not in have_m:
            try:
                n = upsert("margin", fetch_twse_margin(d), MARGIN_COLS)
                print(f"回補融資 {iso}：{n} 檔" if n else f"{iso} 無融資資料")
            except Exception as e:
                print(f"回補融資 {iso} 失敗：{e}")
            time.sleep(2)
        if iso not in have_i:
            try:
                n = upsert("institutional", fetch_twse_inst(d), INST_COLS)
                print(f"回補法人 {iso}：{n} 檔" if n else f"{iso} 無法人資料")
            except Exception as e:
                print(f"回補法人 {iso} 失敗：{e}")
            time.sleep(2)


# ── 上櫃（TPEx）歷史資料：openapi 只有最新一天，回補要用櫃買網站的查詢頁 ──
TPEX_NEW = "https://www.tpex.org.tw/www/zh-tw"
TPEX_OLD = "https://www.tpex.org.tw/web/stock"


def _tpex_table(j):
    """櫃買回應可能是新版 {tables:[{fields,data}]} 或舊版 {aaData:[...]}，回傳 (fields 或 None, rows)。"""
    for t in (j.get("tables") or []):
        if t.get("data"):
            return t.get("fields") or None, t["data"]
    if j.get("aaData"):
        return j.get("fields") or None, j["aaData"]
    return None, []


def _tpex_get(new_path, new_params, old_path, old_params):
    """先試新版網站，失敗再試舊版。回傳 (fields, rows, 來源)。"""
    for url, params, tag in ((f"{TPEX_NEW}/{new_path}", new_params, "新版"), (f"{TPEX_OLD}/{old_path}", old_params, "舊版")):
        try:
            j = get_json(url, params, timeout=30, retries=2)
            fields, rows = _tpex_table(j)
            if rows:
                return fields, rows, tag
        except Exception as e:
            print(f"  TPEx {tag} {new_path if tag == '新版' else old_path} 失敗：{e}")
    return None, [], ""


def _roc(d):
    return f"{int(d[:4]) - 1911}/{d[4:6]}/{d[6:]}"


_TPEX_FIELDS_SHOWN = set()


def _show_fields(kind, fields, row):
    if kind not in _TPEX_FIELDS_SHOWN:
        _TPEX_FIELDS_SHOWN.add(kind)
        print(f"  TPEx {kind} 欄位：{fields}")
        print(f"  TPEx {kind} 第一筆：{row}")


def fetch_tpex_quotes_hist(d):
    """上櫃指定日期的收盤行情（d=YYYYMMDD）。"""
    fields, raw, tag = _tpex_get("afterTrading/otcQuotes", {"date": f"{d[:4]}/{d[4:6]}/{d[6:]}", "type": "EW", "response": "json"},
                                 "aftertrading/otc_quotes_no1430/stk_wn1430_result.php",
                                 {"l": "zh-tw", "d": _roc(d), "se": "EW", "o": "json"})
    if not raw:
        return pd.DataFrame(columns=QUOTE_COLS)
    _show_fields("行情", fields, raw[0])

    def idx(*keys, default=None):
        if fields:
            for i, f in enumerate(fields):
                if any(f.strip().startswith(k) for k in keys):
                    return i
        return default
    # 沒有欄位名稱時用舊版固定位置：代號,名稱,收盤,漲跌,開盤,最高,最低,(均價),成交股數,成交金額
    i_c, i_o, i_h, i_l = idx("收盤", default=2), idx("開盤", default=4), idx("最高", default=5), idx("最低", default=6)
    i_v, i_t = idx("成交股數", default=7), idx("成交金額", default=8)
    date = fmt_date(d)
    rows = []
    for r in raw:
        sid = normalize_stock_id(str(r[0]))
        close = safe_float(r[i_c])
        if not is_stock_id(sid) or pd.isna(close) or close <= 0:
            continue
        rows.append({"date": date, "stock_id": sid, "stock_name": str(r[1]).strip(), "market": "TPEx",
                     "close": close, "volume": safe_float(r[i_v]), "turnover": safe_float(r[i_t]),
                     "open": safe_float(r[i_o]), "high": safe_float(r[i_h]), "low": safe_float(r[i_l])})
    return pd.DataFrame(rows, columns=QUOTE_COLS)


def fetch_tpex_inst_hist(d):
    """上櫃指定日期的三大法人買賣超（股）。表格固定 24 欄：
    代號,名稱, 外資不含自營(買,賣,超), 外資自營(買,賣,超), 外資合計(買,賣,超), 投信(買,賣,超),
    自營自行(買,賣,超), 自營避險(買,賣,超), 自營合計(買,賣,超), 三大法人合計"""
    fields, raw, tag = _tpex_get("insti/dailyTrade", {"type": "Daily", "sect": "EW", "date": f"{d[:4]}/{d[4:6]}/{d[6:]}", "response": "json"},
                                 "3insti/daily_trade/3itrade_hedge_result.php",
                                 {"l": "zh-tw", "se": "EW", "t": "D", "d": _roc(d), "o": "json"})
    if not raw:
        return pd.DataFrame(columns=INST_COLS)
    _show_fields("法人", fields, raw[0])
    if len(raw[0]) < 23:
        print(f"⚠ TPEx 法人表只有 {len(raw[0])} 欄，格式跟預期不同，這天略過")
        return pd.DataFrame(columns=INST_COLS)
    date = fmt_date(d)
    rows = []
    for r in raw:
        sid = normalize_stock_id(str(r[0]))
        if not is_stock_id(sid):
            continue
        rows.append({"date": date, "stock_id": sid, "foreign_net": safe_int(r[4]), "trust_net": safe_int(r[13]),
                     "dealer_prop": safe_int(r[16]), "dealer_hedge": safe_int(r[19]),
                     "dealer_total": safe_int(r[22]), "market": "TPEx"})
    return pd.DataFrame(rows, columns=INST_COLS)


def backfill_tpex(days):
    """回補上櫃歷史股價（含開高低）與法人。"""
    with db() as conn:
        have_p = {r[0] for r in conn.execute("SELECT DISTINCT date FROM prices WHERE market='TPEx' AND open IS NOT NULL")}
        have_i = {r[0] for r in conn.execute("SELECT DISTINCT date FROM institutional WHERE market='TPEx'")}
    inst_from = "2026-06-08"                      # 上市法人也是從這天開始，太早的用不到
    fail = 0
    for d in weekdays_back(days):
        iso = fmt_date(d)
        if iso not in have_p:
            try:
                n = upsert("prices", fetch_tpex_quotes_hist(d), QUOTE_COLS)
                print(f"回補上櫃行情 {iso}：{n} 檔" if n else f"{iso} 上櫃無行情（休市？）")
                fail = 0 if n else fail
            except Exception as e:
                print(f"回補上櫃行情 {iso} 失敗：{e}")
                fail += 1
            time.sleep(2)
        if iso >= inst_from and iso not in have_i:
            try:
                n = upsert("institutional", fetch_tpex_inst_hist(d), INST_COLS)
                print(f"回補上櫃法人 {iso}：{n} 檔" if n else f"{iso} 上櫃無法人資料")
            except Exception as e:
                print(f"回補上櫃法人 {iso} 失敗：{e}")
            time.sleep(2)
        if fail >= 5:
            print("⚠ 連續 5 天抓不到上櫃資料，可能被擋，先停")
            break


# ───────────────────────── 月 K（長期趨勢）─────────────────────────
MONTHLY_COLS = ["month", "stock_id", "market", "open", "high", "low", "close", "volume"]
KEEP_DAILY_DAYS = 300      # 每日股價、融資只留這麼多天（資料庫不能超過 GitHub 100MB 上限）；更早的只留月 K 和 output/history/
INST_KEEP_DAYS = 200       # 法人買賣超只留 200 天；長期歷史另外存在 output/history/


def _agg_month(df):
    """把同一個月的每日股價合成月 K。"""
    if df.empty:
        return pd.DataFrame(columns=MONTHLY_COLS)
    df = df.sort_values(["stock_id", "date"]).copy()
    df["month"] = df["date"].str[:7]
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open"] = df["open"].fillna(df["close"])
    df["high"] = df["high"].fillna(df["close"])
    df["low"] = df["low"].fillna(df["close"])
    g = df.groupby(["month", "stock_id"])
    out = pd.DataFrame({"market": g["market"].last(), "open": g["open"].first(), "high": g["high"].max(),
                        "low": g["low"].min(), "close": g["close"].last(), "volume": g["volume"].sum()}).reset_index()
    return out[MONTHLY_COLS]


def backfill_monthly(market, years=3, max_months=40):
    """回補上市（TWSE）或上櫃（TPEx）過去幾年的月 K：一天一天抓，合成月 K 存起來，每日資料不存（省空間）。"""
    fetch = fetch_twse_quotes if market == "TWSE" else fetch_tpex_quotes_hist
    now = now_tw()
    first = (now - timedelta(days=int(365 * years))).replace(day=1)
    months = pd.period_range(first.strftime("%Y-%m"), now.strftime("%Y-%m"), freq="M")[:-1]   # 不含這個月（每天會從每日股價算）
    with db() as conn:
        have = {r[0] for r in conn.execute("SELECT month FROM monthly WHERE market=? GROUP BY month HAVING COUNT(*) >= 100", (market,))}
    empty_run = 0
    for m in months:
        ms = str(m)
        if ms in have:
            continue
        if max_months <= 0:
            print("這次先補到這裡（避免超過 GitHub 90 分鐘上限），再跑一次會接著補")
            break
        max_months -= 1
        days = [d.strftime("%Y%m%d") for d in pd.date_range(m.start_time, m.end_time, freq="B")]
        frames = []
        for d in days:
            try:
                f = fetch(d)
                if not f.empty:
                    frames.append(f)
            except Exception as e:
                print(f"  {market} {d} 失敗：{e}")
            time.sleep(1.5)
        n = upsert("monthly", _agg_month(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame(columns=MONTHLY_COLS),
                   MONTHLY_COLS)
        print(f"回補月 K {market} {ms}：{len(frames)} 個交易日，{n} 檔")
        empty_run = 0 if n else empty_run + 1
        if empty_run >= 3:
            print("⚠ 連續 3 個月抓不到資料，可能被擋，先停")
            break


def update_monthly_and_prune():
    """每天：用每日股價更新月 K（只更新資料完整的月份＋這個月），再把太舊的每日資料刪掉、壓縮資料庫。"""
    px = load_table("prices", KEEP_DAILY_DAYS + 60)
    if not px.empty:
        for mk, g in px.groupby("market"):
            first_month = g["date"].min()[:7]
            g = g[g["date"].str[:7] > first_month]             # 第一個月可能不完整，留給回補的資料
            upsert("monthly", _agg_month(g), MONTHLY_COLS)
    try:                                                   # 刪之前先把資料存進長期歷史檔（output/history/）
        import importlib.util as _iu
        _sp = _iu.spec_from_file_location("radar_hist", os.path.join(os.path.dirname(os.path.abspath(__file__)), "歷史.py"))
        _h = _iu.module_from_spec(_sp)
        _sp.loader.exec_module(_h)
        _h.daily_export()
    except Exception as e:
        print(f"⚠ 歷史資料存檔失敗，這次先不刪舊資料：{e}")
        return
    cutoff = (now_tw() - timedelta(days=KEEP_DAILY_DAYS)).strftime("%Y-%m-%d")
    with db() as conn:
        n1 = conn.execute("DELETE FROM prices WHERE date < ?", (cutoff,)).rowcount
        n2 = conn.execute("DELETE FROM margin WHERE date < ?", (cutoff,)).rowcount
        n2 += conn.execute("DELETE FROM institutional WHERE date < ?",
                           ((now_tw() - timedelta(days=INST_KEEP_DAYS)).strftime("%Y-%m-%d"),)).rowcount
    if n1 or n2:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("VACUUM")
        print(f"清掉 {cutoff} 以前的每日股價 {n1} 筆、法人和融資 {n2} 筆")


# ───────────────────────── 我真正買的股票 ─────────────────────────
LONG_HOLD_GROUP = "長期持股"    # 這一區只給長線週報偵測「營收轉弱」用，每天的信不列
LONG_HOLDINGS = {}           # {代號: 區名}
HOLDING_STOPS = {}          # 持股檔第三欄（舊格式相容，目前沒用到）


def load_holdings():
    """讀持股檔。每行：股票代號,買進價（買進價可不填）。空行、# 開頭、標題列都會略過。賣掉了就把那一行刪掉。
    用 [區名] 一行把股票分區（例如 [原本持股]、[雷達短線]），信裡會分開列；沒寫區名的歸在「我的持股」。
    回傳 {股票代號: (買進價或 None, 區名)}，順序同檔案。"""
    global HOLDINGS_FILE
    out = {}
    group = "我的持股"
    found = next((p for p in HOLDINGS_FILES if p and os.path.exists(p)), None)
    if found is None and HOLDINGS_FILE and os.path.exists(HOLDINGS_FILE):
        found = HOLDINGS_FILE                      # 讓測試或環境變數指定的路徑也能用
    if found is None:
        print(f"沒有找到持股檔（{' / '.join(p for p in HOLDINGS_FILES if p)}），略過我的持股")
        return out
    HOLDINGS_FILE = found
    try:
        with open(HOLDINGS_FILE, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip().replace("，", ",")
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and "]" in line:           # 分區標題
                    group = line[1:line.index("]")].strip() or "我的持股"
                    continue
                parts = [p.strip() for p in line.replace("\t", ",").replace(" ", ",").split(",") if p.strip()]
                sid = normalize_stock_id(parts[0])
                if group == LONG_HOLD_GROUP and re.fullmatch(r"00\d{2,4}[A-Z]?", sid):
                    LONG_HOLDINGS[sid] = group             # ETF（例：006208、00878）：長線週報只列價格，不看營收
                    continue
                if not is_stock_id(sid):
                    continue                           # 標題列或亂打的字
                if group == LONG_HOLD_GROUP:
                    LONG_HOLDINGS[sid] = group
                    continue
                price = safe_float(parts[1]) if len(parts) > 1 else np.nan
                out[sid] = (None if pd.isna(price) or price <= 0 else float(price), group)
                stop = safe_float(parts[2]) if len(parts) > 2 else np.nan
                if pd.notna(stop) and stop > 0:
                    HOLDING_STOPS[sid] = float(stop)
    except Exception as e:
        print(f"讀取 {HOLDINGS_FILE} 失敗：{e}")
    print(f"我的持股：{len(out)} 檔（{HOLDINGS_FILE}）")
    return out


def fmt_now(c):
    t = f"{c:,.2f}".rstrip("0").rstrip(".")
    return t


# ───────────────────────── 寄信 ─────────────────────────
def send_email(subject, body):
    user, pwd, to = (os.getenv(k) for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "RECIPIENT_EMAIL"))
    if not (user and pwd and to):
        print("未設定 Gmail 環境變數，略過寄信")
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    for i in range(3):                                     # Gmail 偶爾連線失敗：重試 3 次，警示才不會漏寄
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
                s.login(user, pwd)
                s.send_message(msg)
            print("Email 已寄出")
            return True
        except smtplib.SMTPAuthenticationError as e:       # 密碼錯了重試也沒用
            print(f"寄信失敗（Gmail 帳號或應用程式密碼錯誤）：{e}")
            return False
        except Exception as e:
            print(f"寄信失敗（第 {i + 1} 次）：{e}")
            time.sleep(20 * (i + 1))
    return False


# ───────────────────────── 主流程 ─────────────────────────
def run(send_mail=True, collect_only=True):
    """每天收資料：上市上櫃股價、三大法人、融資融券、集保千張大戶（每週），更新月 K、存歷史檔、清理舊資料，最後跑大跌加碼器。
    11/15 起短線雷達（選股、追蹤、短線信）全部拿掉；collect_only 參數保留只是為了相容舊的 workflow 指令。"""
    init_db()
    print(f"=== 台股收資料 開始 {now_tw():%Y-%m-%d %H:%M} ===")
    hist_price = load_table("prices", 120)
    quotes = collect_quotes(hist_price)
    if quotes.empty:
        print("今天抓不到行情")
    else:
        upsert("prices", quotes, QUOTE_COLS)
    for frame in collect_inst():
        upsert("institutional", frame, INST_COLS)
    holders = collect_holders()
    if not holders.empty:
        upsert("holders", holders, HOLDER_COLS)
    margin = collect_margin()
    if not margin.empty:
        upsert("margin", margin, MARGIN_COLS)
    try:
        update_monthly_and_prune()
    except Exception as e:
        print(f"月 K 更新／清理失敗：{e}")
    try:                                                   # 大跌加碼器：有觸發才寄信
        import importlib.util as _iu
        _sp = _iu.spec_from_file_location("radar_dip", os.path.join(os.path.dirname(os.path.abspath(__file__)), "加碼.py"))
        _d = _iu.module_from_spec(_sp)
        _sp.loader.exec_module(_d)
        _d.check(send_mail=send_mail)
    except Exception as e:
        print(f"大跌加碼器失敗（不影響收資料）：{e}")
    print("=== 收資料完成 ===")


def main():
    ap = argparse.ArgumentParser(description="台股每日收資料（長線、加碼器用）")
    ap.add_argument("--backfill", type=int, default=0, metavar="N", help="先回補最近 N 個日曆天的上市行情與法人")
    ap.add_argument("--backfill-prices", type=int, default=0, metavar="N", help="只回補最近 N 個日曆天的上市股價（含高低價），不補法人與融資")
    ap.add_argument("--backfill-tpex", type=int, default=0, metavar="N", help="回補最近 N 個日曆天的上櫃股價與法人")
    ap.add_argument("--backfill-monthly", default="", metavar="TWSE|TPEx", help="回補上市或上櫃過去 3 年的月 K")
    ap.add_argument("--no-email", action="store_true", help="不寄信")
    ap.add_argument("--collect-only", action="store_true", help="（相容舊指令，現在本來就只收資料）")
    args = ap.parse_args()
    if args.backfill:
        init_db()
        backfill(args.backfill)
    if args.backfill_prices:
        init_db()
        backfill(args.backfill_prices, prices_only=True)
    if args.backfill_tpex:
        init_db()
        backfill_tpex(args.backfill_tpex)
    if args.backfill_monthly:
        init_db()
        backfill_monthly(args.backfill_monthly, years=3)
    run(send_mail=not args.no_email, collect_only=args.collect_only)


if __name__ == "__main__":
    main()
