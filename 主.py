# -*- coding: utf-8 -*-
"""
台股「主動資金疑似布局」雷達 v5.0
============================================================
用途：
1. 掃描上市／上櫃股票。
2. 尋找「投信持續買超、價格尚在盤整、非避險主導」的候選股。
3. 外資只做輔助；外資單獨流入不列為布局訊號。
4. 排除大型權值／ETF／工具股與避險流量主導的股票。
5. 月營收僅用於排除確認性惡化，而不是當作買進觸發。
6. 保留核心關注股的個別健檢，但不給額外分數，避免破壞客觀排序。
7. 保存每日價格、法人、營收快照，逐步建立 MA10、MA20 與 5/20 日法人趨勢。

注意：
- 本工具是公開資料的篩選與研究輔助，不是投資建議。
- 「中期資金流入待驗證」不代表可以確認特定主力、自然人或機構正在布局。
- 第 5 個交易日後，5 日資料才較有意義；第 20 個交易日後 MA20 才會完整。
"""

import io
import os
import smtplib
import time
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

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    )
}

# ==========================================================
# 1. 策略參數
# ==========================================================

# 每日成交額低於此門檻，流動性偏低，不列為藍／綠燈。
MIN_DAILY_TURNOVER = 30_000_000

# 投信 5 日淨買至少占「5 日平均成交量 × 5」的比例。
MIN_TRUST_5D_VOLUME_RATIO = 0.01

# 外資 5 日淨買至少占「5 日平均成交量 × 5」的比例。
MIN_FOREIGN_5D_VOLUME_RATIO = 0.03

# 避險絕對流量若過大，不將它當方向性布局。
MAX_HEDGE_5D_VOLUME_RATIO = 0.03

# 價格仍盤整、非過熱的條件。
MIN_RETURN_5D_FOR_ACCUMULATION = -7.0
MAX_RETURN_5D_FOR_ACCUMULATION = 6.0
MAX_RETURN_5D_FOR_MIDTERM = 8.0
MAX_RANGE_10D_FOR_ACCUMULATION = 16.0
MAX_RANGE_10D_FOR_MIDTERM = 20.0
MAX_VOLUME_RATIO_FOR_ACCUMULATION = 2.0
MAX_DISTANCE_MA20_FOR_ACCUMULATION = 7.0
MAX_DISTANCE_MA20_FOR_MIDTERM = 8.0
MAX_DISTANCE_MA10_FOR_GREEN = 8.0
MAX_RETURN_5D_FOR_GREEN = 10.0

# 「基本面確認惡化」只作排雷。
FUNDAMENTAL_BAD_MOM = -20.0
FUNDAMENTAL_WARNING_MOM = -10.0
FUNDAMENTAL_BAD_YOY = 0.0
YOY_DROP_BAD_PP = -20.0

# 最多讀取 90 天歷史 CSV。
MAX_HISTORY_FILES = 90

# Email 每個分類最多顯示檔數。
MAX_PURPLE_EMAIL = 10
MAX_BLUE_EMAIL = 10
MAX_GREEN_EMAIL = 10
MAX_YELLOW_EMAIL = 10
MAX_RED_EMAIL = 15
MAX_ORANGE_EMAIL = 10

# 可選的「理論風險試算」設定。
# 僅產生研究用數字，不構成買進建議。
ENABLE_THEORETICAL_RISK_SCENARIO = True
TOTAL_CAPITAL = 1_000_000
MAX_RISK_PCT = 0.01
REWARD_RISK_RATIO = 2.0


# ==========================================================
# 2. 個人關注清單與題材標籤
# ==========================================================

FOCUS_STOCKS = [
    "2383",  # 台光電
    "2368",  # 金像電
    "6197",  # 佳必琪
    "3293",  # 鈊象
    "4763",  # 材料-KY
    "1808",  # 潤隆
    "6919",  # 康霈
    "1503",  # 士電
]

# 這些是「研究備忘」而非即時估值、即時投資評等。
FOCUS_PROFILES = {
    "2383": {
        "name": "台光電",
        "theme": "AI伺服器／高速CCL",
        "research_note": "留意高速材料需求、產品組合與原物料成本。",
        "risk_note": "原物料成本波動、估值與景氣循環風險。",
    },
    "2368": {
        "name": "金像電",
        "theme": "AI伺服器／交換器PCB",
        "research_note": "留意高階伺服器與交換器 PCB 拉貨節奏。",
        "risk_note": "產能擴充、客戶拉貨與景氣循環風險。",
    },
    "6197": {
        "name": "佳必琪",
        "theme": "AI高速傳輸線束",
        "research_note": "留意高速線纜產品、伺服器需求與客戶集中度。",
        "risk_note": "伺服器出貨節奏與競爭壓力。",
    },
    "3293": {
        "name": "鈊象",
        "theme": "網路遊戲／授權",
        "research_note": "留意海外授權、產品營運與現金流變化。",
        "risk_note": "海外法規、遊戲營運與評價修正風險。",
    },
    "4763": {
        "name": "材料-KY",
        "theme": "材料／絲束",
        "research_note": "留意供需、擴產效益與營收趨勢。",
        "risk_note": "營收月減、供需反轉與估值變化。",
    },
    "1808": {
        "name": "潤隆",
        "theme": "營建／高股息",
        "research_note": "留意完工認列、現金流與股利政策。",
        "risk_note": "政策、工程進度與房市景氣風險。",
    },
    "6919": {
        "name": "康霈",
        "theme": "生技新藥",
        "research_note": "留意臨床試驗、授權與資金需求。",
        "risk_note": "臨床結果與生技研發不確定性高。",
    },
    "1503": {
        "name": "士電",
        "theme": "重電／變壓器／綠能",
        "research_note": "留意電網投資、外銷訂單與產能。",
        "risk_note": "原物料成本、交期與評價修正風險。",
    },
}

