# -*- coding: utf-8 -*-
"""
台股每日題材＋基本面＋法人布局雷達
=========================================================
資料來源：
1. TWSE OpenAPI：上市最新交易日行情
2. TPEx OpenAPI：上櫃最新交易日行情
3. TWSE T86：上市三大法人
4. TPEx OpenAPI：上櫃三大法人（若端點暫時失敗，不中止全程）
5. MOPS：月營收資料（若暫時失敗，不中止全程）

輸出：
- output/daily_theme_candidates_YYYY-MM-DD.csv
- output/institutional_history_YYYY-MM-DD.csv
- output/monthly_revenue_history_YYYY-MM-DD.csv

E 區三色燈：
🟢 綠燈：投信連買 3 個交易日，且基本面條件符合
🟡 黃燈：外資最近 5 個交易日累計買超，且基本面條件符合
🔴 紅燈：投信連賣 3 日／外資 5 日累計賣超／營收 YoY 轉負
"""

import os
import io
import re
import time
import smtplib
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import numpy as np
import pandas as pd
import requests


# =========================================================
# 基本設定
# =========================================================

TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)
TODAY = NOW.strftime("%Y-%m-%d")

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

DAILY_CSV = os.path.join(
    OUTPUT_DIR,
    f"daily_theme_candidates_{TODAY}.csv"
)

INST_CSV = os.path.join(
    OUTPUT_DIR,
    f"institutional_history_{TODAY}.csv"
)

REV_CSV = os.path.join(
    OUTPUT_DIR,
    f"monthly_revenue_history_{TODAY}.csv"
)

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    )
}

# 最低流動性：單日成交額 2,000 萬
MIN_TURNOVER = 20_000_000

# 最多讀取近幾日法人 CSV；足夠計算外資 5 日與投信 3 日
HISTORY_DAYS_LIMIT = 20


# =========================================================
# 題材池
# 可依你的偏好自行增刪代號
# =========================================================

THEMES = {
    "AI 伺服器／ODM": [
        "2317", "2382", "3231", "6669", "6805", "3017", "2356"
    ],
    "散熱": [
        "3017", "3324", "6205", "6131", "3338"
    ],
    "PCB／CCL／載板": [
        "2368", "2385", "3037", "4967", "6274", "3046", "6153"
    ],
    "電源管理／電源供應": [
        "2308", "6504", "3533", "2421", "6415"
    ],
    "光通訊／矽光子": [
        "3081", "3163", "6443", "4989", "4979", "3363"
    ],
    "先進封裝／測試": [
        "2330", "3711", "3131", "3374", "6196", "3264"
    ],
    "半導體設備／材料": [
        "6116", "3532", "4763", "6187", "6664", "6640"
    ],
    "機器人／自動化": [
        "2393", "4526", "1582", "4576", "2464", "6206"
    ],
}

# B 區：額外關注的延伸大型股
EXTENDED_POOL = [
    "2330", "2317", "2454", "2382", "3231", "2308", "6504",
    "2356", "3034", "3044", "3711", "6669", "6805", "3017",
    "2368", "2385", "3037", "4967", "6274", "3081", "3163",
    "3443", "4979", "3533", "6415", "6116", "3532",
]


# =========================================================
# 共用工具
# =========================================================

def safe_float(value):
    """安全轉 float；無法轉換時回傳 NaN。"""
    try:
        text = str(value).strip()
        text = text.replace(",", "")
        text = text.replace("%", "")
        text = text.replace("＋", "+")
        text = text.replace("－", "-")
        text = text.replace("—", "")
        text = text.replace("--", "")
        text = text.replace("---", "")

        if text in ["", "-", "+", "nan", "NaN", "None", "null"]:
            return np.nan

        return float(text)

    except Exception:
        return np.nan


def safe_int(value):
    """安全轉 int；無法轉換時回傳 NaN。"""
    number = safe_float(value)

    if pd.isna(number):
        return np.nan

    return int(number)


def normalize_stock_id(value):
    """統一股票代號為字串。"""
    text = str(value).strip()

    if text.endswith(".0"):
        text = text[:-2]

    return text


def request_get(url, params=None, timeout=30):
    """HTTP GET，最多重試 3 次。"""
    last_error = None

    for attempt in range(3):
        try:
            response = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=timeout
            )
            response.raise_for_status()
            return response

        except Exception as error:
            last_error = error
            seconds = (attempt + 1) * 2
            print(f"連線失敗，{seconds} 秒後重試：{error}")
            time.sleep(seconds)

    raise last_error


