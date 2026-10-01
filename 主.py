# -*- coding: utf-8 -*-
"""
台股雷達 v9.0 搭順風車版（重構＋修正版）

用法
    python tw_radar.py                 # 每日執行：抓行情＋法人 → 算訊號 → 寄信
    python tw_radar.py --no-email      # 不寄信
    python tw_radar.py --backfill 45   # 先回補最近 45 天的「上市」行情與法人，再正常執行
寄信用環境變數：GMAIL_USER、GMAIL_APP_PASSWORD、RECIPIENT_EMAIL

核心想法（沿用 v8）：跟著投信／外資一起買，買在還沒爆發前（吸籌末端、快突破／剛突破的第一根），
散戶衝進來（量爆＋急漲）時不追，土洋雙殺就下車。

v9 修正重點
  1. 法人資料不再重複計算（原本今天的資料被算兩次）
  2. 日期一律用 API 回傳的真實日期（原本全部標成「今天」，假日／備援會污染均線）
  3. 5 日漲幅 shift(4) → shift(5)
  4. 前高改用「昨天以前的 20 日高點」：0.97~1.0 = 快突破；首次站上 = 剛突破第一根
  5. 移除亂改成交金額單位的邏輯、SQLite 分批寫入、不再到處 except: pass
  6. 訊號名稱集中在 SIGNAL_TABLE 一處管理
  7. 成交金額門檻改套用在所有「買進類」訊號
"""
import argparse
import os
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

WATCHLIST = ["2383", "2368", "6197", "3293", "4763", "1808", "6919"]  # 只觀察，不加分
EXCLUDE_TOOL_STOCKS = {"2330", "2454", "2308", "3711", "2881", "2882", "2884", "2886",
                       "2891", "2892", "2880", "0050", "0056", "00878", "006208", "00919", "00929"}
MIN_DAILY_TURNOVER = 30_000_000   # 成交金額下限（元）
# 版本標記：每次改選股規則就換一個名字（環境變數 RADAR_VERSION），evaluate.py 會依版本分開算成績
VERSION = os.getenv("RADAR_VERSION", "v9.0")

QUOTE_COLS = ["date", "stock_id", "stock_name", "market", "close", "volume", "turnover"]
INST_COLS = ["date", "stock_id", "foreign_net", "trust_net", "dealer_prop", "dealer_hedge", "dealer_total", "market"]
SIGNAL_COLS = ["signal_date", "stock_id", "stock_name", "market", "signal", "reason", "score",
               "close_at_signal", "trust_5d_net", "foreign_5d_net", "ma5", "ma10", "ma20", "tech"]
_SIG_TYPES = {"score": "INTEGER", "close_at_signal": "REAL", "trust_5d_net": "INTEGER",
              "foreign_5d_net": "INTEGER", "ma5": "REAL", "ma10": "REAL", "ma20": "REAL"}
EXPECTED_COLUMNS = {   # 表 → {欄位: 型別}，用來把舊資料庫補齊
    "prices": {c: ("REAL" if c in ("close", "turnover") else "INTEGER" if c == "volume" else "TEXT")
               for c in QUOTE_COLS},
    "institutional": {c: ("TEXT" if c in ("date", "stock_id", "market") else "INTEGER") for c in INST_COLS},
    "signals": {c: _SIG_TYPES.get(c, "TEXT") for c in SIGNAL_COLS},
}

