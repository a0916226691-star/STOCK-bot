# -*- coding: utf-8 -*-
"""
台股雷達 v9.0 搭順風車版（重構＋修正版）
v10 新增：型態分類（🚀突破型／🎯低接型／📦整理股）分區列在信件最前面，附參考進場價與停損價（見 classify_setup）

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
import io
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

# 我真正買的股票：放在「股票追蹤」檔（每行「股票代號,買進價」），程式每天幫你檢查該不該賣。
# 檔名可以是 股票追蹤.csv／股票追蹤.txt／股票追蹤（沒有副檔名）／holdings.csv，放在專案最外層（跟 主.py 同一層）
HOLDINGS_FILES = [os.getenv("RADAR_HOLDINGS")] if os.getenv("RADAR_HOLDINGS") else [
    "股票追蹤.csv", "股票追蹤.txt", "股票追蹤", "holdings.csv"]
HOLDINGS_FILE = HOLDINGS_FILES[0]

# ── 追蹤規則（一檔股票出現後：觀察中 → 買進 → 持有中 → 賣出）──────────────
TRACK_START = {"PRE_BOTH", "NEAR_BOTH", "FOREIGN_FIRST", "PRE_FOREIGN", "PRE_TRUST", "BOTH_WAIT", "TRUST_WAIT"}
WATCH_MAX_DAYS = 20        # 觀察超過幾個交易日還沒到買點，就放棄
COOLDOWN_DAYS = 14         # 賣出或放棄之後，幾個日曆天內不重複加入追蹤（約 10 個交易日）
STOP_LOSS_PCT = 7.0        # 停損：虧損達此 % 就賣
BUY_MAX_VOL_RATIO = 2.5    # 買進條件：量比要小於此（還沒爆量）
BUY_MAX_RET5 = 12.0        # 買進條件：5日漲幅要小於此 %（還沒噴）
# 賣出條件「已爆發」的標準（量比 >= 2.5 且 5日漲 >= 12%）定義在 make_price_features 的 exploded
EXCLUDE_TOOL_STOCKS = {"2330", "2454", "2308", "3711", "2881", "2882", "2884", "2886",
                       "2891", "2892", "2880", "0050", "0056", "00878", "006208", "00919", "00929"}
MAX_PRICE = float(os.getenv("RADAR_MAX_PRICE", "1e9"))   # 股價上限（元）：不設上限，買不起整張就買零股
MIN_PRICE = float(os.getenv("RADAR_MIN_PRICE", "20"))    # 股價下限（元）：低於此的雞蛋水餃股不列入可買／觀察
ACCUM_MIN_POS = 50.0       # 佈局型：股價在箱子中上段（箱底 0%～箱頂 100%）
ACCUM_MAX_VOL_RATIO = 1.2  # 佈局型：量還沒放大（散戶還沒注意到）
DUMP_GIVEBACK = 0.5        # 單日大賣：當天賣超吐掉前幾天累積買超的此比例以上 → 直接🔴
DUMP_VOL_PCT = 10.0        # 單日大賣：當天法人賣超佔當天成交量此 % 以上 → 直接🔴
DUMP_MIN_VOL_PCT = 3.0     # 吐回比例那條，賣超至少也要佔成交量此 %（避免幾張就觸發）
DROP_VOL_RATIO = 2.0       # 爆量下跌：成交量至少是前 5 日均量的此倍數
DROP_PCT = 3.0             # 爆量下跌：當天跌幅至少此 % → 🟡
BELOW_COST_PCT = 3.0       # 跌破法人成本此 % → 🔴（法人自己都套牢了）
SHADOW_VOL_RATIO = 2.0     # 爆量長上影線：成交量至少是前 5 日均量的此倍數
SHADOW_MIN_PCT = 3.0       # 爆量長上影線：上影線（最高價到收盤）至少此 %
SHADOW_MAX_POS = 0.5       # 爆量長上影線：收盤落在當天高低區間的下半段（0＝最低、1＝最高）
DUMP_AVGVOL_PCT = 15.0     # 單日大賣：當天法人賣超超過「前 5 日平均成交量」此 % → 直接🔴（倒貨當天爆量也不會被稀釋）
INST_MIN_PART = 5.0        # 法人參與度：土洋 5 日買超張數至少要佔 5 日成交量的此 %（避免主力／投機大戶主導的股票）
MIN_DAILY_TURNOVER = 30_000_000   # 成交金額下限（元）
# ── 型態分類（v10）：突破型／低接型／整理股，報告分開列 ──────────────
MA20_SLOPE_MIN = 1.5       # 強勢：月線 5 天內至少上升此 %（低接型要求）
BREAKOUT_VOL_RATIO = 1.5   # 突破型：量要大於 5 日均量的幾倍
BREAKOUT_STOP_PCT = 3.0    # 突破型停損：跌回箱頂下方此 %
PULLBACK_MAX_K = 50        # 低接型：K 值要低於此（還在低檔）
PULLBACK_MAX_VOL_RATIO = 0.8   # 低接型：量縮（小於 5 日均量的 0.8 倍）
PULLBACK_STOP_PCT = 3.0    # 低接型停損：跌破月線下方此 %
BOX_DAYS = 30              # 整理股：看最近幾個交易日的區間
BOX_MAX_RANGE = 20.0       # 整理股：區間高低差小於此 %
BOX_FLAT_SLOPE = 1.0       # 整理股：月線 5 日變化在 ±此 % 內（走平）
OVEREXTEND_PCT = 10.0      # 乖離月線超過此 % → 排序往後、標 ⚠
# ── 版本：同一次執行可以同時追蹤好幾個「規則版本」，成績單會並排比較 ──────────────
# 環境變數 RADAR_VERSIONS（逗號分隔），第一個是主要版本：信件詳細內容與持股檢查都用它；其他版本在信裡只列一行對照。
VERSIONS = [v.strip() for v in os.getenv("RADAR_VERSIONS", "v9.2,v9.1,v9.0,v9.3").split(",") if v.strip()]
VERSION = VERSIONS[0]
SIGNAL_VERSION = os.getenv("RADAR_SIGNAL_VERSION", "v9.0")   # 「訊號分類」本身的版本標記（v9.0／v9.1／v9.2 的訊號分類相同）
# 每個版本開哪些額外條件；沒列在這裡的版本名稱一律當作 v9.0（不開額外條件）
RULESETS = {
    "v9.0": {"dealer": False, "big": False},
    "v9.1": {"dealer": True, "big": True},    # v9.0 ＋ 自營商（三大法人合計）＋ 千張大戶持股變化
    "v9.2": {"dealer": True, "big": True, "market": True, "margin": True},   # v9.1 ＋ 大盤環境過濾 ＋ 融資（散戶）過熱
    "v9.3": {"dealer": True, "big": True, "market": True, "margin": True, "early": True},   # v9.2 ＋ 提早買（快突破就買，不等突破）
}
RULE_DEFAULTS = {"dealer": False, "big": False, "market": False, "margin": False, "early": False}
EARLY_BUY_RATIO = 0.99     # 提早買：收盤價達到前 20 日高點的 99% 就買（離前高不到 1%）
# 大盤環境：全市場「收盤站上月線的股票」占幾 %。低於門檻＝大盤偏弱，v9.2 暫停新的買進
MARKET_MIN_BREADTH = 40.0
# 融資（散戶借錢買股）：5 個交易日增加太多＝散戶衝進來
MARGIN_BUY_MAX_CHG = 10.0    # 買進：融資餘額 5 日增加不能超過此 %（資料不足時不擋）
MARGIN_SELL_CHG = 20.0       # 賣出：融資餘額 5 日增加達此 % 就賣
MARGIN_MIN_BAL = 1000        # 融資餘額太小（張）的股票，百分比會失真，不判斷
MARGIN_COLS = ["date", "stock_id", "margin_bal", "short_bal", "market"]
# 千張大戶（集保「股權分散表」每週公布）
BIG_HOLDER_LEVEL = 15      # 持股分級 15 ＝ 1,000,001 股（1000 張）以上
BIG_BUY_MIN_DELTA = 0.0    # 買進：千張大戶持股比例本週增減（百分點）要大於此；資料還不夠算增減時不擋
BIG_SELL_DELTA = -0.5      # 賣出／放棄：千張大戶持股比例本週減少達此百分點（例如 -0.5 ＝ 減少 0.5 個百分點）
HOLDER_COLS = ["date", "stock_id", "big_ratio", "big_people"]
QFII_COLS = ["date", "stock_id", "qfii_ratio", "qfii_shares"]     # 外資及陸資持股比率（%）、持有股數

QUOTE_COLS = ["date", "stock_id", "stock_name", "market", "close", "volume", "turnover", "high", "low", "open"]
INST_COLS = ["date", "stock_id", "foreign_net", "trust_net", "dealer_prop", "dealer_hedge", "dealer_total", "market"]
SIGNAL_COLS = ["signal_date", "stock_id", "stock_name", "market", "signal", "reason", "score",
               "close_at_signal", "trust_5d_net", "foreign_5d_net", "ma5", "ma10", "ma20", "tech"]
_SIG_TYPES = {"score": "INTEGER", "close_at_signal": "REAL", "trust_5d_net": "INTEGER",
              "foreign_5d_net": "INTEGER", "ma5": "REAL", "ma10": "REAL", "ma20": "REAL"}
EXPECTED_COLUMNS = {   # 表 → {欄位: 型別}，用來把舊資料庫補齊
    "prices": {c: ("REAL" if c in ("close", "turnover", "high", "low", "open") else "INTEGER" if c == "volume" else "TEXT")
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
                     "close REAL, volume INTEGER, turnover REAL, high REAL, low REAL, PRIMARY KEY(date, stock_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS monthly (month TEXT, stock_id TEXT, market TEXT, open REAL, high REAL, "
                     "low REAL, close REAL, volume REAL, PRIMARY KEY(month, stock_id))")
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
        # 追蹤紀錄：每一次「出現 → 買進 → 賣出」是一筆
        conn.execute("CREATE TABLE IF NOT EXISTS tracking (version TEXT, stock_id TEXT, first_seen TEXT, "
                     "stock_name TEXT, status TEXT, start_signal TEXT, entry_date TEXT, entry_price REAL, "
                     "exit_date TEXT, exit_price REAL, exit_reason TEXT, last_date TEXT, "
                     "days_watched INTEGER, days_held INTEGER, PRIMARY KEY(version, stock_id, first_seen))")
        # 融資融券餘額（每日；單位：張）
        conn.execute("CREATE TABLE IF NOT EXISTS margin (date TEXT, stock_id TEXT, margin_bal REAL, "
                     "short_bal REAL, market TEXT, PRIMARY KEY(date, stock_id, market))")
        # 每天信裡推薦的股票（A／B 兩套誰比較好，之後用這張表算成績）
        conn.execute("CREATE TABLE IF NOT EXISTS picks (date TEXT, stock_id TEXT, stock_name TEXT, grp TEXT, "
                     "close REAL, note TEXT, PRIMARY KEY(date, stock_id, grp))")
        # 集保千張大戶持股（每週一筆）
        conn.execute("CREATE TABLE IF NOT EXISTS foreign_hold (date TEXT, stock_id TEXT, qfii_ratio REAL, "
                     "qfii_shares REAL, PRIMARY KEY(date, stock_id))")
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
    fields, raw = find_table(j, ["股票代號"])
    id_name = "股票代號"
    if not fields:
        fields, raw = find_table(j, ["證券代號"])
        id_name = "證券代號"
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


def make_margin_features(m):
    """每檔股票：最新融資餘額，以及 5 個交易日的增減 %（資料不到 4 天或餘額太小就是空值，不影響判斷）。"""
    cols = ["stock_id", "margin_bal", "margin_chg5"]
    if m is None or m.empty:
        return pd.DataFrame(columns=cols)
    m = m.drop_duplicates(["date", "stock_id", "market"], keep="last").sort_values(["stock_id", "date"]).copy()
    m["margin_bal"] = pd.to_numeric(m["margin_bal"], errors="coerce")

    def chg(s):
        v = s.dropna().values
        k = min(5, len(v) - 1)
        if k < 3 or v[-1 - k] <= 0 or v[-1] < MARGIN_MIN_BAL:
            return np.nan
        return (v[-1] / v[-1 - k] - 1) * 100

    g = m.groupby("stock_id")["margin_bal"]
    out = pd.DataFrame({"margin_bal": g.last(), "margin_chg5": g.apply(chg)}).reset_index()
    print(f"融資特徵：{len(out)} 檔｜可算5日增減 {int(out['margin_chg5'].notna().sum())} 檔")
    return out[cols]


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


def make_holder_features(h):
    """每檔股票：最新一週的千張大戶佔比，以及相較上一週的增減（百分點）。只有一週資料時增減為空。"""
    cols = ["stock_id", "big_ratio", "big_delta", "big_date"]
    if h is None or h.empty:
        return pd.DataFrame(columns=cols)
    h = h.drop_duplicates(["date", "stock_id"], keep="last").sort_values(["stock_id", "date"]).copy()
    h["prev"] = h.groupby("stock_id")["big_ratio"].shift(1)
    last = h.groupby("stock_id").tail(1).copy()
    last["big_delta"] = last["big_ratio"] - last["prev"]
    last = last.rename(columns={"date": "big_date"})
    n_delta = int(last["big_delta"].notna().sum())
    print(f"千張大戶特徵：{len(last)} 檔｜可算週增減 {n_delta} 檔（資料日 {last['big_date'].max()}）")
    return last[cols]


# ───────────────────────── 抓資料：外資持股比例（長線追蹤用） ─────────────────────────
def fetch_twse_qfii(d):
    """上市「外資及陸資投資持股統計」（指定日期 d=YYYYMMDD）。休市日回傳空表。"""
    empty = pd.DataFrame(columns=QFII_COLS)
    j = get_json("https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS",
                 {"response": "json", "date": d, "selectType": "ALLBUT0999"}, timeout=30)
    if j.get("stat") != "OK":
        return empty
    fields, raw = find_table(j, ["證券代號"])
    if not fields:
        print("MI_QFIIS 找不到資料表，API 格式可能改了；回應欄位：", list(j.keys()))
        return empty
    c_ratio = next((f for f in fields if "持股比率" in f and "上限" not in f and "尚可" not in f), None)
    c_shares = next((f for f in fields if "持有股數" in f), None)
    if not c_ratio:
        print("MI_QFIIS 找不到外資持股比率欄位；欄位：", fields)
        return empty
    date, rows = fmt_date(d), []
    for r in raw:
        it = dict(zip(fields, r))
        sid = normalize_stock_id(it.get("證券代號", ""))
        ratio = safe_float(it.get(c_ratio))
        if is_stock_id(sid) and not pd.isna(ratio):
            rows.append({"date": date, "stock_id": sid, "qfii_ratio": ratio,
                         "qfii_shares": safe_float(it.get(c_shares)) if c_shares else np.nan})
    return pd.DataFrame(rows, columns=QFII_COLS)


def collect_qfii(max_days=6):
    """每天補最近還沒存過的外資持股（最多 max_days 個交易日）。"""
    got = 0
    try:
        with db() as conn:
            have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM foreign_hold")}
        for d in recent_trading_days(10):
            if got >= max_days:
                break
            if fmt_date(d) in have:
                continue
            q = fetch_twse_qfii(d)
            if not q.empty:
                upsert("foreign_hold", q, QFII_COLS)
                got += 1
            time.sleep(1.5)
        print(f"外資持股：新增 {got} 天")
    except Exception as e:
        print(f"外資持股失敗：{e}")


def backfill_qfii(days):
    """回補外資持股比例：只補資料庫裡有上市行情、但還沒有外資持股的交易日。"""
    cutoff = (now_tw() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db() as conn:
        trade = [r[0] for r in conn.execute("SELECT DISTINCT date FROM prices WHERE market='TWSE' AND date >= ? "
                                            "ORDER BY date DESC", (cutoff,))]
        have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM foreign_hold")}
    todo = [d for d in trade if d not in have]
    print(f"外資持股回補：{len(todo)} 個交易日要補")
    fails = 0
    for iso in todo:
        try:
            n = upsert("foreign_hold", fetch_twse_qfii(iso.replace("-", "")), QFII_COLS)
            print(f"回補外資持股 {iso}：{n} 檔")
            fails = 0
        except Exception as e:
            fails += 1
            print(f"回補外資持股 {iso} 失敗：{e}")
            if fails >= 5:
                print("連續失敗 5 次，先停止（可能被擋），下次再補")
                break
        time.sleep(2.5)


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
KEEP_DAILY_DAYS = 380      # 每日股價只留這麼多天（資料庫不能超過 GitHub 100MB 上限）；更早的只留月 K


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


def backfill_monthly(market, years=3):
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
    cutoff = (now_tw() - timedelta(days=KEEP_DAILY_DAYS)).strftime("%Y-%m-%d")
    with db() as conn:
        n1 = conn.execute("DELETE FROM prices WHERE date < ?", (cutoff,)).rowcount
        n2 = conn.execute("DELETE FROM foreign_hold WHERE date < ?", (cutoff,)).rowcount
    if n1 or n2:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("VACUUM")
        print(f"清掉 {cutoff} 以前的每日股價 {n1} 筆、外資持股 {n2} 筆")


def monthly_bull(sids=None):
    """月 K 多頭：月收盤站上 5 月線、5 月線在 10 月線上面、5 月線往上。回傳 {代號: True/False}（資料不夠的不回傳）。"""
    with db() as conn:
        m = pd.read_sql("SELECT month, stock_id, close FROM monthly ORDER BY stock_id, month", conn, dtype={"stock_id": str})
    out = {}
    for sid, g in m.groupby("stock_id"):
        if sids is not None and sid not in sids:
            continue
        cl = g["close"].astype(float).to_numpy()
        if len(cl) < 11:
            continue
        ma5 = pd.Series(cl).rolling(5).mean().to_numpy()
        ma10 = pd.Series(cl).rolling(10).mean().to_numpy()
        out[sid] = bool(cl[-1] > ma5[-1] and ma5[-1] > ma10[-1] and ma5[-1] > ma5[-2])
    return out


# ───────────────────────── 特徵計算 ─────────────────────────
def grp_roll(s, key, n, fn):
    """依 key 分組做 rolling，結果對齊回 s 的 index。"""
    return getattr(s.groupby(key).rolling(n, min_periods=n), fn)().reset_index(level=0, drop=True)


def calc_kd(df, n=9):
    """KD(9,3)。有最高／最低價就用；舊資料沒有高低價時用收盤價代替（會略有誤差，資料累積後自然變準）。
    df 必須已依 stock_id、date 排序。"""
    close = df["close"]
    hi = pd.to_numeric(df["high"], errors="coerce").fillna(close) if "high" in df.columns else close
    lo = pd.to_numeric(df["low"], errors="coerce").fillna(close) if "low" in df.columns else close
    key = df["stock_id"]
    h9 = hi.groupby(key).rolling(n, min_periods=n).max().reset_index(level=0, drop=True)
    l9 = lo.groupby(key).rolling(n, min_periods=n).min().reset_index(level=0, drop=True)
    rsv = ((close - l9) / (h9 - l9).where(h9 > l9) * 100).fillna(50).where(h9.notna())
    k_out, d_out = np.full(len(df), np.nan), np.full(len(df), np.nan)
    for idx in df.groupby("stock_id").indices.values():
        k = d = 50.0
        for i in idx:
            v = rsv.iat[i]
            if pd.isna(v):
                continue
            k = k * 2 / 3 + v / 3
            d = d * 2 / 3 + k / 3
            k_out[i], d_out[i] = k, d
    return pd.Series(k_out, index=df.index), pd.Series(d_out, index=df.index)


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
    df["chg_1d"] = (close / prev_close - 1) * 100
    low60 = close.groupby(key).rolling(60, min_periods=20).min().reset_index(level=0, drop=True)
    df["runup60"] = (close / low60 - 1) * 100                              # 離 60 日最低點已經漲了幾 %
    hi_s = pd.to_numeric(df["high"], errors="coerce").fillna(close) if "high" in df.columns else close
    df["res30"] = hi_s.groupby(key).rolling(30, min_periods=10).max().reset_index(level=0, drop=True)   # 近 30 日最高點＝上方壓力
    df["room_pct"] = (df["res30"] / close - 1) * 100                       # 離壓力還有幾 %

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

    # ── v10 型態分類用 ──
    df["ma20_slope5"] = (df["ma20"] / df["ma20"].groupby(key).shift(5) - 1) * 100   # 月線 5 日斜率 %
    df["return_20d"] = (close / close.groupby(key).shift(20) - 1) * 100
    df["new_high20"] = close > df["prev_high20"]                                    # 近 20 日新高
    df["prev_high60"] = prev_close.groupby(key).rolling(60, min_periods=20).max().reset_index(level=0, drop=True)
    df["new_high60"] = close > df["prev_high60"]                                    # 近 60 日新高（資料不足 60 天時用現有天數）
    # 箱型：最近 BOX_DAYS 天（不含今天）的高低點
    df["box_top"] = prev_close.groupby(key).rolling(BOX_DAYS, min_periods=20).max().reset_index(level=0, drop=True)
    df["box_bottom"] = prev_close.groupby(key).rolling(BOX_DAYS, min_periods=20).min().reset_index(level=0, drop=True)
    df["box_range"] = (df["box_top"] / df["box_bottom"] - 1) * 100
    df["box_pos"] = (close - df["box_bottom"]) / (df["box_top"] - df["box_bottom"]) * 100   # 在箱子裡的位置 0=箱底 100=箱頂
    df["is_box"] = (df["box_range"] < BOX_MAX_RANGE) & (df["ma20_slope5"].abs() < BOX_FLAT_SLOPE)
    df["k9"], df["d9"] = calc_kd(df)
    df["k9_prev"] = df["k9"].groupby(key).shift(1)

    latest = df["date"].max()
    out = df[df["date"] == latest].copy()
    ok = out["ma20"].notna()
    rs_ok = out["return_20d"].notna()
    mkt_r20 = float(out.loc[rs_ok, "return_20d"].median()) if rs_ok.sum() >= 200 else np.nan
    out["rel_strength"] = out["return_20d"] - mkt_r20       # 近 20 日漲幅 − 全市場中位數（>0 = 贏大盤）
    breadth = float((out.loc[ok, "close"] > out.loc[ok, "ma20"]).mean() * 100) if ok.sum() >= 200 else np.nan
    out["mkt_breadth"] = breadth            # 大盤環境：站上月線的股票占幾 %（有 MA20 的股票不到 200 檔時算不準，記為空值）
    print("大盤環境：" + ("資料不足，無法判斷" if np.isnan(breadth) else
          f"{breadth:.0f}% 的股票站上月線（低於 {MARKET_MIN_BREADTH:g}% 算偏弱，v9.2 會暫停新買進）"))
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
    for c in ("foreign_net", "trust_net", "dealer_hedge", "dealer_prop"):
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
        trust_days=("t_pos", "sum"), foreign_days=("f_pos", "sum"), hedge_abs=("h_abs", "sum"),
        dealer_5d=("dealer_prop", "sum")).reset_index()          # 自營商「自行買賣」5日加總（避險部位不算）
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
    return agg[["stock_id", "trust_5d", "foreign_5d", "dealer_5d", "trust_days", "foreign_days",
                "trust_acc", "foreign_acc", "hedge_dominant"]]


INST_COST_DAYS = 10        # 法人成本：看最近幾個交易日的買超
INST_COST_MIN_DAYS = 2     # 至少要有幾天買超才算得出成本


def make_inst_cost(inst, prices):
    """法人（投信＋外資）近 INST_COST_DAYS 天的平均買進成本。
    只算「當天土洋合計是買超」的日子，用當天均價（(高+低+收)/3，沒有高低價就用收盤）乘買超張數做加權平均。"""
    cols = ["stock_id", "inst_cost", "inst_cost_days", "inst_1d", "inst_3d", "inst_sell_streak", "inst_20d", "inst_buydays10"]
    if inst is None or inst.empty or prices is None or prices.empty:
        return pd.DataFrame(columns=cols)
    i = inst.drop_duplicates(["date", "stock_id"], keep="last").copy()
    i["net"] = pd.to_numeric(i["foreign_net"], errors="coerce").fillna(0) + pd.to_numeric(i["trust_net"], errors="coerce").fillna(0)
    p = prices.drop_duplicates(["date", "stock_id"], keep="last").copy()
    c = pd.to_numeric(p["close"], errors="coerce")
    hi = pd.to_numeric(p["high"], errors="coerce") if "high" in p.columns else c
    lo = pd.to_numeric(p["low"], errors="coerce") if "low" in p.columns else c
    p["px"] = ((hi.fillna(c) + lo.fillna(c) + c) / 3)
    df = i.merge(p[["date", "stock_id", "px"]], on=["date", "stock_id"], how="inner").sort_values(["stock_id", "date"])
    df = df[df.groupby("stock_id").cumcount(ascending=False) < INST_COST_DAYS]
    buy = df[(df["net"] > 0) & df["px"].notna()].copy()
    buy["amt"] = buy["net"] * buy["px"]
    g = buy.groupby("stock_id").agg(amt=("amt", "sum"), net=("net", "sum"), inst_cost_days=("net", "size")).reset_index()
    g["inst_cost"] = g["amt"] / g["net"]
    g.loc[g["inst_cost_days"] < INST_COST_MIN_DAYS, "inst_cost"] = np.nan
    # 近期法人資金動向：最近 1 天、3 天土洋合計，以及連續賣超天數
    i = i.sort_values(["stock_id", "date"])
    recent = i[i.groupby("stock_id").cumcount(ascending=False) < 3]

    def streak(x):
        n = 0
        for v in x[::-1]:
            if v < 0:
                n += 1
            else:
                break
        return n
    flow = recent.groupby("stock_id")["net"].agg(inst_1d="last", inst_3d="sum",
                                                 inst_sell_streak=lambda x: streak(list(x))).reset_index()
    w20 = i[i.groupby("stock_id").cumcount(ascending=False) < 20]
    w10 = i[i.groupby("stock_id").cumcount(ascending=False) < 10]
    longer = (w20.groupby("stock_id")["net"].sum().rename("inst_20d").to_frame()
              .join(w10.assign(b=(w10["net"] > 0).astype(int)).groupby("stock_id")["b"].sum().rename("inst_buydays10"))
              .reset_index())
    flow = flow.merge(longer, on="stock_id", how="left")
    g = flow.merge(g, on="stock_id", how="left")
    print(f"法人成本：{int(g['inst_cost'].notna().sum())} 檔算得出來（近 {INST_COST_DAYS} 個交易日）")
    return g[cols]


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


# ───────────────────────── 型態分類（v10）：突破型／低接型／整理股 ─────────────────────────
SETUP_LABEL = {"BREAKOUT": "🚀 突破型", "PULLBACK": "🎯 低接型", "ACCUM": "🧲 佈局型", "PULLBACK_WAIT": "⏳ 低接等勾頭",
               "BOX": "📦 整理股", "": ""}


def classify_setup(r):
    """回傳 (型態, 說明, 參考進場價, 停損價)。不符合任何型態回傳 ("", "", nan, nan)。
    突破型：已經發動，確認後追進（帶量站上箱頂／前高）
    低接型：強勢股拉回、法人還在買，提早埋伏
    整理股：箱型來回、沒有方向，只列觀察，標出突破價與低接區"""
    none = ("", "", np.nan, np.nan)
    sid, c = str(r["stock_id"]), r["close"]
    if sid in EXCLUDE_TOOL_STOCKS or sid.startswith("00") or r["hedge_dominant"] or c > MAX_PRICE or c < MIN_PRICE:
        return none
    if not r["turnover"] >= MIN_DAILY_TURNOVER:
        return none
    if r["trust_5d"] < 0 and r["foreign_5d"] < 0:          # 土洋雙殺不列
        return none
    inst_net = r["trust_5d"] + r["foreign_5d"]
    vol5 = r["avg_vol_5d"] * 5
    if not (pd.notna(vol5) and vol5 > 0 and inst_net / vol5 * 100 >= INST_MIN_PART):
        return none                                         # 法人參與太少：成交量多半是主力、散戶，不列
    inst_buy = inst_net > 0 and (r["trust_5d"] > 0 or r["foreign_5d"] > 0)   # 土洋 5 日合計要淨買
    inst_acc = (r["trust_acc"] or r["foreign_acc"]) and inst_net > 0          # 連續買（投信／外資任一）＋合計淨買
    slope, rs, vr, k, kp = r["ma20_slope5"], r["rel_strength"], r["vol_ratio_5d"], r["k9"], r["k9_prev"]
    if pd.isna(r["ma20"]) or pd.isna(slope) or pd.isna(vr):
        return none
    beats_mkt = pd.isna(rs) or rs > 0                       # 大盤資料不足時不擋

    # 突破型：帶量站上箱頂（或前 20 日高），月線沒有往下，贏大盤，法人有買
    top = r["box_top"] if pd.notna(r["box_top"]) else r["prev_high20"]
    if (pd.notna(top) and c > top and vr >= BREAKOUT_VOL_RATIO and slope >= 0 and beats_mkt
            and inst_acc and not r["exploded"]):
        hi = "60日新高" if r["new_high60"] else "20日新高"
        note = f"帶量 {vr:.1f} 倍站上箱頂 {top:.1f}｜{hi}｜月線 5日{slope:+.1f}%"
        stop = max(top * (1 - BREAKOUT_STOP_PCT / 100), c * (1 - STOP_LOSS_PCT / 100))   # 最多虧 STOP_LOSS_PCT
        return "BREAKOUT", note, c, round(stop, 2)

    # 低接型：強勢股（月線明顯上揚＋贏大盤），法人持續買，拉回月線／10日線附近、量縮、K 低檔往上勾
    near_ma = (c >= r["ma20"] * 0.98) and pd.notna(r["ma10"]) and (c <= r["ma10"] * 1.03)
    if (slope >= MA20_SLOPE_MIN and beats_mkt and inst_acc and near_ma and vr < PULLBACK_MAX_VOL_RATIO
            and pd.notna(k) and pd.notna(kp) and k < PULLBACK_MAX_K):
        stop = round(r["ma20"] * (1 - PULLBACK_STOP_PCT / 100), 2)
        if k > kp:
            note = f"拉回均線量縮（量比{vr:.1f}）｜K {kp:.0f}→{k:.0f} 往上勾｜月線 5日{slope:+.1f}%"
            return "PULLBACK", note, c, stop
        note = f"拉回均線量縮（量比{vr:.1f}）｜K {kp:.0f}→{k:.0f} 還在往下，等勾頭再買｜月線 5日{slope:+.1f}%"
        return "PULLBACK_WAIT", note, np.nan, stop

    # 佈局型：箱型整理中、還沒突破，法人連續買、量還沒放大、股價在箱子中上段、站在月線上 → 散戶進來前先上車
    if (pd.notna(r["box_top"]) and pd.notna(r["box_pos"]) and r["box_range"] < BOX_MAX_RANGE and slope > -BOX_FLAT_SLOPE
            and c <= r["box_top"] and r["box_pos"] >= ACCUM_MIN_POS and vr < ACCUM_MAX_VOL_RATIO
            and c >= r["ma20"] and inst_acc and (pd.isna(r["inst_3d"]) or r["inst_3d"] > 0)):
        note = (f"箱型 {r['box_bottom']:.1f}～{r['box_top']:.1f} 整理中｜位置 {r['box_pos']:.0f}%｜量比 {vr:.1f}"
                f"｜法人連續買，還沒突破")
        return "ACCUM", note, c, round(r["box_bottom"] * 0.97, 2)

    # 整理股：箱型、月線走平，法人有在買 → 只觀察，標出突破價與低接區
    if r["is_box"] and inst_buy and pd.notna(r["box_pos"]):
        pos = r["box_pos"]
        kd = f"K{k:.0f}" if pd.notna(k) else "K-"
        if pos >= 80 and pd.notna(k) and k >= 70:
            where = "箱頂＋KD高檔，先別追"
        elif pos <= 30:
            where = "靠近箱底，可留意低接"
        else:
            where = "箱子中間"
        note = (f"箱型 {r['box_bottom']:.1f}～{r['box_top']:.1f}（{r['box_range']:.0f}%）｜位置 {pos:.0f}%｜{kd}｜{where}"
                f"｜帶量站上 {r['box_top']:.1f} 才算突破")
        return "BOX", note, np.nan, np.nan
    return none


BOOL_COLS = ["trust_acc", "foreign_acc", "hedge_dominant", "is_multi_up", "above_ma10", "above_ma20",
             "is_consolidation", "near_high", "first_break", "pre_breakout", "exploded", "overheat"]
NUM0_COLS = ["trust_5d", "foreign_5d", "dealer_5d", "trust_days", "foreign_days"]


def tech_text(r):
    vals = [r["ma5"], r["ma10"], r["ma20"], r["prev_high20"]]
    if any(pd.isna(v) for v in vals):
        return ""
    return f"MA5:{vals[0]:.1f} MA10:{vals[1]:.1f} MA20:{vals[2]:.1f} 前20高:{vals[3]:.1f}"


def build_radar(price_feat, inst_feat, holder_feat=None, margin_feat=None):
    if price_feat is None or price_feat.empty:
        print("radar 空")
        return pd.DataFrame()
    df = price_feat.copy()
    if inst_feat is not None and not inst_feat.empty:
        df = df.merge(inst_feat, on="stock_id", how="left")
    if holder_feat is not None and not holder_feat.empty:
        df = df.merge(holder_feat, on="stock_id", how="left")
    if margin_feat is not None and not margin_feat.empty:
        df = df.merge(margin_feat, on="stock_id", how="left")
    for c in ("margin_bal", "margin_chg5", "mkt_breadth"):   # 融資／大盤資料可能還沒有（缺值＝不影響判斷）
        if c not in df.columns:
            df[c] = np.nan
    for c in ("big_ratio", "big_delta"):              # 千張大戶資料可能還沒有（缺值＝不影響判斷）
        if c not in df.columns:
            df[c] = np.nan
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
    for c in ("rel_strength", "ma20_slope5", "k9", "k9_prev", "box_top", "box_bottom", "box_pos", "prev_high60",
              "inst_cost", "inst_1d", "inst_3d", "inst_sell_streak", "inst_20d", "inst_buydays10",
              "room_pct", "res30"):
        if c not in df.columns:
            df[c] = np.nan
    for c in ("is_box", "new_high20", "new_high60"):
        df[c] = df[c].eq(True) if c in df.columns else False
    st = df.apply(classify_setup, axis=1, result_type="expand")
    df["setup"], df["setup_note"], df["setup_entry"], df["setup_stop"] = st[0], st[1], st[2], st[3]
    df["overextended"] = df["dist_ma20"] > OVEREXTEND_PCT
    print("型態分布：", df["setup"].value_counts().to_dict())
    df = df.sort_values(["rank", "turnover"], ascending=[True, False]).reset_index(drop=True)
    print("訊號分布：", df["signal_key"].value_counts().to_dict())
    return df


# ───────────────────────── 追蹤：候選 → 買進 → 賣出 ─────────────────────────
# 觀察中(WATCH) → 達到買進條件 → 持有中(HOLD) → 達到賣出條件 → 已賣出(CLOSED)
# 觀察太久／大戶不買了／已爆發沒買到 → 放棄追蹤(DROPPED)
# 這是「模擬記錄」，不會真的下單；買賣價格以觸發當天收盤價計。
TRACK_COLS = ["version", "stock_id", "first_seen", "stock_name", "status", "start_signal", "entry_date",
              "entry_price", "exit_date", "exit_price", "exit_reason", "last_date", "days_watched", "days_held"]


def rs_of(version):
    """版本 → 額外條件開關；沒登記的版本名稱當作 v9.0。"""
    return {**RULE_DEFAULTS, **RULESETS.get(version, {})}


def inst3_5d(r):
    """三大法人 5 日合計＝投信＋外資＋自營商（自行買賣）。"""
    return r["trust_5d"] + r["foreign_5d"] + r["dealer_5d"]


def buy_check(r, rs=None):
    """買進條件：收盤突破前 20 日高、還沒爆量、還沒急漲、投信或外資 5 日淨買、成交金額夠。
    v9.1 再加：三大法人（含自營商）5 日合計淨買；千張大戶持股比例本週有增加（資料不足時不擋）。
    v9.2 再加：大盤環境不能偏弱；融資 5 日增加不能太多。
    v9.3 再加：提早買——收盤離前 20 日高不到 1% 就買，不等真的突破。"""
    rs = rs or rs_of(VERSION)
    ph, vr, r5 = r["prev_high20"], r["vol_ratio_5d"], r["return_5d"]
    if pd.isna(ph) or pd.isna(vr) or pd.isna(r5):
        return False
    trigger = r["close"] > ph or (rs["early"] and r["close"] >= ph * EARLY_BUY_RATIO)   # 提早買：還沒突破但離前高不到 1%
    if not (trigger and vr < BUY_MAX_VOL_RATIO and r5 < BUY_MAX_RET5
            and (r["trust_5d"] > 0 or r["foreign_5d"] > 0) and r["turnover"] >= MIN_DAILY_TURNOVER):
        return False
    if rs["dealer"] and inst3_5d(r) <= 0:
        return False
    if rs["big"] and pd.notna(r["big_delta"]) and r["big_delta"] <= BIG_BUY_MIN_DELTA:
        return False
    if rs["market"] and pd.notna(r.get("mkt_breadth", np.nan)) and r["mkt_breadth"] < MARKET_MIN_BREADTH:
        return False                                   # 大盤偏弱：暫停新買進
    if rs["margin"] and pd.notna(r.get("margin_chg5", np.nan)) and r["margin_chg5"] > MARGIN_BUY_MAX_CHG:
        return False                                   # 融資已經大增：散戶先進來了
    return True


def sell_reason(r, entry_price=None, rs=None):
    """賣出條件（任一成立）；回傳原因，沒有則 None。順序＝優先序。"""
    rs = rs or rs_of(VERSION)
    c = r["close"]
    if entry_price and c / entry_price - 1 <= -STOP_LOSS_PCT / 100:
        return f"停損 {STOP_LOSS_PCT:g}%"
    if r["exploded"]:
        return "已爆發·散戶衝進來"
    if r["trust_5d"] < 0 and r["foreign_5d"] < 0:
        return "土洋雙殺"
    if rs["dealer"] and inst3_5d(r) < 0:
        return "三大法人合計淨賣"
    if rs["big"] and pd.notna(r["big_delta"]) and r["big_delta"] <= BIG_SELL_DELTA:
        return f"千張大戶減持（本週{r['big_delta']:+.2f}個百分點）"
    if rs["margin"] and pd.notna(r.get("margin_chg5", np.nan)) and r["margin_chg5"] >= MARGIN_SELL_CHG:
        return f"融資暴增（5日{r['margin_chg5']:+.0f}%）·散戶進場"
    if pd.notna(r["ma10"]) and c < r["ma10"]:
        return "跌破MA10"
    return None


def drop_reason(r, days_watched, rs=None):
    rs = rs or rs_of(VERSION)
    if r["exploded"]:
        return "已爆發·沒買到不追"
    # 放棄規則 B：大戶「淨賣出」才放棄；只是暫停沒買（合計為 0）的繼續觀察
    if rs["dealer"]:
        if inst3_5d(r) < 0:
            return "三大法人5日合計淨賣"
    elif r["trust_5d"] < 0 and r["foreign_5d"] < 0:
        return "土洋5日都在賣"
    if rs["big"] and pd.notna(r["big_delta"]) and r["big_delta"] <= BIG_SELL_DELTA:
        return f"千張大戶減持（本週{r['big_delta']:+.2f}個百分點）"
    if days_watched >= WATCH_MAX_DAYS:
        return f"觀察超過{WATCH_MAX_DAYS}天"
    return None


def _chip(r):
    s = (f"投信{int(round(float(r['trust_5d']) / 1000)):+,d}張 外資{int(round(float(r['foreign_5d']) / 1000)):+,d}張 "
         f"自營{int(round(float(r['dealer_5d']) / 1000)):+,d}張")
    if pd.notna(r["big_delta"]):
        s += f" 千張大戶{r['big_delta']:+.2f}pp"
    if pd.notna(r.get("margin_chg5", np.nan)):
        s += f" 融資5日{r['margin_chg5']:+.0f}%"
    return s


def _enter(t, r, data_date, events):
    t.update(status="HOLD", entry_date=data_date, entry_price=float(r["close"]), days_held=0)
    where = (f"突破前20日高 {r['prev_high20']:.1f}" if r["close"] > r["prev_high20"]
             else f"提早買：離前20日高 {r['prev_high20']:.1f} 只差 {(r['prev_high20'] / r['close'] - 1) * 100:.1f}%")
    events["buy"].append(
        f"{t['stock_id']} {t['stock_name']}｜買進價 {r['close']:.1f}｜{where}，"
        f"量比 {r['vol_ratio_5d']:.1f}，5日{r['return_5d']:+.1f}%｜{_chip(r)}｜停損價 {r['close'] * (1 - STOP_LOSS_PCT / 100):.1f}")


def update_tracking(radar, data_date, version=None):
    """每天更新一次某個版本所有追蹤中的股票，回傳 (所有紀錄, 今日事件)。同一天重跑不會重複計算。"""
    version = version or VERSION
    rs = rs_of(version)
    with db() as conn:
        tr = pd.read_sql("SELECT * FROM tracking WHERE version=?", conn, params=(version,), dtype={"stock_id": str})
    tr = tr.astype(object).where(tr.notna(), None) if not tr.empty else tr
    recs = tr.to_dict("records") if not tr.empty else []
    rows = radar.drop_duplicates("stock_id").set_index("stock_id")
    events = {"buy": [], "sell": [], "drop": [], "new": []}

    open_ids = set()
    for t in recs:
        if t["status"] not in ("WATCH", "HOLD"):
            continue
        sid = t["stock_id"]
        open_ids.add(sid)
        if str(t.get("last_date") or "") >= data_date or sid not in rows.index:
            continue                                   # 今天已處理過（重跑），或今天沒有這檔的資料
        r = rows.loc[sid]
        c = float(r["close"])
        if t["status"] == "WATCH":
            t["days_watched"] = int(t["days_watched"] or 0) + 1
            if buy_check(r, rs):
                _enter(t, r, data_date, events)
            else:
                why = drop_reason(r, t["days_watched"], rs)
                if why:
                    t.update(status="DROPPED", exit_date=data_date, exit_price=c, exit_reason=why)
                    events["drop"].append(f"{sid} {t['stock_name']}｜{why}")
        else:
            t["days_held"] = int(t["days_held"] or 0) + 1
            why = sell_reason(r, t["entry_price"], rs)
            if why:
                t.update(status="CLOSED", exit_date=data_date, exit_price=c, exit_reason=why)
                events["sell"].append(
                    f"{sid} {t['stock_name']}｜買 {t['entry_price']:.1f} → 賣 {c:.1f}｜"
                    f"損益 {(c / t['entry_price'] - 1) * 100:+.1f}%（未扣成本）｜持有 {t['days_held']} 天｜原因：{why}")
        t["last_date"] = data_date

    today = datetime.strptime(data_date, "%Y-%m-%d")
    cooling = {t["stock_id"] for t in recs                 # 剛賣出／剛放棄的，一陣子內不重複加入（也避免同一天重跑重複加入）
               if t.get("exit_date") and (today - datetime.strptime(str(t["exit_date"]), "%Y-%m-%d")).days < COOLDOWN_DAYS}
    for sid, r in rows.iterrows():
        if r["signal_key"] not in TRACK_START or sid in open_ids or sid in cooling:
            continue
        if r["close"] > MAX_PRICE:                     # 一張買不起的不追蹤
            continue
        if drop_reason(r, 0, rs):                      # 一出現就會被放棄的（例如千張大戶正在減持），直接不加入，免得紀錄裡一堆雜訊
            continue
        t = {"version": version, "stock_id": sid, "first_seen": data_date, "stock_name": r["stock_name"],
             "status": "WATCH", "start_signal": r["signal"], "entry_date": None, "entry_price": None,
             "exit_date": None, "exit_price": None, "exit_reason": None, "last_date": data_date,
             "days_watched": 0, "days_held": 0}
        if buy_check(r, rs):                           # 一出現就已經是「剛突破第一根」→ 當天就買
            _enter(t, r, data_date, events)
        recs.append(t)
        events["new"].append(sid)

    if recs:
        upsert("tracking", pd.DataFrame(recs, columns=TRACK_COLS), TRACK_COLS)
    n = {s: sum(1 for t in recs if t["status"] == s) for s in ("WATCH", "HOLD", "CLOSED", "DROPPED")}
    events["counts"] = n
    print(f"追蹤[{version}]：觀察中 {n['WATCH']}｜持有中 {n['HOLD']}｜已賣出 {n['CLOSED']}｜已放棄 {n['DROPPED']}"
          f"｜今天 新增 {len(events['new'])} 買 {len(events['buy'])} 賣 {len(events['sell'])} 放棄 {len(events['drop'])}")
    return recs, events


def rules_text(version):
    rs = rs_of(version)
    extra = []
    if rs["dealer"]:
        extra.append("三大法人（含自營商）5日合計要淨買／淨賣才買／賣")
    if rs["big"]:
        extra.append(f"千張大戶持股比例本週增加才買、減少{abs(BIG_SELL_DELTA):g}個百分點以上就賣")
    if rs["early"]:
        extra.append(f"提早買（收盤離前20日高不到{(1 - EARLY_BUY_RATIO) * 100:g}%就買，不等突破）")
    if rs["market"]:
        extra.append(f"大盤偏弱（站上月線的股票<{MARKET_MIN_BREADTH:g}%）不買")
    if rs["margin"]:
        extra.append(f"融資5日增加>{MARGIN_BUY_MAX_CHG:g}%不買、>={MARGIN_SELL_CHG:g}%就賣")
    return "基本規則" + ("＋" + "＋".join(extra) if extra else "（沒有額外條件）")


def compare_line(version, events):
    n = events.get("counts", {})
    return (f"{version}（{rules_text(version)}）：今天買進 {len(events['buy'])}、賣出 {len(events['sell'])}｜"
            f"持有中 {n.get('HOLD', 0)}、觀察中 {n.get('WATCH', 0)}")


def build_tracking_text(recs, radar, events, version=None):
    version = version or VERSION
    rows = radar.drop_duplicates("stock_id").set_index("stock_id")

    def sec(title, lines, limit=None):
        shown = lines if limit is None else lines[:limit]
        more = f"\n   …還有 {len(lines) - len(shown)} 檔" if len(shown) < len(lines) else ""
        return f"{title}\n" + ("\n".join(shown) + more if lines else "（無）")

    hold, watch = [], []
    for t in recs:
        sid = t["stock_id"]
        r = rows.loc[sid] if sid in rows.index else None
        if t["status"] == "HOLD":
            if r is None:
                hold.append((-9, f"{sid} {t['stock_name']}｜買進價 {t['entry_price']:.1f}｜今天沒有行情資料"))
                continue
            c, e = float(r["close"]), t["entry_price"]
            ma = f"｜離MA10 {(c / r['ma10'] - 1) * 100:+.1f}%" if pd.notna(r["ma10"]) else ""
            hold.append((c / e - 1,
                         f"{sid} {t['stock_name']}｜買進價 {e:.1f}｜現價 {c:.1f}｜損益 {(c / e - 1) * 100:+.1f}%｜"
                         f"持有 {t['days_held']} 天{ma}｜停損價 {e * (1 - STOP_LOSS_PCT / 100):.1f}"))
        elif t["status"] == "WATCH":
            if r is None:
                continue
            ph, c = r["prev_high20"], float(r["close"])
            if pd.isna(ph):
                gap, txt = 1e9, "尚無前高資料（上櫃歷史累積中）"
            elif c > ph:
                gap, txt = -1, f"已站上前高 {ph:.1f}，但量比／漲幅／法人未達標"
            else:
                gap, txt = (ph / c - 1) * 100, f"還差 {(ph / c - 1) * 100:.1f}% 到買點（前高 {ph:.1f}）"
            watch.append((gap, f"{sid} {t['stock_name']}｜已觀察 {t['days_watched']} 天｜{txt}｜{t['start_signal']}"))
    hold = [x[1] for x in sorted(hold, key=lambda x: x[0], reverse=True)]     # 賺最多的排最前面
    watch = [x[1] for x in sorted(watch, key=lambda x: x[0])]                 # 離買點最近的排最前面

    head = (f"追蹤版本 {version}：{rules_text(version)}\n"
            f"今天新加入 {len(events['new'])} 檔、放棄 {len(events['drop'])} 檔｜"
            f"觀察中共 {len(watch)} 檔、持有中 {len(hold)} 檔（模擬記錄，價格以收盤價計）")
    return "\n\n".join([head,
                        sec(f"🟢 今天觸發買進 {len(events['buy'])} 檔", events["buy"]),
                        sec(f"🔴 今天觸發賣出 {len(events['sell'])} 檔", events["sell"]),
                        sec(f"📈 持有中 {len(hold)} 檔", hold),
                        sec(f"👀 觀察中 {len(watch)} 檔（依離買點的距離排序，列前 20）", watch, 20),
                        sec(f"🗑 今天放棄追蹤 {len(events['drop'])} 檔", events["drop"], 15)])


# ───────────────────────── 我真正買的股票 ─────────────────────────
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
                if not is_stock_id(sid):
                    continue                           # 標題列或亂打的字
                price = safe_float(parts[1]) if len(parts) > 1 else np.nan
                out[sid] = (None if pd.isna(price) or price <= 0 else float(price), group)
    except Exception as e:
        print(f"讀取 {HOLDINGS_FILE} 失敗：{e}")
    print(f"我的持股：{len(out)} 檔（{HOLDINGS_FILE}）")
    return out


def build_holdings_text(radar, holdings):
    """每天檢查你真正買的股票：續抱，還是出現賣出訊號。回傳 (文字, 賣出警示數)。"""
    if not holdings:
        return "", 0
    rows = radar.drop_duplicates("stock_id").set_index("stock_id")
    out, alerts, cur = [], 0, None
    for sid, (cost, group) in holdings.items():
        if group != cur:                                   # 分區標題
            cur = group
            n = sum(1 for g in holdings.values() if g[1] == group)
            out.append(("\n" if out else "") + f"【{group}】{n} 檔")
        if sid not in rows.index:
            out.append(f"❔ {sid}｜今天沒有行情資料（休市、下市或代號打錯？）")
            continue
        r = rows.loc[sid]
        why = sell_reason(r, cost)
        alerts += bool(why)
        pnl = f"｜買進 {cost:g} → 現價 {r['close']:.1f}（{(r['close'] / cost - 1) * 100:+.1f}%）" if cost else f"｜現價 {r['close']:.1f}（沒填買進價，算不出損益與停損）"
        stop = f"｜停損價 {cost * (1 - STOP_LOSS_PCT / 100):.1f}" if cost else ""
        ma = f"｜離MA10 {(r['close'] / r['ma10'] - 1) * 100:+.1f}%" if pd.notna(r["ma10"]) else ""
        out.append(f"{'🚨 該賣：' + why if why else '✅ 續抱'}｜{sid} {r['stock_name']}{pnl}{stop}{ma}\n"
                   f"   {_chip(r)}")
    return "\n".join(out), alerts


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
    log = sig.assign(version=SIGNAL_VERSION)
    with db() as conn:
        conn.execute("DELETE FROM signal_log WHERE signal_date=? AND version=?", (data_date, SIGNAL_VERSION))
    upsert("signal_log", log, ["version"] + SIGNAL_COLS)
    print(f"signal_log 寫入 {len(log)} 筆（版本 {SIGNAL_VERSION}）")


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


def setup_groups(radar):
    """依型態分組；乖離月線太大的排到後面。"""
    out = {}
    for key, sort_col in (("BREAKOUT", "vol_ratio_5d"), ("PULLBACK", "rel_strength"),
                          ("PULLBACK_WAIT", "rel_strength"), ("BOX", "box_pos")):
        g = radar[radar["setup"] == key].copy()
        g["_pen"] = g["overextended"].astype(int)
        asc = key == "BOX"                                  # 整理股：靠近箱底的排前面
        out[key] = g.sort_values(["_pen", sort_col], ascending=[True, asc], na_position="last")
    return out


def fmt_setup_rows(df, n, with_price=True):
    if df.empty:
        return "（無）"
    out = []
    for _, r in df.head(n).iterrows():
        t = f"{int(round(float(r['trust_5d']) / 1000)):+,d}"
        f = f"{int(round(float(r['foreign_5d']) / 1000)):+,d}"
        warn = f"⚠乖離月線{r['dist_ma20']:+.0f}% " if r["overextended"] else ""
        price = (f"\n   參考進場 {r['setup_entry']:.2f}｜停損 {r['setup_stop']:.2f}"
                 f"（{(r['setup_stop'] / r['setup_entry'] - 1) * 100:+.1f}%）"
                 if with_price and pd.notna(r["setup_entry"]) else "")
        rs = f" 贏大盤{r['rel_strength']:+.1f}%" if pd.notna(r["rel_strength"]) else ""
        out.append(f"{warn}{r['stock_id']} {r['stock_name']}｜收盤 {_f(r['close'], '{:.2f}')}｜{r['setup_note']}\n"
                   f"   投信{t}張 外資{f}張{rs}{price}")
    return "\n".join(out)


def build_setup_text(radar):
    g = setup_groups(radar)
    line = "━━━━━━━━━━━━"
    return (f"{line}\n🚀 突破型 {len(g['BREAKOUT'])} 檔：已經發動，確認後追進（列前15）\n"
            f"   停損設箱頂下方 {BREAKOUT_STOP_PCT:g}%；KD 高沒關係，重點是真的帶量突破\n"
            f"{fmt_setup_rows(g['BREAKOUT'], 15)}\n\n"
            f"{line}\n🎯 低接型 {len(g['PULLBACK'])} 檔：強勢股拉回、法人還在買，提早埋伏（列前15）\n"
            f"   停損設月線下方 {PULLBACK_STOP_PCT:g}%；可能要等幾天才會動\n"
            f"{fmt_setup_rows(g['PULLBACK'], 15)}\n\n"
            f"⏳ 低接等勾頭 {len(g['PULLBACK_WAIT'])} 檔：條件都到了，只差 K 值還在往下（列前10，先放自選股）\n"
            f"{fmt_setup_rows(g['PULLBACK_WAIT'], 10, with_price=False)}\n\n"
            f"{line}\n📦 整理股觀察 {len(g['BOX'])} 檔：箱型來回沒方向，不是買點（靠近箱底的排前面，列前15）\n"
            f"   帶量站上箱頂才轉突破型；箱頂＋KD高檔最危險\n"
            f"{fmt_setup_rows(g['BOX'], 15, with_price=False)}\n\n")


LEGEND = """【型態分類（v10）】
🚀 突破型：帶量（大於5日均量""" + f"{BREAKOUT_VOL_RATIO:g}" + """倍）站上近""" + f"{BOX_DAYS}" + """日箱頂、月線沒有往下、近20日漲幅贏大盤、法人有買
🎯 低接型：月線5日上升≥""" + f"{MA20_SLOPE_MIN:g}" + """%、贏大盤、法人持續買、股價拉回月線～10日線附近、量縮（量比<""" + f"{PULLBACK_MAX_VOL_RATIO:g}" + """）、K<""" + f"{PULLBACK_MAX_K}" + """且往上勾
📦 整理股：近""" + f"{BOX_DAYS}" + """日高低差<""" + f"{BOX_MAX_RANGE:g}" + """%且月線走平。位置 0%=箱底、100%=箱頂
⚠ 乖離月線超過""" + f"{OVEREXTEND_PCT:g}" + """%：容易拉回，排到後面
大盤＝全市場股票近20日漲幅的中位數；KD(9,3) 舊資料沒有最高／最低價時用收盤價代替，會略有誤差