def roc_date(yyyymmdd):
    """西元 YYYYMMDD 轉成民國 YYYYMMDD。"""
    year = int(yyyymmdd[:4]) - 1911
    return f"{year}{yyyymmdd[4:]}"


def today_yyyymmdd():
    return NOW.strftime("%Y%m%d")


def format_date_yyyy_mm_dd(yyyymmdd):
    text = str(yyyymmdd)

    if len(text) == 8:
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"

    return text


def find_first_column(columns, possible_names):
    """從欄位名稱中找第一個符合候選關鍵字的欄位。"""
    for name in possible_names:
        for column in columns:
            if name in str(column):
                return column

    return None


# =========================================================
# 1. 上市行情：TWSE OpenAPI
# =========================================================

def get_twse_quotes():
    """
    取得 TWSE 最新可用交易日的上市個股資料。
    使用官方 OpenAPI，避免 MI_INDEX 多表格解析問題。
    """
    print("取得 TWSE 最新上市行情...")

    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"

    response = request_get(url)
    data = response.json()

    if not isinstance(data, list) or not data:
        raise RuntimeError("TWSE OpenAPI 沒有回傳上市行情資料")

    rows = []

    for item in data:
        stock_id = normalize_stock_id(item.get("Code", ""))
        stock_name = str(item.get("Name", "")).strip()

        # 先保留四碼股票；排除 ETF、權證、債券等
        if not stock_id.isdigit() or len(stock_id) != 4:
            continue

        close = safe_float(item.get("ClosingPrice"))

        if pd.isna(close):
            continue

        rows.append({
            "stock_id": stock_id,
            "stock_name": stock_name,
            "market": "TWSE",
            "close": close,
            "change": safe_float(item.get("Change")),
            "volume": safe_int(item.get("TradeVolume")),
            "turnover": safe_float(item.get("TradeValue")),
        })

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError("TWSE OpenAPI 有回傳資料，但未解析到上市四碼股票")

    print(f"TWSE 行情成功：{len(df)} 筆")

    return df


# =========================================================
# 2. 上櫃行情：TPEx OpenAPI
# =========================================================

def get_tpex_quotes():
    """
    取得 TPEx 最新可用交易日的上櫃主板日收盤資料。
    若 TPEx API 欄位變動或暫時失敗，由 main() 捕捉，不中止上市資料流程。
    """
    print("取得 TPEx 最新上櫃行情...")

    url = (
        "https://www.tpex.org.tw/openapi/v1/"
        "tpex_mainboard_daily_close_quotes"
    )

    response = request_get(url)
    data = response.json()

    if not isinstance(data, list) or not data:
        raise RuntimeError("TPEx OpenAPI 沒有回傳上櫃行情資料")

    rows = []

    for item in data:
        stock_id = normalize_stock_id(
            item.get("SecuritiesCompanyCode")
            or item.get("SecuritiesCode")
            or item.get("Code")
            or item.get("股票代號")
            or ""
        )

        stock_name = str(
            item.get("CompanyName")
            or item.get("SecuritiesName")
            or item.get("Name")
            or item.get("股票名稱")
            or ""
        ).strip()

        if not stock_id.isdigit() or len(stock_id) != 4:
            continue

        close = safe_float(
            item.get("Close")
            or item.get("ClosingPrice")
            or item.get("收盤")
            or item.get("收盤價")
        )

        if pd.isna(close):
            continue

        rows.append({
            "stock_id": stock_id,
            "stock_name": stock_name,
            "market": "TPEx",
            "close": close,
            "change": safe_float(
                item.get("Change")
                or item.get("漲跌")
                or item.get("漲跌價差")
            ),
            "volume": safe_int(
                item.get("Volume")
                or item.get("TradeVolume")
                or item.get("成交股數")
            ),
            "turnover": safe_float(
                item.get("Amount")
                or item.get("TradeValue")
                or item.get("成交金額")
            ),
        })

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError("TPEx OpenAPI 有回傳資料，但未解析到上櫃四碼股票")

    print(f"TPEx 行情成功：{len(df)} 筆")

    return df


def get_all_quotes():
    """合併上市＋上櫃行情；TPEx 失敗不影響 TWSE。"""
    quote_frames = []

    twse_df = get_twse_quotes()
    quote_frames.append(twse_df)

    try:
        tpex_df = get_tpex_quotes()
        quote_frames.append(tpex_df)
    except Exception as error:
        print(f"TPEx 行情暫時失敗，今日先使用 TWSE 資料：{error}")

    quotes = pd.concat(quote_frames, ignore_index=True)
    quotes = quotes.drop_duplicates(subset=["stock_id"], keep="first")

    return quotes


