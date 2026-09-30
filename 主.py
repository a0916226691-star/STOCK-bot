# -*- coding: utf-8 -*-
"""
台股主動資金雷達 v6.1 整合版
完整行情 + 法人 + 技術 + RSS快訊 + 長線核心區 + 短線雷達區 + CSV + Gmail

v6.1 修正：
1. _is_nan() 改成 bool(pd.isna())，不會再誤判
2. RSS CDATA 正則從.\*? 修正成.*? ，快訊不會再被清成空白
"""

import os
import smtplib
import time
import random
import re
import html
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from collections import defaultdict

import numpy as np
import pandas as pd
import requests

TZ = timezone(timedelta(hours=8))
OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.twse.com.tw/zh/page/trading/fund/T86.html",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

MIN_DAILY_TURNOVER = 30_000_000
MIN_TRUST_5D_VOLUME_RATIO = 0.01
MIN_FOREIGN_5D_VOLUME_RATIO = 0.03
HEDGE_WATCH_RATIO = 0.03
HEDGE_DOMINANT_RATIO = 0.08
MAX_HISTORY_FILES = 90
MIN_HISTORY_DAYS_FOR_SIGNAL = 20
HIGH_VOLATILITY_STOCKS = {"6919", "4763"}
TW_MARKET_HOLIDAYS = {item.strip() for item in (os.getenv("TW_MARKET_HOLIDAYS") or "").split(",") if item.strip()}

FOCUS_STOCKS = ["2383", "2368", "6197", "3293", "4763", "1808", "6919", "1503"]
FOCUS_PROFILES = {
    "2383": {"name": "台光電", "theme": "AI伺服器／高速CCL", "valuation": "合理偏高（高成長支撐）", "good_catalyst": "高速材料需求與產品組合", "risk_catalyst": "銅箔、玻纖布等原料成本波動", "rating": "持續研究", "long_rating": "持續研究"},
    "2368": {"name": "金像電", "theme": "AI伺服器／交換器PCB", "valuation": "合理區間", "good_catalyst": "高階伺服器與交換器PCB需求", "risk_catalyst": "產能擴充與客戶拉貨節奏", "rating": "持續研究", "long_rating": "持續研究"},
    "6197": {"name": "佳必琪", "theme": "AI高速傳輸線束", "valuation": "成長型估值", "good_catalyst": "高速線纜與伺服器需求", "risk_catalyst": "伺服器出貨節奏與競爭壓力", "rating": "持續研究", "long_rating": "持續研究"},
    "3293": {"name": "鈊象", "theme": "網路遊戲／授權", "valuation": "偏高，須持續追蹤", "good_catalyst": "海外授權與產品營運", "risk_catalyst": "海外法規與評價修正", "rating": "持續研究", "long_rating": "持續研究"},
    "4763": {"name": "材料-KY", "theme": "材料／絲束", "valuation": "需觀察成長持續性", "good_catalyst": "供需與擴產效益", "risk_catalyst": "營收趨勢、供需反轉與評價變化", "rating": "持續研究", "long_rating": "持續研究"},
    "1808": {"name": "潤隆", "theme": "營建／高股息", "valuation": "資產與現金流導向", "good_catalyst": "完工認列、現金流與股利政策", "risk_catalyst": "工程進度、政策與房市景氣", "rating": "持續研究", "long_rating": "持續研究"},
    "6919": {"name": "康霈", "theme": "生技新藥", "valuation": "高不確定性題材估值", "good_catalyst": "臨床、授權與研發進展", "risk_catalyst": "臨床結果與資金需求風險", "rating": "高風險研究", "long_rating": "高風險研究"},
    "1503": {"name": "士電", "theme": "重電／變壓器／綠能", "valuation": "成長型估值", "good_catalyst": "電網投資、外銷訂單與產能", "risk_catalyst": "原物料、交期與評價修正", "rating": "持續研究", "long_rating": "持續研究"},
}
THEMES = {
    "AI伺服器／ODM": ["2317", "2382", "3231", "6669", "6805"],
    "散熱": ["3017", "3324", "6205", "6131"],
    "PCB／CCL／載板": ["2383", "2368", "2385", "3037", "4967", "6274", "6197"],
    "機器人／智慧自動化": ["4588", "1590", "2359", "4566"],
    "矽光子／CPO／光通訊": ["3163", "3363", "4979", "3450", "6451"],
    "重電／綠能": ["1513", "1519", "1503", "6873"],
}
THEME_MAP = {}
for theme_name, stock_ids in THEMES.items():
    for stock_id in stock_ids:
        THEME_MAP.setdefault(stock_id, []).append(theme_name)
EXCLUDE_TOOL_STOCKS = {"2330", "2454", "2308", "3711", "2881", "2882", "2884", "2886", "2891", "2892", "2880", "0050", "0056", "00878", "006208", "00919", "00929"}

def now_tw(): return datetime.now(TZ)
def today_str(): return now_tw().strftime("%Y-%m-%d")
def _is_nan(value):
    try: return bool(pd.isna(value))
    except Exception: return value is None
def normalize_stock_id(value):
    if _is_nan(value): return ""
    text = str(value).strip()
    if text.endswith(".0"): text = text[:-2]
    if text.isdigit() and len(text) < 4: return text.zfill(4)
    return text
def safe_float(value):
    if value is None: return np.nan
    try:
        text = str(value).strip()
        if text in {"", "-", "--", "---", "nan", "NaN", "None", "null", "—", "－"}: return np.nan
        text = text.replace(",", "").replace("%", "").replace("＋", "+").replace("－", "-").replace("—", "-").replace("–", "-")
        if text.startswith("+"): text = text[1:]
        return float(text)
    except Exception: return np.nan
def safe_int(value):
    number = safe_float(value)
    return np.nan if pd.isna(number) else int(number)
def safe_text(value):
    if _is_nan(value): return ""
    return html.escape(str(value).strip(), quote=True)
def sanitize_link(url):
    if not isinstance(url, str): return "#"
    url = url.strip()
    if url.startswith(("https://", "http://")): return html.escape(url, quote=True)
    return "#"
def is_market_trading_day(date_obj):
    if date_obj.weekday() >= 5: return False
    if date_obj.strftime("%Y-%m-%d") in TW_MARKET_HOLIDAYS: return False
    return True
