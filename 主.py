# -*- coding: utf-8 -*-
"""
台股主動資金雷達 v5.1
============================================================
特色：
1. 上市／上櫃行情掃描。
2. 以投信連續買超為主，外資僅作輔助。
3. 排除權值、ETF、工具型流量與自營商避險主導。
4. 用價格盤整、MA10、MA20、成交量與法人資料做雷達分類。
5. 保留 8 大核心持股深度健檢，但不對焦點股人工加分。
6. 每日保存價格與法人資料，逐步建立 5／10／20 日歷史。
7. Email 與 CSV 輸出。
8. 本工具僅供公開資料研究，不構成投資建議。

資料累積提醒：
- 第 5 個交易日後：投信 5 日與均量訊號較有意義。
- 第 10 個交易日後：10 日振幅與中期流入開始有意義。
- 第 20 個交易日後：MA20 與完整中期法人趨勢才完整。
"""

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
        "Mozilla/5.0 (GitHub Actions; stock-radar-v5.1)"
    )
}

# 最低流動性：當日成交額至少 3,000 萬元。
MIN_DAILY_TURNOVER = 30_000_000

# 5 日投信買超占平均成交量比例門檻。
MIN_TRUST_5D_VOLUME_RATIO = 0.01

# 5 日外資買超占平均成交量比例門檻。
MIN_FOREIGN_5D_VOLUME_RATIO = 0.03

# 避險絕對流量太大時，不視為方向性布局。
MAX_HEDGE_5D_VOLUME_RATIO = 0.03

# 最多保留並讀取 90 份日資料。
MAX_HISTORY_FILES = 90


# ==========================================================
# 1. 核心關注清單、題材與排除清單
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

# 這裡是你的研究備忘，不是程式即時估值結論。
FOCUS_PROFILES = {
    "2383": {
        "name": "台光電",
        "theme": "AI伺服器／高速CCL",
        "valuation": "合理偏高（高成長支撐）",
        "good_catalyst": "高速材料需求與產品組合",
        "risk_catalyst": "銅箔、玻纖布等原料成本波動",
        "rating": "持續研究",
    },
    "2368": {
        "name": "金像電",
        "theme": "AI伺服器／交換器PCB",
        "valuation": "合理區間",
        "good_catalyst": "高階伺服器與交換器PCB需求",
        "risk_catalyst": "產能擴充與客戶拉貨節奏",
        "rating": "持續研究",
    },
    "6197": {
        "name": "佳必琪",
        "theme": "AI高速傳輸線束",
        "valuation": "成長型估值",
        "good_catalyst": "高速線纜與伺服器需求",
        "risk_catalyst": "伺服器出貨節奏與競爭壓力",
        "rating": "持續研究",
    },
    "3293": {
        "name": "鈊象",
        "theme": "網路遊戲／授權",
        "valuation": "偏高，須持續追蹤",
        "good_catalyst": "海外授權與產品營運",
        "risk_catalyst": "海外法規與評價修正",
        "rating": "持續研究",
    },
    "4763": {
        "name": "材料-KY",
        "theme": "材料／絲束",
        "valuation": "需觀察成長持續性",
        "good_catalyst": "供需與擴產效益",
        "risk_catalyst": "營收趨勢、供需反轉與評價變化",
        "rating": "持續研究",
    },
    "1808": {
        "name": "潤隆",
        "theme": "營建／高股息",
        "valuation": "資產與現金流導向",
        "good_catalyst": "完工認列、現金流與股利政策",
        "risk_catalyst": "工程進度、政策與房市景氣",
        "rating": "持續研究",
    },
    "6919": {
        "name": "康霈",
        "theme": "生技新藥",
        "valuation": "高不確定性題材估值",
        "good_catalyst": "臨床、授權與研發進展",
        "risk_catalyst": "臨床結果與資金需求風險",
        "rating": "高風險研究",
    },
    "1503": {
        "name": "士電",
        "theme": "重電／變壓器／綠能",
        "valuation": "成長型估值",
        "good_catalyst": "電網投資、外銷訂單與產能",
        "risk_catalyst": "原物料、交期與評價修正",
        "rating": "持續研究",
    },
}

THEMES = {
    "AI伺服器／ODM": [
        "2317",
        "2382",
        "3231",
        "6669",
        "6805",
    ],
    "散熱": [
        "3017",
        "3324",
        "6205",
        "6131",
    ],
    "PCB／CCL／載板": [
        "2383",
        "2368",
        "2385",
        "3037",
        "4967",
        "6274",
        "6197",
    ],
    "機器人／智慧自動化": [
        "4588",
        "1590",
        "2359",
        "4566",
    ],
    "矽光子／CPO／光通訊": [
        "3163",
        "3363",
        "4979",
        "3450",
        "6451",
    ],
    "重電／綠能": [
        "1513",
        "1519",
        "1503",
        "6873",
    ],
}

THEME_MAP = {}

for theme_name, stock_ids in THEMES.items():
    for stock_id in stock_ids:
        THEME_MAP.setdefault(stock_id, []).append(
            theme_name
        )