# =========================================================
# 3. 上市法人：TWSE RWD T86
# =========================================================

def get_twse_institutional():
    """
    取得上市三大法人個股買賣超。
    使用今日日期；若尚未有資料，就回傳空表，避免主程式中止。
    """
    print("取得 TWSE 三大法人資料...")

    url = "https://www.twse.com.tw/rwd/zh/fund/T86"

    params = {
        "response": "json",
        "date": today_yyyymmdd(),
        "selectType": "ALLBUT0999",
    }

    response = request_get(url, params=params)
    data = response.json()

    if data.get("stat") != "OK":
        raise RuntimeError(f"TWSE 法人資料不可用：{data.get('stat')}")

    fields = data.get("fields", [])
    rows = data.get("data", [])

    if not fields or not rows:
        raise RuntimeError("TWSE 法人資料為空")

    parsed = []

    for row in rows:
        item = dict(zip(fields, row))

        stock_id = normalize_stock_id(item.get("證券代號", ""))

        if not stock_id.isdigit() or len(stock_id) != 4:
            continue

        parsed.append({
            "stock_id": stock_id,
            "date": TODAY,
            "foreign_net": safe_int(
                item.get("外陸資買賣超股數(不含外資自營商)")
            ),
            "trust_net": safe_int(
                item.get("投信買賣超股數")
            ),
            "dealer_net": safe_int(
                item.get("自營商買賣超股數")
            ),
        })

    df = pd.DataFrame(parsed)

    if df.empty:
        raise RuntimeError("TWSE 法人資料未解析到四碼上市股票")

    print(f"TWSE 法人成功：{len(df)} 筆")

    return df


# =========================================================
# 4. 上櫃法人：TPEx OpenAPI
# =========================================================

def get_tpex_institutional():
    """
    嘗試取得 TPEx 上櫃法人資料。
    TPEx OpenAPI 的欄位可能調整，因此失敗時主程式仍會繼續。
    """
    print("取得 TPEx 三大法人資料...")

    possible_urls = [
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_institutional_investors",
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_institutional_trading",
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_institutional_investors_trading",
    ]

    last_error = None

    for url in possible_urls:
        try:
            response = request_get(url)
            data = response.json()

            if not isinstance(data, list) or not data:
                continue

            parsed = []

            for item in data:
                stock_id = normalize_stock_id(
                    item.get("SecuritiesCompanyCode")
                    or item.get("SecuritiesCode")
                    or item.get("Code")
                    or item.get("股票代號")
                    or ""
                )

                if not stock_id.isdigit() or len(stock_id) != 4:
                    continue

                parsed.append({
                    "stock_id": stock_id,
                    "date": TODAY,
                    "foreign_net": safe_int(
                        item.get("ForeignNetBuySell")
                        or item.get("ForeignNet")
                        or item.get("外資買賣超")
                        or item.get("外資及陸資買賣超")
                    ),
                    "trust_net": safe_int(
                        item.get("InvestmentTrustNetBuySell")
                        or item.get("TrustNet")
                        or item.get("投信買賣超")
                    ),
                    "dealer_net": safe_int(
                        item.get("DealerNetBuySell")
                        or item.get("DealerNet")
                        or item.get("自營商買賣超")
                    ),
                })

            df = pd.DataFrame(parsed)

            if not df.empty:
                print(f"TPEx 法人成功：{len(df)} 筆")
                return df

        except Exception as error:
            last_error = error

    raise RuntimeError(f"TPEx 法人資料暫時不可用：{last_error}")


def get_all_institutional():
    """
    取得上市＋上櫃法人。
    法人資料失敗不會阻止行情 CSV 與 Email 產生。
    """
    frames = []

    try:
        frames.append(get_twse_institutional())
    except Exception as error:
        print(f"TWSE 法人暫時失敗：{error}")

    try:
        frames.append(get_tpex_institutional())
    except Exception as error:
        print(f"TPEx 法人暫時失敗：{error}")

    if not frames:
        return pd.DataFrame(
            columns=[
                "stock_id",
                "date",
                "foreign_net",
                "trust_net",
                "dealer_net",
            ]
        )

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["stock_id", "date"], keep="first")

    return df


# =========================================================
# 5. 月營收：MOPS
# =========================================================