def get_recent_trading_dates(days=20):
    dates = []; current_date = now_tw().date()
    while len(dates) < days:
        if is_market_trading_day(current_date): dates.append(current_date.strftime("%Y%m%d"))
        current_date -= timedelta(days=1)
    return dates
def get_last_trading_date(): return get_recent_trading_dates(1)[0]
def request_get(url, params=None, timeout=30):
    last_error = None
    for attempt in range(4):
        try:
            response = SESSION.get(url, params=params, timeout=timeout)
            if response.status_code in (403, 429):
                wait_seconds = 30 + random.uniform(1, 5)
                print(f"⚠️ 被風控 {response.status_code}，等待 {wait_seconds:.1f} 秒後重試...")
                time.sleep(wait_seconds); continue
            response.raise_for_status()
            return response
        except Exception as error:
            last_error = error
            wait_seconds = (2 ** attempt) + random.uniform(0, 1)
            print(f"連線警告：{url} 失敗，第 {attempt + 1} 次重試前等待 {wait_seconds:.1f} 秒：{error}")
            time.sleep(wait_seconds)
    raise RuntimeError(f"連線失敗：{url}｜最後錯誤：{last_error}")
def request_json(url, params=None, timeout=30):
    response = request_get(url, params=params, timeout=timeout)
    if not response.text or len(response.text.strip()) < 10: raise RuntimeError(f"API 回傳內容完全空白：{url}")
    if response.text.strip().startswith("<"):
        print(f"❌ API 回傳 HTML 非 JSON，疑似被擋！網址：{url}｜預覽：{response.text[:200]}")
        raise RuntimeError("回傳 HTML，可能被防火牆阻擋")
    try: return response.json()
    except Exception as error:
        print(f"❌ JSON 解析失敗！網址：{url}｜預覽：{response.text[:300]}")
        raise RuntimeError(f"JSON 解析失敗：{error}")
def cleanup_old_files(prefix):
    try:
        files = sorted(filename for filename in os.listdir(OUTPUT_DIR) if filename.startswith(prefix) and filename.endswith(".csv"))
        if len(files) > MAX_HISTORY_FILES:
            for filename in files[:-MAX_HISTORY_FILES]: os.remove(os.path.join(OUTPUT_DIR, filename))
    except Exception: pass

def _parse_twse_openapi(data):
    rows = []
    for item in data:
        stock_id = normalize_stock_id(item.get("Code", ""))
        close = safe_float(item.get("ClosingPrice"))
        if not stock_id.isdigit() or len(stock_id)!= 4 or pd.isna(close): continue
        rows.append({"stock_id": stock_id, "stock_name": str(item.get("Name", "")).strip(), "market": "TWSE", "close": close, "change": safe_float(item.get("Change")), "volume": safe_int(item.get("TradeVolume")), "turnover": safe_float(item.get("TradeValue"))})
    return pd.DataFrame(rows)
def get_twse_quotes_primary():
    print("取得 TWSE 上市行情（主源）...")
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    data = request_json(url); df = _parse_twse_openapi(data)
    if df.empty: raise RuntimeError("TWSE 主源解析後為空")
    print(f"TWSE 主源完成：{len(df)} 檔"); return df
def get_twse_quotes_backup():
    print("取得 TWSE 上市行情（備援 open_data）...")
    url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=open_data"
    data = request_json(url); df = _parse_twse_openapi(data)
    if df.empty: raise RuntimeError("TWSE 備援解析後為空")
    print(f"TWSE 備援完成：{len(df)} 檔"); return df
def get_twse_quotes():
    try: return get_twse_quotes_primary()
    except Exception as error:
        print(f"TWSE 主源失敗：{error}，切換備援"); return get_twse_quotes_backup()
def get_tpex_quotes():
    print("取得 TPEx 上櫃行情...")
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    data = request_json(url); rows = []
    for item in data:
        stock_id = normalize_stock_id(item.get("SecuritiesCompanyCode") or item.get("SecuritiesCode") or item.get("Code") or "")
        close = safe_float(item.get("Close") or item.get("ClosingPrice"))
        if not stock_id.isdigit() or len(stock_id)!= 4 or pd.isna(close): continue
        rows.append({"stock_id": stock_id, "stock_name": str(item.get("CompanyName") or item.get("SecuritiesName") or "").strip(), "market": "TPEx", "close": close, "change": safe_float(item.get("Change")), "volume": safe_int(item.get("Volume")), "turnover": safe_float(item.get("Amount"))})
    df = pd.DataFrame(rows)
    if df.empty: raise RuntimeError("TPEx 行情解析後為空")
    print(f"TPEx 行情完成：{len(df)} 檔"); return df
def get_all_quotes():
    frames = []
    for quote_function in [get_twse_quotes, get_tpex_quotes]:
        try:
            df = quote_function()
            if not df.empty: frames.append(df)
        except Exception as error: print(f"⚠️ 行情取得異常：{error}")
    if not frames: raise RuntimeError("❌ 上市與上櫃行情皆無法取得")
    quotes = pd.concat(frames, ignore_index=True)
    quotes["stock_id"] = quotes["stock_id"].map(normalize_stock_id)
    return quotes.drop_duplicates(subset=["stock_id"], keep="first")

