# -*- coding: utf-8 -*-
"""
台股趨勢選股機器人
- 長線／波段：高成長、穩健成長、資產價值
- 短線：爆量、突破、短均線動能
- 風險：法人賣超、跌破均線、乖離過高
- Email：沿用 Gmail SMTP（MAIL_USER / MAIL_PASSWORD / MAIL_RECEIVER）
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
# 1. 設定
# ============================================================

RUN_TIME = datetime.now()
RUN_DATE = RUN_TIME.strftime("%Y-%m-%d")
RUN_DATETIME = RUN_TIME.strftime("%Y-%m-%d %H:%M:%S")

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"

OUTPUT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)


# 題材池：不是買進清單，而是掃描範圍
# 上市：.TW；上櫃：.TWO
UNIVERSE = {
    # 半導體 / ASIC
    "2330": {"name": "台積電", "theme": "晶圓代工 / AI晶片", "ticker": "2330.TW", "market": "TWSE"},
    "2454": {"name": "聯發科", "theme": "IC設計 / 邊緣AI", "ticker": "2454.TW", "market": "TWSE"},
    "3711": {"name": "日月光投控", "theme": "封測 / AI晶片", "ticker": "3711.TW", "market": "TWSE"},
    "3443": {"name": "創意", "theme": "ASIC設計服務", "ticker": "3443.TW", "market": "TWSE"},
    "3661": {"name": "世芯-KY", "theme": "ASIC / AI晶片", "ticker": "3661.TW", "market": "TWSE"},
    "3035": {"name": "智原", "theme": "ASIC設計服務", "ticker": "3035.TW", "market": "TWSE"},
    "3529": {"name": "力旺", "theme": "矽智財", "ticker": "3529.TW", "market": "TWSE"},

    # AI 伺服器 / ODM
    "2382": {"name": "廣達", "theme": "AI伺服器", "ticker": "2382.TW", "market": "TWSE"},
    "3231": {"name": "緯創", "theme": "AI伺服器", "ticker": "3231.TW", "market": "TWSE"},
    "2356": {"name": "英業達", "theme": "AI伺服器", "ticker": "2356.TW", "market": "TWSE"},
    "6669": {"name": "緯穎", "theme": "雲端 / AI伺服器", "ticker": "6669.TW", "market": "TWSE"},
    "8210": {"name": "勤誠", "theme": "伺服器機殼", "ticker": "8210.TW", "market": "TWSE"},

    # CCL / PCB / 載板
    "2383": {"name": "台光電", "theme": "CCL / AI伺服器材料", "ticker": "2383.TW", "market": "TWSE"},
    "6274": {"name": "台燿", "theme": "CCL / 高速材料", "ticker": "6274.TW", "market": "TWSE"},
    "2368": {"name": "金像電", "theme": "PCB / AI伺服器", "ticker": "2368.TW", "market": "TWSE"},
    "3037": {"name": "欣興", "theme": "ABF載板 / PCB", "ticker": "3037.TW", "market": "TWSE"},
    "3189": {"name": "景碩", "theme": "ABF載板", "ticker": "3189.TW", "market": "TWSE"},
    "8046": {"name": "南電", "theme": "ABF載板", "ticker": "8046.TW", "market": "TWSE"},

    # 散熱 / 電源
    "3017": {"name": "奇鋐", "theme": "散熱 / AI伺服器", "ticker": "3017.TW", "market": "TWSE"},
    "3324": {"name": "雙鴻", "theme": "散熱 / AI伺服器", "ticker": "3324.TW", "market": "TWSE"},
    "3653": {"name": "健策", "theme": "散熱 / 均熱片", "ticker": "3653.TW", "market": "TWSE"},
    "2421": {"name": "建準", "theme": "風扇 / 散熱", "ticker": "2421.TW", "market": "TWSE"},
    "2308": {"name": "台達電", "theme": "電源 / 資料中心", "ticker": "2308.TW", "market": "TWSE"},
    "6409": {"name": "旭隼", "theme": "UPS / 電源", "ticker": "6409.TW", "market": "TWSE"},

    # 網通 / 光通訊 / CPO
    "2345": {"name": "智邦", "theme": "網通 / 資料中心", "ticker": "2345.TW", "market": "TWSE"},
    "4979": {"name": "華星光", "theme": "光通訊", "ticker": "4979.TW", "market": "TWSE"},
    "3363": {"name": "上詮", "theme": "光通訊 / CPO", "ticker": "3363.TWO", "market": "TPEX"},
    "3081": {"name": "聯亞", "theme": "光通訊", "ticker": "3081.TWO", "market": "TPEX"},
    "6442": {"name": "光聖", "theme": "光通訊", "ticker": "6442.TW", "market": "TWSE"},
    "5388": {"name": "中磊", "theme": "網通", "ticker": "5388.TW", "market": "TWSE"},
    "3596": {"name": "智易", "theme": "網通", "ticker": "3596.TW", "market": "TWSE"},

    # 半導體設備 / 記憶體
    "1560": {"name": "中砂", "theme": "半導體耗材", "ticker": "1560.TW", "market": "TWSE"},
    "3583": {"name": "辛耘", "theme": "半導體設備", "ticker": "3583.TW", "market": "TWSE"},
    "6187": {"name": "萬潤", "theme": "半導體設備", "ticker": "6187.TW", "market": "TWSE"},
    "3131": {"name": "弘塑", "theme": "半導體設備", "ticker": "3131.TW", "market": "TWSE"},
    "3680": {"name": "家登", "theme": "半導體設備 / EUV", "ticker": "3680.TW", "market": "TWSE"},
    "2344": {"name": "華邦電", "theme": "記憶體", "ticker": "2344.TW", "market": "TWSE"},
    "2408": {"name": "南亞科", "theme": "DRAM", "ticker": "2408.TW", "market": "TWSE"},
    "8299": {"name": "群聯", "theme": "NAND控制IC", "ticker": "8299.TWO", "market": "TPEX"},

    # 資產價值 / 緩慢成長示範
    "2881": {"name": "富邦金", "theme": "金融 / 資產價值", "ticker": "2881.TW", "market": "TWSE"},
    "2882": {"name": "國泰金", "theme": "金融 / 資產價值", "ticker": "2882.TW", "market": "TWSE"},
    "2002": {"name": "中鋼", "theme": "鋼鐵 / 景氣循環", "ticker": "2002.TW", "market": "TWSE"},
}


SETTINGS = {
    # 資料與流動性
    "history_period": "2y",
    "min_history_days": 261,
    "min_close_price": 10.0,
    "min_avg_turnover_20d": 20_000_000,

    # 均線
    "ma5": 5,
    "ma10": 10,
    "ma20": 20,
    "ma60": 60,
    "ma240": 240,
    "ma240_slope_days": 20,

    # 長線／波段
    "max_deviation_ma240_pct": 35.0,
    "high_growth_pct": 20.0,
    "stable_growth_pct": 5.0,

    # 保守 PEG = PE / min(G, 40)
    "peg_growth_cap_pct": 40.0,
    "peg_good": 1.0,
    "peg_warning": 2.0,

    # 價值股
    "value_max_pe": 15.0,
    "value_max_pb": 1.5,
    "value_min_yield_pct": 3.0,

    # 短線動能條件
    "breakout_days": 20,
    "min_daily_change_pct": 2.5,
    "min_volume_ratio": 1.5,
    "max_short_term_deviation_ma20_pct": 20.0,

    # 法人單日大賣超警示
    "large_single_day_net_sell": -2_000_000,

    # 請求設定
    "request_timeout": 20,
    "smtp_timeout": 30,
    "sleep_between_yf_calls": 0.35,
}


# ============================================================
# 2. Log
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


# ============================================================
# 3. 工具函式
# ============================================================

def safe_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def to_pct(value):
    value = safe_float(value)

    if value is None:
        return None

    return value * 100 if abs(value) <= 5 else value


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


# ============================================================
# 4. 成長、估值、長線、短線判斷
# ============================================================

def classify_stock(data):
    eps = data["trailing_eps"]
    pe = data["trailing_pe"]
    pb = data["pb_ratio"]
    dividend_yield = data["dividend_yield_pct"]
    revenue_growth = data["revenue_growth_pct"]
    earnings_growth = data["earnings_growth_pct"]

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
    pe = data["trailing_pe"]
    growth = data["earnings_growth_pct"]

    if pe is None or pe <= 0 or growth is None or growth <= 0:
        return None

    capped_growth = min(growth, SETTINGS["peg_growth_cap_pct"])

    if capped_growth <= 0:
        return None

    return round(pe / capped_growth, 2)


def make_long_term_signal(data):
    """
    長線／波段：
    股價在 MA60 與 MA240 上方、
    MA240 上揚、年線乖離不過熱。
    """
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
    """
    短線動能：
    1. 股價在 MA5 / MA10 / MA20 上方
    2. 當日漲幅達門檻
    3. 當日量 >= 20日均量 * 倍數
    4. 收盤突破「前 20 日最高收盤價」
    5. 不要離 MA20 太遠，避免爆衝追高

    短線訊號只用來列入觀察，不等於建議買進。
    """
    if not data["short_ma_ok"]:
        return "短線趨勢弱"

    if data["daily_change_pct"] <= 0:
        return "短線正常，未發動"

    if data["volume_ratio_20d"] < SETTINGS["min_volume_ratio"]:
        return "短線上漲但量能不足"

    if (
        data["breakout_20d"]
        and data["daily_change_pct"] >= SETTINGS["min_daily_change_pct"]
        and data["ma20_deviation_pct"]
        <= SETTINGS["max_short_term_deviation_ma20_pct"]
    ):
        return "爆量突破，短線觀察"

    if (
        data["daily_change_pct"] >= SETTINGS["min_daily_change_pct"]
        and data["volume_ratio_20d"] >= SETTINGS["min_volume_ratio"]
    ):
        return "量價轉強，短線觀察"

    return "短線偏強，等待突破"


def get_stock_snapshot(code, meta):
    ticker = meta["ticker"]
    name = meta["name"]

    try:
        stock = yf.Ticker(ticker)

        hist = stock.history(
            period=SETTINGS["history_period"],
            interval="1d",
            auto_adjust=False,
            actions=False,
        ).dropna(subset=["Close"]).copy()

        min_required = max(
            SETTINGS["min_history_days"],
            SETTINGS["ma240"] + SETTINGS["ma240_slope_days"] + 1,
        )

        if len(hist) < min_required:
            return None, f"{code} {name}：日K資料不足。"

        hist["MA5"] = hist["Close"].rolling(SETTINGS["ma5"]).mean()
        hist["MA10"] = hist["Close"].rolling(SETTINGS["ma10"]).mean()
        hist["MA20"] = hist["Close"].rolling(SETTINGS["ma20"]).mean()
        hist["MA60"] = hist["Close"].rolling(SETTINGS["ma60"]).mean()
        hist["MA240"] = hist["Close"].rolling(SETTINGS["ma240"]).mean()

        latest = hist.iloc[-1]
        previous = hist.iloc[-2]

        close = safe_float(latest["Close"])
        prev_close = safe_float(previous["Close"])
        ma5 = safe_float(latest["MA5"])
        ma10 = safe_float(latest["MA10"])
        ma20 = safe_float(latest["MA20"])
        ma60 = safe_float(latest["MA60"])
        ma240 = safe_float(latest["MA240"])
        ma240_past = safe_float(
            hist["MA240"].iloc[-(SETTINGS["ma240_slope_days"] + 1)]
        )

        values = [close, prev_close, ma5, ma10, ma20, ma60, ma240, ma240_past]

        if any(value is None for value in values):
            return None, f"{code} {name}：均線資料不完整。"

        avg_volume_20d = safe_float(hist["Volume"].tail(20).mean())
        avg_turnover_20d = safe_float(
            (hist["Close"] * hist["Volume"]).tail(20).mean()
        )

        if close < SETTINGS["min_close_price"]:
            return None, f"{code} {name}：股價過低，略過。"

        if (
            avg_turnover_20d is None
            or avg_turnover_20d < SETTINGS["min_avg_turnover_20d"]
        ):
            return None, f"{code} {name}：20日均成交額不足，略過。"

        daily_change_pct = (close / prev_close - 1) * 100
        ma240_slope_pct = (ma240 / ma240_past - 1) * 100
        deviation_ma240_pct = (close / ma240 - 1) * 100
        ma20_deviation_pct = (close / ma20 - 1) * 100

        volume = safe_float(latest["Volume"])
        volume_ratio_20d = (
            volume / avg_volume_20d
            if avg_volume_20d and avg_volume_20d > 0
            else None
        )

        # 不把今天算進去，避免「自己突破自己」
        previous_20d_high = safe_float(
            hist["Close"].iloc[-21:-1].max()
        )

        breakout_20d = (
            previous_20d_high is not None
            and close > previous_20d_high
        )

        try:
            info = stock.info
        except Exception as e:
            logging.warning("%s %s 基本面讀取失敗：%s", code, name, e)
            info = {}

        data = {
            "code": code,
            "name": name,
            "theme": meta["theme"],
            "ticker": ticker,
            "market": meta["market"],
            "price_date": hist.index[-1].strftime("%Y-%m-%d"),

            "close": round(close, 2),
            "daily_change_pct": round(daily_change_pct, 2),

            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "ma240": round(ma240, 2),

            "ma240_slope_pct": round(ma240_slope_pct, 2),
            "deviation_ma240_pct": round(deviation_ma240_pct, 2),
            "ma20_deviation_pct": round(ma20_deviation_pct, 2),

            "volume": int(volume) if volume is not None else None,
            "avg_volume_20d": int(avg_volume_20d) if avg_volume_20d else None,
            "volume_ratio_20d": round(volume_ratio_20d, 2) if volume_ratio_20d else None,
            "avg_turnover_20d": round(avg_turnover_20d, 0),

            "previous_20d_high": round(previous_20d_high, 2) if previous_20d_high else None,
            "breakout_20d": breakout_20d,

            "trailing_eps": safe_float(info.get("trailingEps")),
            "trailing_pe": safe_float(info.get("trailingPE")),
            "forward_pe": safe_float(info.get("forwardPE")),
            "pb_ratio": safe_float(info.get("priceToBook")),
            "dividend_yield_pct": to_pct(info.get("dividendYield")),
            "revenue_growth_pct": to_pct(info.get("revenueGrowth")),
            "earnings_growth_pct": to_pct(info.get("earningsGrowth")),
        }

        data["price_above_ma5"] = close > ma5
        data["price_above_ma10"] = close > ma10
        data["price_above_ma20"] = close > ma20
        data["price_above_ma60"] = close > ma60
        data["price_above_ma240"] = close > ma240
        data["ma240_rising"] = ma240 > ma240_past
        data["deviation_ok"] = (
            deviation_ma240_pct <= SETTINGS["max_deviation_ma240_pct"]
        )

        data["long_trend_ok"] = (
            data["price_above_ma60"]
            and data["price_above_ma240"]
            and data["ma240_rising"]
            and data["deviation_ok"]
        )

        data["short_ma_ok"] = (
            data["price_above_ma5"]
            and data["price_above_ma10"]
            and data["price_above_ma20"]
        )

        data["growth_type"] = classify_stock(data)
        data["conservative_peg"] = calculate_conservative_peg(data)
        data["long_term_signal"] = make_long_term_signal(data)
        data["short_term_signal"] = make_short_term_signal(data)

        return data, None

    except Exception as e:
        return None, f"{code} {name}：{type(e).__name__}: {e}"


# ============================================================
# 5. TWSE 三大法人
# ============================================================

def get_twse_institutional_data():
    url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json"

    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=SETTINGS["request_timeout"],
    )
    response.raise_for_status()

    data = response.json()

    if data.get("stat") != "OK":
        raise RuntimeError(f"TWSE 回傳異常：{data.get('stat')}")

    df = pd.DataFrame(data["data"], columns=data["fields"])

    code_col = find_column(df.columns, ["證券代號"])
    name_col = find_column(df.columns, ["證券名稱"])
    net_col = find_column(df.columns, ["三大法人", "買賣超股數"])
    foreign_col = find_column(df.columns, ["外資", "買賣超股數"])
    trust_col = find_column(df.columns, ["投信", "買賣超股數"])
    dealer_col = find_column(df.columns, ["自營商", "買賣超股數"])

    if not code_col or not name_col or not net_col:
        raise KeyError(f"TWSE 法人欄位找不到：{df.columns.tolist()}")

    rename_map = {
        code_col: "code",
        name_col: "chip_name",
        net_col: "institutional_net",
    }

    if foreign_col:
        rename_map[foreign_col] = "foreign_net"

    if trust_col:
        rename_map[trust_col] = "trust_net"

    if dealer_col:
        rename_map[dealer_col] = "dealer_net"

    df = df.rename(columns=rename_map)

    for column in ["foreign_net", "trust_net", "dealer_net", "institutional_net"]:
        if column not in df.columns:
            df[column] = None
        else:
            df[column] = df[column].apply(clean_number)

    df["code"] = df["code"].astype(str).str.strip()

    return df[
        [
            "code",
            "chip_name",
            "foreign_net",
            "trust_net",
            "dealer_net",
            "institutional_net",
        ]
    ]


def merge_institutional_data(df):
    warnings = []

    for column in ["foreign_net", "trust_net", "dealer_net", "institutional_net"]:
        df[column] = None

    df["chip_note"] = ""

    try:
        chips_df = get_twse_institutional_data()
        twse_mask = df["market"] == "TWSE"

        merged_twse = df.loc[twse_mask].merge(
            chips_df,
            on="code",
            how="left",
            suffixes=("", "_api"),
        )

        for column in ["foreign_net", "trust_net", "dealer_net", "institutional_net"]:
            df.loc[twse_mask, column] = merged_twse[column].values

        df.loc[twse_mask, "chip_note"] = "TWSE 最新可取得交易日資料"

        missing_mask = twse_mask & df["institutional_net"].isna()
        df.loc[missing_mask, "chip_note"] = "TWSE 法人資料未找到"

    except Exception as e:
        warnings.append(f"法人資料抓取失敗：{type(e).__name__}: {e}")
        df.loc[df["market"] == "TWSE", "chip_note"] = "TWSE 法人資料抓取失敗"

    df.loc[df["market"] == "TPEX", "chip_note"] = "上櫃股：尚未串接 TPEx 法人資料"

    return df, warnings


# ============================================================
# 6. 出場警示
# ============================================================

def make_exit_warning(row):
    warnings = []

    # 長線／波段風險
    if row["close"] < row["ma240"]:
        warnings.append("跌破MA240：長線轉弱")
    elif row["close"] < row["ma60"]:
        warnings.append("跌破MA60：波段轉弱")

    # 短線風險
    if row["close"] < row["ma10"] and row["daily_change_pct"] < 0:
        warnings.append("跌破MA10：短線注意")

    # 資金風險
    net = row.get("institutional_net")

    if net is not None and pd.notna(net):
        if net <= SETTINGS["large_single_day_net_sell"]:
            warnings.append(f"法人單日明顯賣超{format_int(net)}股")

    # 估值／乖離
    peg = row.get("conservative_peg")

    if peg is not None and pd.notna(peg) and peg > SETTINGS["peg_warning"]:
        warnings.append(f"PEG {peg:.2f}偏高")

    if row["deviation_ma240_pct"] > SETTINGS["max_deviation_ma240_pct"]:
        warnings.append("年線乖離過大")

    return "；".join(warnings) if warnings else "目前無即時警示"


# ============================================================
# 7. 歷史 CSV：法人 3 日 / 5 日
# ============================================================

def load_history():
    files = sorted(
        OUTPUT_DIR.glob("daily_candidates_*.csv"),
        reverse=True,
    )

    frames = []

    for file_path in files[:10]:
        try:
            history_df = pd.read_csv(file_path, dtype={"code": str})

            if {"code", "institutional_net", "run_date"}.issubset(history_df.columns):
                frames.append(history_df)

        except Exception as e:
            logging.warning("讀取歷史檔失敗 %s：%s", file_path.name, e)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)


def add_institutional_history_signals(df):
    df = df.copy()

    df["institutional_3d_net"] = None
    df["institutional_5d_net"] = None
    df["institutional_sell_streak"] = None

    history = load_history()

    if history.empty:
        return df

    current = df.copy()
    current["run_date"] = RUN_DATE

    combined = pd.concat([history, current], ignore_index=True, sort=False)
    combined["run_date"] = pd.to_datetime(combined["run_date"], errors="coerce")
    combined = combined.sort_values(["code", "run_date"])

    metrics = []

    for code, group in combined.groupby("code"):
        group = group.dropna(subset=["institutional_net"]).tail(5)

        if group.empty:
            continue

        nets = group["institutional_net"].tolist()
        sell_streak = 0

        for value in reversed(nets):
            if value < 0:
                sell_streak += 1
            else:
                break

        metrics.append({
            "code": code,
            "institutional_3d_net": sum(nets[-3:]) if len(nets) >= 3 else None,
            "institutional_5d_net": sum(nets[-5:]) if len(nets) >= 5 else None,
            "institutional_sell_streak": sell_streak,
        })

    if not metrics:
        return df

    metrics_df = pd.DataFrame(metrics)

    return df.drop(
        columns=[
            "institutional_3d_net",
            "institutional_5d_net",
            "institutional_sell_streak",
        ],
        errors="ignore",
    ).merge(metrics_df, on="code", how="left")


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

    if warning == "目前無即時警示":
        return "；".join(additions)

    return warning + "；" + "；".join(additions)


# ============================================================
# 8. 掃描與排序
# ============================================================

def scan_universe():
    results = []
    warnings = []

    for index, (code, meta) in enumerate(UNIVERSE.items(), start=1):
        logging.info("掃描 %s/%s：%s %s", index, len(UNIVERSE), code, meta["name"])

        result, warning = get_stock_snapshot(code, meta)

        if result is not None:
            results.append(result)

        if warning:
            warnings.append(warning)

        time.sleep(SETTINGS["sleep_between_yf_calls"])

    return results, warnings


def sort_results(df):
    df = df.copy()

    category_rank = {
        "高成長": 1,
        "穩健成長": 2,
        "資產價值": 3,
        "其他": 4,
    }

    df["category_rank"] = df["growth_type"].map(category_rank).fillna(9)

    return df.sort_values(
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


def save_csv(df):
    if df.empty:
        return None

    output_df = df.copy()
    output_df["run_date"] = RUN_DATE
    output_df["run_datetime"] = RUN_DATETIME

    file_path = OUTPUT_DIR / f"daily_candidates_{RUN_DATE}.csv"

    output_df.to_csv(
        file_path,
        index=False,
        encoding="utf-8-sig",
    )

    return file_path


# ============================================================
# 9. Email 報表
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
            color = "#008000" if net > 0 else "#c00000" if net < 0 else "#333333"
            net_html = f"<span style='color:{color};font-weight:700;'>{format_int(net)}</span>"

        warning = row.get("exit_warning", "--")
        warning_color = "#008000" if warning == "目前無即時警示" else "#c00000"

        if mode == "long":
            signal = row["long_term_signal"]
            extra_columns = (
                f"<td>{html.escape(str(row['growth_type']))}</td>"
                f"<td>{format_price(row['trailing_pe'])}</td>"
                f"<td>{format_price(row['conservative_peg'])}</td>"
                f"<td>{format_pct(row['ma240_slope_pct'])}</td>"
            )
        else:
            signal = row["short_term_signal"]
            extra_columns = (
                f"<td>{format_pct(row['daily_change_pct'])}</td>"
                f"<td>{format_price(row['volume_ratio_20d'])}</td>"
                f"<td>{'是' if row['breakout_20d'] else '否'}</td>"
                f"<td>{format_price(row['ma10'])}</td>"
            )

        rows.append(
            "<tr>"
            f"<td>{html.escape(str(row['code']))}</td>"
            f"<td>{html.escape(str(row['name']))}</td>"
            f"<td>{html.escape(str(row['theme']))}</td>"
            f"<td>{html.escape(str(signal))}</td>"
            f"<td>{format_price(row['close'])}</td>"
            f"{extra_columns}"
            f"<td>{net_html}</td>"
            f"<td style='color:{warning_color};'>{html.escape(str(warning))}</td>"
            "</tr>"
        )

    if mode == "long":
        headers = """
        <th>代號</th><th>名稱</th><th>題材</th><th>長線狀態</th>
        <th>收盤</th><th>分類</th><th>PE</th><th>PEG</th>
        <th>MA240斜率</th><th>三大法人</th><th>風險提醒</th>
        """
    else:
        headers = """
        <th>代號</th><th>名稱</th><th>題材</th><th>短線狀態</th>
        <th>收盤</th><th>當日漲跌</th><th>量比</th><th>突破20日高</th>
        <th>MA10</th><th>三大法人</th><th>風險提醒</th>
        """

    return f"""
    <table>
        <thead><tr>{headers}</tr></thead>
        <tbody>{''.join(rows)}</tbody>
    </table>
    """


def build_html_report(df, warnings):
    if df.empty:
        long_html = "<p>沒有可用資料。</p>"
        short_html = "<p>沒有可用資料。</p>"
        risk_html = "<p>沒有可用資料。</p>"
    else:
        long_df = df[df["long_trend_ok"] == True].copy()

        short_df = df[
            df["short_term_signal"].isin([
                "爆量突破，短線觀察",
                "量價轉強，短線觀察",
            ])
        ].copy()

        risk_df = df[
            df["exit_warning"] != "目前無即時警示"
        ].copy()

        long_html = make_html_table(long_df.head(40), "long")
        short_html = make_html_table(short_df.head(30), "short")
        risk_html = make_html_table(risk_df.head(40), "long")

    warning_html = ""

    if warnings:
        items = "".join(
            f"<li>{html.escape(str(item))}</li>"
            for item in warnings[:15]
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
        </style>
    </head>
    <body>
        <h1>📊 台股趨勢、短線動能與資金報告</h1>

        <p>
            <b>執行時間：</b>{html.escape(RUN_DATETIME)}<br>
            <b>掃描題材池：</b>{len(UNIVERSE)} 檔
        </p>

        <div class="notice">
            <b>怎麼看：</b><br>
            長線區：看高成長、穩健成長、價值股與 MA240 趨勢。<br>
            短線區：看爆量、突破 20 日高、MA5/MA10/MA20 多頭。<br>
            風險區：看跌破 MA10、MA60、MA240、法人明顯賣超與 PEG 過熱。<br>
            上櫃股法人資料尚未串 TPEx，空白不代表法人沒有買賣。
        </div>

        <h2>A. 長線／波段順風車候選</h2>
        {long_html}

        <h2>B. 短線資金動能候選</h2>
        {short_html}

        <h2>C. 資金流出／趨勢轉弱警示</h2>
        {risk_html}

        {warning_html}

        <div class="notice">
            <b>PEG：</b>
            採保守公式 PEG = PE / min(盈餘成長率%, 40)。<br>
            PEG &lt; 1 是相對合理，不等於必買；PEG &gt; 2 是估值警戒，不等於立刻賣。
        </div>

        <p>
            本報告為研究與追蹤工具，不構成投資建議。
            實際交易前請以券商 App、公開資訊觀測站、公司公告與交易所資料確認。
        </p>
    </body>
    </html>
    """