# 這些股票／ETF 不納入「提前布局」雷達。
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
# 2. 共用函式
# ==========================================================

def now_tw():
    """取得台灣時區現在時間。"""
    return datetime.now(TZ)


def today_str():
    """取得 YYYY-MM-DD 日期字串。"""
    return now_tw().strftime("%Y-%m-%d")


def normalize_stock_id(value):
    """統一股票代號格式。"""
    text = str(value).strip()

    if text.endswith(".0"):
        text = text[:-2]

    if text.isdigit() and len(text) < 4:
        return text.zfill(4)

    return text


def safe_float(value):
    """安全把來源文字轉成浮點數。"""
    if value is None:
        return np.nan

    try:
        text = str(value).strip()

        if text in {
            "",
            "-",
            "--",
            "---",
            "nan",
            "NaN",
            "None",
            "null",
            "—",
            "－",
        }:
            return np.nan

        text = text.replace(",", "")
        text = text.replace("%", "")
        text = text.replace("＋", "+")
        text = text.replace("－", "-")
        text = text.replace("—", "-")
        text = text.replace("–", "-")

        if text.startswith("+"):
            text = text[1:]

        return float(text)

    except Exception:
        return np.nan


def safe_int(value):
    """安全把來源文字轉成整數。"""
    number = safe_float(value)

    if pd.isna(number):
        return np.nan

    return int(number)


def request_get(url, params=None, timeout=30):
    """HTTP GET，最多重試三次。"""
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

    raise RuntimeError(
        f"連線失敗：{url}｜最後錯誤：{last_error}"
    )


def get_recent_dates(days=20):
    """取得最近 N 個日期，供法人資料找最近交易日。"""
    return [
        (now_tw() - timedelta(days=offset)).strftime(
            "%Y%m%d"
        )
        for offset in range(days)
    ]


# ==========================================================
# 3. 取得行情
# ==========================================================

def get_twse_quotes():
    """取得 TWSE 上市最新行情。"""
    print("取得 TWSE 上市行情...")

    url = (
        "https://openapi.twse.com.tw/v1/"
        "exchangeReport/STOCK_DAY_ALL"
    )

    data = request_get(url).json()
    rows = []

    for item in data:
        stock_id = normalize_stock_id(
            item.get("Code", "")
        )

        close = safe_float(
            item.get("ClosingPrice")
        )

        if not stock_id.isdigit():
            continue

        if len(stock_id) != 4:
            continue

        if pd.isna(close):
            continue

        rows.append({
            "stock_id": stock_id,
            "stock_name": str(
                item.get("Name", "")
            ).strip(),
            "market": "TWSE",
            "close": close,
            "change": safe_float(
                item.get("Change")
            ),
            "volume": safe_int(
                item.get("TradeVolume")
            ),
            "turnover": safe_float(
                item.get("TradeValue")
            ),
        })

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError("TWSE 行情解析後為空。")

    print(f"TWSE 行情完成：{len(df)} 檔。")
    return df


def get_tpex_quotes():
    """取得 TPEx 上櫃最新行情。"""
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

        close = safe_float(
            item.get("Close")
            or item.get("ClosingPrice")
            or item.get("收盤價")
            or item.get("收盤")
        )

        if not stock_id.isdigit():
            continue

        if len(stock_id) != 4:
            continue

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
        raise RuntimeError("TPEx 行情解析後為空。")

    print(f"TPEx 行情完成：{len(df)} 檔。")
    return df


def get_all_quotes():
    """
    上市資料必抓。
    上櫃 API 失敗時，仍使用上市資料完成本次執行。
    """
    frames = [get_twse_quotes()]

    try:
        frames.append(get_tpex_quotes())

    except Exception as error:
        print(
            f"TPEx 行情暫時不可用，今天只掃描上市：{error}"
        )

    quotes = pd.concat(frames, ignore_index=True)

    quotes["stock_id"] = quotes["stock_id"].map(
        normalize_stock_id
    )

    return quotes.drop_duplicates(
        subset=["stock_id"],
        keep="first",
    )


# ==========================================================
# 4. 取得 TWSE 法人資料
# ==========================================================