【搭順風車邏輯 - 買在還沒爆發前】
🔵 吸籌末端·土洋同買·第一根：盤整末端+土洋同買+快突破（收盤在前20日高點97%~100%）或首次站上前高+量比<2 = 上車點
🔵 吸籌中·土洋同買·快突破：盤整吸籌+土洋同買+近前高
🟣 先洋後土·跟：外資先佈局投信後跟，大戶先上車了
🟣/🔵 吸籌末端·外資/投信先上車：單邊大戶先吸籌+快突破
🟡 吸籌中·等站回：大戶還在吸但未站上月線，等站回MA20再跟
⚪ 洋買土賣·觀察 / 🟡 洋賣土買·小心接刀：大戶意見分歧
🔴 已爆發·勿追／過熱·勿追：量爆2.5倍+5日>12%，散戶衝進來的地方，不是買點
🔴 雙殺·快下車：洋賣土賣，大戶下車了
技術：MA5=週線 MA10=10日 MA20=月線 多頭排列=5>10>20 盤整=前20日高低差<18%
成交金額低於3000萬的標的不列入買進類訊號

【追蹤規則（模擬記錄，不會真的下單）】
開始追蹤：出現「吸籌末端／快突破／先洋後土／吸籌中等站回」類訊號
買進：收盤突破前20日高 + 量比<""" + f"{BUY_MAX_VOL_RATIO:g}" + """ + 5日漲幅<""" + f"{BUY_MAX_RET5:g}" + """% + 投信或外資5日淨買
賣出（任一）：停損""" + f"{STOP_LOSS_PCT:g}" + """%／已爆發（量比≥2.5且5日漲≥12%）／土洋雙殺／跌破MA10
放棄：觀察超過""" + f"{WATCH_MAX_DAYS}" + """天／投信與外資5日合計都淨賣（只是沒買不算）／已爆發沒買到
v9.1 另外加：買進要三大法人（投信＋外資＋自營商自行買賣）5日合計淨買、千張大戶持股比例本週增加；
　　　　　　賣出多了「三大法人5日合計淨賣」「千張大戶本週減持""" + f"{abs(BIG_SELL_DELTA):g}" + """個百分點以上」
v9.2 再加：大盤環境（全市場站上月線的股票<""" + f"{MARKET_MIN_BREADTH:g}" + """%＝偏弱）時不買；融資（散戶借錢買股）5日增加超過""" + f"{MARGIN_BUY_MAX_CHG:g}" + """%不買、達""" + f"{MARGIN_SELL_CHG:g}" + """%就賣
v9.3 另外：提早買——收盤離前20日高不到""" + f"{(1 - EARLY_BUY_RATIO) * 100:g}" + """%就買，不等真的突破（跟 v9.2 比較「提早買」和「等突破才買」哪個比較好）
千張大戶＝持股1000張以上的人合計佔比（集保每週公布一次，所以這項資料最多落後約一週；資料還不夠算週增減時，不會因此擋買進）
損益未扣交易成本（來回約0.6%：手續費買賣各0.1425%＋證交稅0.3%，實際依券商折扣）"""


def build_email_body(radar, data_date, track_text="", hold_text=""):
    k = radar["signal_key"]
    cheap = radar["close"] <= MAX_PRICE                # 只列買得起的（股價上限見 MAX_PRICE）
    best, follow = radar[k.isin(GROUP_BEST) & cheap], radar[k.isin(GROUP_FOLLOW) & cheap]
    danger = radar[k.isin(GROUP_DANGER)]
    line = "━━━━━━━━━━━━"
    b = radar["mkt_breadth"].iloc[0] if "mkt_breadth" in radar.columns and len(radar) else np.nan
    mkt = ("大盤環境：資料不足，暫時無法判斷" if pd.isna(b) else
           f"大盤環境：{b:.0f}% 的股票站上月線｜" + ("偏弱（v9.2 暫停新買進）" if b < MARKET_MIN_BREADTH else "正常"))
    hold = f"{line}\n💼 我實際持有的股票（賣出檢查，賣掉了請從「股票追蹤」檔刪掉那一行）\n{hold_text}\n\n" if hold_text else ""
    return (f"台股雷達 {VERSION} 搭順風車版｜資料日 {data_date}\n{mkt}\n\n"
            f"{hold}"
            f"{build_setup_text(radar)}"
            f"{line}\n🔵🟣 今日候選：吸籌末端·第一根 {len(best)} 檔（列前30）\n{fmt_rows(best, 30)}\n\n"
            f"{line}\n📌 追蹤中（候選股的買賣模擬）\n{track_text}\n\n"
            f"{line}\n🔵🟣 跟著大戶 {len(follow)} 檔（列前20）\n{fmt_rows(follow, 20)}\n\n"
            f"{line}\n🔴 已爆發／過熱／雙殺 {len(danger)} 檔（只列前15）\n{fmt_rows(danger, 15)}\n\n"
            f"{line}\n{LEGEND}\n")


# ───────────────────────── 精簡信件（v10.1）：只寫 可買／觀察／賣出 ─────────────────────────
BUY_MAX_N = 5              # 可買最多列幾檔
WATCH_MAX_N = 8            # 觀察最多列幾檔
TARGET_MIN_PCT = 8.0       # 目標價至少 +8%
TARGET_MAX_PCT = 10.0      # 目標價最多 +10%
MAX_RUNUP_PCT = 20.0       # 可買：離 60 日最低點最多漲了此 %（漲太多＝肉不多了）
MIN_ROOM_PCT = 5.0         # 可買：離近 30 日最高點（壓力）至少還有此 % 空間；太近改列觀察「站上再買」
INST20_MIN_BUY = 0         # 可買：法人近 20 日累計要是買超（不能只看 5 天）
INST10_MIN_DAYS = 6        # 可買：法人近 10 天至少幾天買超（穩定在買，不是買賣交錯）
MIN_RUNUP_PCT = 5.0        # 可買：離 60 日最低點至少彈上來此 %（確認已經離開低點，不是還在破底）
SETUP_ORDER = {"ACCUM": 0, "PULLBACK": 1, "BREAKOUT": 2}   # 可買排序：佈局最前（最早上車）
INST_COST_MAX_GAP = 5.0    # 現價離法人成本超過此 % → 不列可買，改列觀察「等拉回法人成本」
INST_COST_STOP_PCT = 3.0   # 停損：跌破法人成本此 %


def tick(p):
    """台股升降單位。"""
    return 0.01 if p < 10 else 0.05 if p < 50 else 0.1 if p < 100 else 0.5 if p < 500 else 1 if p < 1000 else 5


def to_tick(p, up=False):
    t = tick(p)
    n = p / t
    n = np.ceil(n - 1e-9) if up else np.floor(n + 1e-9)
    return round(n * t, 2)


def fmt_p(p):
    return f"{p:,.2f}"


def daily_target(r):
    """當日目標價：每天用最新收盤重新算，限制在現價 +TARGET_MIN_PCT%～+TARGET_MAX_PCT%。
    還在箱子裡或剛突破 → 箱頂＋一個箱子高度；在前高下方 → 前 20 日高點；都算不出來 → 現價 +8%。"""
    c = float(r["close"])
    top, bot, ph = r.get("box_top", np.nan), r.get("box_bottom", np.nan), r.get("prev_high20", np.nan)
    if r.get("setup") == "PULLBACK" and pd.notna(ph):
        tgt = ph
    elif pd.notna(top) and pd.notna(bot) and top > bot:
        tgt = top + (top - bot)
    elif pd.notna(ph) and ph > c:
        tgt = ph
    else:
        tgt = c * (1 + TARGET_MIN_PCT / 100)
    tgt = min(max(tgt, c * (1 + TARGET_MIN_PCT / 100)), c * (1 + TARGET_MAX_PCT / 100))
    return to_tick(tgt)


def buy_plan(r):
    """可買股票的 委託價格區間／目標價／停損價／持股週期。"""
    c, cost = float(r["close"]), r["inst_cost"]
    if r["setup"] == "BREAKOUT":
        top, bot = r["box_top"], r["box_bottom"]
        tgt = (top + (top - bot)) if pd.notna(top) and pd.notna(bot) else c * 1.08   # 箱型突破：再漲一個箱子高度
        days = "3-7 天"
    else:                                                    # PULLBACK
        tgt = r["prev_high20"] if pd.notna(r["prev_high20"]) else c * 1.08          # 低接：回到前高
        days = "5-10 天"
    tgt = min(max(tgt, c * (1 + TARGET_MIN_PCT / 100)), c * (1 + TARGET_MAX_PCT / 100))
    if pd.notna(cost):
        # 籌碼面：委託價掛在法人成本～現價之間（最多往下 3%，太低會買不到）；停損＝跌破法人成本 3%
        low = min(max(cost, c * 0.97), c)
        stop = cost * (1 - INST_COST_STOP_PCT / 100)
    else:
        # 算不出法人成本（買超天數太少）時退回技術面
        low = c * 0.99 if r["setup"] == "BREAKOUT" else max(float(r["ma20"]), c * 0.98)
        stop = float(r["setup_stop"])
    stop = max(stop, c * (1 - STOP_LOSS_PCT / 100))          # 最多虧 STOP_LOSS_PCT
    return {"low": to_tick(low, up=True), "high": to_tick(c), "target": to_tick(tgt),
            "stop": to_tick(stop), "days": days}


def far_from_cost(r):
    return pd.notna(r["inst_cost"]) and r["close"] > r["inst_cost"] * (1 + INST_COST_MAX_GAP / 100)


def pick_buys(radar):
    """可買：突破型＋低接型，乖離太大的不算（移到觀察）。依法人買超力道排序。"""
    b = radar[radar["setup"].isin(["BREAKOUT", "PULLBACK", "ACCUM"]) & ~radar["overextended"]].copy()
    b = b[~b.apply(far_from_cost, axis=1)] if not b.empty else b
    b = b[b.apply(lambda r: inst_light(r).startswith("🟢"), axis=1)] if not b.empty else b   # 可買一定要法人🟢買進
    if not b.empty and "runup60" in b.columns:
        b = b[~(b["runup60"] > MAX_RUNUP_PCT) & ~(b["runup60"] < MIN_RUNUP_PCT)]   # 離低點 +5%～+20% 才列可買
    if not b.empty:
        b = b[~(b["inst_20d"] <= INST20_MIN_BUY) & ~(b["inst_buydays10"] < INST10_MIN_DAYS)]   # 法人真的在回補
        near = (b["setup"] != "BREAKOUT") & (b["room_pct"] < MIN_ROOM_PCT)                   # 壓力太近（突破型已站上，不算）
        b = b[~near]
    if b.empty:
        return b
    b["_force"] = (b["trust_5d"] + b["foreign_5d"]) / b["avg_vol_5d"].where(b["avg_vol_5d"] > 0)
    b["_ord"] = b["setup"].map(SETUP_ORDER).fillna(9)
    return b.sort_values(["_ord", "_force"], ascending=[True, False], na_position="last").head(BUY_MAX_N)


def pick_watches(radar, buy_ids):
    """觀察：等勾頭的低接、乖離太大的突破（等拉回）、靠近箱底的整理股。回傳 [(row, 等什麼)]。"""
    out = []
    for _, r in radar[radar["setup"] == "PULLBACK_WAIT"].iterrows():
        base = r["inst_cost"] if pd.notna(r["inst_cost"]) else max(float(r["ma20"]), r["close"] * 0.98)
        out.append((r, f"等 K 值勾頭，{fmt_p(to_tick(min(float(base), float(r['close'])), True))} 附近可買"))
    cand = radar[radar["setup"].isin(["BREAKOUT", "PULLBACK"])]
    ext = cand[cand["overextended"] | cand.apply(far_from_cost, axis=1)] if not cand.empty else cand
    for _, r in ext.sort_values("vol_ratio_5d", ascending=False).iterrows():
        back = r["inst_cost"] if pd.notna(r["inst_cost"]) else r["ma10"]
        out.append((r, f"離法人成本太遠，等拉回 {fmt_p(to_tick(float(back), True))} 附近"))
    box = radar[(radar["setup"] == "BOX") & (radar["box_pos"] <= 30)].sort_values("box_pos")
    for _, r in box.iterrows():
        out.append((r, f"整理中，帶量站上 {fmt_p(to_tick(float(r['box_top']), True))} 再買"))
    near = radar[radar["setup"].isin(["ACCUM", "PULLBACK"]) & (radar["room_pct"] < MIN_ROOM_PCT)]
    for _, r in near.iterrows():
        out.insert(0, (r, f"壓力 {fmt_p(to_tick(float(r['res30']), True))} 太近，站上再買"))
    out = [x for x in out if not inst_light(x[0]).startswith("🔴")]      # 法人在賣的不列觀察
    return [x for x in out if x[0]["stock_id"] not in buy_ids][:WATCH_MAX_N]


def hold_verdict(r, cost):
    """持股判斷：回傳 (等級, 原因, 建議)。等級：SELL／WARN／HOLD。"""
    c = float(r["close"])
    t5, f5 = r["trust_5d"], r["foreign_5d"]
    i3, streak, icost = r["inst_3d"], r["inst_sell_streak"], r["inst_cost"]
    exit_px = icost * (1 - INST_COST_STOP_PCT / 100) if pd.notna(icost) else (r["ma10"] if pd.notna(r["ma10"]) else np.nan)
    if cost:
        exit_px = max(exit_px, cost * (1 - STOP_LOSS_PCT / 100)) if pd.notna(exit_px) else cost * (1 - STOP_LOSS_PCT / 100)
    ex = f"{fmt_p(to_tick(exit_px))}" if pd.notna(exit_px) else "-"
    # 🔴 賣出：法人明顯撤走，或已經觸發停損
    if cost and c <= cost * (1 - STOP_LOSS_PCT / 100):
        return "SELL", f"虧損超過 {STOP_LOSS_PCT:g}%", "已到停損，建議賣出"
    if t5 < 0 and f5 < 0:
        return "SELL", "投信、外資 5 日都在賣", "法人資金撤走，建議賣出"
    if pd.notna(icost) and c < icost * (1 - INST_COST_STOP_PCT / 100):
        return "SELL", f"跌破法人成本 {fmt_p(icost)}", "法人也套牢了，建議賣出"
    if r["exploded"]:
        return "SELL", "爆量急漲，散戶衝進來", "建議獲利了結"
    # ⚠ 警示：法人開始撤的跡象
    if pd.notna(streak) and streak >= 2:
        return "WARN", f"法人連 {int(streak)} 天賣超", f"先減碼或不加碼，跌破 {ex} 全賣"
    if pd.notna(i3) and i3 < 0 and (t5 + f5) > 0:
        return "WARN", "5 日還是買超，但近 3 日轉賣", f"留意，跌破 {ex} 就賣"
    if (t5 + f5) < 0:
        return "WARN", "土洋 5 日合計賣超", f"留意，跌破 {ex} 就賣"
    if pd.notna(r["ma10"]) and c < r["ma10"]:
        return "WARN", "跌破 10 日線", f"留意，跌破 {ex} 就賣"
    return "HOLD", "法人還在買", f"續抱，跌破 {ex} 再走"


def check_holdings(radar, holdings):
    """回傳 (該賣清單, 續抱清單)，每筆 dict。"""
    rows = radar.drop_duplicates("stock_id").set_index("stock_id")
    sell, keep = [], []
    for sid, (cost, group) in holdings.items():
        if sid not in rows.index:
            keep.append({"sid": sid, "name": "", "close": np.nan, "cost": cost, "why": "今天沒有行情", "group": group})
            continue
        r = rows.loc[sid]
        why = sell_reason(r, cost)
        d = {"sid": sid, "name": r["stock_name"], "close": float(r["close"]), "cost": cost, "why": why, "group": group}
        (sell if why else keep).append(d)
    return sell, keep


def long_upper_shadow(r):
    """爆量長上影線：量 ≥ 前 5 日均量 2 倍、上影線 ≥ 3%、收盤在當天高低區間下半段。沒有高低價資料時不判斷。"""
    hi, lo, c, vr = r.get("high", np.nan), r.get("low", np.nan), r.get("close", np.nan), r.get("vol_ratio_5d", np.nan)
    if any(pd.isna(x) for x in (hi, lo, c, vr)) or hi <= lo or c <= 0:
        return False
    return (vr >= SHADOW_VOL_RATIO and (hi - c) / c * 100 >= SHADOW_MIN_PCT
            and (c - lo) / (hi - lo) <= SHADOW_MAX_POS)


def inst_light(r):
    """法人動態燈號：🟢 買進／🟡 持有／🔴 賣出。"""
    if long_upper_shadow(r):
        return "🔴 賣出"                              # 爆量長上影線：不管是法人還是主力在倒貨
    icost = r.get("inst_cost", np.nan)
    if pd.notna(icost) and r["close"] < icost * (1 - BELOW_COST_PCT / 100):
        return "🔴 賣出"                              # 跌破法人成本 3%：這一波失敗
    t5, f5, i3, streak = r["trust_5d"], r["foreign_5d"], r["inst_3d"], r["inst_sell_streak"]
    net5 = t5 + f5
    if (t5 < 0 and f5 < 0) or (net5 < 0 and pd.notna(streak) and streak >= 2):
        return "🔴 賣出"
    # 單日大賣：法人突然倒貨，不等 5 日合計轉負
    d1, vol = r.get("inst_1d", np.nan), r.get("volume", np.nan)
    if pd.notna(d1) and d1 < 0 and pd.notna(vol) and vol > 0:
        sell, prior = -d1, net5 - d1                 # prior＝今天以前那幾天的累積買超
        vol_pct = sell / vol * 100
        if vol_pct >= DUMP_VOL_PCT or (prior > 0 and sell >= prior * DUMP_GIVEBACK and vol_pct >= DUMP_MIN_VOL_PCT):
            return "🔴 賣出"
    avg5 = r.get("avg_vol_5d", np.nan)
    avg_pct = (-d1 / avg5 * 100) if (pd.notna(d1) and d1 < 0 and pd.notna(avg5) and avg5 > 0) else 0.0
    if avg_pct >= DUMP_AVGVOL_PCT:
        return "🔴 賣出"                              # 賣超超過前 5 日均量 15%
    chg, vr = r.get("chg_1d", np.nan), r.get("vol_ratio_5d", np.nan)
    if pd.notna(chg) and pd.notna(vr) and chg <= -DROP_PCT and vr >= DROP_VOL_RATIO:
        return "🟡 持有"                              # 爆量下跌：有人在倒貨（多半是主力）
    if net5 > 0 and (pd.isna(i3) or i3 > 0) and not (pd.notna(streak) and streak >= 1):
        return "🟢 買進"
    return "🟡 持有"


def fmt_now(c):
    t = f"{c:,.2f}".rstrip("0").rstrip(".")
    return t


SETUP_SHORT = {"BREAKOUT": "突破", "PULLBACK": "低接", "ACCUM": "佈局", "REBOUND": "回補",
               "LT_ACCUM": "佈局（法人在底部一直買，先買一半）", "LT_BREAK": "噴出（帶量長紅；可以買，已經買了就加碼）",
               "LT_TURN": "轉強（低點盤整後站上 5 日、10 日線，還在月線下）"}


def stock_block(r, sid=None, target=False, kind=False):
    sid = sid or r["stock_id"]
    tgt = f"目標價格：{fmt_now(daily_target(r))}\n" if target else ""
    if kind and SETUP_SHORT.get(r.get("setup", "")):
        tgt += f"類型：{SETUP_SHORT[r['setup']]}\n"
    return (f"股票代號：{r['stock_name']}({sid})\n"
            f"目前價格：{fmt_now(float(r['close']))}\n"
            f"{tgt}"
            f"法人動態：{inst_light(r)}")


BEST_MAX_N = 20            # 「第一根候選」最多列幾檔
PICK_GROUPS = {"A候選": "A 套：還沒突破、快突破（吸籌末端·第一根）", "A買進": "A 套：已突破、模擬買進",
               "B可買": "B 套：精簡信的「可買」", "B觀察": "B 套：精簡信的「觀察」"}


def select_picks(radar):
    """今天信裡要列的股票。股價上限 MAX_PRICE 對 A、B 兩套都有效。回傳 (A候選, B可買, B觀察[(row, 等什麼)])。"""
    cheap = radar[radar["close"] <= MAX_PRICE]
    best = cheap[cheap["signal_key"].isin(GROUP_BEST)].head(BEST_MAX_N)     # radar 已依訊號等級、成交金額排序
    buys = pick_buys(cheap)
    watches = pick_watches(cheap, set(buys["stock_id"]) if not buys.empty else set())
    return best, buys, watches


def save_picks(radar, data_date, version=None):
    """把今天 A、B 兩套的推薦存進資料庫（同一天重跑會覆蓋，不會重複）。"""
    version = version or VERSION
    best, buys, watches = select_picks(radar)
    rows = []
    for _, r in best.iterrows():
        rows.append((data_date, r["stock_id"], r["stock_name"], "A候選", float(r["close"]),
                     f"突破價 {fmt_now(float(r['prev_high20']))}" if pd.notna(r["prev_high20"]) else ""))
    for _, r in buys.iterrows():
        try:
            p = buy_plan(r)
            note = f"掛單 {fmt_now(p['low'])}~{fmt_now(p['high'])} 目標 {fmt_now(p['target'])} 停損 {fmt_now(p['stop'])}"
        except Exception:
            note = ""
        rows.append((data_date, r["stock_id"], r["stock_name"], "B可買", float(r["close"]), note))
    for r, why in watches:
        rows.append((data_date, r["stock_id"], r["stock_name"], "B觀察", float(r["close"]), why))
    try:                                           # A 套「已突破、模擬買進」：從追蹤紀錄抓當天買進的（重跑也不會漏）
        with db() as conn:
            tb = pd.read_sql("SELECT stock_id, stock_name, entry_price FROM tracking WHERE version=? AND entry_date=?",
                             conn, params=(version, data_date), dtype={"stock_id": str})
        for _, t in tb.iterrows():
            rows.append((data_date, t["stock_id"], t["stock_name"], "A買進", float(t["entry_price"]), ""))
    except Exception as e:
        print(f"讀取 A 套買進紀錄失敗：{e}")
    with db() as conn:
        conn.execute("DELETE FROM picks WHERE date=?", (data_date,))
        conn.executemany("INSERT OR REPLACE INTO picks (date, stock_id, stock_name, grp, close, note) "
                         "VALUES (?,?,?,?,?,?)", rows)
    from collections import Counter
    print("今日推薦已存檔：", dict(Counter(x[3] for x in rows)))


def plan_text(r):
    """可買股票的「建議操作」：掛單價格區間、停損價、預計持有天數（算不出來的欄位用現價與停損比例補上）。"""
    try:
        p = buy_plan(r)
    except Exception:
        return ""
    c = float(r["close"])
    ok = lambda v: v is not None and not pd.isna(v)
    low = p["low"] if ok(p["low"]) else to_tick(c, up=True)
    high = p["high"] if ok(p["high"]) else to_tick(c)
    stop = p["stop"] if ok(p["stop"]) else to_tick(c * (1 - STOP_LOSS_PCT / 100))
    rng = fmt_now(high) if low >= high else f"{fmt_now(low)}～{fmt_now(high)}"
    return (f"\n建議操作：{rng} 掛單，買不到不追"
            f"\n停損價格：{fmt_now(stop)}，跌破就賣"
            f"\n預計持有：{p['days']}")


# ───────────────────────── 長線追蹤：法人賣到快沒貨 → 等法人回來 ─────────────────────────
LT_PIVOT_WIN = 10          # 波段高點：前後 10 天內最高的那一天
LT_PEAK_RECENT = 60        # 這個高點要是最近 60 個交易日內的（這一輪剛下來）
LT_MIN_ATR_DROP = 4.0      # 從高點下來的幅度至少是平常日波動（ATR）的此倍數，才算真的「下來」（跟著每檔自己的波動算）
LT_MAX_RETRACE = 0.5       # 還在低檔：從低點彈回不超過「這一輪跌幅」的一半
LT_REBOUND_DAYS = 5        # 🟢 低點立即反彈：低點出現後幾天內的長紅
LT_CANDLE_ATR = 1.0        # 🟢 長紅：今天漲幅至少 1 倍 ATR，而且收在當天高低區間的上段
LT_CANDLE_POS = 0.6        # 長紅收盤位置：0＝最低、1＝最高
LT_BREAK_VOL = 1.5         # 🟢 長紅要帶量：成交量至少是前 5 日均量的此倍數
LT_MIN_ROOM = 8.0          # 🟢 上面空間：離前高、離最近的大黑棒至少此 %
LT_BLACK_ATR = 1.2         # 大黑棒：單日跌幅（收盤對收盤）至少 1.2 倍 ATR
LT_BLACK_FADE_ATR = 2.0    # 大黑棒：或是「開高走低」，盤中最高到收盤跌了 2 倍 ATR 以上
LT_BLACK_BODY_ATR = 1.0    # 大黑棒（有開盤價時）：實體（開盤－收盤）至少 1 倍 ATR，不管量大量小
LT_WICK_ATR = 1.0          # 長上影線：最高價到實體上緣至少 1 倍 ATR
LT_TOP_BEFORE = 5          # 頭部範圍：高點前幾天
LT_TOP_AFTER = 10          # 頭部範圍：高點後幾天
LT_TOP_BAND = 0.25         # 頭部套牢區只算價位在這一波最上面 25% 的 K 棒（下跌途中的不算）
LT_CRASH_ATR = 2.0         # 下跌途中單日（收盤對收盤，含跳空）跌超過 2 倍 ATR＝大黑棒，也算套牢區（鈊象 8/21 只有 1.5 倍，不算）
LT_ZONE_NEAR = 5.0         # 🔴 現價卡在頭部套牢區裡，或離套牢區底部不到此 %：一噴就撞到黑 K／上影線被壓回
LT_CHOP_WAVE = 0.10        # 亂不亂：把一年走勢切成漲跌超過 10% 的波段
LT_CHOP_DAYS = 10.0        # 每段波段中位數不到 10 天＝上下太快（例：士電 9.5 天），整檔不追；好例子鈊象 49、佳必琪 20、富邦媒 15.5、晶技 13
LT_TURN_MIN_D = 2          # 🟢 轉強：低點 2～10 天前（低點後盤整幾天）
LT_TURN_MAX_D = 10
# 長均保護短均：🟡、🟢 都要半年線（120 日）比 20 天前高（回測：半年線往上勝率 73%、+10.5%；往下 64%、+4.3%）
LT_TURN_MIN_GAP = 2.0      # 🟢 轉強：收盤離月線至少 2%（太近一碰月線就被壓回；回測 0～2% 勝率 52%，2% 以上 65～78%）
LT_INTRADAY_N = 40         # 盤中信最多盯幾檔 🟡
LT_MIN_DAYS = 80           # 至少要有幾天股價資料才判斷
LT_MAX_N = 15              # 信裡最多列幾檔
LT_ACCUM_DAYS = 7          # 🟢 佈局：法人近 10 天至少幾天買超（在底部一直買）
LT_WATCH_DAYS = 5          # 🟡 法人開始買：近 10 天至少幾天買超


def _swing(hi, lo, cl, atr):
    """找最近一個「從高點下來」的波段：回傳 (高點索引, 高點, 低點索引, 低點)，找不到回傳 None。"""
    n = len(hi)
    for h in range(n - 2, max(LT_PIVOT_WIN, n - LT_PEAK_RECENT) - 1, -1):
        if hi[h] < np.nanmax(hi[max(0, h - LT_PIVOT_WIN):min(n, h + LT_PIVOT_WIN + 1)]):
            continue                                            # 不是波段高點
        k = h + int(np.nanargmin(lo[h:]))
        if hi[h] - lo[k] >= LT_MIN_ATR_DROP * atr:
            return h, float(hi[h]), k, float(lo[k])
    return None


def _wave_days(cl, th=LT_CHOP_WAVE):
    """把一年的收盤切成「漲或跌超過 th」的波段，回傳每段的中位天數；波段太少回傳無限大。"""
    cl = cl[~np.isnan(cl)]
    if len(cl) < 2:
        return np.inf
    hi_i = lo_i = 0
    dirn, piv = 0, []
    for i in range(1, len(cl)):
        if cl[i] > cl[hi_i]:
            hi_i = i
        if cl[i] < cl[lo_i]:
            lo_i = i
        if dirn >= 0 and cl[i] <= cl[hi_i] * (1 - th):
            piv.append(hi_i); dirn, lo_i = -1, i
        elif dirn <= 0 and cl[i] >= cl[lo_i] * (1 + th):
            piv.append(lo_i); dirn, hi_i = 1, i
    return float(np.median(np.diff(piv))) if len(piv) >= 3 else np.inf


def build_longtrack(radar):
    """從高點下來的股票：🔴 正在下來 → 🟡 低點整理、法人有撐 → 🟢 起漲第一根（立即反彈或整理後噴出），上面要有空間。"""
    cols = ["stock_id", "stock_name", "close", "qfii_now", "qfii_peak", "lt_light", "lt_text", "lt_order", "drop_rel",
            "bottom", "base_high", "peak", "room", "base_days", "turn_px", "s4", "s9", "s19"]
    px = load_table("prices", 365)
    px = px.sort_values(["stock_id", "date"])                 # 上市、上櫃都看
    if px.empty:
        return pd.DataFrame(columns=cols)
    inst = load_table("institutional", 45)
    inst["net"] = pd.to_numeric(inst["foreign_net"], errors="coerce").fillna(0) + pd.to_numeric(inst["trust_net"], errors="coerce").fillna(0)
    ig = {k: g.sort_values("date") for k, g in inst.groupby("stock_id")}
    fh = load_table("foreign_hold", 400)
    fq = {k: g.sort_values("date")["qfii_ratio"].astype(float).to_numpy() for k, g in fh.groupby("stock_id")} if not fh.empty else {}
    rows = radar.drop_duplicates("stock_id").set_index("stock_id")
    out = []
    for sid, p in px.groupby("stock_id"):
        if sid not in rows.index or sid in EXCLUDE_TOOL_STOCKS or sid.startswith("00") or len(p) < LT_MIN_DAYS:
            continue
        r = rows.loc[sid]
        c = float(r["close"])
        if c < MIN_PRICE or not r.get("turnover", 0) >= MIN_DAILY_TURNOVER:
            continue
        cl = pd.to_numeric(p["close"], errors="coerce").to_numpy()
        if _wave_days(cl[-240:]) < LT_CHOP_DAYS:
            continue                                            # 一年來上下太快、很亂（士電型）：來不及跑也來不及追，不追
        hi = pd.to_numeric(p["high"], errors="coerce").fillna(pd.Series(cl, index=p.index)).to_numpy()
        lo = pd.to_numeric(p["low"], errors="coerce").fillna(pd.Series(cl, index=p.index)).to_numpy()
        vol = pd.to_numeric(p["volume"], errors="coerce").to_numpy()
        n = len(cl)
        tr = np.maximum(hi[1:], cl[:-1]) - np.minimum(lo[1:], cl[:-1])
        atr = float(np.nanmean(tr[-20:]))
        if not atr > 0:
            continue
        sw = _swing(hi, lo, cl, atr)
        if sw is None:
            continue
        h, H, k, L = sw
        if H <= L:
            continue
        retr = (c - L) / (H - L)
        retr_prev = (cl[-2] - L) / (H - L)                      # 昨天還在不在低檔
        d = n - 1 - k                                           # 低點是幾天前
        # 頭上的壓力：「頭部那一段」（高點前 5 天到後 10 天、而且價位在這一波上段）的黑 K 和長上影線＝套牢區
        # 下跌途中的黑 K 不算頭（例：鈊象 8/21）
        op = pd.to_numeric(p["open"], errors="coerce").to_numpy() if "open" in p.columns else np.full(n, np.nan)
        top_line = H - LT_TOP_BAND * (H - L)
        zones = []                                                         # (套牢區下緣, 上緣)
        for j in range(max(1, h - LT_TOP_BEFORE), min(n - 1, h + LT_TOP_AFTER + 1)):
            if hi[j] < top_line:
                continue
            o = op[j] if pd.notna(op[j]) else cl[j - 1]                   # 舊資料沒開盤價：用前一天收盤代替
            body_top, body_bot = max(o, cl[j]), min(o, cl[j])
            black = (o - cl[j]) >= LT_BLACK_BODY_ATR * atr                 # 開高收低的黑 K
            wick = (hi[j] - body_top) >= LT_WICK_ATR * atr                 # 長上影線：盤中拉高被賣下來
            if black or wick:
                zones.append((float(body_bot if black else body_top), float(hi[j])))
        # 下跌途中的「大跌黑棒」（含跳空）：前一天收盤到當天收盤跌超過 2 倍 ATR，前一天收盤以下到當天收盤都是套牢區（例：胡連 9/3 跳空殺 7%）
        tr_all = np.r_[np.nan, np.maximum(hi[1:], cl[:-1]) - np.minimum(lo[1:], cl[:-1])]
        atr_then = pd.Series(tr_all).rolling(20, min_periods=10).mean().shift(1).to_numpy()   # 那一天之前的平常波動
        for j in range(h + 1, k + 1):
            if pd.notna(atr_then[j]) and cl[j - 1] - cl[j] >= LT_CRASH_ATR * atr_then[j]:
                zones.append((float(cl[j]), float(max(cl[j - 1], hi[j]))))

        def headroom(x):
            """從價格 x 往上看：回傳 (空間 %, 壓力價, 說明)。"""
            if any(zb <= x < zt for zb, zt in zones):
                return 0.0, x, f"已經卡在大黑棒／上影線的套牢區（前高 {fmt_now(H)}），一噴就會被壓回"
            above = [zb for zb, _ in zones if zb > x]
            if above and (min(above) / x - 1) * 100 < LT_ZONE_NEAR:
                return (min(above) / x - 1) * 100, min(above), f"上面 {fmt_now(min(above))} 就是大黑棒／上影線的套牢區，一噴就會被壓回"
            return (H / x - 1) * 100, H, f"上面前高 {fmt_now(H)} 空間不夠"
        # 今天是不是「長紅」：帶量、漲幅夠、收在上段
        rng = hi[-1] - lo[-1]
        strong = (cl[-1] - cl[-2] >= LT_CANDLE_ATR * atr and rng > 0 and (cl[-1] - lo[-1]) / rng >= LT_CANDLE_POS
                  and vol[-1] >= LT_BREAK_VOL * np.nanmean(vol[-6:-1]))
        cons_high = float(np.nanmax(cl[k + 1:-1])) if d >= 2 else np.nan   # 噴出價格＝低點之後整理區的最高收盤（不含今天；上影線不算站穩）
        room, nearest, press = headroom(c)                                # 現在買：從現價往上算
        wait_px = max(c, cons_high) if pd.notna(cons_high) else c
        room_w, nearest_w, press_w = headroom(wait_px)                    # 等噴出：從噴出價往上算（盤中站上噴出價就買）
        i5 = ig.get(sid)
        net = i5["net"].to_numpy() if i5 is not None else np.array([])
        inst_today = len(net) > 0 and net[-1] > 0
        inst3 = len(net) >= 3 and net[-3:].sum() > 0
        bd10 = int((net[-10:] > 0).sum()) if len(net) >= 10 else 0
        sum10 = float(net[-10:].sum()) if len(net) >= 10 else 0.0
        accum = sum10 > 0 and bd10 >= LT_ACCUM_DAYS                # 法人在底部一直買
        inst10 = sum10 > 0 and bd10 >= LT_WATCH_DAYS               # 法人開始買
        quiet = not inst_light(r).startswith("🔴")              # 沒有爆量上影、法人倒貨
        # 轉強：低點後在 5 日、10 日線下盤整，第一次收盤同時站上 5 日、10 日線，但還沒站上月線（例：東陽 8/4、凌華 9/18）
        ser = pd.Series(cl)
        m5, m10, m20 = (ser.rolling(w).mean().to_numpy() for w in (5, 10, 20))
        above510 = (cl > m5) & (cl > m10)
        turn = (LT_TURN_MIN_D <= d <= LT_TURN_MAX_D and bool(above510[-1]) and c < m20[-1]
                and not above510[k + 1:-1].any())
        turn_px = float(max(m5[-1], m10[-1])) if not above510[-1] else np.nan   # 收盤要站上的價位
        rebound = strong and d <= LT_REBOUND_DAYS               # 低點立即反彈
        breakout = strong and d > LT_REBOUND_DAYS and pd.notna(cons_high) and c > cons_high   # 整理後噴出
        if retr > LT_MAX_RETRACE and not ((rebound or breakout) and retr_prev <= LT_MAX_RETRACE):
            continue                                            # 已經彈回一半以上、又不是「昨天還在低檔、今天噴出」：不是低檔了
        # 燈號（10/7 版）：🔴 還在破底／跌破低點 → 🟡 止跌、低點盤整（低點不能破）→ 🟢 收盤同時站上 5 日、10 日線、還沒到月線
        shadow = long_upper_shadow(r)
        if c >= m20[-1]:
            continue                                            # 已經站上月線：不管轉強還是噴出都太晚了，不列（已經買的看「我的持股」）
        room_w, nearest_w, press_w = headroom(max(c, turn_px) if pd.notna(turn_px) else c)   # 🟡：從轉強價格往上算空間
        inst5 = len(net) >= 5 and net[-5:].sum() > 0              # 法人 5 日買超（回測：有買超勝率 66%，賣超只有 52%）
        gap20 = (m20[-1] / c - 1) * 100                          # 離月線還有幾 %
        m120 = ser.rolling(120).mean().to_numpy()
        up120 = len(m120) > 140 and m120[-1] > m120[-21]           # 半年線往上：長均保護短均
        if not up120:
            light, order = "🔴", 2
            text = (f"半年線往下（{fmt_now(float(m120[-1]))}），長期趨勢向下，反彈容易被壓回" if pd.notna(m120[-1])
                    else "資料不到半年，看不出長期趨勢")
        elif turn and gap20 < LT_TURN_MIN_GAP:
            light, text, order = "🟡", f"站上 5 日、10 日線了，但離月線只剩 {gap20:.1f}%，太近容易被壓回", 1
        elif turn and room >= LT_MIN_ROOM and not shadow and inst5 and quiet:
            light, text, order = "🟢", "站上 5 日、10 日線（還在月線下），法人也在買，可以買", 0
        elif turn and room >= LT_MIN_ROOM and not shadow:
            light, text, order = "🟡", "站上 5 日、10 日線了，但法人還在賣，等法人回來", 1
        elif turn and room < LT_MIN_ROOM:
            light, text, order = "🔴", f"站上 5 日、10 日線了，但{press}", 2
        elif (rebound or breakout) and room >= LT_MIN_ROOM and inst_today and inst3 and quiet:
            light, order = "🟢", 0
            text = "低點立即反彈，可以買" if rebound else "整理後噴出一根，可以買（已經買了就加碼）"
        elif (rebound or breakout) and room < LT_MIN_ROOM:
            light, text, order = "🔴", f"出長紅了，但{press}", 2
        elif d < LT_TURN_MIN_D:
            light, text, order = "🔴", "還在破底，等止跌", 2
        elif room_w < LT_MIN_ROOM:
            light, text, order = "🔴", f"止跌了，但{press_w}", 2
        elif sum10 < 0:
            light, text, order = "🔴", f"止跌了，但法人近 10 天還在賣超 {abs(sum10) / 1000:,.0f} 張（沒有在收貨）", 2
        else:
            chip = "，法人在底部一直買" if accum else ("，法人開始買" if inst10 else "")
            light, text, order = "🟡", f"止跌盤整{chip}，等站上 5 日、10 日線（低點不能破）", 1
        q = fq.get(sid, np.array([]))
        out.append({"stock_id": sid, "stock_name": r["stock_name"], "close": c,
                    "qfii_now": float(q[-1]) if len(q) else np.nan, "qfii_peak": float(np.nanmax(q[-250:])) if len(q) else np.nan,
                    "lt_light": light, "lt_text": text, "lt_order": order,
                    "drop_rel": float(net[-10:].sum()) if len(net) else 0.0,
                    "bottom": L, "base_high": cons_high if pd.notna(cons_high) else c, "peak": H,
                    "room": room_w if light == "🟡" else room, "base_days": d, "turn_px": turn_px,
                    "s4": float(np.nansum(cl[-4:])), "s9": float(np.nansum(cl[-9:])), "s19": float(np.nansum(cl[-19:]))})
    df = pd.DataFrame(out, columns=cols)
    if not df.empty:
        df = df.sort_values(["lt_order", "base_days", "drop_rel"], ascending=[True, True, False]).reset_index(drop=True)  # 整理越短排越前面
    print(f"長線追蹤：{len(df)} 檔｜" + str(df["lt_light"].value_counts().to_dict() if not df.empty else {}))
    return df


def longtrack_buys(radar, track):
    """長線追蹤裡亮🟢（法人回來了）、而且當天法人燈號也是🟢的 → 列可買，類型「回補」。"""
    if track is None or track.empty:
        return radar.iloc[0:0]
    ids = set(track.loc[track["lt_light"] == "🟢", "stock_id"])
    b = radar[radar["stock_id"].isin(ids)].drop_duplicates("stock_id").copy()
    kind = track.set_index("stock_id")["lt_text"].to_dict()
    turn = {s for s, t in kind.items() if "站上 5 日" in t}
    # 轉強型（已經要求法人 5 日買超）：法人燈號不能是 🔴；其他類型要法人 🟢
    b = b[b.apply(lambda r: (not inst_light(r).startswith("🔴")) if r["stock_id"] in turn
                  else inst_light(r).startswith("🟢"), axis=1)] if not b.empty else b
    b["setup"] = b["stock_id"].map(lambda s: "LT_TURN" if s in turn else ("LT_ACCUM" if "先佈局" in kind.get(s, "") else "LT_BREAK"))
    b["lt_stop"] = b["stock_id"].map(track.set_index("stock_id")["bottom"].to_dict())
    b["_o"] = b["setup"].map({"LT_TURN": 0, "LT_BREAK": 1, "LT_ACCUM": 2})
    b = b.sort_values("_o").drop(columns="_o")
    return b


def add_points(sids):
    """持股的「加碼點」：法人 5 日買超＋今天帶量長紅、從月線下貫穿月線（回測：轉強後出現貫穿，加碼那筆平均 +10%）。"""
    out = {}
    if not sids:
        return out
    px = load_table("prices", 90)
    px = px[px["stock_id"].isin(sids)].sort_values(["stock_id", "date"])
    inst = load_table("institutional", 20)
    inst = inst[inst["stock_id"].isin(sids)].sort_values("date")
    for sid, p in px.groupby("stock_id"):
        cl = pd.to_numeric(p["close"], errors="coerce").to_numpy()
        if len(cl) < 26:
            continue
        hi = pd.to_numeric(p["high"], errors="coerce").fillna(pd.Series(cl, index=p.index)).to_numpy()
        lo = pd.to_numeric(p["low"], errors="coerce").fillna(pd.Series(cl, index=p.index)).to_numpy()
        vol = pd.to_numeric(p["volume"], errors="coerce").to_numpy()
        m20 = pd.Series(cl).rolling(20).mean().to_numpy()
        tr = np.maximum(hi[1:], cl[:-1]) - np.minimum(lo[1:], cl[:-1])
        atr = float(np.nanmean(tr[-21:-1]))
        g = inst[inst["stock_id"] == sid]
        n5 = float((pd.to_numeric(g["foreign_net"], errors="coerce").fillna(0)
                    + pd.to_numeric(g["trust_net"], errors="coerce").fillna(0)).tail(5).sum())
        if (cl[-1] > m20[-1] and cl[-2] <= m20[-2] and vol[-1] >= LT_BREAK_VOL * np.nanmean(vol[-6:-1])
                and cl[-1] - cl[-2] >= LT_CANDLE_ATR * atr and n5 > 0):
            out[sid] = f"🟢 加碼點：法人買超＋帶量長紅貫穿月線 {fmt_now(float(m20[-1]))}"
    return out


def build_simple_email(radar, data_date, holdings, track=None):
    """信件只放「長線追蹤」這套：法人賣到底 → 一直買、底部守住 → 帶量噴出才買。
    舊的佈局／突破／低接還是會算、存進紀錄（之後比較勝率用），但不寫進信裡。"""
    rows = radar.drop_duplicates("stock_id").set_index("stock_id")
    buys = longtrack_buys(radar, track)
    parts = ["法人動態：🟢 買進　🟡 持有／等待　🔴 賣出／繼續等"]

    parts.append(f"\n【可買】{len(buys)} 檔（從高點下來、低點盤整後轉強或噴出，上面有空間）")
    if buys.empty:
        parts.append("今天沒有，耐心等")
    for _, r in buys.iterrows():
        stop = r.get("lt_stop")
        blk = stock_block(r, target=True, kind=True)
        parts.append("\n" + blk
                     + (f"\n停損價格：{fmt_now(float(stop))}（低點，收盤跌破就賣）" if pd.notna(stop) else ""))

    watches = []
    if track is not None and not track.empty:
        t = track.head(LT_MAX_N)
        n_y = int((track["lt_light"] == "🟡").sum())
        parts.append(f"\n\n【追蹤：從高點下來】{len(track)} 檔（🟡 {n_y} 檔止跌盤整，列前 {len(t)} 檔）")
        for _, x in t.iterrows():
            parts.append(f"\n股票代號：{x['stock_name']}({x['stock_id']})\n"
                         f"目前價格：{fmt_now(float(x['close']))}\n"
                         f"前高／低點：{fmt_now(float(x['peak']))}／{fmt_now(float(x['bottom']))}（低點跌破就變 🔴）\n"
                         + (f"轉強價格：{fmt_now(float(x['turn_px']))}（收盤站上 5 日、10 日線就轉 🟢）\n" if pd.notna(x.get("turn_px")) and x["lt_light"] == "🟡" else "")

                         + f"法人動態：{x['lt_light']} {x['lt_text']}")
        watches = list(track.loc[track["lt_light"] == "🟡", "stock_id"])
    else:
        parts.append("\n\n【追蹤：從高點下來】今天沒有資料")

    n_sell = 0
    parts.append(f"\n\n【我的持股】{len(holdings)} 檔")
    if not holdings:
        parts.append("（股票追蹤清單是空的）")
    multi = len({g for _, g in holdings.values()}) > 1
    adds = add_points(list(holdings))
    cur = None
    for sid, (cost, group) in holdings.items():
        if multi and group != cur:
            cur = group
            parts.append(f"\n〔{group}〕")
        if sid not in rows.index:
            parts.append(f"\n股票代號：{sid}\n目前價格：今天沒有行情資料")
            continue
        r = rows.loc[sid]
        n_sell += inst_light(r).startswith("🔴")
        parts.append("\n" + stock_block(r, sid, target=True) + (f"\n{adds[sid]}" if sid in adds else ""))
    return "\n".join(parts) + "\n", len(buys), len(watches), n_sell


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
def run(send_mail=True, collect_only=False):
    init_db()
    print(f"=== 台股雷達 開始 {now_tw():%Y-%m-%d %H:%M}｜追蹤版本 {VERSIONS}（主要：{VERSION}）===")
    hist_price = load_table("prices", 120)
    quotes = collect_quotes(hist_price)
    if quotes.empty:
        print("今天抓不到行情，改用資料庫裡最新的資料計算")
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
    collect_qfii()
    try:
        update_monthly_and_prune()
    except Exception as e:
        print(f"月 K 更新／清理失敗（不影響寄信）：{e}")
    if collect_only:
        print("=== 只收資料完成（舊版訊號與信件已略過）===")
        return

    pf = make_price_features(load_table("prices", 120))
    inst_hist = load_table("institutional", 45)    # 45 個日曆天 ≈ 30 個交易日，夠算近 20 日累計
    inf = make_inst_features(inst_hist, pf)
    cost = make_inst_cost(inst_hist, load_table("prices", 30))
    if not inf.empty and not cost.empty:
        inf = inf.merge(cost, on="stock_id", how="left")
    hf = make_holder_features(load_table("holders", 60))
    mf = make_margin_features(load_table("margin", 30))
    radar = build_radar(pf, inf, hf, mf)
    if radar.empty:
        print("沒有可用資料，結束")
        raise SystemExit(1)

    data_date = str(pf["date"].max())
    save_signals(radar, data_date)
    try:
        radar.to_csv(os.path.join(OUTPUT_DIR, f"radar_{data_date}.csv"), index=False, encoding="utf-8-sig")
    except OSError as e:
        print(f"寫 CSV 失敗：{e}")
    results = {v: update_tracking(radar, data_date, v) for v in VERSIONS}
    recs, events = results[VERSION]
    track_text = build_tracking_text(recs, radar, events, VERSION)
    if len(VERSIONS) > 1:
        track_text += ("\n\n【版本對照（成績單會比較哪個版本表現比較好）】\n"
                       + "\n".join(compare_line(v, results[v][1]) for v in VERSIONS))
    holdings = load_holdings()
    hold_text, alerts = build_holdings_text(radar, holdings)
    # 詳細版（原因、指標、模擬追蹤、版本對照）只印在 GitHub Actions 執行紀錄裡，信件只寄精簡版
    print("\n===== 詳細報告（不寄信）=====\n" + build_email_body(radar, data_date, track_text, hold_text))
    try:
        save_picks(radar, data_date)
    except Exception as e:
        print(f"存推薦紀錄失敗（不影響寄信）：{e}")
    try:
        track = build_longtrack(radar)
        if not track.empty:
            track.to_csv(os.path.join(OUTPUT_DIR, f"longtrack_{data_date}.csv"), index=False, encoding="utf-8-sig")
            rb = longtrack_buys(radar, track)
            yl = track[track["lt_light"] == "🟡"].head(LT_INTRADAY_N)
            recs = ([(data_date, x["stock_id"], x["stock_name"], "C回補", float(x["close"]), "") for _, x in rb.iterrows()]
                    + [(data_date, x["stock_id"], x["stock_name"], "C等噴出", float(x["close"]),
                        f"{x['s4']}|{x['s9']}|{x['s19']}|{x['bottom']}") for _, x in yl.iterrows()])
            with db() as conn:
                conn.execute("DELETE FROM picks WHERE date=? AND grp IN ('C回補','C等噴出')", (data_date,))
                if recs:
                    conn.executemany("INSERT OR REPLACE INTO picks (date, stock_id, stock_name, grp, close, note) "
                                     "VALUES (?,?,?,?,?,?)", recs)
    except Exception as e:
        print(f"長線追蹤失敗（不影響寄信）：{e}")
        track = None
    body, n_buy, n_watch, n_sell = build_simple_email(radar, data_date, holdings, track)
    print("\n===== 信件內容 =====\n" + body)
    if send_mail:
        md = f"{int(data_date[5:7])}/{data_date[8:]}"
        subject = f"{'🚨' if n_sell else ''}台股雷達 {md}｜可買{n_buy} 止跌{n_watch}" + (f" 持股賣出{n_sell}" if n_sell else "")
        send_email(subject, body)
    print("=== 完成 ===")


def main():
    ap = argparse.ArgumentParser(description="台股雷達 v9.2")
    ap.add_argument("--backfill", type=int, default=0, metavar="N", help="先回補最近 N 個日曆天的上市行情與法人")
    ap.add_argument("--backfill-prices", type=int, default=0, metavar="N", help="只回補最近 N 個日曆天的上市股價（含高低價），不補法人與融資")
    ap.add_argument("--backfill-qfii", type=int, default=0, metavar="N", help="回補最近 N 個日曆天的外資持股比例（長線追蹤用）")
    ap.add_argument("--backfill-tpex", type=int, default=0, metavar="N", help="回補最近 N 個日曆天的上櫃股價與法人")
    ap.add_argument("--backfill-monthly", default="", metavar="TWSE|TPEx", help="回補上市或上櫃過去 3 年的月 K")
    ap.add_argument("--no-email", action="store_true", help="不寄信")
    ap.add_argument("--collect-only", action="store_true", help="只收資料進資料庫，不跑舊版訊號與信件")
    args = ap.parse_args()
    if args.backfill:
        init_db()
        backfill(args.backfill)
    if args.backfill_prices:
        init_db()
        backfill(args.backfill_prices, prices_only=True)
    if args.backfill_qfii:
        init_db()
        backfill_qfii(args.backfill_qfii)
    if args.backfill_tpex:
        init_db()
        backfill_tpex(args.backfill_tpex)
    if args.backfill_monthly:
        init_db()
        backfill_monthly(args.backfill_monthly, years=3)
    run(send_mail=not args.no_email, collect_only=args.collect_only)


if __name__ == "__main__":
    main()