def build_text_report(df):
    lines = [
        "台股趨勢、短線動能與資金報告",
        "=" * 48,
        f"執行時間：{RUN_DATETIME}",
        f"掃描題材池：{len(UNIVERSE)} 檔",
        "",
    ]

    if df.empty:
        lines.append("本次沒有可用資料。")
        return "\n".join(lines)

    long_df = df[df["long_trend_ok"] == True]
    short_df = df[
        df["short_term_signal"].isin([
            "爆量突破，短線觀察",
            "量價轉強，短線觀察",
        ])
    ]
    risk_df = df[df["exit_warning"] != "目前無即時警示"]

    lines.append(f"【A. 長線／波段候選】{len(long_df)} 檔")

    for _, row in long_df.head(20).iterrows():
        lines.append(
            f"{row['code']} {row['name']} | "
            f"{row['long_term_signal']} | "
            f"收盤 {format_price(row['close'])} | "
            f"PEG {format_price(row['conservative_peg'])} | "
            f"法人 {format_int(row['institutional_net'])}"
        )

    lines.append("")
    lines.append(f"【B. 短線動能候選】{len(short_df)} 檔")

    for _, row in short_df.head(20).iterrows():
        lines.append(
            f"{row['code']} {row['name']} | "
            f"{row['short_term_signal']} | "
            f"漲跌 {format_pct(row['daily_change_pct'])} | "
            f"量比 {format_price(row['volume_ratio_20d'])} | "
            f"法人 {format_int(row['institutional_net'])}"
        )

    lines.append("")
    lines.append(f"【C. 風險警示】{len(risk_df)} 檔")

    for _, row in risk_df.head(30).iterrows():
        lines.append(
            f"{row['code']} {row['name']} | {row['exit_warning']}"
        )

    lines.append("")
    lines.append("提醒：短線爆量不等於必買；法人單日賣超也不等於必跑。")
    lines.append("請搭配你的持股成本、停損規則、產業與財報確認。")

    return "\n".join(lines)


