# -*- coding: utf-8 -*-
"""
台股選股機器人
A. 長線／波段順風車
B. 題材池短線動能
C. 全市場激進小型股雷達
D. 資金流出／趨勢轉弱警示

Gmail 仍沿用：
MAIL_USER / MAIL_PASSWORD / MAIL_RECEIVER
"""

import os
import time
import html
import smtplib
import logging
from pathlib import Path
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
import requests
import yfinance as yf


# ============================================================
# 1. 基本設定
# ============================================================

RUN_TIME = datetime.now()
RUN_DATE = RUN_TIME.strftime("%Y-%m-%d")
RUN_DATETIME = RUN_TIME.strftime("%Y-%m-%d %H:%M:%S")

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"

OUTPUT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

SETTINGS = {
    "request_timeout": 25,
    "smtp_timeout": 30,
    "sleep_between_yf_calls": 0.35,

    "history_period": "2y",
    "min_history_days": 261,

    "ma5": 5,
    "ma10": 10,
    "ma20": 20,
    "ma60": 60,
    "ma240": 240,
    "ma240_slope_days": 20,

    "min_close_price": 10.0,
    "min_avg_turnover_20d": 20_000_000,

    "max_deviation_ma240_pct": 35.0,
    "high_growth_pct": 20.0,
    "stable_growth_pct": 5.0,

    "peg_growth_cap_pct": 40.0,
    "peg_good": 1.0,
    "peg_warning": 2.0,

    "value_max_pe": 15.0,
    "value_max_pb": 1.5,
    "value_min_yield_pct": 3.0,

    "short_min_daily_change_pct": 2.5,
    "short_min_volume_ratio": 1.5,
    "short_max_ma20_deviation_pct": 20.0,

    "large_single_day_net_sell": -2_000_000,

    # C 區：全市場激進小型股雷達
    "radar_min_price": 10.0,
    "radar_max_price": 300.0,
    "radar_min_daily_change_pct": 5.0,
    "radar_min_volume_ratio": 3.0,
    "radar_min_today_turnover": 30_000_000,
    "radar_min_avg_turnover_20d": 10_000_000,
    "radar_close_near_high_pct": 2.0,
    "radar_max_candidates_per_market": 40,
    "radar_max_final_results": 30,

    # 大型權值股不列入「小型激進雷達」
    "radar_exclude_codes": {
        "1101", "1216", "1301", "1303", "2002", "2303",
        "2308", "2317", "2324", "2330", "2353", "2356",
        "2382", "2412", "2454", "2603", "2881", "2882",
        "2891", "3711", "4904", "6505", "6669",
    },
}


# A / B / D：題材池
UNIVERSE = {
    "2330": {"name": "台積電", "theme": "晶圓代工 / AI晶片", "ticker": "2330.TW", "market": "TWSE"},
    "2454": {"name": "聯發科", "theme": "IC設計 / 邊緣AI", "ticker": "2454.TW", "market": "TWSE"},
    "3711": {"name": "日月光投控", "theme": "封測 / AI晶片", "ticker": "3711.TW", "market": "TWSE"},
    "3443": {"name": "創意", "theme": "ASIC設計服務", "ticker": "3443.TW", "market": "TWSE"},
    "3661": {"name": "世芯-KY", "theme": "ASIC / AI晶片", "ticker": "3661.TW", "market": "TWSE"},
    "3035": {"name": "智原", "theme": "ASIC設計服務", "ticker": "3035.TW", "market": "TWSE"},
    "3529": {"name": "力旺", "theme": "矽智財", "ticker": "3529.TW", "market": "TWSE"},

    "2382": {"name": "廣達", "theme": "AI伺服器", "ticker": "2382.TW", "market": "TWSE"},
    "3231": {"name": "緯創", "theme": "AI伺服器", "ticker": "3231.TW", "market": "TWSE"},
    "2356": {"name": "英業達", "theme": "AI伺服器", "ticker": "2356.TW", "market": "TWSE"},
    "6669": {"name": "緯穎", "theme": "雲端 / AI伺服器", "ticker": "6669.TW", "market": "TWSE"},
    "8210": {"name": "勤誠", "theme": "伺服器機殼", "ticker": "8210.TW", "market": "TWSE"},

    "2383": {"name": "台光電", "theme": "CCL / AI伺服器材料", "ticker": "2383.TW", "market": "TWSE"},
    "6274": {"name": "台燿", "theme": "CCL / 高速材料", "ticker": "6274.TW", "market": "TWSE"},
    "2368": {"name": "金像電", "theme": "PCB / AI伺服器", "ticker": "2368.TW", "market": "TWSE"},
    "3037": {"name": "欣興", "theme": "ABF載板 / PCB", "ticker": "3037.TW", "market": "TWSE"},
    "3189": {"name": "景碩", "theme": "ABF載板", "ticker": "3189.TW", "market": "TWSE"},
    "8046": {"name": "南電", "theme": "ABF載板", "ticker": "8046.TW", "market": "TWSE"},

    "3017": {"name": "奇鋐", "theme": "散熱 / AI伺服器", "ticker": "3017.TW", "market": "TWSE"},
    "3324": {"name": "雙鴻", "theme": "散熱 / AI伺服器", "ticker": "3324.TW", "market": "TWSE"},
    "3653": {"name": "健策", "theme": "散熱 / 均熱片", "ticker": "3653.TW", "market": "TWSE"},
    "2421": {"name": "建準", "theme": "風扇 / 散熱", "ticker": "2421.TW", "market": "TWSE"},
    "2308": {"name": "台達電", "theme": "電源 / 資料中心", "ticker": "2308.TW", "market": "TWSE"},
    "6409": {"name": "旭隼", "theme": "UPS / 電源", "ticker": "6409.TW", "market": "TWSE"},

    "2345": {"name": "智邦", "theme": "網通 / 資料中心", "ticker": "2345.TW", "market": "TWSE"},
    "4979": {"name": "華星光", "theme": "光通訊", "ticker": "4979.TW", "market": "TWSE"},
    "3363": {"name": "上詮", "theme": "光通訊 / CPO", "ticker": "3363.TWO", "market": "TPEX"},
    "3081": {"name": "聯亞", "theme": "光通訊", "ticker": "3081.TWO", "market": "TPEX"},
    "6442": {"name": "光聖", "theme": "光通訊", "ticker": "6442.TW", "market": "TWSE"},
    "5388": {"name": "中磊", "theme": "網通", "ticker": "5388.TW", "market": "TWSE"},
    "3596": {"name": "智易", "theme": "網通", "ticker": "3596.TW", "market": "TWSE"},

    "1560": {"name": "中砂", "theme": "半導體耗材", "ticker": "1560.TW", "market": "TWSE"},
    "3583": {"name": "辛耘", "theme": "半導體設備", "ticker": "3583.TW", "market": "TWSE"},
    "6187": {"name": "萬潤", "theme": "半導體設備", "ticker": "6187.TW", "market": "TWSE"},
    "3131": {"name": "弘塑", "theme": "半導體設備", "ticker": "3131.TW", "market": "TWSE"},
    "3680": {"name": "家登", "theme": "半導體設備 / EUV", "ticker": "3680.TW", "market": "TWSE"},
    "2344": {"name": "華邦電", "theme": "記憶體", "ticker": "2344.TW", "market": "TWSE"},
    "2408": {"name": "南亞科", "theme": "DRAM", "ticker": "2408.TW", "market": "TWSE"},
    "8299": {"name": "群聯", "theme": "NAND控制IC", "ticker": "8299.TWO", "market": "TPEX"},

    "2881": {"name": "富邦金", "theme": "金融 / 資產價值", "ticker": "2881.TW", "market": "TWSE"},
    "2882": {"name": "國泰金", "theme": "金融 / 資產價值", "ticker": "2882.TW", "market": "TWSE"},
    "2002": {"name": "中鋼", "theme": "鋼鐵 / 景氣循環", "ticker": "2002.TW", "market": "TWSE"},
}