# (key, 顯示名稱, 分數)；順序 = 排序優先序
SIGNAL_TABLE = [
    ("PRE_BOTH",          "🔵 吸籌末端·土洋同買·第一根", 10),
    ("NEAR_BOTH",         "🔵 吸籌中·土洋同買·快突破", 9),
    ("FOREIGN_FIRST",     "🟣 先洋後土·跟", 9),
    ("PRE_FOREIGN",       "🟣 吸籌末端·外資先上車", 8),
    ("PRE_TRUST",         "🔵 吸籌末端·投信先上車", 7),
    ("BOTH_MULTI",        "🔵 土洋同買·多頭", 8),
    ("BOTH_FOLLOW",       "🔵 土洋同買·跟", 7),
    ("FOREIGN_MULTI",     "🟣 外資單邊·多頭跟", 6),
    ("FOREIGN_FOLLOW",    "🟣 外資單邊·跟", 5),
    ("TRUST_MULTI",       "🔵 投信單邊·多頭跟", 5),
    ("TRUST_FOLLOW",      "🔵 投信單邊·跟", 4),
    ("BOTH_WAIT",         "🟡 吸籌中·等站回", 4),
    ("TRUST_WAIT",        "🟡 投信吸籌·等站回", 3),
    ("F_BUY_T_SELL",      "⚪ 洋買土賣·觀察", 2),
    ("KNIFE",             "🟡 洋賣土買·小心接刀", 3),
    ("EXPLODED",          "🔴 已爆發·勿追", -3),
    ("OVERHEAT",          "🔴 過熱·勿追", -3),
    ("DOUBLE_SELL",       "🔴 雙殺·快下車", -10),
    ("DOUBLE_SELL_BREAK", "🔴 雙殺破線·快下車", -10),
    ("HEDGE",             "🟠 排除：避險主導", -4),
    ("EXCLUDED",          "⚪ 排除：權值／ETF", -5),
    ("NONE",              "⚪ 不列入", 0),
]
SIGNAL_LABEL = {k: n for k, n, _ in SIGNAL_TABLE}
SIGNAL_SCORE = {k: s for k, _, s in SIGNAL_TABLE}
SIGNAL_RANK = {k: i for i, (k, _, _) in enumerate(SIGNAL_TABLE)}
GROUP_BEST = {"PRE_BOTH", "NEAR_BOTH", "FOREIGN_FIRST", "PRE_FOREIGN", "PRE_TRUST"}
GROUP_FOLLOW = {"BOTH_MULTI", "BOTH_FOLLOW", "FOREIGN_MULTI", "FOREIGN_FOLLOW", "TRUST_MULTI", "TRUST_FOLLOW"}
GROUP_DANGER = {"EXPLODED", "OVERHEAT", "DOUBLE_SELL", "DOUBLE_SELL_BREAK"}


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
                     "close REAL, volume INTEGER, turnover REAL, PRIMARY KEY(date, stock_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS institutional (date TEXT, stock_id TEXT, foreign_net INTEGER, "
                     "trust_net INTEGER, dealer_prop INTEGER, dealer_hedge INTEGER, dealer_total INTEGER, "
                     "market TEXT, PRIMARY KEY(date, stock_id, market))")
        conn.execute("CREATE TABLE IF NOT EXISTS signals (signal_date TEXT, stock_id TEXT, stock_name TEXT, "
                     "market TEXT, signal TEXT, reason TEXT, score INTEGER, close_at_signal REAL, "
                     "trust_5d_net INTEGER, foreign_5d_net INTEGER, ma5 REAL, ma10 REAL, ma20 REAL, tech TEXT, "
                     "PRIMARY KEY(signal_date, stock_id))")
        # 舊版資料庫的表可能缺欄位（CREATE IF NOT EXISTS 不會補），這裡自動補上
        for table, cols in EXPECTED_COLUMNS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                    print(f"資料庫升級：{table} 補上欄位 {name}")
        # 帶版本的訊號紀錄：主鍵含 version，不同版本同一天、同一檔股票不會互相覆蓋
        conn.execute("CREATE TABLE IF NOT EXISTS signal_log (signal_date TEXT, stock_id TEXT, version TEXT, "
                     "stock_name TEXT, market TEXT, signal TEXT, reason TEXT, score INTEGER, "
                     "close_at_signal REAL, trust_5d_net INTEGER, foreign_5d_net INTEGER, "
                     "ma5 REAL, ma10 REAL, ma20 REAL, tech TEXT, PRIMARY KEY(signal_date, stock_id, version))")


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
    date_col = "signal_date" if table == "signals" else "date"
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
                     "turnover": safe_float(it.get("成交金額"))})
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
                     "turnover": safe_float(it.get("TradeValue"))})
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
                     "turnover": safe_float(pick(it, "TransactionAmount", "TradeValue", "Amount"))})
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
    print(f"TPEx 法人欄位對應：外資={f_key}｜投信={t_key}")
    date = parse_date(pick(data[0], "Date")) or guess_last_close_date()
    rows = []
    for it in data:
        sid = normalize_stock_id(pick(it, "SecuritiesCompanyCode", "Code") or "")
        if not is_stock_id(sid):
            continue
        rows.append({"date": date, "stock_id": sid, "foreign_net": safe_int(it.get(f_key)),
                     "trust_net": safe_int(it.get(t_key)), "dealer_prop": 0, "dealer_hedge": 0,
                     "dealer_total": 0, "market": "TPEx"})
    return pd.DataFrame(rows, columns=INST_COLS)


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


