# -*- coding: utf-8 -*-
"""
台股「主動資金疑似布局」雷達（修正版 v2）
修正重點：
1. [致命] safe_float / safe_int 不再把負號吃掉
2. 法人資料回補天數 5 -> 15 天，避開連假
3. 量比改用「昨日前 5 日均量」，避免自己稀釋自己
4. 月營收增加 big5 編碼處理 + 更穩健的欄位判斷
5. 增加 ETF/權值排除，補上常見 0050/0056 等
"""

import os
import io
import time
import smtplib
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import numpy as np
import pandas as pd
import requests

# ==========================================================
# 0. 基本設定
# ==========================================================

TZ = timezone(timedelta(hours=8))
OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def get_today_str():
    return datetime.now(TZ).strftime("%Y-%m-%d")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    )
}

EXCLUDE_TOOL_STOCKS = {
    "2330", "2317", "2454", "2308", "2382", "3231", "6669", "3711",
    "2881", "2882", "2884", "2886", "2891", "2892", "2880",
    "0050", "0056", "00878", "006208", "00919", "00929",
}

MIN_DAILY_TURNOVER = 30_000_000
MIN_TRUST_5D_VOLUME_RATIO = 0.01
MIN_FOREIGN_5D_VOLUME_RATIO = 0.03

MAX_5D_RETURN_FOR_ACCUMULATION = 6.0
MIN_5D_RETURN_FOR_ACCUMULATION = -7.0
MAX_10D_RANGE_FOR_ACCUMULATION = 16.0
MAX_VOLUME_SPIKE_RATIO = 2.0

MAX_5D_RETURN_FOR_GREEN = 10.0
MAX_DISTANCE_MA10_FOR_GREEN = 8.0
MAX_DISTANCE_MA20_FOR_BLUE = 7.0

FUNDAMENTAL_BAD_MOM = -20.0
FUNDAMENTAL_WARNING_MOM = -10.0
FUNDAMENTAL_BAD_YOY = 0.0
YOY_DROP_BAD_PP = -20.0

MAX_BLUE_EMAIL = 10
MAX_GREEN_EMAIL = 10
MAX_YELLOW_EMAIL = 10
MAX_RED_EMAIL = 15
MAX_HISTORY_FILES = 90

# ==========================================================
# 1. 共用工具
# ==========================================================

def safe_float(value):
    """修正版：保留負號，只清逗號、%、全形符號"""
    if value is None:
        return np.nan
    try:
        text = str(value).strip()
        if text == "" or text in ["--", "---", "nan", "NaN", "None", "null", "—"]:
            return np.nan
        text = text.replace(",", "").replace("%", "")
        text = text.replace("＋", "+").replace("－", "-")
        text = text.replace("—", "-").replace("–", "-")
        if text.startswith("+"):
            text = text[1:]
        return float(text)
    except Exception:
        return np.nan

def safe_int(value):
    number = safe_float(value)
    if pd.isna(number):
        return np.nan
    return int(number)

def normalize_stock_id(value):
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text.zfill(4) if text.isdigit() and len(text) < 4 else text

def request_get(url, params=None, timeout=30):
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as error:
            last_error = error
            delay = 2 * (attempt + 1)
            print(f"連線失敗，{delay} 秒後重試：{error}")
            time.sleep(delay)
    raise last_error

def find_first_column(columns, keywords):
    cols = [str(c) for c in columns]
    for keyword in keywords:
        for col in cols:
            if keyword in col:
                return col
    return None

def get_recent_dates(days=15):
    """取得最近 N 天的 YYYYMMDD，回補連假用"""
    dates = []
    now = datetime.now(TZ)
    for i in range(days):
        d = now - timedelta(days=i)
        dates.append(d.strftime("%Y%m%d"))
    return dates

# ==========================================================
# 2. 行情資料
# ==========================================================