# ============================================================
# 2. Log 與工具
# ============================================================

logging.basicConfig(
    filename=LOG_DIR / f"taiwan_stock_report_{RUN_DATE}.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(console_handler)


def safe_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def clean_number(value):
    if value is None:
        return 0

    text = str(value).strip()

    if text in {"", "--", "---", "nan", "None"}:
        return 0

    text = text.replace(",", "").replace("+", "")

    try:
        return int(float(text))
    except ValueError:
        return 0


def to_pct(value):
    value = safe_float(value)

    if value is None:
        return None

    return value * 100 if abs(value) <= 5 else value


def format_price(value):
    return "--" if value is None or pd.isna(value) else f"{value:,.2f}"


def format_int(value):
    return "--" if value is None or pd.isna(value) else f"{int(value):,}"


def format_pct(value):
    return "--" if value is None or pd.isna(value) else f"{value:+.2f}%"


def find_column(columns, keywords):
    for column in columns:
        normalized = "".join(str(column).split())

        if all(keyword in normalized for keyword in keywords):
            return column

    return None


def ticker_from_code(code, market):
    suffix = ".TW" if market == "TWSE" else ".TWO"
    return f"{str(code).zfill(4)}{suffix}"


# ============================================================
# 3. 基本面與技術判斷
# ============================================================

def classify_stock(data):
    eps = data.get("trailing_eps")
    pe = data.get("trailing_pe")
    pb = data.get("pb_ratio")
    dividend_yield = data.get("dividend_yield_pct")
    revenue_growth = data.get("revenue_growth_pct")
    earnings_growth = data.get("earnings_growth_pct")

    positive_eps = eps is not None and eps > 0

    if positive_eps and (
        (revenue_growth is not None and revenue_growth >= SETTINGS["high_growth_pct"])
        or (earnings_growth is not None and earnings_growth >= SETTINGS["high_growth_pct"])
    ):
        return "高成長"

    if (
        positive_eps
        and earnings_growth is not None
        and earnings_growth >= SETTINGS["stable_growth_pct"]
    ):
        return "穩健成長"

    if (
        pe is not None
        and pe > 0
        and (
            pe <= SETTINGS["value_max_pe"]
            or (pb is not None and pb > 0 and pb <= SETTINGS["value_max_pb"])
            or (
                dividend_yield is not None
                and dividend_yield >= SETTINGS["value_min_yield_pct"]
            )
        )
    ):
        return "資產價值"

    return "其他"


def calculate_conservative_peg(data):
    pe = data.get("trailing_pe")
    growth = data.get("earnings_growth_pct")

    if pe is None or pe <= 0 or growth is None or growth <= 0:
        return None

    return round(
        pe / min(growth, SETTINGS["peg_growth_cap_pct"]),
        2,
    )


def make_long_term_signal(data):
    if not data["long_trend_ok"]:
        return "趨勢未達標"

    if (
        data["growth_type"] == "高成長"
        and data["conservative_peg"] is not None
        and data["conservative_peg"] <= SETTINGS["peg_warning"]
    ):
        return "高成長順風車候選"

    if data["growth_type"] == "穩健成長":
        return "穩健成長候選"

    if data["growth_type"] == "資產價值":
        return "價值股觀察"

    return "趨勢健康，基本面待確認"


def make_short_term_signal(data):
    if not data["short_ma_ok"]:
        return "短線趨勢弱"

    if data["daily_change_pct"] <= 0:
        return "短線正常，未發動"

    if (
        data["volume_ratio_20d"] is None
        or data["volume_ratio_20d"] < SETTINGS["short_min_volume_ratio"]
    ):
        return "短線上漲但量能不足"

    if (
        data["breakout_20d"]
        and data["daily_change_pct"] >= SETTINGS["short_min_daily_change_pct"]
        and data["ma20_deviation_pct"] <= SETTINGS["short_max_ma20_deviation_pct"]
    ):
        return "爆量突破，短線觀察"

    if data["daily_change_pct"] >= SETTINGS["short_min_daily_change_pct"]:
        return "量價轉強，短線觀察"

    return "短線偏強，等待突破"


def build_technical_snapshot(ticker, code, name, market, theme, with_fundamentals=False):
    try:
        stock = yf.Ticker(ticker)

        hist = stock.history(
            period=SETTINGS["history_period"],
            interval="1d",
            auto_adjust=False,
            actions=False,
        ).dropna(subset=["Close"]).copy()

        minimum_days = max(
            SETTINGS["min_history_days"],
            SETTINGS["ma240"] + SETTINGS["ma240_slope_days"] + 1,
        )

        if len(hist) < minimum_days:
            return None, f"{code} {name}：日K資料不足。"

        hist["MA5"] = hist["Close"].rolling(SETTINGS["ma5"]).mean()
        hist["MA10"] = hist["Close"].rolling(SETTINGS["ma10"]).mean()
        hist["MA20"] = hist["Close"].rolling(SETTINGS["ma20"]).mean()
        hist["MA60"] = hist["Close"].rolling(SETTINGS["ma60"]).mean()
        hist["MA240"] = hist["Close"].rolling(SETTINGS["ma240"]).mean()

        latest = hist.iloc[-1]
        previous = hist.iloc[-2]

        close = safe_float(latest["Close"])
        high = safe_float(latest["High"])
        low = safe_float(latest["Low"])
        prev_close = safe_float(previous["Close"])

        ma5 = safe_float(latest["MA5"])
        ma10 = safe_float(latest["MA10"])
        ma20 = safe_float(latest["MA20"])
        ma60 = safe_float(latest["MA60"])
        ma240 = safe_float(latest["MA240"])

        ma240_past = safe_float(
            hist["MA240"].iloc[-(SETTINGS["ma240_slope_days"] + 1)]
        )

        if any(value is None for value in [
            close, high, low, prev_close, ma5, ma10,
            ma20, ma60, ma240, ma240_past,
        ]):
            return None, f"{code} {name}：均線資料不完整。"

        volume = safe_float(latest["Volume"])
        avg_volume_20d = safe_float(hist["Volume"].tail(20).mean())
        avg_turnover_20d = safe_float(
            (hist["Close"] * hist["Volume"]).tail(20).mean()
        )

        if volume is None or avg_volume_20d is None or avg_volume_20d <= 0:
            return None, f"{code} {name}：成交量資料不完整。"

        daily_change_pct = (close / prev_close - 1) * 100
        ma240_slope_pct = (ma240 / ma240_past - 1) * 100
        deviation_ma240_pct = (close / ma240 - 1) * 100
        ma20_deviation_pct = (close / ma20 - 1) * 100
        volume_ratio_20d = volume / avg_volume_20d

        previous_20d_high = safe_float(hist["Close"].iloc[-21:-1].max())
        breakout_20d = (
            previous_20d_high is not None
            and close > previous_20d_high
        )

        data = {
            "code": str(code).zfill(4),
            "name": name,
            "theme": theme,
            "ticker": ticker,
            "market": market,
            "price_date": hist.index[-1].strftime("%Y-%m-%d"),

            "close": round(close, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "daily_change_pct": round(daily_change_pct, 2),

            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "ma240": round(ma240, 2),

            "ma240_slope_pct": round(ma240_slope_pct, 2),
            "deviation_ma240_pct": round(deviation_ma240_pct, 2),
            "ma20_deviation_pct": round(ma20_deviation_pct, 2),

            "volume": int(volume),
            "avg_volume_20d": int(avg_volume_20d),
            "volume_ratio_20d": round(volume_ratio_20d, 2),
            "today_turnover": round(close * volume, 0),
            "avg_turnover_20d": round(avg_turnover_20d, 0) if avg_turnover_20d else None,

            "previous_20d_high": round(previous_20d_high, 2) if previous_20d_high else None,
            "breakout_20d": breakout_20d,
            "close_below_high_pct": round((high / close - 1) * 100, 2),

            "trailing_eps": None,
            "trailing_pe": None,
            "forward_pe": None,
            "pb_ratio": None,
            "dividend_yield_pct": None,
            "revenue_growth_pct": None,
            "earnings_growth_pct": None,
        }

        data["price_above_ma5"] = close > ma5
        data["price_above_ma10"] = close > ma10
        data["price_above_ma20"] = close > ma20
        data["price_above_ma60"] = close > ma60
        data["price_above_ma240"] = close > ma240
        data["ma240_rising"] = ma240 > ma240_past

        data["short_ma_ok"] = (
            data["price_above_ma5"]
            and data["price_above_ma10"]
            and data["price_above_ma20"]
        )

        data["long_trend_ok"] = (
            data["price_above_ma60"]
            and data["price_above_ma240"]
            and data["ma240_rising"]
            and data["deviation_ma240_pct"] <= SETTINGS["max_deviation_ma240_pct"]
        )

        if with_fundamentals:
            try:
                info = stock.info
            except Exception as e:
                logging.warning("%s %s 基本面讀取失敗：%s", code, name, e)
                info = {}

            data["trailing_eps"] = safe_float(info.get("trailingEps"))
            data["trailing_pe"] = safe_float(info.get("trailingPE"))
            data["forward_pe"] = safe_float(info.get("forwardPE"))
            data["pb_ratio"] = safe_float(info.get("priceToBook"))
            data["dividend_yield_pct"] = to_pct(info.get("dividendYield"))
            data["revenue_growth_pct"] = to_pct(info.get("revenueGrowth"))
            data["earnings_growth_pct"] = to_pct(info.get("earningsGrowth"))

        data["growth_type"] = classify_stock(data)
        data["conservative_peg"] = calculate_conservative_peg(data)
        data["long_term_signal"] = make_long_term_signal(data)
        data["short_term_signal"] = make_short_term_signal(data)

        return data, None

    except Exception as e:
        return None, f"{code} {name}：{type(e).__name__}: {e}"


# ============================================================
# 4. 題材池掃描
# ============================================================

def scan_theme_universe():
    results = []
    warnings = []

    for code, meta in UNIVERSE.items():
        result, warning = build_technical_snapshot(
            ticker=meta["ticker"],
            code=code,
            name=meta["name"],
            market=meta["market"],
            theme=meta["theme"],
            with_fundamentals=True,
        )

        if result is not None:
            if (
                result["close"] >= SETTINGS["min_close_price"]
                and result["avg_turnover_20d"] is not None
                and result["avg_turnover_20d"] >= SETTINGS["min_avg_turnover_20d"]
            ):
                results.append(result)

        if warning:
            warnings.append(warning)

        time.sleep(SETTINGS["sleep_between_yf_calls"])

    return results, warnings


# ============================================================
# 5. 全市場激進小型股雷達
# ============================================================

def fetch_twse_market_snapshot():
    """
    TWSE 最新全市場收盤資料。
    確認欄位使用英文：
    Code, Name, TradeVolume, TradeValue,
    OpeningPrice, HighestPrice, LowestPrice, ClosingPrice
    """
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"

    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=SETTINGS["request_timeout"],
    )
    response.raise_for_status()

    raw = response.json()

    if not isinstance(raw, list):
        raise RuntimeError("TWSE 全市場資料格式不是 list。")

    df = pd.DataFrame(raw)

    required_columns = [
        "Code",
        "Name",
        "TradeVolume",
        "TradeValue",
        "OpeningPrice",
        "HighestPrice",
        "LowestPrice",
        "ClosingPrice",
    ]

    missing = [col for col in required_columns if col not in df.columns]

    if missing:
        raise KeyError(
            f"TWSE 全市場欄位缺少：{missing}；"
            f"欄位：{df.columns.tolist()}"
        )

    out = pd.DataFrame()
    out["code"] = df["Code"].astype(str).str.strip()
    out["name"] = df["Name"].astype(str).str.strip()
    out["open"] = df["OpeningPrice"].apply(safe_float)
    out["high"] = df["HighestPrice"].apply(safe_float)
    out["low"] = df["LowestPrice"].apply(safe_float)
    out["close"] = df["ClosingPrice"].apply(safe_float)
    out["volume"] = df["TradeVolume"].apply(clean_number)
    out["today_turnover"] = df["TradeValue"].apply(clean_number)
    out["daily_change_pct"] = None
    out["market"] = "TWSE"
    out["ticker"] = out["code"].apply(lambda x: ticker_from_code(x, "TWSE"))

    return out


def fetch_tpex_market_snapshot():
    """
    TPEx 最新上櫃主板收盤資料。
    欄位採動態辨識，若官方更名會拋出清楚錯誤。
    """
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=SETTINGS["request_timeout"],
    )
    response.raise_for_status()

    raw = response.json()

    if not isinstance(raw, list):
        raise RuntimeError("TPEx 全市場資料格式不是 list。")

    df = pd.DataFrame(raw)

    if df.empty:
        raise RuntimeError("TPEx 全市場資料為空。")

    possible_columns = {
        "code": ["SecuritiesCompanyCode", "Code", "證券代號"],
        "name": ["CompanyName", "SecuritiesCompanyName", "Name", "證券名稱"],
        "open": ["Open", "OpeningPrice", "開盤"],
        "high": ["High", "HighestPrice", "最高"],
        "low": ["Low", "LowestPrice", "最低"],
        "close": ["Close", "ClosingPrice", "收盤"],
        "volume": ["TradingShares", "TradeVolume", "成交股數"],
        "turnover": ["TradingMoney", "TradeValue", "成交金額"],
    }

    resolved = {}

    for key, options in possible_columns.items():
        resolved[key] = next(
            (option for option in options if option in df.columns),
            None,
        )

    missing = [
        key
        for key in ["code", "name", "close", "volume"]
        if resolved[key] is None
    ]

    if missing:
        raise KeyError(
            f"TPEx 欄位缺少：{missing}；"
            f"欄位：{df.columns.tolist()}"
        )

    out = pd.DataFrame()
    out["code"] = df[resolved["code"]].astype(str).str.strip()
    out["name"] = df[resolved["name"]].astype(str).str.strip()
    out["close"] = df[resolved["close"]].apply(safe_float)
    out["volume"] = df[resolved["volume"]].apply(clean_number)

    out["open"] = (
        df[resolved["open"]].apply(safe_float)
        if resolved["open"] else None
    )

    out["high"] = (
        df[resolved["high"]].apply(safe_float)
        if resolved["high"] else None
    )

    out["low"] = (
        df[resolved["low"]].apply(safe_float)
        if resolved["low"] else None
    )

    out["today_turnover"] = (
        df[resolved["turnover"]].apply(clean_number)
        if resolved["turnover"] else None
    )

    out["daily_change_pct"] = None
    out["market"] = "TPEX"
    out["ticker"] = out["code"].apply(lambda x: ticker_from_code(x, "TPEX"))

    return out