def get_twse_institutional():
    """
    取得 TWSE T86 法人資料。

    保留外資、投信、自營商自行買賣與避險資料。
    找不到避險欄位時，明確標記為不可用，
    不會把它錯誤當成 0。
    """
    print("取得 TWSE 法人資料...")

    url = "https://www.twse.com.tw/rwd/zh/fund/T86"

    data = None
    used_date = None

    for date_code in get_recent_dates(20):
        try:
            result = request_get(
                url,
                params={
                    "response": "json",
                    "date": date_code,
                    "selectType": "ALLBUT0999",
                },
                timeout=20,
            ).json()

            if (
                result.get("stat") == "OK"
                and result.get("data")
            ):
                data = result
                used_date = date_code
                break

        except Exception:
            continue

    if data is None:
        raise RuntimeError(
            "找不到可用的 TWSE 法人資料。"
        )

    fields = data.get("fields", [])
    raw_rows = data.get("data", [])

    hedge_column = None
    proprietary_column = None

    for column in fields:
        if (
            "自營商買賣超股數" in column
            and "避險" in column
        ):
            hedge_column = column

        if (
            "自營商買賣超股數" in column
            and "自行買賣" in column
        ):
            proprietary_column = column

    formatted_date = (
        f"{used_date[:4]}-{used_date[4:6]}-{used_date[6:]}"
    )

    rows = []

    for raw_row in raw_rows:
        item = dict(zip(fields, raw_row))

        stock_id = normalize_stock_id(
            item.get("證券代號", "")
        )

        if not stock_id.isdigit():
            continue

        if len(stock_id) != 4:
            continue

        rows.append({
            "stock_id": stock_id,
            "date": formatted_date,
            "foreign_net": safe_int(
                item.get(
                    "外陸資買賣超股數(不含外資自營商)"
                )
            ),
            "trust_net": safe_int(
                item.get("投信買賣超股數")
            ),
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
            "hedge_data_available": (
                hedge_column is not None
            ),
        })

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError(
            "TWSE 法人資料解析後為空。"
        )

    df["foreign_net"] = pd.to_numeric(
        df["foreign_net"],
        errors="coerce",
    ).fillna(0)

    df["trust_net"] = pd.to_numeric(
        df["trust_net"],
        errors="coerce",
    ).fillna(0)

    df["dealer_proprietary_net"] = pd.to_numeric(
        df["dealer_proprietary_net"],
        errors="coerce",
    )

    df["dealer_hedge_net"] = pd.to_numeric(
        df["dealer_hedge_net"],
        errors="coerce",
    )

    hedge_text = "可用" if hedge_column else "未提供"

    print(
        f"TWSE 法人完成：{len(df)} 檔，"
        f"資料日期：{formatted_date}，"
        f"避險欄位：{hedge_text}。"
    )

    return df


# ==========================================================
# 5. 歷史資料讀取與保存
# ==========================================================

def load_history(prefix):
    """讀取 output 下指定前綴的歷史 CSV。"""
    filenames = sorted([
        filename
        for filename in os.listdir(OUTPUT_DIR)
        if filename.startswith(prefix)
        and filename.endswith(".csv")
    ])[-MAX_HISTORY_FILES:]

    frames = []

    for filename in filenames:
        path = os.path.join(
            OUTPUT_DIR,
            filename,
        )

        try:
            frame = pd.read_csv(
                path,
                dtype={"stock_id": str},
            )

            if not frame.empty:
                frame["stock_id"] = frame[
                    "stock_id"
                ].map(normalize_stock_id)

                frames.append(frame)

        except Exception as error:
            print(
                f"歷史檔讀取失敗，已略過："
                f"{filename}｜{error}"
            )

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)


def save_today_history(
    quotes,
    institutional,
    date_text,
):
    """儲存今天行情與法人資料。"""
    price_path = os.path.join(
        OUTPUT_DIR,
        f"layout_price_history_{date_text}.csv",
    )

    quotes.copy().assign(
        date=date_text
    ).to_csv(
        price_path,
        index=False,
        encoding="utf-8-sig",
    )

    if (
        institutional is not None
        and not institutional.empty
    ):
        institutional_path = os.path.join(
            OUTPUT_DIR,
            f"layout_institutional_history_{date_text}.csv",
        )

        institutional.to_csv(
            institutional_path,
            index=False,
            encoding="utf-8-sig",
        )


# ==========================================================
# 6. 價格特徵
# ==========================================================

def make_price_features(
    quotes,
    history,
    date_text,
):
    """
    計算：
    - MA10
    - MA20
    - 5 日漲跌幅
    - 10 日振幅
    - 前 5 日均量
    - 今日相對量
    - MA10／MA20 乖離率
    """
    needed_columns = [
        "stock_id",
        "stock_name",
        "market",
        "close",
        "volume",
        "turnover",
        "date",
    ]

    today_frame = quotes.copy()
    today_frame["date"] = date_text

    if history is None or history.empty:
        full = today_frame[needed_columns].copy()

    else:
        old = history.copy()

        for column in needed_columns:
            if column not in old.columns:
                old[column] = np.nan

        full = pd.concat(
            [
                old[needed_columns],
                today_frame[needed_columns],
            ],
            ignore_index=True,
        )

    full["stock_id"] = full["stock_id"].map(
        normalize_stock_id
    )

    full["date"] = full["date"].astype(str)

    for column in [
        "close",
        "volume",
        "turnover",
    ]:
        full[column] = pd.to_numeric(
            full[column],
            errors="coerce",
        )

    full = (
        full
        .drop_duplicates(
            subset=["stock_id", "date"],
            keep="last",
        )
        .sort_values(["stock_id", "date"])
    )

    grouped = full.groupby(
        "stock_id",
        group_keys=False,
    )

    full["ma10"] = grouped["close"].transform(
        lambda values: values.rolling(
            10,
            min_periods=10,
        ).mean()
    )

    full["ma20"] = grouped["close"].transform(
        lambda values: values.rolling(
            20,
            min_periods=20,
        ).mean()
    )

    full["close_5d_ago"] = grouped["close"].transform(
        lambda values: values.shift(4)
    )

    full["return_5d_pct"] = (
        full["close"] / full["close_5d_ago"] - 1
    ) * 100

    full["high_10d"] = grouped["close"].transform(
        lambda values: values.rolling(
            10,
            min_periods=10,
        ).max()
    )

    full["low_10d"] = grouped["close"].transform(
        lambda values: values.rolling(
            10,
            min_periods=10,
        ).min()
    )

    full["range_10d_pct"] = (
        full["high_10d"] / full["low_10d"] - 1
    ) * 100

    # 用前五日均量，不把今日成交量混進基準。
    full["avg_volume_5d"] = grouped[
        "volume"
    ].transform(
        lambda values: values.shift(1).rolling(
            5,
            min_periods=5,
        ).mean()
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
        full["date"] == date_text,
        output_columns,
    ].copy()