def get_mops_monthly_revenue():
    """
    嘗試取得 MOPS 月營收。
    MOPS HTML 格式常變；失敗時不終止主程式。
    目前會回傳可解析到的最新月營收 YoY。
    """
    print("取得月營收資料...")

    urls = [
        "https://mops.twse.com.tw/nas/t21/sii/t21sc03_if.html",
        "https://mops.twse.com.tw/nas/t21/otc/t21sc03_if.html",
    ]

    result_rows = []

    for url in urls:
        try:
            response = request_get(url, timeout=45)
            tables = pd.read_html(io.StringIO(response.text))

            for table in tables:
                if table.empty:
                    continue

                table.columns = [
                    str(column).replace("\n", "").strip()
                    for column in table.columns
                ]

                code_column = find_first_column(
                    table.columns,
                    ["公司代號", "代號"]
                )

                yoy_column = find_first_column(
                    table.columns,
                    ["去年同月增減(%)", "去年同月增減", "年增率", "YoY"]
                )

                revenue_column = find_first_column(
                    table.columns,
                    ["當月營收", "本月營收"]
                )

                if not code_column or not yoy_column:
                    continue

                for _, item in table.iterrows():
                    stock_id = normalize_stock_id(item.get(code_column, ""))

                    if not stock_id.isdigit() or len(stock_id) != 4:
                        continue

                    result_rows.append({
                        "stock_id": stock_id,
                        "month_revenue": safe_float(
                            item.get(revenue_column, np.nan)
                        ),
                        "month_revenue_yoy": safe_float(
                            item.get(yoy_column, np.nan)
                        ),
                        "announce_date": TODAY,
                    })

        except Exception as error:
            print(f"MOPS 月營收來源失敗：{url}｜{error}")

    df = pd.DataFrame(result_rows)

    if df.empty:
        print("本次沒有取得可用月營收；E 區將以法人與行情訊號為主。")
        return pd.DataFrame(
            columns=[
                "stock_id",
                "month_revenue",
                "month_revenue_yoy",
                "announce_date",
            ]
        )

    df = df.drop_duplicates(subset=["stock_id"], keep="last")

    print(f"月營收成功：{len(df)} 筆")

    return df


# =========================================================
# 6. 歷史 CSV
# =========================================================

def read_csv_safely(file_path):
    try:
        return pd.read_csv(file_path, dtype={"stock_id": str})
    except Exception:
        return pd.DataFrame()


def load_institutional_history():
    """
    讀取 output/ 過去法人 CSV。
    只取最近 HISTORY_DAYS_LIMIT 份，避免 repo 越久越慢。
    """
    files = []

    for filename in os.listdir(OUTPUT_DIR):
        if (
            filename.startswith("institutional_history_")
            and filename.endswith(".csv")
        ):
            files.append(filename)

    files = sorted(files)[-HISTORY_DAYS_LIMIT:]

    frames = []

    for filename in files:
        df = read_csv_safely(os.path.join(OUTPUT_DIR, filename))

        if not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame(
            columns=[
                "stock_id",
                "date",
                "foreign_net",
                "trust_net",
                "dealer_net",
            ]
        )

    history = pd.concat(frames, ignore_index=True)

    history["stock_id"] = history["stock_id"].map(normalize_stock_id)
    history["date"] = history["date"].astype(str)

    for column in ["foreign_net", "trust_net", "dealer_net"]:
        if column not in history.columns:
            history[column] = np.nan

        history[column] = pd.to_numeric(
            history[column],
            errors="coerce"
        ).fillna(0)

    history = history.drop_duplicates(
        subset=["stock_id", "date"],
        keep="last"
    )

    return history.sort_values(["stock_id", "date"])


def load_revenue_history():
    """讀取過去月營收快照，供未來擴充連續營收判斷。"""
    files = []

    for filename in os.listdir(OUTPUT_DIR):
        if (
            filename.startswith("monthly_revenue_history_")
            and filename.endswith(".csv")
        ):
            files.append(filename)

    files = sorted(files)[-12:]

    frames = []

    for filename in files:
        df = read_csv_safely(os.path.join(OUTPUT_DIR, filename))

        if not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame(
            columns=[
                "stock_id",
                "month_revenue",
                "month_revenue_yoy",
                "announce_date",
            ]
        )

    history = pd.concat(frames, ignore_index=True)

    history["stock_id"] = history["stock_id"].map(normalize_stock_id)

    if "announce_date" in history.columns:
        history["announce_date"] = history["announce_date"].astype(str)

    return history.drop_duplicates(
        subset=["stock_id"],
        keep="last"
    )