def choose_market_radar_candidates(snapshot_df):
    if snapshot_df.empty:
        return snapshot_df

    df = snapshot_df.copy()

    df = df[df["code"].str.match(r"^\d{4}$", na=False)].copy()
    df = df[~df["code"].isin(SETTINGS["radar_exclude_codes"])].copy()

    df = df[
        (df["close"] >= SETTINGS["radar_min_price"])
        & (df["close"] <= SETTINGS["radar_max_price"])
    ].copy()

    if "today_turnover" in df.columns:
        df = df[
            df["today_turnover"].fillna(0)
            >= SETTINGS["radar_min_today_turnover"]
        ].copy()

    if df.empty:
        return df

    frames = []

    for market, group in df.groupby("market"):
        frames.append(
            group.sort_values(
                "today_turnover",
                ascending=False,
                na_position="last",
            ).head(SETTINGS["radar_max_candidates_per_market"])
        )

    return pd.concat(frames, ignore_index=True)


def run_full_market_aggressive_radar():
    warnings = []
    snapshots = []

    try:
        twse_df = fetch_twse_market_snapshot()
        snapshots.append(twse_df)
        logging.info("TWSE 全市場快照：%s 檔", len(twse_df))
    except Exception as e:
        warnings.append(f"TWSE 全市場雷達資料失敗：{type(e).__name__}: {e}")

    try:
        tpex_df = fetch_tpex_market_snapshot()
        snapshots.append(tpex_df)
        logging.info("TPEx 全市場快照：%s 檔", len(tpex_df))
    except Exception as e:
        warnings.append(f"TPEx 全市場雷達資料失敗：{type(e).__name__}: {e}")

    if not snapshots:
        return pd.DataFrame(), warnings

    snapshot_df = pd.concat(snapshots, ignore_index=True)
    candidate_df = choose_market_radar_candidates(snapshot_df)

    results = []

    for _, row in candidate_df.iterrows():
        result, warning = build_technical_snapshot(
            ticker=row["ticker"],
            code=row["code"],
            name=row["name"],
            market=row["market"],
            theme="全市場激進雷達",
            with_fundamentals=False,
        )

        if warning:
            warnings.append(warning)

        if result is not None:
            is_hit = (
                result["close"] >= SETTINGS["radar_min_price"]
                and result["close"] <= SETTINGS["radar_max_price"]
                and result["daily_change_pct"] >= SETTINGS["radar_min_daily_change_pct"]
                and result["volume_ratio_20d"] >= SETTINGS["radar_min_volume_ratio"]
                and result["avg_turnover_20d"] is not None
                and result["avg_turnover_20d"] >= SETTINGS["radar_min_avg_turnover_20d"]
                and result["breakout_20d"]
                and result["short_ma_ok"]
                and result["close_below_high_pct"] <= SETTINGS["radar_close_near_high_pct"]
            )

            if is_hit:
                result["radar_signal"] = "激進雷達：爆量突破"
                results.append(result)

        time.sleep(SETTINGS["sleep_between_yf_calls"])

    radar_df = pd.DataFrame(results)

    if radar_df.empty:
        return radar_df, warnings

    return radar_df.sort_values(
        by=["daily_change_pct", "volume_ratio_20d", "today_turnover"],
        ascending=[False, False, False],
    ).head(SETTINGS["radar_max_final_results"]).reset_index(drop=True), warnings