def get_twse_institutional():
    print("取得 TWSE 法人資料...")
    url = "https://www.twse.com.tw/rwd/zh/fund/T86"
    data = None; used_date = None
    for date_code in get_recent_trading_dates(20):
        try:
            time.sleep(0.8)
            result = request_json(url, params={"response": "json", "date": date_code, "selectType": "ALLBUT0999"}, timeout=20)
            if result.get("stat") == "OK" and result.get("data"): data = result; used_date = date_code; break
        except Exception: continue
    if data is None: raise RuntimeError("找不到可用的 TWSE 法人資料")
    fields = data.get("fields", []); raw_rows = data.get("data", [])
    hedge_col = next((col for col in fields if "自營商買賣超股數" in col and "避險" in col), None)
    prop_col = next((col for col in fields if "自營商買賣超股數" in col and "自行買賣" in col), None)
    formatted_date = f"{used_date[:4]}-{used_date[4:6]}-{used_date[6:]}"
    rows = []
    for raw_row in raw_rows:
        item = dict(zip(fields, raw_row))
        stock_id = normalize_stock_id(item.get("證券代號", ""))
        if not stock_id.isdigit() or len(stock_id)!= 4: continue
        rows.append({"stock_id": stock_id, "date": formatted_date, "foreign_net": safe_int(item.get("外陸資買賣超股數(不含外資自營商)")), "trust_net": safe_int(item.get("投信買賣超股數")), "dealer_proprietary_net": safe_int(item.get(prop_col)) if prop_col else np.nan, "dealer_hedge_net": safe_int(item.get(hedge_col)) if hedge_col else np.nan, "hedge_data_available": hedge_col is not None})
    df = pd.DataFrame(rows)
    for column in ["foreign_net", "trust_net", "dealer_proprietary_net", "dealer_hedge_net"]: df[column] = pd.to_numeric(df[column], errors="coerce")
    print(f"TWSE 法人完成：{len(df)} 檔，資料日期：{formatted_date}")
    return df
def get_tpex_institutional():
    print("取得 TPEx 上櫃法人資料...")
    rows = []
    for date_code in get_recent_trading_dates(10):
        try:
            time.sleep(0.8)
            roc_year = int(date_code[:4]) - 1911
            roc_date = f"{roc_year}/{date_code[4:6]}/{date_code[6:]}"
            url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
            result = request_json(url, params={"l": "zh-tw", "o": "json", "se": "AL", "t": "D", "d": roc_date}, timeout=20)
            if not result.get("aaData"): continue
            for raw_row in result["aaData"]:
                stock_id = normalize_stock_id(raw_row[0])
                if not stock_id.isdigit() or len(stock_id)!= 4: continue
                rows.append({"stock_id": stock_id, "date": f"{date_code[:4]}-{date_code[4:6]}-{date_code[6:]}", "foreign_net": safe_int(raw_row[2]), "trust_net": safe_int(raw_row[3]), "dealer_hedge_net": safe_int(raw_row[5]) if len(raw_row) > 5 else np.nan, "hedge_data_available": True})
            if rows: break
        except Exception as error:
            print(f"TPEx {date_code} 失敗：{error}"); continue
    if not rows: print("⚠️ TPEx 法人無資料，回傳空表"); return pd.DataFrame()
    df = pd.DataFrame(rows); print(f"TPEx 法人完成：{len(df)} 檔"); return df
def get_all_institutional():
    frames = []
    try:
        twse = get_twse_institutional()
        if not twse.empty: frames.append(twse)
    except Exception as error: print(f"TWSE 法人失敗：{error}")
    try:
        tpex = get_tpex_institutional()
        if not tpex.empty: frames.append(tpex)
    except Exception as error: print(f"TPEx 法人失敗：{error}")
    if not frames: return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["stock_id", "date"], keep="last")

def load_history(prefix):
    filenames = sorted(filename for filename in os.listdir(OUTPUT_DIR) if filename.startswith(prefix) and filename.endswith(".csv"))[-MAX_HISTORY_FILES:]
    frames = []
    for filename in filenames:
        try:
            frame = pd.read_csv(os.path.join(OUTPUT_DIR, filename), dtype={"stock_id": str})
            if not frame.empty:
                frame["stock_id"] = frame["stock_id"].map(normalize_stock_id)
                frames.append(frame)
        except Exception: pass
    if frames: return pd.concat(frames, ignore_index=True)
    return pd.DataFrame()
def save_history(quotes, institutional, data_date):
    price_path = os.path.join(OUTPUT_DIR, f"layout_price_history_{data_date}.csv")
    quotes.copy().assign(date=data_date).to_csv(price_path, index=False, encoding="utf-8-sig")
    if institutional is not None and not institutional.empty:
        institutional_path = os.path.join(OUTPUT_DIR, f"layout_institutional_history_{data_date}.csv")
        institutional.to_csv(institutional_path, index=False, encoding="utf-8-sig")
    cleanup_old_files("layout_price_history_"); cleanup_old_files("layout_institutional_history_"); cleanup_old_files("layout_radar_")
def make_price_features(quotes, history, data_date):
    needed_columns = ["stock_id", "stock_name", "market", "close", "volume", "turnover", "date"]
    today = quotes.copy(); today["date"] = data_date
    if history is None or history.empty: full = today[needed_columns].copy()
    else: full = pd.concat([history[needed_columns], today[needed_columns]], ignore_index=True)
    full["stock_id"] = full["stock_id"].map(normalize_stock_id)
    for column in ["close", "volume", "turnover"]: full[column] = pd.to_numeric(full[column], errors="coerce")
    full = full.drop_duplicates(subset=["stock_id", "date"], keep="last").sort_values(["stock_id", "date"])
    grouped = full.groupby("stock_id", group_keys=False)
    full["ma10"] = grouped["close"].transform(lambda series: series.rolling(10, min_periods=10).mean())
    full["ma20"] = grouped["close"].transform(lambda series: series.rolling(20, min_periods=20).mean())
    full["close_5d_ago"] = grouped["close"].transform(lambda series: series.shift(4))
    full["return_5d_pct"] = (full["close"] / full["close_5d_ago"] - 1) * 100
    full["high_10d"] = grouped["close"].transform(lambda series: series.rolling(10, min_periods=10).max())
    full["low_10d"] = grouped["close"].transform(lambda series: series.rolling(10, min_periods=10).min())
    full["range_10d_pct"] = (full["high_10d"] / full["low_10d"] - 1) * 100
    full["avg_volume_5d"] = grouped["volume"].transform(lambda series: series.shift(1).rolling(5, min_periods=5).mean())
    full["volume_ratio_5d"] = (full["volume"] / full["avg_volume_5d"])
    full["distance_ma10_pct"] = (full["close"] / full["ma10"] - 1) * 100
    full["distance_ma20_pct"] = (full["close"] / full["ma20"] - 1) * 100
    full["history_days"] = grouped["date"].transform("count")
    full["history_ready"] = (full["history_days"] >= MIN_HISTORY_DAYS_FOR_SIGNAL)
    output_columns = ["stock_id", "ma10", "ma20", "return_5d_pct", "range_10d_pct", "avg_volume_5d", "volume_ratio_5d", "distance_ma10_pct", "distance_ma20_pct", "history_days", "history_ready"]
    return full.loc[full["date"] == data_date, output_columns].copy()