THEMES = {
    "⭐ 個人重點關注焦點股": FOCUS_STOCKS,
    "AI伺服器／ODM": ["2317", "2382", "3231", "6669", "6805"],
    "散熱": ["3017", "3324", "6205", "6131"],
    "PCB／CCL／載板": [
        "2383", "2368", "2385", "3037",
        "4967", "6274", "6197"
    ],
    "機器人／智慧自動化": ["4588", "1590", "2359", "4566"],
    "矽光子／CPO／光通訊": ["3163", "3363", "4979", "3450", "6451"],
    "重電／綠能": ["1513", "1519", "1503", "6873"],
}

THEME_MAP = {}

for theme_name, stock_ids in THEMES.items():
    for stock_id in stock_ids:
        THEME_MAP.setdefault(stock_id, []).append(theme_name)

# 不納入「提前主動布局雷達」的工具型／高權值股票與 ETF。
EXCLUDE_TOOL_STOCKS = {
    "2330",  # 台積電
    "2454",  # 聯發科
    "2308",  # 台達電
    "3711",  # 日月光投控
    "2881",  # 富邦金
    "2882",  # 國泰金
    "2884",  # 玉山金
    "2886",  # 兆豐金
    "2891",  # 中信金
    "2892",  # 第一金
    "2880",  # 華南金
    "0050",
    "0056",
    "00878",
    "006208",
    "00919",
    "00929",
}


# ==========================================================
# 3. 共用函式
# ==========================================================

def get_now():
    """取得台灣時區現在時間。"""
    return datetime.now(TZ)


def get_today_str():
    """取得 YYYY-MM-DD 日期字串。"""
    return get_now().strftime("%Y-%m-%d")


def normalize_stock_id(value):
    """統一股票代號格式為四碼字串。"""
    text = str(value).strip()

    if text.endswith(".0"):
        text = text[:-2]

    if text.isdigit() and len(text) < 4:
        return text.zfill(4)

    return text


def safe_float(value):
    """安全轉換文字數字為浮點數。"""
    if value is None:
        return np.nan

    try:
        text = str(value).strip()

        if text in {
            "", "--", "---", "-", "nan", "NaN",
            "None", "null", "—", "－"
        }:
            return np.nan

        text = (
            text.replace(",", "")
            .replace("%", "")
            .replace("＋", "+")
            .replace("－", "-")
            .replace("—", "-")
            .replace("–", "-")
        )

        if text.startswith("+"):
            text = text[1:]

        return float(text)

    except Exception:
        return np.nan


def safe_int(value):
    """安全轉換文字數字為整數。"""
    number = safe_float(value)

    if pd.isna(number):
        return np.nan

    return int(number)


def request_get(url, params=None, timeout=30):
    """GET 請求最多嘗試三次。"""
    last_error = None

    for attempt in range(3):
        try:
            response = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=timeout,
            )
            response.raise_for_status()
            return response

        except Exception as error:
            last_error = error
            wait_seconds = 2 * (attempt + 1)
            print(
                f"連線失敗，第 {attempt + 1} 次重試前等待 "
                f"{wait_seconds} 秒：{error}"
            )
            time.sleep(wait_seconds)

    raise RuntimeError(f"連線失敗：{url}｜{last_error}")


def find_first_column(columns, keywords):
    """從欄位名稱找第一個符合關鍵字的欄位。"""
    column_names = [str(column).replace("\n", "").strip() for column in columns]

    for keyword in keywords:
        for column in column_names:
            if keyword in column:
                return column

    return None