def backfill(days):
    """回補上市（TWSE）歷史行情與法人。TPEx 的歷史資料沒有穩定的公開 API，只能每天累積。"""
    with db() as conn:
        have_p = {r[0] for r in conn.execute("SELECT DISTINCT date FROM prices WHERE market='TWSE'")}
        have_i = {r[0] for r in conn.execute("SELECT DISTINCT date FROM institutional WHERE market='TWSE'")}
    for d in weekdays_back(days):
        iso = fmt_date(d)
        if iso not in have_p:
            try:
                n = upsert("prices", fetch_twse_quotes(d), QUOTE_COLS)
                print(f"回補行情 {iso}：{n} 檔" if n else f"{iso} 無行情（休市？）")
            except Exception as e:
                print(f"回補行情 {iso} 失敗：{e}")
            time.sleep(2)
        if iso not in have_i:
            try:
                n = upsert("institutional", fetch_twse_inst(d), INST_COLS)
                print(f"回補法人 {iso}：{n} 檔" if n else f"{iso} 無法人資料")
            except Exception as e:
                print(f"回補法人 {iso} 失敗：{e}")
            time.sleep(2)


# ───────────────────────── 特徵計算 ─────────────────────────
def grp_roll(s, key, n, fn):
    """依 key 分組做 rolling，結果對齊回 s 的 index。"""
    return getattr(s.groupby(key).rolling(n, min_periods=n), fn)().reset_index(level=0, drop=True)