def make_institutional_features(history, price_features):
    output_columns = ["stock_id", "trust_buy_days_5", "trust_5d_net", "foreign_buy_days_5", "foreign_5d_net", "dealer_hedge_5d_abs", "trust_volume_ratio", "foreign_volume_ratio", "hedge_volume_ratio", "hedge_data_status", "hedge_dominant", "trust_accumulation", "foreign_support", "trust_20d_net", "foreign_20d_net", "trust_buy_days_20", "foreign_buy_days_20", "institutional_days_20", "midterm_inflow_to_verify"]
    if history is None or history.empty: return pd.DataFrame(columns=output_columns)
    df = history.copy(); df["stock_id"] = df["stock_id"].map(normalize_stock_id); df["date"] = df["date"].astype(str)
    for column in ["foreign_net", "trust_net", "dealer_hedge_net"]:
        if column not in df.columns: df[column] = np.nan
        df[column] = pd.to_numeric(df[column], errors="coerce")
    if "hedge_data_available" not in df.columns: df["hedge_data_available"] = df["dealer_hedge_net"].notna()
    df["hedge_data_available"] = df["hedge_data_available"].fillna(False).astype(bool)
    df["foreign_net"] = df["foreign_net"].fillna(0); df["trust_net"] = df["trust_net"].fillna(0)
    df = df.drop_duplicates(subset=["stock_id", "date"], keep="last").sort_values(["stock_id", "date"])
    volume_map = {}
    if price_features is not None and not price_features.empty: volume_map = price_features.set_index("stock_id")["avg_volume_5d"].to_dict()
    rows = []
    for stock_id, group in df.groupby("stock_id"):
        group = group.sort_values("date")
        group_5d = group.tail(5); group_20d = group.tail(20)
        trust_5d = float(group_5d["trust_net"].sum()); foreign_5d = float(group_5d["foreign_net"].sum())
        trust_20d = float(group_20d["trust_net"].sum()); foreign_20d = float(group_20d["foreign_net"].sum())
        trust_buy_days_5 = int((group_5d["trust_net"] > 0).sum())
        foreign_buy_days_5 = int((group_5d["foreign_net"] > 0).sum())
        trust_buy_days_20 = int((group_20d["trust_net"] > 0).sum())
        foreign_buy_days_20 = int((group_20d["foreign_net"] > 0).sum())
        institutional_days_20 = len(group_20d)
        hedge_available = bool(len(group_5d) >= 5 and group_5d["hedge_data_available"].all() and group_5d["dealer_hedge_net"].notna().all())
        hedge_abs = float(group_5d["dealer_hedge_net"].abs().sum()) if hedge_available else np.nan
        avg_volume = volume_map.get(stock_id, np.nan)
        if pd.isna(avg_volume) or avg_volume <= 0:
            trust_volume_ratio = np.nan; foreign_volume_ratio = np.nan; hedge_volume_ratio = np.nan
        else:
            five_day_average_volume = avg_volume * 5
            trust_volume_ratio = trust_5d / five_day_average_volume
            foreign_volume_ratio = foreign_5d / five_day_average_volume
            hedge_volume_ratio = hedge_abs / five_day_average_volume if hedge_available else np.nan
        hedge_status = "資料不足"; hedge_dominant = False
        if hedge_available:
            if pd.isna(hedge_volume_ratio): hedge_status = "資料不足"
            elif hedge_volume_ratio >= HEDGE_DOMINANT_RATIO: hedge_status = "避險主導"; hedge_dominant = True
            elif hedge_volume_ratio >= HEDGE_WATCH_RATIO: hedge_status = "注意"
            else: hedge_status = "正常"
        trust_accumulation = bool(len(group_5d) >= 5 and trust_buy_days_5 >= 3 and trust_5d > 0 and (pd.isna(trust_volume_ratio) or trust_volume_ratio >= MIN_TRUST_5D_VOLUME_RATIO))
        foreign_support = bool(len(group_5d) >= 5 and foreign_buy_days_5 >= 3 and foreign_5d > 0 and (pd.isna(foreign_volume_ratio) or foreign_volume_ratio >= MIN_FOREIGN_5D_VOLUME_RATIO))
        midterm_inflow = bool(institutional_days_20 >= 10 and trust_buy_days_20 >= max(5, int(institutional_days_20 * 0.4)) and trust_20d > 0)
        rows.append({"stock_id": stock_id, "trust_buy_days_5": trust_buy_days_5, "trust_5d_net": trust_5d, "foreign_buy_days_5": foreign_buy_days_5, "foreign_5d_net": foreign_5d, "dealer_hedge_5d_abs": hedge_abs, "trust_volume_ratio": trust_volume_ratio, "foreign_volume_ratio": foreign_volume_ratio, "hedge_volume_ratio": hedge_volume_ratio, "hedge_data_status": hedge_status, "hedge_dominant": hedge_dominant, "trust_accumulation": trust_accumulation, "foreign_support": foreign_support, "trust_20d_net": trust_20d, "foreign_20d_net": foreign_20d, "trust_buy_days_20": trust_buy_days_20, "foreign_buy_days_20": foreign_buy_days_20, "institutional_days_20": institutional_days_20, "midterm_inflow_to_verify": midterm_inflow})
    return pd.DataFrame(rows, columns=output_columns)

def get_rss_urls_from_env():
    raw = os.getenv("GOOGLE_ALERTS_RSS") or os.getenv("GOOGLE_ALERTS_RSS_URLS") or ""
    urls = []
    for part in raw.replace("\n", ",").split(","):
        url = part.strip()
        if url.startswith(("https://", "http://")): urls.append(url)
    return urls
def parse_rss_date(item_text):
    match = re.search(r"<pubDate>(.*?)</pubDate>|<published>(.*?)</published>|<updated>(.*?)</updated>", item_text, re.DOTALL | re.IGNORECASE)
    if not match: return None
    raw_date = next((item for item in match.groups() if item), None)
    if not raw_date: return None
    try: return parsedate_to_datetime(raw_date)
    except Exception: return None