# ==========================================================
# 7. 法人特徵
# ==========================================================

def make_institutional_features(
    history,
    price_features,
):
    """
    計算：
    - 投信／外資 5 日、20 日淨買賣。
    - 投信／外資 5 日、20 日買超天數。
    - 自營商避險絕對流量。
    - 投信是否形成短期吸籌。
    - 外資是否形成輔助支持。
    - 中期投信流入待驗證。
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

    if history is None or history.empty:
        return pd.DataFrame(
            columns=output_columns
        )

    df = history.copy()

    df["stock_id"] = df["stock_id"].map(
        normalize_stock_id
    )

    df["date"] = df["date"].astype(str)

    for column in [
        "foreign_net",
        "trust_net",
        "dealer_hedge_net",
    ]:
        if column not in df.columns:
            df[column] = np.nan

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    if "hedge_data_available" not in df.columns:
        df["hedge_data_available"] = (
            df["dealer_hedge_net"].notna()
        )

    df["hedge_data_available"] = (
        df["hedge_data_available"]
        .fillna(False)
        .astype(bool)
    )

    # 外資、投信若空缺可補零；避險資料不可補零。
    df["foreign_net"] = df["foreign_net"].fillna(0)
    df["trust_net"] = df["trust_net"].fillna(0)

    df = (
        df
        .drop_duplicates(
            subset=["stock_id", "date"],
            keep="last",
        )
        .sort_values(["stock_id", "date"])
    )

    volume_map = {}

    if (
        price_features is not None
        and not price_features.empty
    ):
        volume_map = (
            price_features
            .set_index("stock_id")["avg_volume_5d"]
            .to_dict()
        )

    rows = []

    for stock_id, group in df.groupby("stock_id"):
        group = group.sort_values("date")

        group_5 = group.tail(5)
        group_20 = group.tail(20)

        trust_5d_net = float(
            group_5["trust_net"].sum()
        )

        foreign_5d_net = float(
            group_5["foreign_net"].sum()
        )

        trust_20d_net = float(
            group_20["trust_net"].sum()
        )

        foreign_20d_net = float(
            group_20["foreign_net"].sum()
        )

        trust_buy_days_5 = int(
            (group_5["trust_net"] > 0).sum()
        )

        foreign_buy_days_5 = int(
            (group_5["foreign_net"] > 0).sum()
        )

        trust_buy_days_20 = int(
            (group_20["trust_net"] > 0).sum()
        )

        foreign_buy_days_20 = int(
            (group_20["foreign_net"] > 0).sum()
        )

        institutional_days_20 = len(group_20)

        hedge_available = bool(
            len(group_5) >= 5
            and group_5[
                "hedge_data_available"
            ].all()
            and group_5[
                "dealer_hedge_net"
            ].notna().all()
        )

        if hedge_available:
            dealer_hedge_5d_abs = float(
                group_5[
                    "dealer_hedge_net"
                ].abs().sum()
            )

            hedge_data_status = "available"

        else:
            dealer_hedge_5d_abs = np.nan
            hedge_data_status = "unknown"

        average_volume = volume_map.get(
            stock_id,
            np.nan,
        )

        if pd.isna(average_volume) or average_volume <= 0:
            trust_volume_ratio = np.nan
            foreign_volume_ratio = np.nan
            hedge_volume_ratio = np.nan

        else:
            five_day_volume = average_volume * 5

            trust_volume_ratio = (
                trust_5d_net / five_day_volume
            )

            foreign_volume_ratio = (
                foreign_5d_net / five_day_volume
            )

            if hedge_available:
                hedge_volume_ratio = (
                    dealer_hedge_5d_abs / five_day_volume
                )
            else:
                hedge_volume_ratio = np.nan

        hedge_dominant = False

        if hedge_available:
            if (
                not pd.isna(hedge_volume_ratio)
                and hedge_volume_ratio
                >= MAX_HEDGE_5D_VOLUME_RATIO
            ):
                hedge_dominant = True

            if (
                dealer_hedge_5d_abs
                >= abs(trust_5d_net)
                + abs(foreign_5d_net)
                and dealer_hedge_5d_abs > 0
            ):
                hedge_dominant = True

        trust_accumulation = (
            len(group_5) >= 5
            and trust_buy_days_5 >= 3
            and trust_5d_net > 0
            and (
                pd.isna(trust_volume_ratio)
                or trust_volume_ratio
                >= MIN_TRUST_5D_VOLUME_RATIO
            )
        )

        foreign_support = (
            len(group_5) >= 5
            and foreign_buy_days_5 >= 3
            and foreign_5d_net > 0
            and (
                pd.isna(foreign_volume_ratio)
                or foreign_volume_ratio
                >= MIN_FOREIGN_5D_VOLUME_RATIO
            )
        )

        midterm_inflow_to_verify = (
            institutional_days_20 >= 10
            and trust_buy_days_20
            >= max(
                5,
                int(institutional_days_20 * 0.4),
            )
            and trust_20d_net > 0
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
            "midterm_inflow_to_verify": (
                midterm_inflow_to_verify
            ),
        })

    return pd.DataFrame(
        rows,
        columns=output_columns,
    )


# ==========================================================
# 8. 分類規則
# ==========================================================

def classify_stock(row):
    """
    分類順序：

    1. 權值／ETF／工具股排除。
    2. 避險主導排除。
    3. 過熱與法人同步轉賣列紅燈。
    4. 跌破 MA20 且仍有法人支持列黃燈。
    5. 中期投信流入列紫燈待驗證。
    6. 投信盤整吸籌列藍燈。
    7. 投信吸籌後站上 MA10 列綠燈。
    8. 僅外資流入列灰色待驗證。
    """
    stock_id = row["stock_id"]

    if stock_id in EXCLUDE_TOOL_STOCKS:
        return (
            "⚪ 排除：權值／ETF／工具股",
            "高權值或工具型流量，不納入提前布局雷達。",
            -5,
        )

    if bool(row.get("hedge_dominant", False)):
        return (
            "🟠 排除：避險流量主導",
            "自營商避險流量偏高，不當作方向性買盤。",
            -4,
        )

    return_5d = row.get("return_5d_pct", np.nan)
    range_10d = row.get("range_10d_pct", np.nan)
    volume_ratio = row.get("volume_ratio_5d", np.nan)
    distance_ma10 = row.get("distance_ma10_pct", np.nan)
    distance_ma20 = row.get("distance_ma20_pct", np.nan)

    trust_5d_net = float(
        row.get("trust_5d_net", 0) or 0
    )

    foreign_5d_net = float(
        row.get("foreign_5d_net", 0) or 0
    )

    trust_buy_days_5 = int(
        row.get("trust_buy_days_5", 0) or 0
    )

    trust_accumulation = bool(
        row.get("trust_accumulation", False)
    )

    foreign_support = bool(
        row.get("foreign_support", False)
    )

    midterm_inflow = bool(
        row.get("midterm_inflow_to_verify", False)
    )

    turnover = safe_float(
        row.get("turnover", np.nan)
    )

    liquid = (
        not pd.isna(turnover)
        and turnover >= MIN_DAILY_TURNOVER
    )

    # 先排除過熱，不追急拉或爆量拉抬。
    if not pd.isna(return_5d) and return_5d > 10:
        return (
            "🔴 排除：拉高／過熱",
            "5日漲幅超過10%，不符合盤整吸籌。",
            -3,
        )

    if (
        not pd.isna(volume_ratio)
        and volume_ratio > 2.0
        and not pd.isna(return_5d)
        and return_5d > 3
    ):
        return (
            "🔴 排除：拉高／過熱",
            "爆量且股價走強，不符合盤整吸籌。",
            -3,
        )

    # 法人同步轉賣。
    if (
        trust_5d_net < 0
        and foreign_5d_net < 0
        and trust_buy_days_5 == 0
    ):
        return (
            "🔴 排除：法人轉賣",
            "投信與外資近5日同步偏賣。",
            -3,
        )

    # 跌破 MA20，但籌碼沒有完全轉壞時，列黃燈。
    if (
        not pd.isna(distance_ma20)
        and distance_ma20 < 0
        and (
            trust_accumulation
            or foreign_support
            or midterm_inflow
        )
    ):
        return (
            "🟡 籌碼尚在、價格轉弱",
            "跌破MA20月線；等待重新站回，不加碼。",
            2,
        )

    # 跌破 MA10 且投信偏賣。
    if (
        not pd.isna(distance_ma10)
        and distance_ma10 < 0
        and trust_5d_net < 0
    ):
        return (
            "🟡 籌碼鬆動、跌破MA10",
            "股價跌破MA10，且投信近5日偏賣。",
            1,
        )

    # 藍燈：仍處盤整、未爆量、距 MA20 不遠。
    price_ok = (
        (
            pd.isna(return_5d)
            or -7 <= return_5d <= 6
        )
        and (
            pd.isna(range_10d)
            or range_10d <= 16
        )
        and (
            pd.isna(volume_ratio)
            or volume_ratio <= 2.0
        )
        and (
            pd.isna(distance_ma20)
            or abs(distance_ma20) <= 7
        )
    )

    # 紫燈：中期趨勢可放入觀察名單。
    midterm_price_ok = (
        (
            pd.isna(return_5d)
            or -7 <= return_5d <= 8
        )
        and (
            pd.isna(range_10d)
            or range_10d <= 20
        )
        and (
            pd.isna(distance_ma20)
            or abs(distance_ma20) <= 8
        )
    )

    # 綠燈：投信持續買、股價已溫和站回 MA10。
    green_price_confirmation = (
        not pd.isna(distance_ma10)
        and 0 <= distance_ma10 <= 8
        and (
            pd.isna(return_5d)
            or 0 < return_5d <= 10
        )
    )

    # 綠燈優先於藍燈，避免「已轉強」仍被列為純盤整。
    if (
        liquid
        and trust_accumulation
        and green_price_confirmation
    ):
        return (
            "🟢 吸籌延續／初步確認",
            "投信持續買超後，股價溫和站上MA10。",
            9,
        )

    if (
        liquid
        and trust_accumulation
        and price_ok
    ):
        return (
            "🔵 主動資金疑似布局",
            "投信5日持續買超、價格仍盤整，未見避險主導。",
            8,
        )

    if (
        liquid
        and midterm_inflow
        and midterm_price_ok
    ):
        return (
            "🟣 中期資金流入待驗證",
            "投信中期買盤具持續性，仍需研究基本面與消息。",
            7,
        )

    if foreign_support and not trust_accumulation:
        return (
            "⚪ 待驗證：僅外資流入",
            "外資流入未獲投信確認，可能是指數或被動流量。",
            1,
        )

    return (
        "⚪ 不列入",
        "未同時符合投信吸籌與價格型態條件。",
        0,
    )


# ==========================================================
# 9. 建構雷達表
# ==========================================================

def build_radar(
    quotes,
    price_features,
    institutional_features,
):
    """合併行情、價格特徵與法人特徵。"""
    df = quotes.copy()

    for feature_df in [
        price_features,
        institutional_features,
    ]:
        if (
            feature_df is not None
            and not feature_df.empty
        ):
            df = df.merge(
                feature_df,
                on="stock_id",
                how="left",
            )

    defaults = {
        "ma10": np.nan,
        "ma20": np.nan,
        "return_5d_pct": np.nan,
        "range_10d_pct": np.nan,
        "avg_volume_5d": np.nan,
        "volume_ratio_5d": np.nan,
        "distance_ma10_pct": np.nan,
        "distance_ma20_pct": np.nan,
        "trust_buy_days_5": 0,
        "trust_5d_net": 0,
        "foreign_buy_days_5": 0,
        "foreign_5d_net": 0,
        "trust_volume_ratio": np.nan,
        "foreign_volume_ratio": np.nan,
        "hedge_volume_ratio": np.nan,
        "hedge_data_status": "unknown",
        "hedge_dominant": False,
        "trust_accumulation": False,
        "foreign_support": False,
        "trust_20d_net": 0,
        "foreign_20d_net": 0,
        "trust_buy_days_20": 0,
        "foreign_buy_days_20": 0,
        "institutional_days_20": 0,
        "midterm_inflow_to_verify": False,
    }

    for column, default_value in defaults.items():
        if column not in df.columns:
            df[column] = default_value

        elif isinstance(default_value, bool):
            df[column] = (
                df[column]
                .fillna(default_value)
                .astype(bool)
            )

        else:
            df[column] = df[column].fillna(
                default_value
            )

    df["theme"] = df["stock_id"].map(
        lambda stock_id: "／".join(
            THEME_MAP.get(stock_id, [])
        )
    )

    df["is_focus_stock"] = df["stock_id"].isin(
        FOCUS_STOCKS
    )

    classified = df.apply(
        classify_stock,
        axis=1,
        result_type="expand",
    )

    classified.columns = [
        "signal",
        "reason",
        "base_score",
    ]

    df = pd.concat([df, classified], axis=1)

    df["score"] = df["base_score"]

    # 不因核心持股加分，只依客觀法人條件排序。
    df.loc[
        df["trust_buy_days_5"] >= 4,
        "score",
    ] += 2

    df.loc[
        df["foreign_support"] == True,
        "score",
    ] += 1

    signal_order = {
        "🟢 吸籌延續／初步確認": 1,
        "🔵 主動資金疑似布局": 2,
        "🟣 中期資金流入待驗證": 3,
        "🟡 籌碼尚在、價格轉弱": 4,
        "🟡 籌碼鬆動、跌破MA10": 5,
        "⚪ 待驗證：僅外資流入": 6,
        "🔴 排除：拉高／過熱": 7,
        "🔴 排除：法人轉賣": 8,
        "🟠 排除：避險流量主導": 9,
        "⚪ 排除：權值／ETF／工具股": 10,
        "⚪ 不列入": 99,
    }

    df["sort_order"] = df["signal"].map(
        signal_order
    ).fillna(99)

    return (
        df
        .sort_values(
            [
                "sort_order",
                "score",
                "turnover",
            ],
            ascending=[
                True,
                False,
                False,
            ],
            na_position="last",
        )
        .drop(columns=["sort_order"])
    )


# ==========================================================
# 10. Email 報告格式
# ==========================================================

def fmt_price(value):
    """格式化價格。"""
    if pd.isna(value):
        return "-"

    return f"{float(value):.2f}"


def fmt_pct(value):
    """格式化百分比。"""
    if pd.isna(value):
        return "累積中"

    return f"{float(value):+.1f}%"


def fmt_ratio(value):
    """格式化比例。"""
    if pd.isna(value):
        return "待驗證"

    return f"{float(value) * 100:.2f}%"


def fmt_shares(value):
    """格式化股數為張或股。"""
    if pd.isna(value):
        return "-"

    value = int(value)

    if abs(value) >= 1000:
        return f"{value / 1000:+.1f} 張"

    return f"{value:+,} 股"


def make_focus_report(radar):
    """產生 8 檔核心持股健檢。"""
    lines = [
        "=" * 54,
        "⭐ 8大核心持股研究健檢",
        "=" * 54,
    ]

    for stock_id in FOCUS_STOCKS:
        profile = FOCUS_PROFILES.get(
            stock_id,
            {},
        )

        name = profile.get("name", stock_id)
        theme = profile.get("theme", "未分類")
        valuation = profile.get("valuation", "-")
        good_catalyst = profile.get(
            "good_catalyst",
            "-",
        )
        risk_catalyst = profile.get(
            "risk_catalyst",
            "-",
        )
        rating = profile.get("rating", "持續研究")

        match = radar[
            radar["stock_id"] == stock_id
        ]

        if match.empty:
            lines.append(
                f"• {stock_id} {name}｜未取得當日行情"
            )
            lines.append("-" * 54)
            continue

        row = match.iloc[0]

        lines.append(
            f"• {stock_id} {name}｜產業：{theme}"
        )

        lines.append(
            f"  收盤：{fmt_price(row.get('close', np.nan))}"
            f"｜月線乖離："
            f"{fmt_pct(row.get('distance_ma20_pct', np.nan))}"
        )

        lines.append(
            f"  估值研究備忘：{valuation}"
        )

        lines.append(
            f"  題材：{good_catalyst}"
        )

        lines.append(
            f"  風險：{risk_catalyst}"
        )

        lines.append(
            f"  即時雷達：{row.get('signal', '-')}"
            f"｜投信5日："
            f"{fmt_shares(row.get('trust_5d_net', np.nan))}"
        )

        lines.append(
            f"  研究標籤：{rating}"
        )

        lines.append("-" * 54)

    return "\n".join(lines)


def stock_lines(frame, maximum):
    """格式化某燈號分類的股票清單。"""
    if frame is None or frame.empty:
        return "（今日無股票）"

    lines = []

    for _, row in frame.head(maximum).iterrows():
        focus_mark = ""

        if bool(row.get("is_focus_stock", False)):
            focus_mark = " ⭐"

        if row.get("hedge_data_status") == "available":
            hedge_text = fmt_ratio(
                row.get("hedge_volume_ratio", np.nan)
            )
        else:
            hedge_text = "資料未取得"

        lines.append(
            f"{row['signal']}{focus_mark}"
            f"｜{row['stock_id']} {row['stock_name']}"
            f"｜{row['market']}"
            f"｜收盤 {fmt_price(row['close'])}"
            f"｜{row.get('theme', '')}"
        )

        lines.append(
            f"投信5日 "
            f"{fmt_shares(row.get('trust_5d_net', np.nan))}"
            f"（{int(row.get('trust_buy_days_5', 0) or 0)}日買）"
            f"｜投信20日 "
            f"{fmt_shares(row.get('trust_20d_net', np.nan))}"
            f"｜外資5日 "
            f"{fmt_shares(row.get('foreign_5d_net', np.nan))}"
        )

        lines.append(
            f"5日 {fmt_pct(row.get('return_5d_pct', np.nan))}"
            f"｜10日振幅 "
            f"{fmt_pct(row.get('range_10d_pct', np.nan))}"
            f"｜距MA20 "
            f"{fmt_pct(row.get('distance_ma20_pct', np.nan))}"
            f"｜避險相對量 {hedge_text}"
        )

        lines.append(
            f"判定：{row['reason']}"
        )

        lines.append("")

    return "\n".join(lines).rstrip()


def make_email_body(
    radar,
    date_text,
    execution_time,
):
    """建立 Email 文字內容。"""
    focus_report = make_focus_report(radar)

    purple = radar[
        radar["signal"] == "🟣 中期資金流入待驗證"
    ]

    blue = radar[
        radar["signal"] == "🔵 主動資金疑似布局"
    ]

    green = radar[
        radar["signal"] == "🟢 吸籌延續／初步確認"
    ]

    yellow = radar[
        radar["signal"].str.startswith(
            "🟡",
            na=False,
        )
    ]

    red = radar[
        radar["signal"].str.startswith(
            "🔴",
            na=False,
        )
    ]

    orange = radar[
        radar["signal"] == "🟠 排除：避險流量主導"
    ]

    lines = [
        "台股主動資金雷達與風控觀察報告",
        f"日期：{date_text}",
        f"執行時間：{execution_time}（台灣時間）",
        "",
        "策略原則：",
        "- 藍燈與綠燈必須有投信持續買超。",
        "- 外資只作輔助；僅外資流入只列待驗證。",
        "- 自營商避險流量不當作看多。",
        "- 核心關注股僅用於健檢，不會獲得人工加分。",
        "",
        focus_report,
        "",
        "=" * 54,
        f"🟢 吸籌延續／初步確認｜{len(green)} 檔",
        "=" * 54,
        stock_lines(green, 10),
        "",
        "=" * 54,
        f"🔵 主動資金疑似布局｜{len(blue)} 檔",
        "=" * 54,
        stock_lines(blue, 10),
        "",
        "=" * 54,
        f"🟣 中期資金流入待驗證｜{len(purple)} 檔",
        "=" * 54,
        stock_lines(purple, 10),
        "",
        "=" * 54,
        f"🟡 轉弱／籌碼鬆動｜{len(yellow)} 檔",
        "=" * 54,
        stock_lines(yellow, 10),
        "",
        "=" * 54,
        f"🔴 排除｜{len(red)} 檔",
        "=" * 54,
        stock_lines(red, 15),
        "",
        "=" * 54,
        f"🟠 避險主導｜{len(orange)} 檔",
        "=" * 54,
        stock_lines(orange, 10),
        "",
        "提醒：",
        "- 第5個交易日後，投信5日與均量資料才較有參考價值。",
        "- 第10個交易日後，10日振幅與中期法人趨勢較有意義。",
        "- 第20個交易日後，MA20與完整中期法人趨勢才完整。",
        "- 本報告為公開資料研究工具，不構成投資建議。",
    ]

    return "\n".join(lines)


# ==========================================================
# 11. Email 寄送
# ==========================================================

def send_email(subject, body):
    """寄送 Email；沒有 Secrets 時只略過寄信。"""
    if not all([
        GMAIL_USER,
        GMAIL_APP_PASSWORD,
        RECIPIENT_EMAIL,
    ]):
        print(
            "未設定 Gmail Secrets，略過寄信；"
            "CSV 仍會正常產生。"
        )

        return

    message = MIMEMultipart()
    message["From"] = GMAIL_USER
    message["To"] = RECIPIENT_EMAIL
    message["Subject"] = subject

    message.attach(
        MIMEText(body, "plain", "utf-8")
    )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
    ) as server:
        server.login(
            GMAIL_USER,
            GMAIL_APP_PASSWORD,
        )

        server.send_message(message)

    print("Email 寄送完成。")


# ==========================================================
# 12. 主流程
# ==========================================================

def main():
    """主程式。"""
    now = now_tw()
    date_text = now.strftime("%Y-%m-%d")

    print("=" * 60)
    print(
        f"開始執行台股主動資金雷達 v5.1：{date_text}"
    )
    print("=" * 60)

    # 1. 行情。
    quotes = get_all_quotes()

    print(
        f"成功取得行情：{len(quotes)} 檔。"
    )

    # 2. 法人資料。
    # 失敗時仍讓價格歷史與雷達 CSV 繼續產出。
    try:
        institutional_today = get_twse_institutional()

    except Exception as error:
        print(
            f"法人資料失敗，今天略過法人計算：{error}"
        )

        institutional_today = pd.DataFrame()

    # 3. 讀取既有歷史。
    price_history = load_history(
        "layout_price_history_"
    )

    institutional_history = load_history(
        "layout_institutional_history_"
    )

    # 4. 把今天法人資料加入記憶體後先計算。
    if not institutional_today.empty:
        institutional_history = pd.concat(
            [
                institutional_history,
                institutional_today,
            ],
            ignore_index=True,
        )

    # 5. 特徵計算。
    price_features = make_price_features(
        quotes,
        price_history,
        date_text,
    )

    institutional_features = make_institutional_features(
        institutional_history,
        price_features,
    )

    # 6. 建構雷達。
    radar = build_radar(
        quotes,
        price_features,
        institutional_features,
    )

    # 7. 儲存每日歷史與結果。
    save_today_history(
        quotes,
        institutional_today,
        date_text,
    )

    radar_path = os.path.join(
        OUTPUT_DIR,
        f"layout_radar_{date_text}.csv",
    )

    radar.to_csv(
        radar_path,
        index=False,
        encoding="utf-8-sig",
    )

    print(f"雷達 CSV 已輸出：{radar_path}")

    # 8. Email。
    body = make_email_body(
        radar,
        date_text,
        now.strftime("%Y-%m-%d %H:%M"),
    )

    print("\n" + "=" * 60)
    print(body)
    print("=" * 60 + "\n")

    send_email(
        f"主動資金雷達 v5.1｜{date_text}",
        body,
    )

    print("執行完成。")


if __name__ == "__main__":
    try:
        main()

    except Exception:
        error_text = traceback.format_exc()

        print("\n程式發生錯誤：")
        print(error_text)

        try:
            send_email(
                f"【錯誤】主動資金雷達 v5.1｜"
                f"{today_str()}",
                error_text,
            )

        except Exception:
            pass

        raise