# ============================================================
# 6. TWSE 三大法人
# ============================================================

def get_twse_institutional_data():
    url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json"

    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=SETTINGS["request_timeout"],
    )
    response.raise_for_status()

    raw = response.json()

    if raw.get("stat") != "OK":
        raise RuntimeError(f"TWSE 三大法人回傳異常：{raw.get('stat')}")

    df = pd.DataFrame(raw["data"], columns=raw["fields"])

    code_col = find_column(df.columns, ["證券代號"])
    name_col = find_column(df.columns, ["證券名稱"])
    net_col = find_column(df.columns, ["三大法人", "買賣超股數"])
    foreign_col = find_column(df.columns, ["外資", "買賣超股數"])
    trust_col = find_column(df.columns, ["投信", "買賣超股數"])
    dealer_col = find_column(df.columns, ["自營商", "買賣超股數"])

    if not code_col or not name_col or not net_col:
        raise KeyError(f"TWSE 法人欄位不完整：{df.columns.tolist()}")

    out = pd.DataFrame()
    out["code"] = df[code_col].astype(str).str.strip()
    out["chip_name"] = df[name_col].astype(str).str.strip()
    out["institutional_net"] = df[net_col].apply(clean_number)
    out["foreign_net"] = df[foreign_col].apply(clean_number) if foreign_col else None
    out["trust_net"] = df[trust_col].apply(clean_number) if trust_col else None
    out["dealer_net"] = df[dealer_col].apply(clean_number) if dealer_col else None

    return out