def fetch_google_alerts_rss():
    urls = get_rss_urls_from_env()
    if not urls: return {}
    alerts_by_stock = defaultdict(list)
    junk_keywords = ["同學會", "爆料", "散戶", "發言", "聊天", "閒聊", "心情", "權證", "猜測", "看多", "看空"]
    risk_keywords = ["資安", "入侵", "重訊", "重大", "減資", "違約", "處置", "警示", "下市", "搜索", "檢調", "火災", "裁罰", "重罰"]
    print(f"RSS 模式：找到 {len(urls)} 個 RSS 連結")
    for rss_url in urls:
        try:
            response = request_get(rss_url, timeout=20)
            items = re.findall(r"<item>(.*?)</item>", response.text, re.DOTALL | re.IGNORECASE)
            for item_text in items:
                title_match = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", item_text, re.DOTALL | re.IGNORECASE)
                link_match = re.search(r"<link>(.*?)</link>", item_text, re.DOTALL | re.IGNORECASE)
                if not title_match: continue
                title = (title_match.group(1) or title_match.group(2) or "").strip()
                title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
                link = link_match.group(1).strip() if link_match else ""
                published = parse_rss_date(item_text)
                if len(title) < 4: continue
                if any(keyword in title for keyword in junk_keywords): continue
                for stock_id in FOCUS_STOCKS:
                    stock_name = FOCUS_PROFILES.get(stock_id, {}).get("name", "")
                    if stock_name and stock_name in title:
                        level = "高風險" if any(keyword in title for keyword in risk_keywords) else "中性"
                        alerts_by_stock[stock_id].append({"title": title, "link": link, "level": level, "published": published})
        except Exception as error:
            print(f"⚠️ RSS 抓取失敗：{error}"); continue
    for stock_id in list(alerts_by_stock.keys()):
        seen_titles = set(); unique_items = []
        for item in alerts_by_stock[stock_id]:
            if item.get("title", "") not in seen_titles:
                unique_items.append(item); seen_titles.add(item.get("title", ""))
        unique_items.sort(key=lambda item: item["published"].timestamp() if item.get("published") else 0, reverse=True)
        alerts_by_stock[stock_id] = unique_items[:2]
    return alerts_by_stock
def fetch_google_alerts():
    try:
        rss = fetch_google_alerts_rss()
        if rss: print("✅ 成功透過 RSS 取得並過濾快訊資料"); return rss
    except Exception as error: print(f"⚠️ RSS 快訊模組異常，略過快訊：{error}")
    print("ℹ️ 目前無 RSS 快訊設定或無資料，略過快訊區塊。"); return {}

def classify_stock(row):
    stock_id = row["stock_id"]
    if stock_id in EXCLUDE_TOOL_STOCKS: return "⚪ 排除：權值／ETF／工具股", "高權值或工具型流量，不納入雷達。", -5
    if not bool(row.get("history_ready", False)): return "⚪ 歷史資料不足", f"歷史交易日不足 {MIN_HISTORY_DAYS_FOR_SIGNAL} 日，暫不給正式訊號。", 0
    if bool(row.get("hedge_dominant", False)): return "🟠 排除：避險流量主導", "自營商避險流量偏高，不當作方向性買盤。", -4
    return_5d = row.get("return_5d_pct", np.nan); range_10d = row.get("range_10d_pct", np.nan); volume_ratio = row.get("volume_ratio_5d", np.nan)
    distance_ma10 = row.get("distance_ma10_pct", np.nan); distance_ma20 = row.get("distance_ma20_pct", np.nan)
    trust_5d_net = float(row.get("trust_5d_net", 0) or 0); foreign_5d_net = float(row.get("foreign_5d_net", 0) or 0)
    trust_buy_days_5 = int(row.get("trust_buy_days_5", 0) or 0)
    trust_accumulation = bool(row.get("trust_accumulation", False)); foreign_support = bool(row.get("foreign_support", False)); midterm_inflow = bool(row.get("midterm_inflow_to_verify", False))
    turnover = safe_float(row.get("turnover", np.nan))
    liquid = not pd.isna(turnover) and turnover >= MIN_DAILY_TURNOVER
    overheat_limit = 18 if stock_id in HIGH_VOLATILITY_STOCKS else 10
    if not pd.isna(return_5d) and return_5d > overheat_limit: return "🔴 排除：拉高／過熱", f"5日漲幅超過 {overheat_limit}%，不符合盤整吸籌。", -3
    if not pd.isna(volume_ratio) and volume_ratio > 2.0 and not pd.isna(return_5d) and return_5d > 3: return "🔴 排除：拉高／過熱", "爆量且股價走強，不符合盤整吸籌。", -3
    if trust_5d_net < 0 and foreign_5d_net < 0 and trust_buy_days_5 == 0: return "🔴 排除：法人轉賣", "投信與外資近5日同步偏賣。", -3
    if not pd.isna(distance_ma20) and distance_ma20 < 0 and (trust_accumulation or foreign_support or midterm_inflow): return "🟡 籌碼尚在、價格轉弱", "跌破MA20月線；等待重新站回，不加碼。", 2
    if not pd.isna(distance_ma10) and distance_ma10 < 0 and trust_5d_net < 0: return "🟡 籌碼鬆動、跌破MA10", "股價跌破MA10，且投信近5日偏賣。", 1
    price_ok = (pd.isna(return_5d) or -7 <= return_5d <= 6) and (pd.isna(range_10d) or range_10d <= 16) and (pd.isna(volume_ratio) or volume_ratio <= 2.0) and (pd.isna(distance_ma20) or abs(distance_ma20) <= 7)
    midterm_price_ok = (pd.isna(return_5d) or -7 <= return_5d <= 8) and (pd.isna(range_10d) or range_10d <= 20) and (pd.isna(distance_ma20) or abs(distance_ma20) <= 8)
    green_price_confirmation = not pd.isna(distance_ma10) and 0 <= distance_ma10 <= 8 and (pd.isna(return_5d) or 0 < return_5d <= 10)
    if liquid and trust_accumulation and green_price_confirmation: return "🟢 吸籌延續／初步確認", "投信持續買超後，股價溫和站上MA10。", 9
    if liquid and trust_accumulation and price_ok: return "🔵 主動資金疑似布局", "投信5日持續買超、價格仍盤整，未見避險主導。", 8
    if liquid and midterm_inflow and midterm_price_ok: return "🟣 中期資金流入待驗證", "投信中期買盤具持續性，仍需研究基本面與消息。", 7
    if foreign_support and not trust_accumulation: return "⚪ 待驗證：僅外資流入", "外資流入未獲投信確認，可能是被動流量。", 1
    return "⚪ 不列入", "未同時符合投信吸籌與價格型態條件。", 0