# =========================================================
# 7. 三色燈與分數
# =========================================================

def calculate_institutional_signals(stock_id, institutional_history):
    """
    計算：
    - 投信連買／連賣 3 日
    - 外資最近 5 日累計買賣超
    """

    history = institutional_history[
        institutional_history["stock_id"] == stock_id
    ].copy()

    if history.empty:
        return {
            "trust_buy_days": 0,
            "trust_sell_days": 0,
            "trust_buy_3": False,
            "trust_sell_3": False,
            "foreign_5d_net": 0,
            "foreign_buy_5": False,
            "foreign_sell_5": False,
        }

    history = history.sort_values("date")

    trust_values = history["trust_net"].tail(3).tolist()
    foreign_values = history["foreign_net"].tail(5).tolist()

    trust_buy_days = 0
    trust_sell_days = 0

    for value in reversed(history["trust_net"].tolist()):
        if value > 0:
            trust_buy_days += 1
        else:
            break

    for value in reversed(history["trust_net"].tolist()):
        if value < 0:
            trust_sell_days += 1
        else:
            break

    trust_buy_3 = (
        len(trust_values) >= 3
        and all(value > 0 for value in trust_values)
    )

    trust_sell_3 = (
        len(trust_values) >= 3
        and all(value < 0 for value in trust_values)
    )

    foreign_5d_net = (
        int(sum(foreign_values))
        if foreign_values
        else 0
    )

    return {
        "trust_buy_days": trust_buy_days,
        "trust_sell_days": trust_sell_days,
        "trust_buy_3": trust_buy_3,
        "trust_sell_3": trust_sell_3,
        "foreign_5d_net": foreign_5d_net,
        "foreign_buy_5": foreign_5d_net > 0,
        "foreign_sell_5": foreign_5d_net < 0,
    }


def build_signal_radar(all_data, institutional_history):
    """
    建立 E 區：
    基本面加速＋法人布局雷達。
    """

    rows = []

    for _, item in all_data.iterrows():
        stock_id = item["stock_id"]

        signal_info = calculate_institutional_signals(
            stock_id,
            institutional_history
        )

        yoy = item.get("month_revenue_yoy", np.nan)
        turnover = item.get("turnover", np.nan)

        revenue_positive = not pd.isna(yoy) and yoy > 0
        revenue_growth = not pd.isna(yoy) and yoy > 10
        liquid = not pd.isna(turnover) and turnover >= MIN_TURNOVER

        score = 0

        # 基本面
        if revenue_positive:
            score += 2

        if revenue_growth:
            score += 2

        # 流動性
        if liquid:
            score += 1

        # 法人：A 為主，B 為輔
        if signal_info["trust_buy_3"]:
            score += 3

        if signal_info["foreign_buy_5"]:
            score += 1

        if signal_info["trust_sell_3"]:
            score -= 3

        if signal_info["foreign_sell_5"]:
            score -= 1

        # 三色燈邏輯
        if (
            signal_info["trust_buy_3"]
            and revenue_growth
            and liquid
        ):
            signal = "🟢 投信布局綠燈"
            action = "優先研究；確認產品、客戶與產業趨勢後，可考慮分批小部位。"

        elif (
            signal_info["foreign_buy_5"]
            and revenue_positive
            and liquid
            and not signal_info["trust_buy_3"]
        ):
            signal = "🟡 外資流入黃燈"
            action = "加入自選；等待投信跟進或下一次營收／價格確認。"

        elif (
            signal_info["trust_sell_3"]
            or signal_info["foreign_sell_5"]
            or (not pd.isna(yoy) and yoy < 0)
        ):
            signal = "🔴 資金／基本面紅燈"
            action = "若持有，重新檢查持股理由與風險；未持有者避免因跌深追進。"

        else:
            signal = "⚪ 觀察"
            action = "尚未形成完整法人布局訊號，持續追蹤。"

        rows.append({
            "signal": signal,
            "stock_id": stock_id,
            "stock_name": item.get("stock_name", ""),
            "market": item.get("market", ""),
            "close": item.get("close", np.nan),
            "turnover": turnover,
            "month_revenue_yoy": yoy,
            "trust_buy_days": signal_info["trust_buy_days"],
            "trust_sell_days": signal_info["trust_sell_days"],
            "foreign_5d_net": signal_info["foreign_5d_net"],
            "score": score,
            "action": action,
        })

    radar = pd.DataFrame(rows)

    if radar.empty:
        return radar

    signal_order = {
        "🟢 投信布局綠燈": 1,
        "🟡 外資流入黃燈": 2,
        "⚪ 觀察": 3,
        "🔴 資金／基本面紅燈": 4,
    }

    radar["signal_order"] = radar["signal"].map(signal_order).fillna(99)

    radar = radar.sort_values(
        by=["signal_order", "score", "turnover"],
        ascending=[True, False, False],
        na_position="last"
    ).drop(columns=["signal_order"])

    return radar