def merge_institutional_data(df):
    if df.empty:
        return df, []

    result = df.copy()
    warnings = []

    for column in ["foreign_net", "trust_net", "dealer_net", "institutional_net"]:
        result[column] = None

    result["chip_note"] = ""

    try:
        chips = get_twse_institutional_data()
        twse_mask = result["market"] == "TWSE"

        merged = result.loc[twse_mask].merge(
            chips,
            on="code",
            how="left",
            suffixes=("", "_api"),
        )

        for column in ["foreign_net", "trust_net", "dealer_net", "institutional_net"]:
            result.loc[twse_mask, column] = merged[column].values

        result.loc[twse_mask, "chip_note"] = "TWSE 最新可取得法人資料"
        missing_mask = twse_mask & result["institutional_net"].isna()
        result.loc[missing_mask, "chip_note"] = "TWSE 法人資料未找到"

    except Exception as e:
        warnings.append(f"TWSE 法人資料失敗：{type(e).__name__}: {e}")
        result.loc[result["market"] == "TWSE", "chip_note"] = "TWSE 法人資料抓取失敗"

    result.loc[
        result["market"] == "TPEX",
        "chip_note",
    ] = "上櫃股：尚未串接 TPEx 法人資料"

    return result, warnings


# ============================================================
# 7. 風險、歷史、排序、CSV
# ============================================================