def build_radar(quotes, price_features, institutional_features):
    df = quotes.copy()
    for feature_df in [price_features, institutional_features]:
        if feature_df is not None and not feature_df.empty: df = df.merge(feature_df, on="stock_id", how="left")
    defaults = {"ma10": np.nan, "ma20": np.nan, "return_5d_pct": np.nan, "range_10d_pct": np.nan, "avg_volume_5d": np.nan, "volume_ratio_5d": np.nan, "distance_ma10_pct": np.nan, "distance_ma20_pct": np.nan, "history_days": 0, "history_ready": False, "trust_buy_days_5": 0, "trust_5d_net": 0, "foreign_buy_days_5": 0, "foreign_5d_net": 0, "trust_volume_ratio": np.nan, "foreign_volume_ratio": np.nan, "hedge_volume_ratio": np.nan, "hedge_data_status": "資料不足", "hedge_dominant": False, "trust_accumulation": False, "foreign_support": False, "trust_20d_net": 0, "foreign_20d_net": 0, "trust_buy_days_20": 0, "foreign_buy_days_20": 0, "institutional_days_20": 0, "midterm_inflow_to_verify": False}
    for column, default_value in defaults.items():
        if column not in df.columns: df[column] = default_value
        elif isinstance(default_value, bool): df[column] = df[column].fillna(default_value).astype(bool)
        else: df[column] = df[column].fillna(default_value)
    df["theme"] = df["stock_id"].map(lambda stock_id: "／".join(THEME_MAP.get(stock_id, [])))
    df["is_focus_stock"] = df["stock_id"].isin(FOCUS_STOCKS)
    classified = df.apply(classify_stock, axis=1, result_type="expand"); classified.columns = ["signal", "reason", "base_score"]
    df = pd.concat([df, classified], axis=1)
    df["score"] = df["base_score"]
    df.loc[df["trust_buy_days_5"] >= 4, "score"] += 2
    df.loc[df["foreign_support"] == True, "score"] += 1
    signal_order = {"🟢 吸籌延續／初步確認": 1, "🔵 主動資金疑似布局": 2, "🟣 中期資金流入待驗證": 3, "🟡 籌碼尚在、價格轉弱": 4, "🟡 籌碼鬆動、跌破MA10": 5, "⚪ 待驗證：僅外資流入": 6, "🔴 排除：拉高／過熱": 7, "🔴 排除：法人轉賣": 8, "🟠 排除：避險流量主導": 9, "⚪ 排除：權值／ETF／工具股": 10, "⚪ 歷史資料不足": 11, "⚪ 不列入": 99}
    df["sort_order"] = df["signal"].map(signal_order).fillna(99)
    return df.sort_values(["sort_order", "score", "turnover"], ascending=[True, False, False], na_position="last").drop(columns=["sort_order"])

def fmt_price(value):
    if _is_nan(value): return "-"
    try: return f"{float(value):,.2f}"
    except Exception: return safe_text(value)
def fmt_pct(value, empty_text="-"):
    if _is_nan(value): return empty_text
    try: return f"{float(value):+.2f}%"
    except Exception: return safe_text(value)
def fmt_shares(value):
    if _is_nan(value): return "-"
    try:
        shares = float(value); lots = shares / 1000
        if lots == 0: return "0 張"
        if abs(lots) >= 100: return f"{lots:+,.0f} 張"
        return f"{lots:+,.1f} 張"
    except Exception: return safe_text(value)
def get_signal_color(signal):
    text = str(signal).strip()
    if "🟢" in text or "吸籌延續" in text: return "#2e7d32"
    if "🔵" in text or "主動資金疑似布局" in text: return "#1565c0"
    if "🟣" in text or "中期資金流入" in text: return "#6a1b9a"
    if "🟡" in text or "轉弱" in text or "鬆動" in text: return "#f57c00"
    if "🔴" in text or "排除" in text: return "#c62828"
    if "🟠" in text or "避險" in text: return "#ef6c00"
    if "歷史資料不足" in text or "不列入" in text: return "#777777"
    if "待驗證" in text: return "#546e7a"
    return "#555555"
def get_long_rating_color(rating):
    text = str(rating).upper().strip()
    if "A" in text: return "#2e7d32"
    if "B" in text: return "#388e3c"
    if "C" in text: return "#f57f17"
    if "D" in text or "高風險" in text: return "#c62828"
    return "#666666"
def is_short_term_candidate(signal):
    text = str(signal).strip()
    excluded_keywords = ["歷史資料不足", "不列入", "權值", "ETF", "工具股", "拉高", "過熱", "法人轉賣", "避險流量主導", "僅外資流入"]
    return not any(keyword in text for keyword in excluded_keywords)
def get_alerts_for_stock(alerts_dict, stock_id):
    if not alerts_dict: return []
    normalized = normalize_stock_id(stock_id)
    candidates = [normalized, str(stock_id).strip()]
    if normalized.isdigit(): candidates.append(str(int(normalized)))
    for key in candidates:
        alerts = alerts_dict.get(key)
        if alerts: return alerts
    return []