# =========================================================
# 8. A／B／C 區報表
# =========================================================

def make_theme_section(quotes):
    rows = []

    for theme, stock_ids in THEMES.items():
        for stock_id in stock_ids:
            matched = quotes[quotes["stock_id"] == stock_id]

            if matched.empty:
                rows.append({
                    "theme": theme,
                    "stock_id": stock_id,
                    "stock_name": "資料未取得",
                    "market": "",
                    "close": np.nan,
                    "change": np.nan,
                    "turnover": np.nan,
                })
            else:
                item = matched.iloc[0].to_dict()
                item["theme"] = theme
                rows.append(item)

    df = pd.DataFrame(rows)

    return df[[
        "theme",
        "stock_id",
        "stock_name",
        "market",
        "close",
        "change",
        "turnover",
    ]].sort_values(
        by=["theme", "turnover"],
        ascending=[True, False],
        na_position="last"
    )


def make_extended_section(quotes):
    rows = []

    for stock_id in EXTENDED_POOL:
        matched = quotes[quotes["stock_id"] == stock_id]

        if matched.empty:
            continue

        rows.append(matched.iloc[0].to_dict())

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows).sort_values(
        by="turnover",
        ascending=False,
        na_position="last"
    )


def make_fundamental_candidates(all_data):
    """
    C 區：
    月營收年增 > 10% ＋ 單日成交額至少 2,000 萬。
    若當次月營收抓取失敗，這區會是空的，不影響 A／B／E 與 CSV。
    """

    if "month_revenue_yoy" not in all_data.columns:
        return pd.DataFrame()

    candidates = all_data[
        (all_data["month_revenue_yoy"] > 10)
        & (all_data["turnover"] >= MIN_TURNOVER)
    ].copy()

    if candidates.empty:
        return candidates

    candidates["fundamental_score"] = 0
    candidates.loc[candidates["month_revenue_yoy"] > 0, "fundamental_score"] += 2
    candidates.loc[candidates["month_revenue_yoy"] > 10, "fundamental_score"] += 2
    candidates.loc[candidates["turnover"] >= MIN_TURNOVER, "fundamental_score"] += 1

    return candidates.sort_values(
        by=["fundamental_score", "month_revenue_yoy", "turnover"],
        ascending=[False, False, False],
        na_position="last"
    ).head(50)


# =========================================================
# 9. CSV 與 Email
# =========================================================

def format_price(value):
    if pd.isna(value):
        return "-"

    return f"{float(value):.2f}"


def format_change(value):
    if pd.isna(value):
        return "-"

    return f"{float(value):+.2f}"


def format_turnover(value):
    if pd.isna(value):
        return "-"

    amount = float(value)

    if abs(amount) >= 100_000_000:
        return f"{amount / 100_000_000:.2f} 億"

    if abs(amount) >= 1_000_000:
        return f"{amount / 1_000_000:.2f} 百萬"

    return f"{amount:,.0f}"


def format_net_shares(value):
    if pd.isna(value):
        return "-"

    value = int(value)

    if abs(value) >= 1000:
        return f"{value / 1000:.1f} 張"

    return f"{value:,} 股"


def dataframe_to_text(df, columns, max_rows=30):
    """
    將 DataFrame 轉成適合 Email 的純文字內容。
    """

    if df is None or df.empty:
        return "（今日無符合條件資料）"

    lines = []

    for _, item in df.head(max_rows).iterrows():
        parts = []

        for column, label, formatter in columns:
            value = item.get(column, np.nan)
            parts.append(f"{label}{formatter(value)}")

        lines.append("｜".join(parts))

    return "\n".join(lines)