def make_price_features(full):
    if full is None or full.empty:
        print("價格資料空")
        return pd.DataFrame()
    df = full.copy()
    for c in ("close", "volume", "turnover"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = (df.dropna(subset=["close"]).drop_duplicates(["stock_id", "date"], keep="last")
            .sort_values(["stock_id", "date"]).reset_index(drop=True))
    key, close, vol = df["stock_id"], df["close"], df["volume"]

    df["ma5"] = grp_roll(close, key, 5, "mean")
    df["ma10"] = grp_roll(close, key, 10, "mean")
    df["ma20"] = grp_roll(close, key, 20, "mean")
    prev_close = close.groupby(key).shift(1)
    df["return_5d"] = (close / close.groupby(key).shift(5) - 1) * 100

    # 前 20 日（不含今天）的高低點：用來判斷盤整與突破
    df["prev_high20"] = grp_roll(prev_close, key, 20, "max")
    prev_low20 = grp_roll(prev_close, key, 20, "min")
    df["range_20d"] = (df["prev_high20"] / prev_low20 - 1) * 100
    df["avg_vol_5d"] = grp_roll(vol.groupby(key).shift(1), key, 5, "mean")
    df["vol_ratio_5d"] = vol / df["avg_vol_5d"]

    df["dist_ma10"] = (close / df["ma10"] - 1) * 100
    df["dist_ma20"] = (close / df["ma20"] - 1) * 100
    df["ratio_prev_high"] = close / df["prev_high20"]

    df["above_ma10"] = close > df["ma10"]
    df["above_ma20"] = close > df["ma20"]
    df["is_multi_up"] = (df["ma5"] > df["ma10"]) & (df["ma10"] > df["ma20"]) & (close > df["ma5"])
    df["is_consolidation"] = df["range_20d"] < 18                       # 盤整：前 20 日高低差 < 18%
    df["near_high"] = (df["ratio_prev_high"] >= 0.97) & (df["ratio_prev_high"] < 1.0)   # 快突破
    prev_ph = df["prev_high20"].groupby(key).shift(1)
    df["first_break"] = (close > df["prev_high20"]) & (prev_close <= prev_ph)           # 首次站上前高
    df["pre_breakout"] = (df["is_consolidation"] & (df["near_high"] | df["first_break"])
                          & (df["vol_ratio_5d"] < 2.0) & (df["return_5d"] < 8))          # 還沒爆量
    df["exploded"] = (df["vol_ratio_5d"] >= 2.5) & (df["return_5d"] >= 12)             # 散戶衝進來
    df["overheat"] = (df["dist_ma20"] > 12) | (df["dist_ma10"] > 10) | (df["return_5d"] > 15)

    latest = df["date"].max()
    out = df[df["date"] == latest].copy()
    print(f"價格特徵：資料日 {latest}，{len(out)} 檔｜MA20 有值 {int(out['ma20'].notna().sum())} 檔｜"
          f"吸籌末端 {int(out['pre_breakout'].sum())}｜已爆發 {int(out['exploded'].sum())}")
    if out["ma20"].notna().sum() == 0:
        print("⚠ 歷史不足 20 個交易日，MA20／前高都算不出來。請先跑 --backfill 回補，或讓程式每天累積。")
    return out


def make_inst_features(inst, price_feat):
    if inst is None or inst.empty:
        print("法人資料空")
        return pd.DataFrame()
    df = inst.copy()
    for c in ("foreign_net", "trust_net", "dealer_hedge"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df = df.drop_duplicates(["date", "stock_id"], keep="last").sort_values(["stock_id", "date"])
    n_days = df["date"].nunique()
    needed = 1 if n_days < 5 else 2            # 資料還不夠 5 天時放寬門檻
    last5 = df[df.groupby("stock_id").cumcount(ascending=False) < 5].copy()
    last5["t_pos"] = (last5["trust_net"] > 0).astype(int)
    last5["f_pos"] = (last5["foreign_net"] > 0).astype(int)
    last5["h_abs"] = last5["dealer_hedge"].abs()
    agg = last5.groupby("stock_id").agg(
        n_days=("date", "size"), trust_5d=("trust_net", "sum"), foreign_5d=("foreign_net", "sum"),
        trust_days=("t_pos", "sum"), foreign_days=("f_pos", "sum"), hedge_abs=("h_abs", "sum")).reset_index()
    if price_feat is not None and not price_feat.empty:
        agg = agg.merge(price_feat[["stock_id", "avg_vol_5d"]], on="stock_id", how="left")
    else:
        agg["avg_vol_5d"] = np.nan
    denom = agg["avg_vol_5d"] * agg["n_days"]
    h_ratio = agg["hedge_abs"] / denom.where(denom > 0)
    agg["hedge_dominant"] = ((h_ratio >= 0.03) |
                             ((agg["hedge_abs"] > 0) &
                              (agg["hedge_abs"] >= agg["trust_5d"].abs() + agg["foreign_5d"].abs())))
    agg["trust_acc"] = (agg["trust_days"] >= needed) & (agg["trust_5d"] > 0) & ~agg["hedge_dominant"]
    agg["foreign_acc"] = (agg["foreign_days"] >= needed) & (agg["foreign_5d"] > 0) & (agg["foreign_5d"].abs() >= 100_000)
    print(f"法人特徵：{len(agg)} 檔｜資料 {n_days} 天（門檻 {needed} 天）｜避險主導 {int(agg['hedge_dominant'].sum())}")
    return agg[["stock_id", "trust_5d", "foreign_5d", "trust_days", "foreign_days",
                "trust_acc", "foreign_acc", "hedge_dominant"]]


# ───────────────────────── 分類 ─────────────────────────
def classify(r):
    """回傳 (訊號 key, 原因)。"""
    if str(r["stock_id"]) in EXCLUDE_TOOL_STOCKS:
        return "EXCLUDED", "高權值/ETF"
    if r["hedge_dominant"]:
        return "HEDGE", "避險量大"

    t5, f5 = r["trust_5d"], r["foreign_5d"]
    t_acc, f_acc = r["trust_acc"], r["foreign_acc"]
    both = t_acc and f_acc
    above20, multi, pre = r["above_ma20"], r["is_multi_up"], r["pre_breakout"]

    # 賣點類：散戶衝進來／過熱／大戶一起賣
    if r["exploded"] and not both:
        return "EXPLODED", "量爆2.5倍+5日>12% 散戶衝進來的地方 搭車的要下車了"
    if r["overheat"] and not both and r["return_5d"] > 15:
        return "OVERHEAT", "乖離過大等拉回"
    if t5 < 0 and f5 < 0:
        if pd.notna(r["ma10"]) and not r["above_ma10"]:
            return "DOUBLE_SELL_BREAK", "洋賣土賣+跌破MA10 大戶下車了 快跟著下車"
        return "DOUBLE_SELL", "洋賣土賣 一起賣 大戶下車了"

    # 買進類：成交金額太小的不列
    if not r["turnover"] >= MIN_DAILY_TURNOVER:
        return "NONE", "未達標"

    if both:
        if pre:
            return "PRE_BOTH", "大戶吸籌末端+土洋同買+快突破/剛突破前高 搭順風車上車點"
        if r["near_high"] and r["is_consolidation"]:
            return "NEAR_BOTH", "盤整吸籌+土洋同買+近前高 準備第一根"
        if above20 and multi:
            return "BOTH_MULTI", "土洋同買+多頭排列 搭車中"
        if above20:
            if abs(f5) > abs(t5) * 1.8:
                return "FOREIGN_FIRST", "外資先佈局投信後跟 大戶先上車了"
            return "BOTH_FOLLOW", "土洋同買 跟著大戶"
        return "BOTH_WAIT", "土洋同買但未站上月線 等站回MA20再跟"
    if f_acc:
        if pre:
            return "PRE_FOREIGN", "外資先吸籌+吸籌末端+快突破 大戶先上車了"
        if above20 and multi:
            return "FOREIGN_MULTI", "外資佈局+多頭排列 跟著大戶"
        if above20:
            return "FOREIGN_FOLLOW", "外資佈局+站上月線 跟著"
        return "F_BUY_T_SELL", ("外資買投信賣 大戶意見分歧 觀察" if t5 < 0 else "外資買、投信未跟 觀察")
    if t_acc:
        if pre:
            return "PRE_TRUST", "投信先吸籌+吸籌末端+快突破 大戶先上車了"
        if above20 and multi:
            return "TRUST_MULTI", "投信佈局+多頭排列 跟著大戶"
        if above20:
            return "TRUST_FOLLOW", "投信佈局+站上月線 跟著"
        if f5 < 0:
            return "KNIFE", "外資賣投信買 小心投信接刀 大戶不同調"
        return "TRUST_WAIT", "投信吸籌但未站上月線 等站回"
    return "NONE", "未達標"


BOOL_COLS = ["trust_acc", "foreign_acc", "hedge_dominant", "is_multi_up", "above_ma10", "above_ma20",
             "is_consolidation", "near_high", "first_break", "pre_breakout", "exploded", "overheat"]
NUM0_COLS = ["trust_5d", "foreign_5d", "trust_days", "foreign_days"]


def tech_text(r):
    vals = [r["ma5"], r["ma10"], r["ma20"], r["prev_high20"]]
    if any(pd.isna(v) for v in vals):
        return ""
    return f"MA5:{vals[0]:.1f} MA10:{vals[1]:.1f} MA20:{vals[2]:.1f} 前20高:{vals[3]:.1f}"


def build_radar(price_feat, inst_feat):
    if price_feat is None or price_feat.empty:
        print("radar 空")
        return pd.DataFrame()
    df = price_feat.copy()
    if inst_feat is not None and not inst_feat.empty:
        df = df.merge(inst_feat, on="stock_id", how="left")
    for c in BOOL_COLS:
        df[c] = df[c].eq(True) if c in df.columns else False
    for c in NUM0_COLS:
        df[c] = df[c].fillna(0) if c in df.columns else 0
    cls = df.apply(classify, axis=1, result_type="expand")
    df["signal_key"], df["reason"] = cls[0], cls[1]
    df["signal"] = df["signal_key"].map(SIGNAL_LABEL)
    df["score"] = df["signal_key"].map(SIGNAL_SCORE)
    df["rank"] = df["signal_key"].map(SIGNAL_RANK)
    df["tech"] = df.apply(tech_text, axis=1)
    df = df.sort_values(["rank", "turnover"], ascending=[True, False]).reset_index(drop=True)
    print("訊號分布：", df["signal_key"].value_counts().to_dict())
    return df


# ───────────────────────── 輸出：存檔／Email ─────────────────────────
def save_signals(radar, data_date):
    sig = pd.DataFrame({
        "signal_date": data_date, "stock_id": radar["stock_id"], "stock_name": radar["stock_name"],
        "market": radar["market"], "signal": radar["signal"], "reason": radar["reason"],
        "score": radar["score"], "close_at_signal": radar["close"], "trust_5d_net": radar["trust_5d"],
        "foreign_5d_net": radar["foreign_5d"], "ma5": radar["ma5"], "ma10": radar["ma10"],
        "ma20": radar["ma20"], "tech": radar["tech"]})
    with db() as conn:
        conn.execute("DELETE FROM signals WHERE signal_date=?", (data_date,))
    n = upsert("signals", sig, SIGNAL_COLS)
    print(f"signals 寫入 {n} 筆")
    log = sig.assign(version=VERSION)
    with db() as conn:
        conn.execute("DELETE FROM signal_log WHERE signal_date=? AND version=?", (data_date, VERSION))
    upsert("signal_log", log, ["version"] + SIGNAL_COLS)
    print(f"signal_log 寫入 {len(log)} 筆（版本 {VERSION}）")


def _f(x, spec="{:.1f}"):
    return "-" if pd.isna(x) else spec.format(x)


def fmt_rows(df, n):
    if df.empty:
        return "（無）"
    out = []
    for _, r in df.head(n).iterrows():
        t = f"{int(round(float(r['trust_5d']) / 1000)):+,d}"
        f = f"{int(round(float(r['foreign_5d']) / 1000)):+,d}"
        out.append(f"{r['signal']}｜{r['stock_id']} {r['stock_name']}｜{r['reason']}\n"
                   f"   投信{t}張 外資{f}張 收盤{_f(r['close'])} 量比{_f(r['vol_ratio_5d'])}"
                   + (f"｜{r['tech']}" if r["tech"] else ""))
    return "\n".join(out)


LEGEND = """【搭順風車邏輯 - 買在還沒爆發前】
🔵 吸籌末端·土洋同買·第一根：盤整末端+土洋同買+快突破（收盤在前20日高點97%~100%）或首次站上前高+量比<2 = 上車點
🔵 吸籌中·土洋同買·快突破：盤整吸籌+土洋同買+近前高
🟣 先洋後土·跟：外資先佈局投信後跟，大戶先上車了
🟣/🔵 吸籌末端·外資/投信先上車：單邊大戶先吸籌+快突破
🟡 吸籌中·等站回：大戶還在吸但未站上月線，等站回MA20再跟
⚪ 洋買土賣·觀察 / 🟡 洋賣土買·小心接刀：大戶意見分歧
🔴 已爆發·勿追／過熱·勿追：量爆2.5倍+5日>12%，散戶衝進來的地方，不是買點
🔴 雙殺·快下車：洋賣土賣，大戶下車了
WATCHLIST（只觀察、不加分）：""" + " ".join(WATCHLIST) + """
技術：MA5=週線 MA10=10日 MA20=月線 多頭排列=5>10>20 盤整=前20日高低差<18%
成交金額低於3000萬的標的不列入買進類訊號"""


def build_email_body(radar, data_date):
    k = radar["signal_key"]
    best, follow = radar[k.isin(GROUP_BEST)], radar[k.isin(GROUP_FOLLOW)]
    danger = radar[k.isin(GROUP_DANGER)]
    watch = radar[radar["stock_id"].isin(WATCHLIST)]
    line = "━━━━━━━━━━━━"
    return (f"台股雷達 v9.0 搭順風車版｜資料日 {data_date}\n{LEGEND}\n\n"
            f"{line}\n🔵🟣 吸籌末端·第一根 {len(best)} 檔\n{fmt_rows(best, 30)}\n\n"
            f"{line}\n🔵🟣 跟著大戶 {len(follow)} 檔\n{fmt_rows(follow, 20)}\n\n"
            f"{line}\n🔴 已爆發／過熱／雙殺 {len(danger)} 檔（只列前15）\n{fmt_rows(danger, 15)}\n\n"
            f"{line}\n👀 WATCHLIST 觀察名單（不加分）\n{fmt_rows(watch, 10)}\n")


def send_email(subject, body):
    user, pwd, to = (os.getenv(k) for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "RECIPIENT_EMAIL"))
    if not (user and pwd and to):
        print("未設定 Gmail 環境變數，略過寄信")
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pwd)
            s.send_message(msg)
    except Exception as e:
        print(f"寄信失敗：{e}")
        return False
    print("Email 已寄出")
    return True