def make_html_long_term_zone(radar):
    radar_map = {}
    if radar is not None and not radar.empty:
        radar_copy = radar.copy()
        radar_copy["stock_id"] = radar_copy["stock_id"].map(normalize_stock_id)
        radar_copy = radar_copy.drop_duplicates(subset=["stock_id"], keep="first")
        radar_map = {row["stock_id"]: row for _, row in radar_copy.iterrows()}
    html_content = """<div style="margin-bottom:25px; border:1px solid #c8e6c9; border-radius:8px; padding:15px; background-color:#f9fdf9;"><h3 style="color:#2e7d32; border-bottom:2px solid #2e7d32; padding-bottom:8px; margin-top:0;">🏛️ 長線核心區－8大持股體質健檢</h3>"""
    for raw_stock_id in FOCUS_STOCKS:
        stock_id = normalize_stock_id(raw_stock_id)
        profile = FOCUS_PROFILES.get(stock_id, {})
        stock_name = profile.get("name", stock_id); theme = profile.get("theme", "未分類")
        valuation = profile.get("valuation", "尚未建檔")
        long_rating = profile.get("long_rating", profile.get("rating", "持續研究"))
        roe_5y = profile.get("roe_5y", profile.get("roe")); rev_3y = profile.get("rev_3y", profile.get("rev_growth"))
        debt_ratio = profile.get("debt_ratio"); fcf_yield = profile.get("fcf_yield")
        row = radar_map.get(stock_id)
        if row is not None:
            close_text = fmt_price(row.get("close")); signal = row.get("signal", "未分類"); signal_color = get_signal_color(signal); signal_text = safe_text(signal)
            trust_5d = fmt_shares(row.get("trust_5d_net")); foreign_5d = fmt_shares(row.get("foreign_5d_net"))
        else:
            close_text = "-"; signal_color = "#777777"; signal_text = "未取得當日行情"; trust_5d = "-"; foreign_5d = "-"
        html_content += f"""<div style="background:#ffffff; border-left:5px solid #2e7d32; padding:12px 15px; margin-bottom:12px; border-radius:4px;"><div style="font-size:16px; font-weight:bold; color:#222;">{safe_text(stock_id)} {safe_text(stock_name)}<span style="font-size:12px; background:#e8f5e9; padding:2px 8px; border-radius:10px; color:{get_long_rating_color(long_rating)}; border:1px solid #c8e6c9;">{safe_text(long_rating)}</span><span style="font-size:12px; background:#f1f8e9; padding:2px 6px; border-radius:3px; color:#666;">{safe_text(theme)}</span></div><div style="font-size:13px; color:#444; line-height:1.8; margin-top:6px;"><b>短線訊號：</b><span style="color:{signal_color}; font-weight:bold;">{signal_text}</span>&nbsp;|&nbsp;<b>投信5日：</b>{trust_5d}&nbsp;|&nbsp;<b>外資5日：</b>{foreign_5d}</div><div style="font-size:13px; color:#444; line-height:1.8; margin-top:6px; display:grid; grid-template-columns:1fr 1fr; gap:4px;"><div><b>收盤：</b>{close_text}</div><div><b>估值：</b>{safe_text(valuation)}</div><div><b>ROE 5年：</b>{fmt_pct(roe_5y, "尚未建檔")}</div><div><b>營收3年複合：</b>{fmt_pct(rev_3y, "尚未建檔")}</div><div><b>負債比：</b>{fmt_pct(debt_ratio, "尚未建檔")}</div><div><b>自由現金流殖利率：</b>{fmt_pct(fcf_yield, "尚未建檔")}</div></div></div>"""
    html_content += "</div>"; return html_content

def make_html_short_term_zone(radar, alerts_dict=None, maximum=20):
    if alerts_dict is None: alerts_dict = {}
    if radar is None or radar.empty: return """<div style="border:1px solid #ddd; padding:15px; border-radius:8px; margin-bottom:25px;"><h3 style="color:#1565c0; margin-top:0;">⚡ 短線雷達區</h3><p style="color:#888;">（今日無符合股票）</p></div>"""
    try: maximum = int(maximum)
    except Exception: maximum = 20
    if maximum <= 0: maximum = 20
    frame = radar.copy()
    frame = frame[frame["signal"].map(is_short_term_candidate)]
    if frame.empty: return """<div style="border:1px solid #ddd; padding:15px; border-radius:8px; margin-bottom:25px;"><h3 style="color:#1565c0; margin-top:0;">⚡ 短線雷達區</h3><p style="color:#888;">（今日無可列入短線雷達的標的）</p></div>"""
    html_content = """<div style="margin-bottom:25px; border:1px solid #90caf9; border-radius:8px; padding:15px; background-color:#f5f9ff;"><h3 style="color:#1565c0; border-bottom:2px solid #1565c0; padding-bottom:8px; margin-top:0;">⚡ 短線雷達區－5～20天波段候選＋黃燈風控</h3><div style="overflow-x:auto;"><table style="width:100%; border-collapse:collapse; font-size:13px; background:#fff;"><thead><tr style="background-color:#e3f2fd; text-align:left; border-bottom:2px solid #90caf9;"><th style="padding:8px; border:1px solid #ddd;">訊號／標的</th><th style="padding:8px; border:1px solid #ddd;">法人5日</th><th style="padding:8px; border:1px solid #ddd;">技術表現</th><th style="padding:8px; border:1px solid #ddd;">判定＋快訊</th></tr></thead><tbody>"""
    for _, row in frame.head(maximum).iterrows():
        stock_id = normalize_stock_id(row.get("stock_id", "")); stock_name = row.get("stock_name", ""); signal = row.get("signal", "-")
        focus_mark = " ⭐" if bool(row.get("is_focus_stock", False)) else ""
        alerts = get_alerts_for_stock(alerts_dict, stock_id); alert_html = ""
        if alerts:
            alert_html = """<div style="margin-top:5px; background:#f1f8e9; padding:5px 7px; border-radius:3px; font-size:11px; color:#333;">"""
            for alert in alerts[:2]:
                if not isinstance(alert, dict): continue
                level = alert.get("level", ""); icon = "⚠️" if level == "高風險" else "•"
                title = safe_text(alert.get("title", "")); link = sanitize_link(alert.get("link", ""))
                alert_html += f"""{icon} [{safe_text(level)}] {title}<a href="{link}" target="_blank" style="color:#1565c0; text-decoration:none;"> ↗</a><br>"""
            alert_html += "</div>"
        html_content += f"""<tr style="border-bottom:1px solid #eee;"><td style="padding:8px; border:1px solid #ddd;"><span style="color:{get_signal_color(signal)}; font-weight:bold;">{safe_text(signal)}{focus_mark}</span><br><b>{safe_text(stock_id)}</b> {safe_text(stock_name)}</td><td style="padding:8px; border:1px solid #ddd;">投信5日：{fmt_shares(row.get("trust_5d_net"))}<br>外資5日：{fmt_shares(row.get("foreign_5d_net"))}<br>避險：{safe_text(row.get("hedge_data_status", "資料不足"))}</td><td style="padding:8px; border:1px solid #ddd;">5日漲幅：{fmt_pct(row.get("return_5d_pct"))}<br>距MA20：{fmt_pct(row.get("distance_ma20_pct"))}<br>量比：{fmt_pct((row.get("volume_ratio_5d", np.nan) - 1) * 100 if not _is_nan(row.get("volume_ratio_5d", np.nan)) else np.nan)}</td><td style="padding:8px; border:1px solid #ddd; font-size:12px; color:#555;">{safe_text(row.get("reason", ""))}{alert_html}</td></tr>"""
    html_content += """</tbody></table></div></div>"""; return html_content