def make_exit_warning(row):
    warnings = []

    if row["close"] < row["ma240"]:
        warnings.append("跌破MA240：長線轉弱")
    elif row["close"] < row["ma60"]:
        warnings.append("跌破MA60：波段轉弱")

    if row["close"] < row["ma10"] and row["daily_change_pct"] < 0:
        warnings.append("跌破MA10：短線注意")

    net = row.get("institutional_net")

    if net is not None and pd.notna(net):
        if net <= SETTINGS["large_single_day_net_sell"]:
            warnings.append(f"法人單日明顯賣超{format_int(net)}股")

    peg = row.get("conservative_peg")

    if peg is not None and pd.notna(peg) and peg > SETTINGS["peg_warning"]:
        warnings.append(f"PEG {peg:.2f}偏高")

    if row["deviation_ma240_pct"] > SETTINGS["max_deviation_ma240_pct"]:
        warnings.append("年線乖離過大")

    return "；".join(warnings) if warnings else "目前無即時警示"


def load_history():
    files = sorted(
        OUTPUT_DIR.glob("daily_theme_candidates_*.csv"),
        reverse=True,
    )

    frames = []

    for file_path in files[:10]:
        try:
            df = pd.read_csv(file_path, dtype={"code": str})

            if {"code", "institutional_net", "run_date"}.issubset(df.columns):
                frames.append(df)

        except Exception as e:
            logging.warning("讀取歷史CSV失敗 %s：%s", file_path.name, e)

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def add_institutional_history_signals(df):
    result = df.copy()
    result["institutional_3d_net"] = None
    result["institutional_5d_net"] = None
    result["institutional_sell_streak"] = None

    history = load_history()

    if history.empty:
        return result

    current = result.copy()
    current["run_date"] = RUN_DATE

    combined = pd.concat([history, current], ignore_index=True, sort=False)
    combined["run_date"] = pd.to_datetime(combined["run_date"], errors="coerce")
    combined = combined.sort_values(["code", "run_date"])

    metrics = []

    for code, group in combined.groupby("code"):
        group = group.dropna(subset=["institutional_net"]).tail(5)

        if group.empty:
            continue

        values = group["institutional_net"].tolist()
        streak = 0

        for value in reversed(values):
            if value < 0:
                streak += 1
            else:
                break

        metrics.append({
            "code": code,
            "institutional_3d_net": sum(values[-3:]) if len(values) >= 3 else None,
            "institutional_5d_net": sum(values[-5:]) if len(values) >= 5 else None,
            "institutional_sell_streak": streak,
        })

    if not metrics:
        return result

    return result.drop(
        columns=[
            "institutional_3d_net",
            "institutional_5d_net",
            "institutional_sell_streak",
        ],
        errors="ignore",
    ).merge(pd.DataFrame(metrics), on="code", how="left")


def add_history_warning(row):
    warning = row["exit_warning"]
    additions = []

    streak = row.get("institutional_sell_streak")
    net_5d = row.get("institutional_5d_net")

    if streak is not None and pd.notna(streak) and streak >= 3:
        additions.append(f"法人連續{int(streak)}日賣超")

    if net_5d is not None and pd.notna(net_5d) and net_5d < 0:
        additions.append(f"法人5日累計賣超{format_int(net_5d)}股")

    if not additions:
        return warning

    return (
        "；".join(additions)
        if warning == "目前無即時警示"
        else warning + "；" + "；".join(additions)
    )


def sort_theme_results(df):
    if df.empty:
        return df

    result = df.copy()
    ranks = {"高成長": 1, "穩健成長": 2, "資產價值": 3, "其他": 4}

    result["category_rank"] = result["growth_type"].map(ranks).fillna(9)

    return result.sort_values(
        by=[
            "long_trend_ok",
            "category_rank",
            "conservative_peg",
            "institutional_net",
            "ma240_slope_pct",
        ],
        ascending=[False, True, True, False, False],
        na_position="last",
    ).reset_index(drop=True)


def save_csv(theme_df, radar_df):
    theme_path = None
    radar_path = None

    if not theme_df.empty:
        output = theme_df.copy()
        output["run_date"] = RUN_DATE
        output["run_datetime"] = RUN_DATETIME

        theme_path = OUTPUT_DIR / f"daily_theme_candidates_{RUN_DATE}.csv"
        output.to_csv(theme_path, index=False, encoding="utf-8-sig")

    if not radar_df.empty:
        output = radar_df.copy()
        output["run_date"] = RUN_DATE
        output["run_datetime"] = RUN_DATETIME

        radar_path = OUTPUT_DIR / f"daily_aggressive_radar_{RUN_DATE}.csv"
        output.to_csv(radar_path, index=False, encoding="utf-8-sig")

    return theme_path, radar_path


# ============================================================
# 8. Email 報表
# ============================================================