def get_recent_dates(days=20):
    """取得最近 N 個日曆日期，供找最近交易日資料。"""
    now = get_now()

    return [
        (now - timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(days)
    ]


def to_roc_year_month(year, month):
    """西元年、月轉為民國年月格式，例如 2026, 9 -> 11509。"""
    return f"{year - 1911}{month:02d}"


def infer_reported_revenue_month():
    """
    推定目前 MOPS 最新公布營收對應月份。

    一般月營收會在次月 10 日前公告：
    - 當月 1–10 日：最新通常是前兩個月的營收。
    - 當月 11 日後：最新通常是前一個月的營收。

    這是資料鍵值與去重用途；若公告時程特殊，仍以資料表實際年月欄位為優先。
    """
    now = get_now()

    if now.day <= 10:
        base = now.replace(day=1) - timedelta(days=1)
    else:
        base = now.replace(day=1) - timedelta(days=1)

    return base.strftime("%Y-%m")


# ==========================================================
# 4. 行情資料
# ==========================================================

def get_twse_quotes():
    """抓取 TWSE 上市最新行情。"""
    print("取得 TWSE 上市行情...")

    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    data = request_get(url).json()

    rows = []

    for item in data:
        stock_id = normalize_stock_id(item.get("Code", ""))

        if not stock_id.isdigit() or len(stock_id) != 4:
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
        raise RuntimeError("TWSE 上市行情解析後為空。")

    print(f"TWSE 上市行情完成：{len(df)} 檔。")
    return df


def get_tpex_quotes():
    """抓取 TPEx 上櫃最新行情。"""
    print("取得 TPEx 上櫃行情...")

    url = (
        "https://www.tpex.org.tw/openapi/v1/"
        "tpex_mainboard_daily_close_quotes"
    )
    data = request_get(url).json()

    rows = []

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

        close = safe_float(
            item.get("Close")
            or item.get("ClosingPrice")
            or item.get("收盤價")
            or item.get("收盤")
        )

        if pd.isna(close):
            continue

        rows.append({
            "stock_id": stock_id,
            "stock_name": str(
                item.get("CompanyName")
                or item.get("SecuritiesName")
                or item.get("Name")
                or item.get("股票名稱")
                or ""
            ).strip(),
            "market": "TPEx",
            "close": close,
            "change": safe_float(
                item.get("Change")
                or item.get("漲跌價差")
                or item.get("漲跌")
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
        raise RuntimeError("TPEx 上櫃行情解析後為空。")

    print(f"TPEx 上櫃行情完成：{len(df)} 檔。")
    return df


def get_all_quotes():
    """上市一定抓取；上櫃失敗時仍以上市資料繼續。"""
    frames = [get_twse_quotes()]

    try:
        frames.append(get_tpex_quotes())

    except Exception as error:
        print(f"TPEx 行情資料暫時失敗，今天只掃描上市：{error}")

    quotes = pd.concat(frames, ignore_index=True)
    quotes["stock_id"] = quotes["stock_id"].map(normalize_stock_id)

    return quotes.drop_duplicates(subset=["stock_id"], keep="first")


# ==========================================================
# 5. 法人資料
# ==========================================================

def get_twse_institutional():
    """
    取得 TWSE T86 法人資料。

    保留：
    - foreign_net：外資淨買賣超
    - trust_net：投信淨買賣超
    - dealer_proprietary_net：自營商自行買賣
    - dealer_hedge_net：自營商避險
    - hedge_data_available：避險欄位是否實際存在

    若避險欄位未提供，絕不把它默認成 0。
    """
    print("取得 TWSE 三大法人資料...")

    url = "https://www.twse.com.tw/rwd/zh/fund/T86"
    data = None
    used_date = None

    for date_str in get_recent_dates(20):
        try:
            response = request_get(
                url,
                params={
                    "response": "json",
                    "date": date_str,
                    "selectType": "ALLBUT0999",
                },
                timeout=20,
            )

            result = response.json()

            if result.get("stat") == "OK" and result.get("data"):
                data = result
                used_date = date_str
                break

        except Exception:
            continue

    if data is None:
        raise RuntimeError("找不到可用的 TWSE 法人資料。")

    fields = data.get("fields", [])
    raw_rows = data.get("data", [])

    if not fields or not raw_rows:
        raise RuntimeError("TWSE 法人資料欄位或內容為空。")

    hedge_column = None

    for candidate in [
        "自營商買賣超股數(避險)",
        "自營商買賣超股數（避險）",
    ]:
        if candidate in fields:
            hedge_column = candidate
            break

    proprietary_column = None

    for candidate in [
        "自營商買賣超股數(自行買賣)",
        "自營商買賣超股數（自行買賣）",
    ]:
        if candidate in fields:
            proprietary_column = candidate
            break

    date_formatted = (
        f"{used_date[:4]}-{used_date[4:6]}-{used_date[6:]}"
    )

    rows = []

    for raw_row in raw_rows:
        item = dict(zip(fields, raw_row))
        stock_id = normalize_stock_id(item.get("證券代號", ""))

        if not stock_id.isdigit() or len(stock_id) != 4:
            continue

        hedge_available = hedge_column is not None

        rows.append({
            "stock_id": stock_id,
            "date": date_formatted,
            "foreign_net": safe_int(
                item.get("外陸資買賣超股數(不含外資自營商)")
            ),
            "trust_net": safe_int(item.get("投信買賣超股數")),
            "dealer_proprietary_net": (
                safe_int(item.get(proprietary_column))
                if proprietary_column
                else np.nan
            ),
            "dealer_hedge_net": (
                safe_int(item.get(hedge_column))
                if hedge_column
                else np.nan
            ),
            "hedge_data_available": hedge_available,
            "institutional_source": "TWSE_T86",
        })

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError("TWSE 法人資料解析後為空。")

    for column in [
        "foreign_net",
        "trust_net",
        "dealer_proprietary_net",
        "dealer_hedge_net",
    ]:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    # 外資、投信沒有資料才補 0；避險資料不可補 0。
    df["foreign_net"] = df["foreign_net"].fillna(0)
    df["trust_net"] = df["trust_net"].fillna(0)

    print(
        f"TWSE 法人資料完成：{len(df)} 檔，"
        f"資料日期 {date_formatted}，"
        f"避險欄位 {'可用' if hedge_column else '未提供'}。"
    )

    return df


# ==========================================================
# 6. 月營收資料
# ==========================================================

def get_monthly_revenue():
    """
    取得 MOPS 上市／上櫃月營收。

    關鍵修正：
    - 盡可能從頁面表頭辨識「營收月份」。
    - 若無法辨識，使用推定月份作為 revenue_month。
    - 以 stock_id + revenue_month 去重，避免每天重跑相同月營收時
      被誤當作不同月份，導致 YoY 變化判斷錯誤。
    """
    print("取得月營收資料...")

    urls = [
        "https://mops.twse.com.tw/nas/t21/sii/t21sc03_if.html",
        "https://mops.twse.com.tw/nas/t21/otc/t21sc03_if.html",
    ]

    rows = []
    announce_date = get_today_str()
    fallback_revenue_month = infer_reported_revenue_month()

    for url in urls:
        try:
            response = request_get(url, timeout=45)

            try:
                html = response.content.decode("big5", errors="ignore")
            except Exception:
                html = response.text

            tables = pd.read_html(io.StringIO(html))

            for table in tables:
                if table.empty or len(table.columns) < 3:
                    continue

                table.columns = [
                    str(column).replace("\n", "").strip()
                    for column in table.columns
                ]

                code_column = find_first_column(
                    table.columns,
                    ["公司代號", "代號"],
                )

                revenue_column = find_first_column(
                    table.columns,
                    ["當月營收", "本月營收"],
                )

                yoy_column = find_first_column(
                    table.columns,
                    ["去年同月增減", "YoY"],
                )

                mom_column = find_first_column(
                    table.columns,
                    ["上月比較增減", "MoM"],
                )

                if not code_column or not yoy_column:
                    continue

                for _, item in table.iterrows():
                    stock_id = normalize_stock_id(
                        item.get(code_column, "")
                    )

                    if not stock_id.isdigit() or len(stock_id) != 4:
                        continue

                    rows.append({
                        "stock_id": stock_id,
                        "revenue_month": fallback_revenue_month,
                        "month_revenue": safe_float(
                            item.get(revenue_column, np.nan)
                        ),
                        "month_revenue_yoy": safe_float(
                            item.get(yoy_column, np.nan)
                        ),
                        "month_revenue_mom": (
                            safe_float(item.get(mom_column, np.nan))
                            if mom_column
                            else np.nan
                        ),
                        "announce_date": announce_date,
                    })

        except Exception as error:
            print(f"MOPS 月營收來源失敗：{url}｜{error}")

    df = pd.DataFrame(rows)

    if df.empty:
        print("本次未取得月營收；營收排雷暫時不啟用。")

        return pd.DataFrame(
            columns=[
                "stock_id",
                "revenue_month",
                "month_revenue",
                "month_revenue_yoy",
                "month_revenue_mom",
                "announce_date",
            ]
        )

    df["stock_id"] = df["stock_id"].map(normalize_stock_id)

    df = df.drop_duplicates(
        subset=["stock_id", "revenue_month"],
        keep="last",
    )

    print(f"月營收完成：{len(df)} 檔，營收月份：{fallback_revenue_month}。")
    return df


# ==========================================================
# 7. 歷史資料讀取與儲存
# ==========================================================

def load_history(prefix, limit=MAX_HISTORY_FILES):
    """讀取 output 下的歷史 CSV；壞檔不會使整支程式中斷。"""
    if not os.path.exists(OUTPUT_DIR):
        return pd.DataFrame()

    filenames = [
        filename
        for filename in os.listdir(OUTPUT_DIR)
        if filename.startswith(prefix) and filename.endswith(".csv")
    ]

    filenames = sorted(filenames)[-limit:]
    frames = []

    for filename in filenames:
        path = os.path.join(OUTPUT_DIR, filename)

        try:
            df = pd.read_csv(path, dtype={"stock_id": str})

            if not df.empty:
                df["stock_id"] = df["stock_id"].map(normalize_stock_id)
                frames.append(df)

        except Exception as error:
            print(f"讀取歷史檔失敗，略過：{filename}｜{error}")

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)


def save_today_history(quotes, institutional_df, revenue_df, today_str):
    """保存今日快照，供日後 MA、法人與營收趨勢使用。"""
    price_path = os.path.join(
        OUTPUT_DIR,
        f"layout_price_history_{today_str}.csv",
    )

    quotes.copy().assign(date=today_str).to_csv(
        price_path,
        index=False,
        encoding="utf-8-sig",
    )

    if institutional_df is not None and not institutional_df.empty:
        institutional_path = os.path.join(
            OUTPUT_DIR,
            f"layout_institutional_history_{today_str}.csv",
        )

        institutional_df.to_csv(
            institutional_path,
            index=False,
            encoding="utf-8-sig",
        )

    if revenue_df is not None and not revenue_df.empty:
        revenue_path = os.path.join(
            OUTPUT_DIR,
            f"layout_revenue_history_{today_str}.csv",
        )

        revenue_df.to_csv(
            revenue_path,
            index=False,
            encoding="utf-8-sig",
        )


# ==========================================================
# 8. 價格特徵
# ==========================================================

def make_price_features(quotes, price_history, today_str):
    """
    建立：
    - 5 日報酬
    - 10 日區間振幅
    - 前 5 日平均成交量
    - 當日量／前 5 日平均量
    - MA10、MA20
    - 與 MA10、MA20 的乖離率

    成交量平均採用 shift(1)：
    當日量不會反過來影響自己的平均量基準。
    """
    today_df = quotes.copy()
    today_df["date"] = today_str

    required_columns = [
        "stock_id",
        "stock_name",
        "market",
        "close",
        "volume",
        "turnover",
        "date",
    ]

    if price_history is None or price_history.empty:
        full = today_df[required_columns].copy()

    else:
        history = price_history.copy()

        for column in required_columns:
            if column not in history.columns:
                history[column] = np.nan

        full = pd.concat(
            [
                history[required_columns],
                today_df[required_columns],
            ],
            ignore_index=True,
        )

    full["stock_id"] = full["stock_id"].map(normalize_stock_id)
    full["date"] = full["date"].astype(str)

    for column in ["close", "volume", "turnover"]:
        full[column] = pd.to_numeric(
            full[column],
            errors="coerce",
        )

    full = (
        full
        .drop_duplicates(subset=["stock_id", "date"], keep="last")
        .sort_values(["stock_id", "date"])
    )

    grouped = full.groupby("stock_id", group_keys=False)

    full["ma10"] = grouped["close"].transform(
        lambda values: values.rolling(10, min_periods=10).mean()
    )

    full["ma20"] = grouped["close"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )

    full["close_5d_ago"] = grouped["close"].transform(
        lambda values: values.shift(4)
    )

    full["return_5d_pct"] = (
        full["close"] / full["close_5d_ago"] - 1
    ) * 100

    full["high_10d"] = grouped["close"].transform(
        lambda values: values.rolling(10, min_periods=10).max()
    )

    full["low_10d"] = grouped["close"].transform(
        lambda values: values.rolling(10, min_periods=10).min()
    )

    full["range_10d_pct"] = (
        full["high_10d"] / full["low_10d"] - 1
    ) * 100

    full["avg_volume_5d"] = grouped["volume"].transform(
        lambda values: values.shift(1).rolling(5, min_periods=5).mean()
    )

    full["volume_ratio_5d"] = (
        full["volume"] / full["avg_volume_5d"]
    )

    full["distance_ma10_pct"] = (
        full["close"] / full["ma10"] - 1
    ) * 100

    full["distance_ma20_pct"] = (
        full["close"] / full["ma20"] - 1
    ) * 100

    output_columns = [
        "stock_id",
        "ma10",
        "ma20",
        "return_5d_pct",
        "range_10d_pct",
        "avg_volume_5d",
        "volume_ratio_5d",
        "distance_ma10_pct",
        "distance_ma20_pct",
    ]

    return full.loc[
        full["date"] == today_str,
        output_columns,
    ].copy()


# ==========================================================
# 9. 法人特徵
# ==========================================================

def make_institutional_features(institutional_history, price_features):
    """
    建立：
    - 投信／外資 5 日淨買賣、買超天數
    - 投信／外資 20 日淨買賣、買超天數
    - 自營避險 5 日絕對流量
    - 投信吸籌、外資支持、避險主導
    - 中期資金流入待驗證

    核心原則：
    - 投信是藍燈／綠燈必要條件。
    - 外資只作輔助，不能單獨變成布局訊號。
    - 避險資料缺失時，狀態為 unknown，而非當作 0。
    """
    output_columns = [
        "stock_id",
        "trust_buy_days_5",
        "trust_5d_net",
        "foreign_buy_days_5",
        "foreign_5d_net",
        "dealer_hedge_5d_abs",
        "trust_volume_ratio",
        "foreign_volume_ratio",
        "hedge_volume_ratio",
        "hedge_data_status",
        "hedge_dominant",
        "trust_accumulation",
        "foreign_support",
        "trust_20d_net",
        "foreign_20d_net",
        "trust_buy_days_20",
        "foreign_buy_days_20",
        "institutional_days_20",
        "midterm_inflow_to_verify",
    ]

    if institutional_history is None or institutional_history.empty:
        return pd.DataFrame(columns=output_columns)

    df = institutional_history.copy()
    df["stock_id"] = df["stock_id"].map(normalize_stock_id)
    df["date"] = df["date"].astype(str)

    for column in [
        "foreign_net",
        "trust_net",
        "dealer_proprietary_net",
        "dealer_hedge_net",
    ]:
        if column not in df.columns:
            df[column] = np.nan

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    if "hedge_data_available" not in df.columns:
        df["hedge_data_available"] = df["dealer_hedge_net"].notna()

    df["hedge_data_available"] = (
        df["hedge_data_available"]
        .fillna(False)
        .astype(bool)
    )

    # 外資／投信缺值才補零；避險不補零。
    df["foreign_net"] = df["foreign_net"].fillna(0)
    df["trust_net"] = df["trust_net"].fillna(0)

    df = (
        df
        .drop_duplicates(subset=["stock_id", "date"], keep="last")
        .sort_values(["stock_id", "date"])
    )

    avg_volume_map = {}

    if price_features is not None and not price_features.empty:
        avg_volume_map = (
            price_features
            .set_index("stock_id")["avg_volume_5d"]
            .to_dict()
        )

    rows = []

    for stock_id, group in df.groupby("stock_id"):
        group = group.sort_values("date")

        group_5 = group.tail(5)
        group_20 = group.tail(20)

        trust_5d_net = float(group_5["trust_net"].sum())
        foreign_5d_net = float(group_5["foreign_net"].sum())

        trust_buy_days_5 = int((group_5["trust_net"] > 0).sum())
        foreign_buy_days_5 = int((group_5["foreign_net"] > 0).sum())

        trust_20d_net = float(group_20["trust_net"].sum())
        foreign_20d_net = float(group_20["foreign_net"].sum())

        trust_buy_days_20 = int((group_20["trust_net"] > 0).sum())
        foreign_buy_days_20 = int((group_20["foreign_net"] > 0).sum())

        institutional_days_20 = len(group_20)

        hedge_available_5d = bool(
            group_5["hedge_data_available"].all()
            and group_5["dealer_hedge_net"].notna().all()
            and len(group_5) > 0
        )

        if hedge_available_5d:
            dealer_hedge_5d_abs = float(
                group_5["dealer_hedge_net"].abs().sum()
            )
            hedge_data_status = "available"

        else:
            dealer_hedge_5d_abs = np.nan
            hedge_data_status = "unknown"

        avg_volume = avg_volume_map.get(stock_id, np.nan)

        if pd.isna(avg_volume) or avg_volume <= 0:
            trust_volume_ratio = np.nan
            foreign_volume_ratio = np.nan
            hedge_volume_ratio = np.nan

        else:
            five_day_volume = avg_volume * 5

            trust_volume_ratio = trust_5d_net / five_day_volume
            foreign_volume_ratio = foreign_5d_net / five_day_volume

            hedge_volume_ratio = (
                dealer_hedge_5d_abs / five_day_volume
                if hedge_available_5d
                else np.nan
            )

        hedge_dominant = False

        if hedge_available_5d:
            if (
                not pd.isna(hedge_volume_ratio)
                and hedge_volume_ratio >= MAX_HEDGE_5D_VOLUME_RATIO
            ):
                hedge_dominant = True

            if (
                dealer_hedge_5d_abs
                >= abs(trust_5d_net) + abs(foreign_5d_net)
                and dealer_hedge_5d_abs > 0
            ):
                hedge_dominant = True

        trust_accumulation = (
            len(group_5) >= 5
            and trust_buy_days_5 >= 3
            and trust_5d_net > 0
            and (
                pd.isna(trust_volume_ratio)
                or trust_volume_ratio >= MIN_TRUST_5D_VOLUME_RATIO
            )
        )

        foreign_support = (
            len(group_5) >= 5
            and foreign_buy_days_5 >= 3
            and foreign_5d_net > 0
            and (
                pd.isna(foreign_volume_ratio)
                or foreign_volume_ratio >= MIN_FOREIGN_5D_VOLUME_RATIO
            )
        )

        # 中期流入仍以投信為主；外資只能加強確認。
        # 至少 10 個交易日後才啟動，避免資料剛開始累積就誤判。
        midterm_inflow_to_verify = (
            institutional_days_20 >= 10
            and trust_buy_days_20 >= max(5, int(institutional_days_20 * 0.4))
            and trust_20d_net > 0
            and (
                pd.isna(avg_volume)
                or avg_volume <= 0
                or trust_20d_net / (avg_volume * institutional_days_20)
                >= MIN_TRUST_5D_VOLUME_RATIO
            )
        )

        rows.append({
            "stock_id": stock_id,
            "trust_buy_days_5": trust_buy_days_5,
            "trust_5d_net": trust_5d_net,
            "foreign_buy_days_5": foreign_buy_days_5,
            "foreign_5d_net": foreign_5d_net,
            "dealer_hedge_5d_abs": dealer_hedge_5d_abs,
            "trust_volume_ratio": trust_volume_ratio,
            "foreign_volume_ratio": foreign_volume_ratio,
            "hedge_volume_ratio": hedge_volume_ratio,
            "hedge_data_status": hedge_data_status,
            "hedge_dominant": hedge_dominant,
            "trust_accumulation": trust_accumulation,
            "foreign_support": foreign_support,
            "trust_20d_net": trust_20d_net,
            "foreign_20d_net": foreign_20d_net,
            "trust_buy_days_20": trust_buy_days_20,
            "foreign_buy_days_20": foreign_buy_days_20,
            "institutional_days_20": institutional_days_20,
            "midterm_inflow_to_verify": midterm_inflow_to_verify,
        })

    return pd.DataFrame(rows, columns=output_columns)


# ==========================================================
# 10. 營收特徵
# ==========================================================

def make_revenue_features(revenue_history):
    """
    用 revenue_month 而不是 announce_date 判斷前後月份。

    fundamental_bad 的條件：
    1. MoM <= -20% 且 YoY <= 0%
    或
    2. MoM <= -10%，且 YoY 相對前月下降至少 20 個百分點

    未取得營收資料時，不把股票直接判壞，只標示待驗證。
    """
    output_columns = [
        "stock_id",
        "revenue_month",
        "month_revenue",
        "month_revenue_yoy",
        "month_revenue_mom",
        "previous_yoy",
        "yoy_change_pp",
        "fundamental_bad",
        "fundamental_note",
    ]

    if revenue_history is None or revenue_history.empty:
        return pd.DataFrame(columns=output_columns)

    df = revenue_history.copy()
    df["stock_id"] = df["stock_id"].map(normalize_stock_id)

    if "revenue_month" not in df.columns:
        # 舊檔案沒有 revenue_month 時，退回 announce_date 的年月。
        df["revenue_month"] = (
            df.get("announce_date", "")
            .astype(str)
            .str.slice(0, 7)
        )

    if "announce_date" not in df.columns:
        df["announce_date"] = ""

    for column in [
        "month_revenue",
        "month_revenue_yoy",
        "month_revenue_mom",
    ]:
        if column not in df.columns:
            df[column] = np.nan

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df["revenue_month"] = df["revenue_month"].astype(str)
    df["announce_date"] = df["announce_date"].astype(str)

    df = (
        df
        .sort_values(["stock_id", "revenue_month", "announce_date"])
        .drop_duplicates(
            subset=["stock_id", "revenue_month"],
            keep="last",
        )
    )

    rows = []

    for stock_id, group in df.groupby("stock_id"):
        group = group.sort_values("revenue_month")

        latest = group.iloc[-1]
        previous = group.iloc[-2] if len(group) >= 2 else None

        yoy = safe_float(latest.get("month_revenue_yoy", np.nan))
        mom = safe_float(latest.get("month_revenue_mom", np.nan))

        previous_yoy = (
            safe_float(previous.get("month_revenue_yoy", np.nan))
            if previous is not None
            else np.nan
        )

        yoy_change_pp = (
            yoy - previous_yoy
            if not pd.isna(yoy) and not pd.isna(previous_yoy)
            else np.nan
        )

        bad_case_1 = (
            not pd.isna(mom)
            and not pd.isna(yoy)
            and mom <= FUNDAMENTAL_BAD_MOM
            and yoy <= FUNDAMENTAL_BAD_YOY
        )

        bad_case_2 = (
            not pd.isna(mom)
            and not pd.isna(yoy_change_pp)
            and mom <= FUNDAMENTAL_WARNING_MOM
            and yoy_change_pp <= YOY_DROP_BAD_PP
        )

        if bad_case_1:
            fundamental_bad = True
            note = "MoM 大幅下滑，且 YoY 轉弱或轉負。"

        elif bad_case_2:
            fundamental_bad = True
            note = "MoM 下滑且 YoY 較前月明顯降速。"

        elif pd.isna(yoy) and pd.isna(mom):
            fundamental_bad = False
            note = "營收資料待驗證。"

        else:
            fundamental_bad = False
            note = "營收未出現確認性惡化。"

        rows.append({
            "stock_id": stock_id,
            "revenue_month": latest.get("revenue_month", ""),
            "month_revenue": safe_float(
                latest.get("month_revenue", np.nan)
            ),
            "month_revenue_yoy": yoy,
            "month_revenue_mom": mom,
            "previous_yoy": previous_yoy,
            "yoy_change_pp": yoy_change_pp,
            "fundamental_bad": fundamental_bad,
            "fundamental_note": note,
        })

    return pd.DataFrame(rows, columns=output_columns)


# ==========================================================
# 11. 理論風險試算
# ==========================================================

def calculate_theoretical_risk_scenario(
    close_price,
    ma20_price,
    total_capital=TOTAL_CAPITAL,
    risk_pct=MAX_RISK_PCT,
    rr_ratio=REWARD_RISK_RATIO,
):
    """
    僅供研究用途的理論試算：

    假設：
    - 以 MA20 當作風險線；
    - 不含手續費、交易稅、滑價、跳空與個股波動；
    - 若收盤低於 MA20，則不做此試算。

    回傳：
    - theoretical_max_lots
    - theoretical_risk_amount
    - theoretical_target_price
    - theoretical_profit_amount
    """
    close_price = safe_float(close_price)
    ma20_price = safe_float(ma20_price)

    if pd.isna(close_price) or pd.isna(ma20_price):
        return 0, 0, np.nan, 0

    risk_per_share = close_price - ma20_price

    if risk_per_share <= 0:
        return 0, 0, np.nan, 0

    max_allowable_risk = total_capital * risk_pct
    max_shares = max_allowable_risk / risk_per_share
    theoretical_lots = max(int(max_shares // 1000), 0)

    theoretical_risk_amount = int(
        risk_per_share * theoretical_lots * 1000
    )

    target_profit_per_share = risk_per_share * rr_ratio
    theoretical_target_price = close_price + target_profit_per_share

    theoretical_profit_amount = int(
        target_profit_per_share * theoretical_lots * 1000
    )

    return (
        theoretical_lots,
        theoretical_risk_amount,
        round(theoretical_target_price, 2),
        theoretical_profit_amount,
    )


# ==========================================================
# 12. 燈號判斷
# ==========================================================

def classify_stock(row):
    """
    分類優先順序：

    1. 權值／ETF／工具股排除
    2. 避險主導排除
    3. 基本面確認惡化 + 跌破 MA20 -> 紅燈
    4. 過熱 -> 紅燈
    5. 法人同步轉賣 -> 紅燈
    6. 跌破 MA20 但基本面未壞且法人仍在 -> 黃燈
    7. 中期投信流入待驗證 -> 紫燈
    8. 短期投信盤整吸籌 -> 藍燈
    9. 投信吸籌 + 溫和轉強 -> 綠燈
    10. 僅外資流入 -> 灰色待驗證
    """
    stock_id = row.get("stock_id", "")

    if stock_id in EXCLUDE_TOOL_STOCKS:
        return (
            "⚪ 排除：權值／ETF／工具股",
            "高權值、ETF 或工具型流量可能影響法人數字，不納入提前布局雷達。",
            -5,
        )

    hedge_dominant = bool(row.get("hedge_dominant", False))

    if hedge_dominant:
        return (
            "🟠 排除：避險流量主導",
            "自營商避險流量偏高，可能來自權證、ETF、衍生品或造市對沖。",
            -4,
        )

    return_5d = row.get("return_5d_pct", np.nan)
    range_10d = row.get("range_10d_pct", np.nan)
    volume_ratio = row.get("volume_ratio_5d", np.nan)
    distance_ma10 = row.get("distance_ma10_pct", np.nan)
    distance_ma20 = row.get("distance_ma20_pct", np.nan)

    trust_5d_net = float(row.get("trust_5d_net", 0) or 0)
    foreign_5d_net = float(row.get("foreign_5d_net", 0) or 0)
    trust_buy_days_5 = int(row.get("trust_buy_days_5", 0) or 0)

    trust_accumulation = bool(
        row.get("trust_accumulation", False)
    )
    foreign_support = bool(row.get("foreign_support", False))
    midterm_inflow = bool(
        row.get("midterm_inflow_to_verify", False)
    )
    fundamental_bad = bool(row.get("fundamental_bad", False))

    turnover = safe_float(row.get("turnover", np.nan))
    liquid = not pd.isna(turnover) and turnover >= MIN_DAILY_TURNOVER

    # 紅燈：基本面確認惡化、且價格跌破 MA20。
    if (
        not pd.isna(distance_ma20)
        and distance_ma20 < 0
        and fundamental_bad
    ):
        return (
            "🔴 排除：跌破MA20＋基本面變壞",
            "收盤跌破 MA20，且月營收出現確認性惡化。",
            -5,
        )

    # 紅燈：短期急拉或爆量急拉。
    hot_price = (
        not pd.isna(return_5d)
        and return_5d > MAX_RETURN_5D_FOR_GREEN
    )

    hot_volume = (
        not pd.isna(volume_ratio)
        and volume_ratio > MAX_VOLUME_RATIO_FOR_ACCUMULATION
        and not pd.isna(return_5d)
        and return_5d > 3
    )

    if hot_price or hot_volume:
        return (
            "🔴 排除：拉高／過熱",
            "短期漲幅或爆量明顯，不符合盤整吸籌階段。",
            -3,
        )

    # 紅燈：投信與外資近五日同步轉賣。
    if (
        trust_5d_net < 0
        and foreign_5d_net < 0
        and trust_buy_days_5 == 0
    ):
        return (
            "🔴 排除：法人轉賣",
            "投信與外資近 5 日皆偏賣，且投信沒有買超日。",
            -3,
        )

    # 黃燈：跌破 MA20，但沒有基本面確認惡化，且尚有投信／外資支持。
    if (
        not pd.isna(distance_ma20)
        and distance_ma20 < 0
        and not fundamental_bad
        and (trust_accumulation or foreign_support or midterm_inflow)
    ):
        return (
            "🟡 籌碼尚在、價格轉弱",
            "跌破 MA20，但尚未出現基本面確認惡化；停止加碼，等待站回。",
            2,
        )

    # 黃燈：跌破 MA10 且投信近五日轉賣，但尚未到跌破 MA20。
    if (
        not pd.isna(distance_ma10)
        and distance_ma10 < 0
        and trust_5d_net < 0
        and (
            pd.isna(distance_ma20)
            or distance_ma20 >= 0
        )
    ):
        return (
            "🟡 籌碼鬆動、跌破MA10",
            "股價轉弱且投信近 5 日偏賣，先觀察而非加碼。",
            1,
        )

    midterm_price_ok = (
        (
            pd.isna(return_5d)
            or (
                MIN_RETURN_5D_FOR_ACCUMULATION
                <= return_5d
                <= MAX_RETURN_5D_FOR_MIDTERM
            )
        )
        and (
            pd.isna(range_10d)
            or range_10d <= MAX_RANGE_10D_FOR_MIDTERM
        )
        and (
            pd.isna(distance_ma20)
            or abs(distance_ma20) <= MAX_DISTANCE_MA20_FOR_MIDTERM
        )
    )

    # 紫燈：中期投信流入待驗證。
    if (
        liquid
        and midterm_inflow
        and midterm_price_ok
        and not fundamental_bad
    ):
        return (
            "🟣 中期資金流入待驗證",
            "近 10–20 日投信買盤具持續性，價格未明顯過熱；仍須驗證消息、產業與避險背景。",
            7,
        )

    short_term_price_ok = (
        (
            pd.isna(return_5d)
            or (
                MIN_RETURN_5D_FOR_ACCUMULATION
                <= return_5d
                <= MAX_RETURN_5D_FOR_ACCUMULATION
            )
        )
        and (
            pd.isna(range_10d)
            or range_10d <= MAX_RANGE_10D_FOR_ACCUMULATION
        )
        and (
            pd.isna(volume_ratio)
            or volume_ratio <= MAX_VOLUME_RATIO_FOR_ACCUMULATION
        )
        and (
            pd.isna(distance_ma20)
            or abs(distance_ma20) <= MAX_DISTANCE_MA20_FOR_ACCUMULATION
        )
    )

    # 藍燈：投信吸籌 + 盤整。
    if (
        liquid
        and trust_accumulation
        and short_term_price_ok
        and not fundamental_bad
    ):
        return (
            "🔵 主動資金疑似布局",
            "投信近 5 日持續買超、相對成交量具意義，價格仍在盤整，未見避險主導或爆量急拉。",
            8,
        )

    green_price_confirmation = (
        not pd.isna(distance_ma10)
        and 0 <= distance_ma10 <=