def make_email_body(theme_df, extended_df, candidates_df, radar_df):
    """
    產生完整 Email。
    """

    green_df = radar_df[
        radar_df["signal"] == "🟢 投信布局綠燈"
    ] if not radar_df.empty else pd.DataFrame()

    yellow_df = radar_df[
        radar_df["signal"] == "🟡 外資流入黃燈"
    ] if not radar_df.empty else pd.DataFrame()

    red_df = radar_df[
        radar_df["signal"] == "🔴 資金／基本面紅燈"
    ] if not radar_df.empty else pd.DataFrame()

    lines = [
        "台股每日題材＋基本面＋法人布局雷達",
        f"執行日期：{TODAY}",
        f"執行時間：{NOW.strftime('%Y-%m-%d %H:%M')}（台灣時間）",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "A. 熱門題材主題股",
        "━━━━━━━━━━━━━━━━━━━━",
        dataframe_to_text(
            theme_df,
            [
                ("theme", "", lambda x: f"【{x}】"),
                ("stock_id", "", lambda x: str(x)),
                ("stock_name", " ", lambda x: str(x)),
                ("market", " ", lambda x: f"({x})"),
                ("close", "｜收盤 ", format_price),
                ("change", "｜漲跌 ", format_change),
                ("turnover", "｜成交額 ", format_turnover),
            ],
            max_rows=80
        ),
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "B. 題材延伸關注股",
        "━━━━━━━━━━━━━━━━━━━━",
        dataframe_to_text(
            extended_df,
            [
                ("stock_id", "", lambda x: str(x)),
                ("stock_name", " ", lambda x: str(x)),
                ("market", " ", lambda x: f"({x})"),
                ("close", "｜收盤 ", format_price),
                ("change", "｜漲跌 ", format_change),
                ("turnover", "｜成交額 ", format_turnover),
            ],
            max_rows=35
        ),
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "C. 全市場基本面加速候選",
        "條件：月營收 YoY > 10% ＋ 成交額 ≥ 2,000 萬",
        "━━━━━━━━━━━━━━━━━━━━",
        dataframe_to_text(
            candidates_df,
            [
                ("stock_id", "", lambda x: str(x)),
                ("stock_name", " ", lambda x: str(x)),
                ("market", " ", lambda x: f"({x})"),
                ("close", "｜收盤 ", format_price),
                ("month_revenue_yoy", "｜營收 YoY ", lambda x: f"{float(x):+.1f}%" if not pd.isna(x) else "-"),
                ("turnover", "｜成交額 ", format_turnover),
            ],
            max_rows=30
        ),
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "E. 基本面加速＋法人布局雷達",
        "A 為主：投信連買 3 日＝綠燈",
        "B 為輔：外資近 5 日累買＝黃燈",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "🟢 投信布局綠燈",
        dataframe_to_text(
            green_df,
            [
                ("stock_id", "", lambda x: str(x)),
                ("stock_name", " ", lambda x: str(x)),
                ("month_revenue_yoy", "｜營收 YoY ", lambda x: f"{float(x):+.1f}%" if not pd.isna(x) else "-"),
                ("trust_buy_days", "｜投信連買 ", lambda x: f"{int(x)} 日"),
                ("foreign_5d_net", "｜外資5日 ", format_net_shares),
                ("score", "｜分數 ", lambda x: str(int(x))),
            ],
            max_rows=25
        ),
        "",
        "🟡 外資流入黃燈",
        dataframe_to_text(
            yellow_df,
            [
                ("stock_id", "", lambda x: str(x)),
                ("stock_name", " ", lambda x: str(x)),
                ("month_revenue_yoy", "｜營收 YoY ", lambda x: f"{float(x):+.1f}%" if not pd.isna(x) else "-"),
                ("trust_buy_days", "｜投信連買 ", lambda x: f"{int(x)} 日"),
                ("foreign_5d_net", "｜外資5日 ", format_net_shares),
                ("score", "｜分數 ", lambda x: str(int(x))),
            ],
            max_rows=25
        ),
        "",
        "🔴 資金／基本面紅燈",
        dataframe_to_text(
            red_df,
            [
                ("stock_id", "", lambda x: str(x)),
                ("stock_name", " ", lambda x: str(x)),
                ("month_revenue_yoy", "｜營收 YoY ", lambda x: f"{float(x):+.1f}%" if not pd.isna(x) else "-"),
                ("trust_sell_days", "｜投信連賣 ", lambda x: f"{int(x)} 日"),
                ("foreign_5d_net", "｜外資5日 ", format_net_shares),
                ("score", "｜分數 ", lambda x: str(int(x))),
            ],
            max_rows=25
        ),
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "提醒",
        "━━━━━━━━━━━━━━━━━━━━",
        "1. 綠燈是優先研究名單，不是自動買進訊號。",
        "2. 黃燈代表外資流入，但需等待投信、營收或價格進一步確認。",
        "3. 紅燈代表法人或基本面轉弱，持有者應重新檢查風險。",
        "4. 本信為公開資料整理與研究輔助，不構成投資建議。",
    ]

    return "\n".join(lines)