# ============================================================
# 10. Gmail 寄信：沿用 Gemini 原本方式
# ============================================================

def send_email(subject, text_content, html_content):
    sender_email = os.environ.get("MAIL_USER")
    receiver_email = os.environ.get("MAIL_RECEIVER", sender_email)
    app_password = os.environ.get("MAIL_PASSWORD")

    if not sender_email or not app_password:
        raise EnvironmentError("缺少 MAIL_USER 或 MAIL_PASSWORD。")

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
# 11. 主程式
# ============================================================

def main():
    logging.info("========== 開始執行台股報告 ==========")

    results, warnings = scan_universe()

    if not results:
        raise RuntimeError("沒有取得任何股票資料，請查看 Log。")

    report_df = pd.DataFrame(results)

    report_df, chip_warnings = merge_institutional_data(report_df)
    warnings.extend(chip_warnings)

    report_df = add_institutional_history_signals(report_df)

    report_df["exit_warning"] = report_df.apply(
        make_exit_warning,
        axis=1,
    )

    report_df["exit_warning"] = report_df.apply(
        add_history_warning,
        axis=1,
    )

    report_df = sort_results(report_df)

    csv_path = save_csv(report_df)

    text_report = build_text_report(report_df)
    html_report = build_html_report(report_df, warnings)

    print(text_report)

    if csv_path:
        print(f"\nCSV 已儲存：{csv_path}")

    subject = f"📊 台股趨勢與短線資金報告 - {RUN_DATE}"

    send_email(
        subject=subject,
        text_content=text_report,
        html_content=html_report,
    )

    logging.info("========== 報告執行完成 ==========")


if __name__ == "__main__":
    main()