def get_twse_quotes():
    print("取得 TWSE 上市行情...")
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    response = request_get(url)
    data = response.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError("TWSE OpenAPI 未回傳上市行情")
    rows = []
    for item in data:
        stock_id = normalize_stock_id(item.get("Code", ""))
        if not stock_id.isdigit() or len(stock_id)!= 4:
            continue
        close = safe_float(item.get("ClosingPrice"))
        if pd.isna(close):
            continue
        rows.append({
            "stock_id": stock_id,
            "stock_name": str(item.get("Name", "")).strip(),
            "market": "TWSE",
            "close": close,
            "change": safe_float(item.get("Change")),
            "volume": safe_int(item.get("TradeVolume")),
            "turnover": safe_float(item.get("TradeValue")),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("TWSE 資料無法解析")
    print(f"TWSE 行情完成：{len(df)} 筆")
    return df

def get_tpex_quotes():
    print("取得 TPEx 上櫃行情...")
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    response = request_get(url)
    data = response.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError("TPEx OpenAPI 未回傳上櫃行情")
    rows = []
    for item in data:
        stock_id = normalize_stock_id(
            item.get("SecuritiesCompanyCode") or item.get("SecuritiesCode") or item.get("Code") or ""
        )
        if not stock_id.isdigit() or len(stock_id)!= 4:
            continue
        close = safe_float(item.get("Close") or item.get("ClosingPrice"))
        if pd.isna(close):
            continue
        rows.append({
            "stock_id": stock_id,
            "stock_name": str(item.get("CompanyName") or item.get("SecuritiesName") or item.get("Name") or "").strip(),
            "market": "TPEx",
            "close": close,
            "change": safe_float(item.get("Change")),
            "volume": safe_int(item.get("Volume") or item.get("TradeVolume")),
            "turnover": safe_float(item.get("Amount") or item.get("TradeValue")),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("TPEx 資料無法解析")
    print(f"TPEx 行情完成：{len(df)} 筆")
    return df

def get_all_quotes():
    frames = [get_twse_quotes()]
    try:
        frames.append(get_tpex_quotes())
    except Exception as error:
        print(f"TPEx 行情暫時不可用，今天先使用上市資料：{error}")
    quotes = pd.concat(frames, ignore_index=True)
    quotes = quotes.drop_duplicates(subset=["stock_id"], keep="first")
    return quotes

# ==========================================================
# 3. 法人資料：自動往回推 15 天
# ==========================================================

def get_twse_institutional():
    print("取得 TWSE 三大法人資料...")
    url = "https://www.twse.com.tw/rwd/zh/fund/T86"
    data = None
    used_date_compact = None
    for date_compact in get_recent_dates(days=15):
        params = {"response": "json", "date": date_compact, "selectType": "ALLBUT0999"}
        try:
            response = request_get(url, params=params, timeout=15)
            res_json = response.json()
            if res_json.get("stat") == "OK":
                data = res_json
                used_date_compact = date_compact
                print(f"成功取得法人資料日期：{date_compact}")
                break
        except Exception:
            continue
    if not data:
        raise RuntimeError("最近 15 日內無法取得 TWSE 法人資料")
    fields = data.get("fields", [])
    raw_rows = data.get("data", [])
    formatted_date = f"{used_date_compact[:4]}-{used_date_compact[4:6]}-{used_date_compact[6:]}"
    rows = []
    for raw_row in raw_rows:
        item = dict(zip(fields, raw_row))
        stock_id = normalize_stock_id(item.get("證券代號", ""))
        if not stock_id.isdigit() or len(stock_id)!= 4:
            continue
        dealer_proprietary = safe_int(item.get("自營商買賣超股數(自行買賣)"))
        dealer_hedge = safe_int(item.get("自營商買賣超股數(避險)"))
        dealer_total = safe_int(item.get("自營商買賣超股數"))
        if pd.isna(dealer_proprietary): dealer_proprietary = 0
        if pd.isna(dealer_hedge): dealer_hedge = 0
        if pd.isna(dealer_total): dealer_total = dealer_proprietary + dealer_hedge
        rows.append({
            "stock_id": stock_id,
            "date": formatted_date,
            "foreign_net": safe_int(item.get("外陸資買賣超股數(不含外資自營商)")),
            "trust_net": safe_int(item.get("投信買賣超股數")),
            "dealer_proprietary_net": dealer_proprietary,
            "dealer_hedge_net": dealer_hedge,
            "dealer_net": dealer_total,
        })
    df = pd.DataFrame(rows)
    for col in ["foreign_net", "trust_net", "dealer_proprietary_net", "dealer_hedge_net", "dealer_net"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    print(f"TWSE 法人完成：{len(df)} 筆，日期 {formatted_date}")
    return df

# ==========================================================
# 4. 月營收
# ==========================================================

def get_monthly_revenue():
    print("取得月營收資料...")
    urls = [
        "https://mops.twse.com.tw/nas/t21/sii/t21sc03_if.html",
        "https://mops.twse.com.tw/nas/t21/otc/t21sc03_if.html",
    ]
    rows = []
    TODAY = get_today_str()
    for url in urls:
        try:
            response = request_get(url, timeout=45)
            response.encoding = response.apparent_encoding or 'big5'
            try:
                text = response.content.decode('big5', errors='ignore')
            except:
                text = response.text
            tables = pd.read_html(io.StringIO(text))
            for table in tables:
                if table.empty or len(table.columns) < 3:
                    continue
                table.columns = [str(col).replace("\n", "").strip() for col in table.columns]
                code_col = find_first_column(table.columns, ["公司代號", "代號"])
                revenue_col = find_first_column(table.columns, ["當月營收", "本月營收"])
                yoy_col = find_first_column(table.columns, ["去年同月增減(%)", "去年同月增減", "YoY"])
                mom_col = find_first_column(table.columns, ["上月比較增減(%)", "上月比較增減", "MoM"])
                if not code_col or not yoy_col:
                    continue
                for _, item in table.iterrows():
                    stock_id = normalize_stock_id(item.get(code_col, ""))
                    if not stock_id.isdigit() or len(stock_id)!= 4:
                        continue
                    rows.append({
                        "stock_id": stock_id,
                        "month_revenue": safe_float(item.get(revenue_col, np.nan)) if revenue_col else np.nan,
                        "month_revenue_yoy": safe_float(item.get(yoy_col, np.nan)),
                        "month_revenue_mom": safe_float(item.get(mom_col, np.nan)) if mom_col else np.nan,
                        "announce_date": TODAY,
                    })
        except Exception as error:
            print(f"MOPS 月營收來源暫時失敗：{url} | {error}")
    df = pd.DataFrame(rows)
    if df.empty:
        print("月營收無資料，將標記為待驗證")
        return pd.DataFrame(columns=["stock_id", "month_revenue", "month_revenue_yoy", "month_revenue_mom", "announce_date"])
    df = df.drop_duplicates(subset=["stock_id"], keep="last")
    print(f"月營收完成：{len(df)} 筆")
    return df

# ==========================================================
# 5. 歷史資料讀取與儲存
# ==========================================================

def load_history(prefix, limit=MAX_HISTORY_FILES):
    if not os.path.exists(OUTPUT_DIR):
        return pd.DataFrame()
    files = [f for f in os.listdir(OUTPUT_DIR) if f.startswith(prefix) and f.endswith(".csv")]
    files = sorted(files)[-limit:]
    frames = []
    for filename in files:
        path = os.path.join(OUTPUT_DIR, filename)
        try:
            df = pd.read_csv(path, dtype={"stock_id": str})
            if not df.empty:
                df["stock_id"] = df["stock_id"].map(normalize_stock_id)
                frames.append(df)
        except Exception as error:
            print(f"讀取歷史檔失敗：{filename}｜{error}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)

def save_today_history(quotes, institutional_df, revenue_df, today_str):
    price_df = quotes.copy()
    price_df["date"] = today_str
    price_df.to_csv(os.path.join(OUTPUT_DIR, f"layout_price_history_{today_str}.csv"), index=False, encoding="utf-8-sig")
    if institutional_df is not None and not institutional_df.empty:
        institutional_df.to_csv(os.path.join(OUTPUT_DIR, f"layout_institutional_history_{today_str}.csv"), index=False, encoding="utf-8-sig")
    if revenue_df is not None and not revenue_df.empty:
        revenue_df.to_csv(os.path.join(OUTPUT_DIR, f"layout_revenue_history_{today_str}.csv"), index=False, encoding="utf-8-sig")

# ==========================================================
# 6. 特徵工程與分類邏輯
# ==========================================================

def make_price_features(quotes, price_history, today_str):
    today_df = quotes.copy()
    today_df["date"] = today_str
    if price_history is None or price_history.empty:
        full = today_df.copy()
    else:
        required_cols = ["stock_id", "stock_name", "market", "close", "volume", "turnover", "date"]
        history = price_history.copy()
        for col in required_cols:
            if col not in history.columns:
                history[col] = np.nan
        full = pd.concat([history[required_cols], today_df[required_cols]], ignore_index=True)
    full["stock_id"] = full["stock_id"].map(normalize_stock_id)
    full["date"] = full["date"].astype(str)
    for col in ["close", "volume", "turnover"]:
        full[col] = pd.to_numeric(full[col], errors="coerce")
    full = full.drop_duplicates(subset=["stock_id", "date"], keep="last").sort_values(["stock_id", "date"])
    grouped = full.groupby("stock_id", group_keys=False)
    full["ma10"] = grouped["close"].transform(lambda x: x.rolling(10, min_periods=10).mean())
    full["ma20"] = grouped["close"].transform(lambda x: x.rolling(20, min_periods=20).mean())
    full["close_5d_ago"] = grouped["close"].transform(lambda x: x.shift(4))
    full["close_10d_ago"] = grouped["close"].transform(lambda x: x.shift(9))
    full["return_5d_pct"] = (full["close"] / full["close_5d_ago"] - 1) * 100
    full["return_10d_pct"] = (full["close"] / full["close_10d_ago"] - 1) * 100
    full["high_10d"] = grouped["close"].transform(lambda x: x.rolling(10, min_periods=10).max())
    full["low_10d"] = grouped["close"].transform(lambda x: x.rolling(10, min_periods=10).min())
    full["range_10d_pct"] = (full["high_10d"] / full["low_10d"] - 1) * 100
    full["avg_volume_5d"] = grouped["volume"].transform(lambda x: x.shift(1).rolling(5, min_periods=5).mean())
    full["volume_ratio_5d"] = full["volume"] / full["avg_volume_5d"]
    full["distance_ma10_pct"] = (full["close"] / full["ma10"] - 1) * 100
    full["distance_ma20_pct"] = (full["close"] / full["ma20"] - 1) * 100
    return full[full["date"] == today_str][[
        "stock_id", "ma10", "ma20", "return_5d_pct", "return_10d_pct",
        "range_10d_pct", "avg_volume_5d", "volume_ratio_5d",
        "distance_ma10_pct", "distance_ma20_pct"
    ]].copy()

def make_institutional_features(institutional_history, price_features):
    columns = [
        "stock_id", "trust_buy_days_5", "trust_5d_net", "foreign_buy_days_5",
        "foreign_5d_net", "dealer_hedge_5d_abs", "trust_volume_ratio",
        "foreign_volume_ratio", "hedge_volume_ratio", "hedge_dominant",
        "trust_accumulation", "foreign_support"
    ]
    if institutional_history is None or institutional_history.empty:
        return pd.DataFrame(columns=columns)
    df = institutional_history.copy()
    for col in ["foreign_net", "trust_net", "dealer_proprietary_net", "dealer_hedge_net", "dealer_net"]:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    df["stock_id"] = df["stock_id"].map(normalize_stock_id)
    df["date"] = df["date"].astype(str)
    df = df.drop_duplicates(subset=["stock_id", "date"], keep="last").sort_values(["stock_id", "date"])
    avg_volume_map = {}
    if price_features is not None and not price_features.empty:
        avg_volume_map = price_features.set_index("stock_id")["avg_volume_5d"].to_dict()
    rows = []
    for stock_id, group in df.groupby("stock_id"):
        group = group.sort_values("date").tail(5)
        trust_values = group["trust_net"].tolist()
        foreign_values = group["foreign_net"].tolist()
        hedge_values = group["dealer_hedge_net"].tolist()
        trust_5d_net = float(sum(trust_values))
        foreign_5d_net = float(sum(foreign_values))
        hedge_5d_abs = float(sum(abs(x) for x in hedge_values))
        trust_buy_days = sum(1 for x in trust_values if x > 0)
        foreign_buy_days = sum(1 for x in foreign_values if x > 0)
        avg_volume = avg_volume_map.get(stock_id, np.nan)
        if pd.isna(avg_volume) or avg_volume <= 0:
            trust_ratio = foreign_ratio = hedge_ratio = np.nan
        else:
            trust_ratio = trust_5d_net / (avg_volume * 5)
            foreign_ratio = foreign_5d_net / (avg_volume * 5)
            hedge_ratio = hedge_5d_abs / (avg_volume * 5)
        hedge_dominant = False
        if not pd.isna(hedge_ratio) and hedge_ratio >= 0.03:
            hedge_dominant = True
        if hedge_5d_abs >= (abs(trust_5d_net) + abs(foreign_5d_net)) and hedge_5d_abs > 0:
            hedge_dominant = True
        trust_accumulation = (
            trust_buy_days >= 3 and trust_5d_net > 0 and
            (pd.isna(trust_ratio) or trust_ratio >= MIN_TRUST_5D_VOLUME_RATIO)
        )
        foreign_support = (
            foreign_buy_days >= 3 and foreign_5d_net > 0 and
            (pd.isna(foreign_ratio) or foreign_ratio >= MIN_FOREIGN_5D_VOLUME_RATIO)
        )
        rows.append({
            "stock_id": stock_id,
            "trust_buy_days_5": trust_buy_days,
            "trust_5d_net": trust_5d_net,
            "foreign_buy_days_5": foreign_buy_days,
            "foreign_5d_net": foreign_5d_net,
            "dealer_hedge_5d_abs": hedge_5d_abs,
            "trust_volume_ratio": trust_ratio,
            "foreign_volume_ratio": foreign_ratio,
            "hedge_volume_ratio": hedge_ratio,
            "hedge_dominant": hedge_dominant,
            "trust_accumulation": trust_accumulation,
            "foreign_support": foreign_support,
        })
    return pd.DataFrame(rows)

def make_revenue_features(revenue_history):
    output_columns = [
        "stock_id", "month_revenue", "month_revenue_yoy", "month_revenue_mom",
        "previous_yoy", "yoy_change_pp", "fundamental_bad", "fundamental_note"
    ]
    if revenue_history is None or revenue_history.empty:
        return pd.DataFrame(columns=output_columns)
    df = revenue_history.copy()
    for col in ["stock_id", "month_revenue", "month_revenue_yoy", "month_revenue_mom", "announce_date"]:
        if col not in df.columns:
            df[col] = np.nan
    df["stock_id"] = df["stock_id"].map(normalize_stock_id)
    df["announce_date"] = df["announce_date"].astype(str)
    for col in ["month_revenue", "month_revenue_yoy", "month_revenue_mom"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values(["stock_id", "announce_date"])
    rows = []
    for stock_id, group in df.groupby("stock_id"):
        group = group.drop_duplicates(subset=["announce_date"], keep="last").sort_values("announce_date")
        latest = group.iloc[-1]
        previous = group.iloc[-2] if len(group) >= 2 else None
        yoy = latest["month_revenue_yoy"]
        mom = latest["month_revenue_mom"]
        previous_yoy = previous["month_revenue_yoy"] if previous is not None else np.nan
        yoy_change = (yoy - previous_yoy) if not pd.isna(yoy) and not pd.isna(previous_yoy) else np.nan
        bad_case_1 = not pd.isna(mom) and not pd.isna(yoy) and mom <= FUNDAMENTAL_BAD_MOM and yoy <= FUNDAMENTAL_BAD_YOY
        bad_case_2 = not pd.isna(mom) and not pd.isna(yoy_change) and mom <= FUNDAMENTAL_WARNING_MOM and yoy_change <= YOY_DROP_BAD_PP
        if bad_case_1:
            fundamental_bad, note = True, "MoM 大幅下滑且 YoY 轉弱／轉負。"
        elif bad_case_2:
            fundamental_bad, note = True, "MoM 下滑且 YoY 較前期明顯降速。"
        elif pd.isna(yoy) and pd.isna(mom):
            fundamental_bad, note = False, "營收資料待驗證。"
        else:
            fundamental_bad, note = False, "營收未出現確認性惡化。"
        rows.append({
            "stock_id": stock_id,
            "month_revenue": latest["month_revenue"],
            "month_revenue_yoy": yoy,
            "month_revenue_mom": mom,
            "previous_yoy": previous_yoy,
            "yoy_change_pp": yoy_change,
            "fundamental_bad": fundamental_bad,
            "fundamental_note": note,
        })
    return pd.DataFrame(rows)

def classify_stock(row):
    stock_id = row["stock_id"]
    if stock_id in EXCLUDE_TOOL_STOCKS:
        return "⚪ 排除：權值／避險工具", "高權值、ETF／指數／衍生品流量可能高，不納入提前布局雷達。", -5
    if bool(row.get("hedge_dominant", False)):
        return "🟠 排除：避險流量主導", "自營商避險流量相對過高，不當作布局。", -4
    fundamental_bad = bool(row.get("fundamental_bad", False))
    return_5d = row.get("return_5d_pct", np.nan)
    range_10d = row.get("range_10d_pct", np.nan)
    distance_ma20 = row.get("distance_ma20_pct", np.nan)
    distance_ma10 = row.get("distance_ma10_pct", np.nan)
    volume_ratio = row.get("volume_ratio_5d", np.nan)
    trust_accumulation = bool(row.get("trust_accumulation", False))
    foreign_support = bool(row.get("foreign_support", False))
    trust_5d_net = float(row.get("trust_5d_net", 0) or 0)
    foreign_5d_net = float(row.get("foreign_5d_net", 0) or 0)
    trust_buy_days = int(row.get("trust_buy_days_5", 0) or 0)
    turnover = row.get("turnover", np.nan)
    liquid = not pd.isna(turnover) and turnover >= MIN_DAILY_TURNOVER
    hot_price = (
        (not pd.isna(return_5d) and return_5d > MAX_5D_RETURN_FOR_GREEN) or
        (not pd.isna(volume_ratio) and volume_ratio > MAX_VOLUME_SPIKE_RATIO and not pd.isna(return_5d) and return_5d > 3)
    )
    if hot_price:
        return "🔴 排除：拉高／過熱", "短期漲幅或量能過熱，不是盤整吸籌階段。", -3
    if not pd.isna(distance_ma20) and distance_ma20 < 0 and fundamental_bad:
        return "🔴 排除：跌破MA20＋基本面變壞", "價格跌破 MA20，且月營收確認惡化。", -4
    if trust_5d_net < 0 and foreign_5d_net < 0 and trust_buy_days == 0:
        return "🔴 排除：法人轉賣", "投信與外資近 5 日同步偏賣。", -3
    if not pd.isna(distance_ma20) and distance_ma20 < 0 and not fundamental_bad and (trust_accumulation or foreign_support):
        return "🟡 籌碼尚在、價格轉弱", "仍有法人買盤但跌破 MA20，等待重新站回。", 2
    price_in_range = (
        (pd.isna(return_5d) or (return_5d >= MIN_5D_RETURN_FOR_ACCUMULATION and return_5d <= MAX_5D_RETURN_FOR_ACCUMULATION)) and
        (pd.isna(range_10d) or range_10d <= MAX_10D_RANGE_FOR_ACCUMULATION) and
        (pd.isna(volume_ratio) or volume_ratio <= MAX_VOLUME_SPIKE_RATIO) and
        (pd.isna(distance_ma20) or abs(distance_ma20) <= MAX_DISTANCE_MA20_FOR_BLUE)
    )
    if liquid and trust_accumulation and price_in_range and not fundamental_bad:
        return "🔵 主動資金疑似布局", "投信 5 日持續買超、買超相對成交量有意義，價格仍盤整。", 8
    green_price_confirmation = (
        not pd.isna(distance_ma10) and 0 <= distance_ma10 <= MAX_DISTANCE_MA10_FOR_GREEN and
        (pd.isna(return_5d) or (0 < return_5d <= MAX_5D_RETURN_FOR_GREEN))
    )
    if liquid and trust_accumulation and green_price_confirmation and not fundamental_bad:
        return "🟢 吸籌延續／初步確認", "投信持續買超後，股價溫和站上 MA10。", 9
    if foreign_support and not trust_accumulation:
        return "⚪ 待驗證：僅外資流入", "外資流入但投信未確認。", 1
    return "⚪ 不列入", "未同時符合投信吸籌、盤整、避險排除與基本面條件。", 0

def build_radar(quotes, price_features, inst_features, revenue_features):
    df = quotes.copy()
    for features in [price_features, inst_features, revenue_features]:
        if features is not None and not features.empty:
            df = df.merge(features, on="stock_id", how="left")
    defaults = {
        "ma10": np.nan, "ma20": np.nan, "return_5d_pct": np.nan, "return_10d_pct": np.nan,
        "range_10d_pct": np.nan, "avg_volume_5d": np.nan, "volume_ratio_5d": np.nan,
        "distance_ma10_pct": np.nan, "distance_ma20_pct": np.nan, "trust_buy_days_5": 0,
        "trust_5d_net": 0, "foreign_buy_days_5": 0, "foreign_5d_net": 0, "dealer_hedge_5d_abs": 0,
        "trust_volume_ratio": np.nan, "foreign_volume_ratio": np.nan, "hedge_volume_ratio": np.nan,
        "hedge_dominant": False, "trust_accumulation": False, "foreign_support": False,
        "month_revenue": np.nan, "month_revenue_yoy": np.nan, "month_revenue_mom": np.nan,
        "previous_yoy": np.nan, "yoy_change_pp": np.nan, "fundamental_bad": False,
        "fundamental_note": "營收資料待驗證。",
    }
    for col, default_value in defaults.items():
        if col not in df.columns:
            df[col] = default_value
        else:
            if isinstance(default_value, bool):
                df[col] = df[col].fillna(default_value)
            else:
                df[col] = df[col].fillna(default_value)
    for bcol in ["hedge_dominant", "trust_accumulation", "foreign_support", "fundamental_bad"]:
        if bcol in df.columns:
            df[bcol] = df[bcol].astype(bool)
    classified = df.apply(classify_stock, axis=1, result_type="expand")
    classified.columns = ["signal", "reason", "base_score"]
    df = pd.concat([df, classified], axis=1)
    df["score"] = df["base_score"]
    df.loc[df["trust_buy_days_5"] >= 4, "score"] += 2
    df.loc[df["foreign_support"] == True, "score"] += 1
    order = {
        "🔵 主動資金疑似布局": 1, "🟢 吸籌延續／初步確認": 2, "🟡 籌碼尚在、價格轉弱": 3,
        "⚪ 待驗證：僅外資流入": 4, "🔴 排除：拉高／過熱": 5, "🔴 排除：跌破MA20＋基本面變壞": 6,
        "🔴 排除：法人轉賣": 7, "🟠 排除：避險流量主導": 8, "⚪ 排除：權值／避險工具": 9, "⚪ 不列入": 99,
    }
    df["sort_order"] = df["signal"].map(order).fillna(99)
    df = df.sort_values(by=["sort_order", "score", "turnover"], ascending=[True, False, False], na_position="last").drop(columns=["sort_order"])
    return df

# ==========================================================
# 7. Email 格式與發送
# ==========================================================

def format_price(value):
    return "-" if pd.isna(value) else f"{float(value):.2f}"
def format_pct(value):
    return "累積中" if pd.isna(value) else f"{float(value):+.1f}%"
def format_ratio(value):
    return "累積中" if pd.isna(value) else f"{float(value) * 100:.2f}%"
def format_shares(value):
    if pd.isna(value):
        return "-"
    value = int(value)
    if abs(value) >= 1000:
        return f"{value / 1000:+.1f} 張"
    return f"{value:+,} 股"

def stock_lines(df, max_rows):
    if df is None or df.empty:
        return "（今日無股票）"
    lines = []
    for _, row in df.head(max_rows).iterrows():
        lines.append(f"{row['signal']}｜{row['stock_id']} {row['stock_name']} ｜{row['market']}｜收盤 {format_price(row['close'])}")
        lines.append(f"投信5日 {format_shares(row['trust_5d_net'])}（{int(row['trust_buy_days_5'])} 日買）｜投信相對量 {format_ratio(row['trust_volume_ratio'])}｜外資5日 {format_shares(row['foreign_5d_net'])}")
        lines.append(f"5日股價 {format_pct(row['return_5d_pct'])}｜10日振幅 {format_pct(row['range_10d_pct'])}｜距MA20 {format_pct(row['distance_ma20_pct'])}｜營收YoY {format_pct(row['month_revenue_yoy'])}")
        lines.append(f"判定：{row['reason']}")
        lines.append("")
    return "\n".join(lines).rstrip()

def make_email_body(radar, today_str, exec_time_str):
    blue = radar[radar["signal"] == "🔵 主動資金疑似布局"].copy()
    green = radar[radar["signal"] == "🟢 吸籌延續／初步確認"].copy()
    yellow = radar[radar["signal"] == "🟡 籌碼尚在、價格轉弱"].copy()
    red = radar[radar["signal"].str.startswith("🔴", na=False)].copy()
    orange = radar[radar["signal"] == "🟠 排除：避險流量主導"].copy()
    lines = [
        "台股主動資金疑似布局雷達",
        f"執行時間：{exec_time_str}（台灣時間）｜資料日期：{today_str}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"A. 🔵 主動資金疑似布局（{len(blue)} 檔）",
        "━━━━━━━━━━━━━━━━━━━━",
        stock_lines(blue, MAX_BLUE_EMAIL),
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"B. 🟢 吸籌延續／初步確認（{len(green)} 檔）",
        "━━━━━━━━━━━━━━━━━━━━",
        stock_lines(green, MAX_GREEN_EMAIL),
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"C. 🟡 籌碼尚在、價格轉弱（{len(yellow)} 檔）",
        "━━━━━━━━━━━━━━━━━━━━",
        stock_lines(yellow, MAX_YELLOW_EMAIL),
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"D. 🔴 排除／不布局（{len(red)} 檔）",
        "━━━━━━━━━━━━━━━━━━━━",
        stock_lines(red, MAX_RED_EMAIL),
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"E. 🟠 避險流量主導（{len(orange)} 檔）",
        "━━━━━━━━━━━━━━━━━━━━",
        stock_lines(orange, 10),
    ]
    return "\n".join(lines)

def send_email(subject, body):
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]):
        print("未設定 Gmail Secrets，略過寄信；CSV 仍會建立。")
        return
    message = MIMEMultipart()
    message["From"] = GMAIL_USER
    message["To"] = RECIPIENT_EMAIL
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(message)
    print("Email 寄送完成。")

# ==========================================================
# 8. 主流程
# ==========================================================

def main():
    NOW = datetime.now(TZ)
    TODAY = NOW.strftime("%Y-%m-%d")
    print(f"========== 主動資金布局雷達開始 {NOW} ==========")
    quotes = get_all_quotes()
    try:
        institutional_today = get_twse_institutional()
    except Exception as error:
        print(f"法人資料暫時失敗：{error}")
        institutional_today = pd.DataFrame(columns=["stock_id", "date", "foreign_net", "trust_net", "dealer_proprietary_net", "dealer_hedge_net", "dealer_net"])
    revenue_today = get_monthly_revenue()
    price_history = load_history("layout_price_history_")
    inst_history = load_history("layout_institutional_history_")
    rev_history = load_history("layout_revenue_history_")
    if not institutional_today.empty:
        inst_history = pd.concat([inst_history, institutional_today], ignore_index=True)
    if not revenue_today.empty:
        rev_history = pd.concat([rev_history, revenue_today], ignore_index=True)
    price_features = make_price_features(quotes, price_history, TODAY)
    inst_features = make_institutional_features(inst_history, price_features)
    revenue_features = make_revenue_features(rev_history)
    radar = build_radar(quotes, price_features, inst_features, revenue_features)
    save_today_history(quotes=quotes, institutional_df=institutional_today, revenue_df=revenue_today, today_str=TODAY)
    radar_csv = os.path.join(OUTPUT_DIR, f"layout_radar_{TODAY}.csv")
    radar.to_csv(radar_csv, index=False, encoding="utf-8-sig")
    print(f"雷達 CSV 已儲存：{radar_csv}")
    body = make_email_body(radar, today_str=TODAY, exec_time_str=NOW.strftime('%Y-%m-%d %H:%M'))
    send_email(subject=f"主動資金疑似布局雷達｜{TODAY}", body=body)
    print("========== 主動資金布局雷達完成 ==========")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        error_message = traceback.format_exc()
        print("\n========== 程式失敗 ==========")
        print(error_message)
        try:
            TODAY = datetime.now(TZ).strftime("%Y-%m-%d")
            send_email(subject=f"【錯誤】主動資金布局雷達｜{TODAY}", body=error_message)
        except Exception:
            pass
        raise