def send_email(subject, body):
    """
    Gmail Secrets 未設好時不會阻止 CSV 輸出。
    """

    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]):
        print("未設定 Gmail Secrets，略過寄信；CSV 仍會輸出。")
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


# =========================================================
# 10. 主流程
# =========================================================

def main():
    print("========== 台股雷達開始 ==========")

    # -----------------------------------------------------
    # 行情資料：上市必抓；上櫃失敗時仍保留上市
    # -----------------------------------------------------
    quotes = get_all_quotes()

    # -----------------------------------------------------
    # 法人資料：失敗時仍可輸出 A/B/C 與 CSV
    # -----------------------------------------------------
    institutional_today = get_all_institutional()

    if institutional_today.empty:
        print("本次法人資料為空；綠黃紅燈將等待歷史資料累積。")
    else:
        institutional_today.to_csv(
            INST_CSV,
            index=False,
            encoding="utf-8-sig"
        )
        print(f"法人 CSV 已建立：{INST_CSV}")

    # -----------------------------------------------------
    # 月營收：失敗時保留空欄位
    # -----------------------------------------------------
    revenue_today = get_mops_monthly_revenue()

    if not revenue_today.empty:
        revenue_today.to_csv(
            REV_CSV,
            index=False,
            encoding="utf-8-sig"
        )
        print(f"月營收 CSV 已建立：{REV_CSV}")

    # -----------------------------------------------------
    # 讀取歷史資料
    # -----------------------------------------------------
    institutional_history = load_institutional_history()
    revenue_history = load_revenue_history()

    # 將當日資料確保併入記憶體歷史
    if not institutional_today.empty:
        institutional_history = pd.concat(
            [institutional_history, institutional_today],
            ignore_index=True
        ).drop_duplicates(
            subset=["stock_id", "date"],
            keep="last"
        )

    if not revenue_today.empty:
        revenue_history = pd.concat(
            [revenue_history, revenue_today],
            ignore_index=True
        ).drop_duplicates(
            subset=["stock_id"],
            keep="last"
        )

    # -----------------------------------------------------
    # 合併行情＋最新營收
    # -----------------------------------------------------
    all_data = quotes.copy()

    if not revenue_history.empty:
        revenue_latest = revenue_history[
            [
                "stock_id",
                "month_revenue",
                "month_revenue_yoy",
            ]
        ].drop_duplicates(
            subset=["stock_id"],
            keep="last"
        )

        all_data = all_data.merge(
            revenue_latest,
            on="stock_id",
            how="left"
        )
    else:
        all_data["month_revenue"] = np.nan
        all_data["month_revenue_yoy"] = np.nan

    # -----------------------------------------------------
    # A/B/C/E 區
    # -----------------------------------------------------
    theme_df = make_theme_section(quotes)
    extended_df = make_extended_section(quotes)
    candidates_df = make_fundamental_candidates(all_data)
    radar_df = build_signal_radar(
        all_data,
        institutional_history
    )

    # -----------------------------------------------------
    # 主 CSV：保留 E 區所有訊號
    # -----------------------------------------------------
    daily_export = radar_df.copy()

    if daily_export.empty:
        daily_export = all_data.copy()

    daily_export.to_csv(
        DAILY_CSV,
        index=False,
        encoding="utf-8-sig"
    )

    print(f"每日雷達 CSV 已建立：{DAILY_CSV}")

    # -----------------------------------------------------
    # Email
    # -----------------------------------------------------
    email_body = make_email_body(
        theme_df=theme_df,
        extended_df=extended_df,
        candidates_df=candidates_df,
        radar_df=radar_df
    )

    send_email(
        subject=f"台股題材＋法人布局雷達｜{TODAY}",
        body=email_body
    )

    print("========== 台股雷達完成 ==========")


if __name__ == "__main__":
    try:
        main()

    except Exception:
        error_message = traceback.format_exc()

        print("\n========== 程式失敗 ==========")
        print(error_message)

        try:
            send_email(
                subject=f"【錯誤】台股題材雷達｜{TODAY}",
                body=error_message
            )
        except Exception:
            pass

        raise