def make_html_table(df, mode):
    if df.empty:
        return "<p>今天沒有符合條件的股票。</p>"

    rows = []

    for _, row in df.iterrows():
        net = row.get("institutional_net")

        if net is None or pd.isna(net):
            net_html = "--"
        else:
            color = "#008000" if net > 0 else "#c00000" if net < 0 else "#333"
            net_html = f"<span style='color:{color};font-weight:700;'>{format_int(net)}</span>"

        warning = row.get("exit_warning", "短線雷達，請以昨日低點與MA10控風險")
        warning_color = "#008000" if warning == "目前無即時警示" else "#c00000"

        if mode == "long":
            signal = row["long_term_signal"]
            headers = """
            <th>代號</th><th>名稱</th><th>題材</th><th>長線狀態</th>
            <th>收盤</th><th>分類</th><th>PE</th><th>PEG</th>
            <th>MA240斜率</th><th>三大法人</th><th>風險提醒</th>
            """
            extra = (
                f"<td>{html.escape(str(row['growth_type']))}</td>"
                f"<td>{format_price(row['trailing_pe'])}</td>"
                f"<td>{format_price(row['conservative_peg'])}</td>"
                f"<td>{format_pct(row['ma240_slope_pct'])}</td>"
            )
            label = row["theme"]

        elif mode == "short":
            signal = row["short_term_signal"]
            headers = """
            <th>代號</th><th>名稱</th><th>題材</th><th>短線狀態</th>
            <th>收盤</th><th>當日漲跌</th><th>量比</th><th>突破20日高</th>
            <th>MA10</th><th>三大法人</th><th>風險提醒</th>
            """
            extra = (
                f"<td>{format_pct(row['daily_change_pct'])}</td>"
                f"<td>{format_price(row['volume_ratio_20d'])}</td>"
                f"<td>{'是' if row['breakout_20d'] else '否'}</td>"
                f"<td>{format_price(row['ma10'])}</td>"
            )
            label = row["theme"]

        else:
            signal = row.get("radar_signal", "激進雷達：爆量突破")
            headers = """
            <th>代號</th><th>名稱</th><th>市場</th><th>雷達狀態</th>
            <th>收盤</th><th>當日漲跌</th><th>量比</th><th>成交額</th>
            <th>距日高</th><th>三大法人</th><th>風險提醒</th>
            """
            extra = (
                f"<td>{format_pct(row['daily_change_pct'])}</td>"
                f"<td>{format_price(row['volume_ratio_20d'])}</td>"
                f"<td>{format_int(row['today_turnover'])}</td>"
                f"<td>{format_pct(row['close_below_high_pct'])}</td>"
            )
            label = row["market"]

        rows.append(
            "<tr>"
            f"<td>{html.escape(str(row['code']))}</td>"
            f"<td>{html.escape(str(row['name']))}</td>"
            f"<td>{html.escape(str(label))}</td>"
            f"<td>{html.escape(str(signal))}</td>"
            f"<td>{format_price(row['close'])}</td>"
            f"{extra}"
            f"<td>{net_html}</td>"
            f"<td style='color:{warning_color};'>{html.escape(str(warning))}</td>"
            "</tr>"
        )

    return f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def build_html_report(theme_df, radar_df, warnings):
    if theme_df.empty:
        long_html = "<p>題材池沒有可用資料。</p>"
        short_html = "<p>題材池沒有可用資料。</p>"
        risk_html = "<p>題材池沒有可用資料。</p>"
    else:
        long_df = theme_df[theme_df["long_trend_ok"] == True]
        short_df = theme_df[
            theme_df["short_term_signal"].isin([
                "爆量突破，短線觀察",
                "量價轉強，短線觀察",
            ])
        ]
        risk_df = theme_df[
            theme_df["exit_warning"] != "目前無即時警示"
        ]

        long_html = make_html_table(long_df.head(40), "long")
        short_html = make_html_table(short_df.head(30), "short")
        risk_html = make_html_table(risk_df.head(40), "long")

    radar_html = make_html_table(radar_df.head(30), "radar")

    warning_html = ""

    if warnings:
        items = "".join(
            f"<li>{html.escape(str(item))}</li>"
            for item in warnings[:20]
        )
        warning_html = f"<h2>資料警示</h2><ul>{items}</ul>"

    return f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{
                font-family: Arial, "Microsoft JhengHei", sans-serif;
                color: #222;
                padding: 12px;
                line-height: 1.5;
            }}
            h1 {{ color: #1f4e78; font-size: 22px; }}
            h2 {{ color: #1f4e78; font-size: 18px; margin-top: 28px; }}
            table {{
                border-collapse: collapse;
                width: 100%;
                font-size: 12px;
                margin: 10px 0 20px 0;
            }}
            th {{
                background: #1f4e78;
                color: white;
                padding: 7px;
                border: 1px solid #ddd;
                white-space: nowrap;
            }}
            td {{
                padding: 6px;
                border: 1px solid #ddd;
                text-align: right;
                white-space: nowrap;
            }}
            td:nth-child(1), td:nth-child(2), td:nth-child(3),
            td:nth-child(4), td:last-child {{
                text-align: left;
            }}
            tr:nth-child(even) {{ background: #f7f9fc; }}
            .notice {{
                background: #fff8e1;
                border-left: 4px solid #ffb300;
                padding: 10px;
                margin: 14px 0;
            }}
            .danger {{
                background: #fff1f0;
                border-left: 4px solid #c00000;
                padding: 10px;
                margin: 14px 0;
            }}
        </style>
    </head>
    <body>
        <h1>📊 台股長線、短線與全市場資金雷達</h1>

        <p>
            <b>執行時間：</b>{html.escape(RUN_DATETIME)}<br>
            <b>題材池：</b>{len(UNIVERSE)} 檔
        </p>

        <div class="notice">
            A 看長線／波段；B 看題材池短線；
            C 看全市場突然爆量突破的非權值股；
            D 看風險警示。<br>
            上櫃股票法人欄空白，代表尚未串接 TPEx 法人資料，不是 0。
        </div>

        <h2>A. 長線／波段順風車候選</h2>
        {long_html}

        <h2>B. 題材池短線資金動能候選</h2>
        {short_html}

        <h2>C. 全市場激進小型股雷達</h2>
        {radar_html}

        <div class="danger">
            C 區是異常強勢雷達，不是直接買進清單。<br>
            隔日若跌破前一天低點、跌破 MA10、開高走低或爆量收黑，
            短線不宜硬凹。
        </div>

        <h2>D. 資金流出／趨勢轉弱警示</h2>
        {risk_html}

        {warning_html}

        <div class="notice">
            PEG = PE / min(盈餘成長率%, {SETTINGS['peg_growth_cap_pct']:.0f})。<br>
            PEG &lt; 1 不等於必買；PEG &gt; 2 不等於立刻賣。
        </div>

        <p>
            本報告僅供研究與追蹤，非投資建議。
            下單前請以券商 App、公開資訊觀測站及公司公告確認。
        </p>
    </body>
    </html>
    """


def build_text_report(theme_df, radar_df):
    lines = [
        "台股長線、短線與全市場資金雷達",
        "=" * 56,
        f"執行時間：{RUN_DATETIME}",
        "",
    ]

    if theme_df.empty:
        lines.append("題材池沒有可用資料。")
    else:
        long_df = theme_df[theme_df["long_trend_ok"] == True]
        short_df = theme_df[
            theme_df["short_term_signal"].isin([
                "爆量突破，短線觀察",
                "量價轉強，短線觀察",
            ])
        ]
        risk_df = theme_df[
            theme_df["exit_warning"] != "目前無即時警示"
        ]

        lines.append(f"【A. 長線／波段候選】{len(long_df)} 檔")

        for _, row in long_df.head(20).iterrows():
            lines.append(
                f"{row['code']} {row['name']} | {row['long_term_signal']} | "
                f"收盤 {format_price(row['close'])} | "
                f"PEG {format_price(row['conservative_peg'])}"
            )

        lines.append("")
        lines.append(f"【B. 題材池短線動能】{len(short_df)} 檔")

        for _, row in short_df.head(20).iterrows():
            lines.append(
                f"{row['code']} {row['name']} | {row['short_term_signal']} | "
                f"漲跌 {format_pct(row['daily_change_pct'])} | "
                f"量比 {format_price(row['volume_ratio_20d'])}"
            )

        lines.append("")
        lines.append(f"【D. 風險警示】{len(risk_df)} 檔")

        for _, row in risk_df.head(30).iterrows():
            lines.append(
                f"{row['code']} {row['name']} | {row['exit_warning']}"
            )

    lines.append("")
    lines.append(f"【C. 全市場激進小型股雷達】{len(radar_df)} 檔")

    if radar_df.empty:
        lines.append("今天沒有同時符合急漲、爆量、突破與收盤接近日高的股票。")
    else:
        for _, row in radar_df.head(30).iterrows():
            lines.append(
                f"{row['code']} {row['name']} | {row['market']} | "
                f"漲跌 {format_pct(row['daily_change_pct'])} | "
                f"量比 {format_price(row['volume_ratio_20d'])} | "
                f"收盤 {format_price(row['close'])}"
            )

    lines.append("")
    lines.append("C 區為激進短線雷達，不是買進建議。")

    return "\n".join(lines)


# ============================================================
# 9. Gmail 寄信：保留原本方式
# ============================================================

def send_email(subject, text_content, html_content):
    sender_email = os.environ.get("MAIL_USER")
    receiver_email = os.environ.get("MAIL_RECEIVER", sender_email)
    app_password = os.environ.get("MAIL_PASSWORD")

    if not sender_email:
        raise EnvironmentError("缺少 MAIL_USER。")

    if not receiver_email:
        raise EnvironmentError("缺少 MAIL_RECEIVER。")

    if not app_password:
        raise EnvironmentError("缺少 MAIL_PASSWORD。")

    msg = MIMEMultipart("alternative")
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg["Subject"] = subject

    msg.attach(MIMEText(text_content, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP(
        "smtp.gmail.com",
        587,
        timeout=SETTINGS["smtp_timeout"],
    ) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(sender_email, app_password)
        server.send_message(msg)


# ============================================================
# 10. 主程式
# ============================================================

def main():
    logging.info("========== 台股報告開始 ==========")

    theme_results, warnings = scan_theme_universe()

    if theme_results:
        theme_df = pd.DataFrame(theme_results)
        theme_df, chip_warnings = merge_institutional_data(theme_df)
        warnings.extend(chip_warnings)

        theme_df = add_institutional_history_signals(theme_df)

        theme_df["exit_warning"] = theme_df.apply(
            make_exit_warning,
            axis=1,
        )

        theme_df["exit_warning"] = theme_df.apply(
            add_history_warning,
            axis=1,
        )

        theme_df = sort_theme_results(theme_df)

    else:
        theme_df = pd.DataFrame()
        warnings.append("題材池沒有取得可用股票資料。")

    radar_df, radar_warnings = run_full_market_aggressive_radar()
    warnings.extend(radar_warnings)

    if not radar_df.empty:
        radar_df, radar_chip_warnings = merge_institutional_data(radar_df)
        warnings.extend(radar_chip_warnings)

        radar_df["exit_warning"] = radar_df.apply(
            make_exit_warning,
            axis=1,
        )

    theme_path, radar_path = save_csv(theme_df, radar_df)

    text_report = build_text_report(theme_df, radar_df)
    html_report = build_html_report(theme_df, radar_df, warnings)

    print(text_report)

    if theme_path:
        print(f"題材池 CSV：{theme_path}")

    if radar_path:
        print(f"雷達 CSV：{radar_path}")

    subject = f"📊 台股長線、短線與全市場資金雷達 - {RUN_DATE}"

    print("📧 開始寄送 Gmail 報告...")

    try:
        send_email(subject, text_report, html_report)
        print("✅ Email 報告已成功寄出。")
    except Exception as e:
        print(f"❌ Email 寄送失敗：{type(e).__name__}: {e}")
        logging.exception("Email 寄送失敗。")
        raise

    logging.info("========== 台股報告完成 ==========")


if __name__ == "__main__":
    main()