# ───────────────────────── 主流程 ─────────────────────────
def run(send_mail=True):
    init_db()
    print(f"=== 台股雷達 v9.0 開始 {now_tw():%Y-%m-%d %H:%M} ===")
    hist_price = load_table("prices", 120)
    quotes = collect_quotes(hist_price)
    if quotes.empty:
        print("今天抓不到行情，改用資料庫裡最新的資料計算")
    else:
        upsert("prices", quotes, QUOTE_COLS)
    for frame in collect_inst():
        upsert("institutional", frame, INST_COLS)

    pf = make_price_features(load_table("prices", 120))
    inf = make_inst_features(load_table("institutional", 30), pf)
    radar = build_radar(pf, inf)
    if radar.empty:
        print("沒有可用資料，結束")
        raise SystemExit(1)

    data_date = str(pf["date"].max())
    save_signals(radar, data_date)
    try:
        radar.to_csv(os.path.join(OUTPUT_DIR, f"radar_{data_date}.csv"), index=False, encoding="utf-8-sig")
    except OSError as e:
        print(f"寫 CSV 失敗：{e}")
    if send_mail:
        send_email(f"雷達 v9.0搭順風車·第一根｜{data_date}", build_email_body(radar, data_date))
    print("=== 完成 ===")


def main():
    ap = argparse.ArgumentParser(description="台股雷達 v9.0")
    ap.add_argument("--backfill", type=int, default=0, metavar="N", help="先回補最近 N 個日曆天的上市行情與法人")
    ap.add_argument("--no-email", action="store_true", help="不寄信")
    args = ap.parse_args()
    if args.backfill:
        init_db()
        backfill(args.backfill)
    run(send_mail=not args.no_email)


if __name__ == "__main__":
    main()