def make_html_email_body(radar, data_date, execution_time, alerts_dict=None):
    if alerts_dict is None: alerts_dict = {}
    long_html = make_html_long_term_zone(radar)
    short_html = make_html_short_term_zone(radar=radar, alerts_dict=alerts_dict, maximum=20)
    total_stocks = len(radar) if radar is not None else 0
    green_count = blue_count = purple_count = yellow_count = 0
    if radar is not None and not radar.empty:
        green_count = int((radar["signal"] == "🟢 吸籌延續／初步確認").sum())
        blue_count = int((radar["signal"] == "🔵 主動資金疑似布局").sum())
        purple_count = int((radar["signal"] == "🟣 中期資金流入待驗證").sum())
        yellow_count = int(radar["signal"].astype(str).str.startswith("🟡").sum())
    return f"""<html><head><meta charset="utf-8"></head><body style="font-family:Arial, Microsoft JhengHei, sans-serif; color:#333; line-height:1.5; background-color:#f9f9f9; padding:20px;"><div style="max-width:960px; margin:auto; background:#ffffff; padding:25px; border-radius:8px; box-shadow:0 2px 5px rgba(0,0,0,0.08);"><h2 style="color:#1565c0; border-bottom:3px solid #1565c0; padding-bottom:10px; margin-top:0;">📈 台股主動資金雷達與長短線研究報告 v6.1</h2><p style="font-size:14px; color:#666;"><b>資料日期：</b>{safe_text(data_date)}&nbsp;|&nbsp;<b>執行時間：</b>{safe_text(execution_time)}（台灣時間）&nbsp;|&nbsp;<b>掃描檔數：</b>{total_stocks}</p><div style="margin:12px 0 20px 0; padding:10px; background:#f5f5f5; border-radius:5px; font-size:13px;">🟢 初步確認：{green_count} 檔&nbsp;｜&nbsp;🔵 疑似布局：{blue_count} 檔&nbsp;｜&nbsp;🟣 中期待驗證：{purple_count} 檔&nbsp;｜&nbsp;🟡 黃燈風控：{yellow_count} 檔</div>{long_html}{short_html}<hr style="border:none; border-top:1px solid #eee; margin:30px 0;"><p style="font-size:12px; color:#888; text-align:center;">長線核心區為持股研究摘要；短線雷達區為法人籌碼與價格型態篩選。CSV 檔仍保留所有股票與排除原因。本報告僅供公開資料研究，不構成任何買賣建議。</p></div></body></html>"""

def send_email(subject, html_body):
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]):
        print("未設定 Gmail Secrets，略過寄信；CSV 仍會正常產生。"); return
    message = MIMEMultipart("alternative"); message["From"] = GMAIL_USER; message["To"] = RECIPIENT_EMAIL; message["Subject"] = subject
    message.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD); server.send_message(message)
    print("HTML 彩色 Email 寄送完成。")

def main():
    execution_now = now_tw(); execution_time = execution_now.strftime("%Y-%m-%d %H:%M")
    print("=" * 60); print(f"開始執行台股主動資金雷達 v6.1：{execution_time}"); print("=" * 60)
    try: quotes = get_all_quotes(); print(f"成功取得行情：{len(quotes)} 檔。")
    except Exception as error: print(f"❌ 無法取得任何市場行情，終止本次執行：{error}"); raise
    institutional_today = pd.DataFrame()
    try: institutional_today = get_all_institutional()
    except Exception as error: print(f"⚠️ 法人資料取得失敗，今天略過法人計算：{error}")
    alerts_dict = fetch_google_alerts()
    if institutional_today is not None and not institutional_today.empty and "date" in institutional_today.columns: data_date = str(institutional_today["date"].max())
    else:
        last_date = get_last_trading_date(); data_date = f"{last_date[:4]}-{last_date[4:6]}-{last_date[6:]}"
    print(f"本輪資料日期：{data_date}")
    price_history = load_history("layout_price_history_")
    institutional_history = load_history("layout_institutional_history_")
    if institutional_today is not None and not institutional_today.empty: institutional_history = pd.concat([institutional_history, institutional_today], ignore_index=True)
    price_features = make_price_features(quotes=quotes, history=price_history, data_date=data_date)
    institutional_features = make_institutional_features(history=institutional_history, price_features=price_features)
    radar = build_radar(quotes=quotes, price_features=price_features, institutional_features=institutional_features)
    save_history(quotes=quotes, institutional=institutional_today, data_date=data_date)
    radar_path = os.path.join(OUTPUT_DIR, f"layout_radar_{data_date}.csv")
    radar.to_csv(radar_path, index=False, encoding="utf-8-sig")
    print(f"雷達 CSV 已輸出：{radar_path}")
    html_body = make_html_email_body(radar=radar, data_date=data_date, execution_time=execution_time, alerts_dict=alerts_dict)
    send_email(subject=f"主動資金雷達 v6.1｜{data_date}", html_body=html_body)
    print("執行完成。")

if __name__ == "__main__":
    try: main()
    except Exception:
        error_text = traceback.format_exc()
        print("\n程式發生錯誤："); print(error_text)
        try: send_email(subject=f"【錯誤】主動資金雷達 v6.1｜{today_str()}", html_body=f"<html><body><pre>{safe_text(error_text)}</pre></body></html>")
        except Exception: pass
        raise
