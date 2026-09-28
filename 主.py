# -*- coding: utf-8 -*-
"""
台股每日題材與法人布局雷達
- A 區：熱門題材主題股
- B 區：熱門題材延伸股
- C 區：全市場基本面加速雷達
- D 區：熱門題材強勢／風險雷達
- E 區：基本面加速＋法人布局雷達
  🟢 投信布局綠燈
  🟡 外資流入黃燈
  🔴 資金／趨勢紅燈

A 為主：投信連續買超 3 日
B 為輔：外資最近 5 日累計買超
"""

import os
import io
import json
import time
import math
import smtplib
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import pandas as pd
import numpy as np
import requests

# ==============================
# 基本設定
# ==============================

TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ).strftime("%Y-%m-%d")

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

DAILY_CSV = os.path.join(OUTPUT_DIR, f"daily_theme_candidates_{TODAY}.csv")
INST_CSV = os.path.join(OUTPUT_DIR, f"institutional_history_{TODAY}.csv")
REV_CSV = os.path.join(OUTPUT_DIR, f"monthly_revenue_history_{TODAY}.csv")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

# ==============================
# 題材池：可自行增刪
# ==============================

THEMES = {
    "AI 伺服器／ODM": [
        "2317", "3231", "3149", "3380", "6805", "6669", "3017", "2382"
    ],
    "散熱": [
        "3017", "3324", "6205", "3324", "3533", "6131", "2457", "3597"
    ],
    "電源管理／電源供應": [
        "2308", "6504", "3533", "2421", "6415", "3312"
    ],
    "PCB／CCL／載板": [
        "2368", "2385", "3037", "4967", "6763", "6274", "3046", "2377"
    ],
    "光通訊／矽光子": [
        "3081", "3163", "3380", "6443", "4989", "4934", "3450", "3312"
    ],
    "先進封裝／ABF 載板／測試": [
        "2330", "3711", "6271", "3264", "3374", "6196", "6182", "3131"
    ],
    "機器人／自動化／精密減速": [
        "2393", "4526", "1582", "4576", "3533", "2464", "6206"
    ],
    "半導體設備／材料／關鍵零組件": [
        "6116", "3532", "4763", "2481", "6788", "2330", "2337", "6781"
    ],
}

EXTENDED_POOL = [
    "2454", "3231", "2382", "2317", "2308", "6504", "2385", "2368",
    "3037", "3034", "3017", "3380", "3131", "6271", "6805", "4967",
    "2345", "2344", "2412", "2882", "6505", "3711", "2356", "3044",
    "3016", "3450", "6669", "3163", "3081", "6443", "5269", "6415"
]

# ==============================
# HTTP 工具
# ==============================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

def http_get(url, params=None, timeout=30):
    for i in range(3):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception:
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"HTTP failed: {url}")

def safe_float(x):
    try:
        if x in [None, "", "-", "—", "NaN", "nan"]:
            return np.nan
        return float(str(x).replace(",", "").replace("%", ""))
    except Exception:
        return np.nan

def safe_int(x):
    try:
        if x in [None, "", "-", "—", "NaN", "nan"]:
            return np.nan
        return int(str(x).replace(",", ""))
    except Exception:
        return np.nan

# ==============================
# 台股代號、名稱與產業
# ==============================

def get_twse_stock_list():
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    r = http_get(url)
    data = r.json()
    rows = []
    for x in data:
        code = str(x.get("Code", "")).strip()
        name = str(x.get("Name", "")).strip()
        if code and name:
            rows.append({
                "stock_id": code,
                "stock_name": name,
                "market": "TWSE",
            })
    return pd.DataFrame(rows)

def get_tpex_stock_list():
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
    r = http_get(url)
    data = r.json()
    rows = []
    for x in data:
        code = str(x.get("SecuritiesTraderCode", "") or x.get("SecuritiesCode", "")).strip()
        name = str(x.get("CompanyName", "") or x.get("SecuritiesName", "")).strip()
        if code and name:
            rows.append({
                "stock_id": code,
                "stock_name": name,
                "market": "TPEx",
            })
    return pd.DataFrame(rows)

def build_stock_universe():
    print("建立台股全市場清單...")
    frames = []

    try:
        frames.append(get_twse_stock_list())
    except Exception as e:
        print("TWSE list failed:", e)

    try:
        frames.append(get_tpex_stock_list())
    except Exception as e:
        print("TPEx list failed:", e)

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["stock_id"], keep="first")
    return df

# ==============================
# 每日收盤行情
# ==============================

def get_twse_daily_quotes(date_str=None):
    """
    取得上市股票當日收盤資料。
    date_str: YYYYMMDD，不填則為最近交易日。
    """
    if date_str is None:
        date_str = datetime.now(TZ).strftime("%Y%m%d")

    url = "https://www.twse.com.tw/exchangeReport/MI_INDEX"
    params = {
        "response": "json",
        "date": date_str,
        "type": "ALL",
    }
    r = http_get(url, params=params)
    data = r.json()

    if data.get("stat") != "OK":
        raise RuntimeError(f"TWSE daily quotes failed: {data.get('stat')}")

    target_title = None
    for title in data.get("tables", []):
        t = title.get("title", "")
        if "每日收盤行情" in t and "ETF" not in t:
            target_title = title
            break

    if target_title is None:
        raise RuntimeError("TWSE daily quote table not found")

    fields = target_title.get("fields", [])
    rows = target_title.get("data", [])

    parsed = []
    for row in rows:
        d = dict(zip(fields, row))
        code = str(d.get("證券代號", "")).strip()
        if not code:
            continue
        parsed.append({
            "stock_id": code,
            "date": date_str,
            "close": safe_float(d.get("收盤價")),
            "open": safe_float(d.get("開盤價")),
            "high": safe_float(d.get("最高價")),
            "low": safe_float(d.get("最低價")),
            "volume": safe_int(d.get("成交股數")),
            "turnover": safe_float(d.get("成交金額")),
        })

    return pd.DataFrame(parsed)

def get_tpex_daily_quotes(date_str=None):
    """
    取得上櫃股票當日收盤資料。
    date_str: YYYYMMDD
    """
    if date_str is None:
        date_str = datetime.now(TZ).strftime("%Y%m%d")

    roc_date = f"{int(date_str[:4]) - 1911}{date_str[4:6]}{date_str[6:]}"

    url = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php"
    params = {
        "l": "zh-tw",
        "d": roc_date,
        "se": "AL",
        "s": "0,asc,0",
    }

    r = http_get(url, params=params)
    data = r.json()

    if data.get("aaData") is None:
        raise RuntimeError("TPEx daily quotes failed")

    parsed = []
    for row in data["aaData"]:
        code = str(row[0]).strip()
        parsed.append({
            "stock_id": code,
            "date": date_str,
            "close": safe_float(row[2]),
            "open": safe_float(row[5]),
            "high": safe_float(row[6]),
            "low": safe_float(row[7]),
            "volume": safe_int(row[3]),
            "turnover": safe_float(row[4]),
        })

    return pd.DataFrame(parsed)

def get_daily_quotes():
    print("取得上市與上櫃每日收盤資料...")
    frames = []

    try:
        frames.append(get_twse_daily_quotes())
    except Exception as e:
        print("TWSE quotes failed:", e)

    try:
        frames.append(get_tpex_daily_quotes())
    except Exception as e:
        print("TPEx quotes failed:", e)

    if not frames:
        raise RuntimeError("No daily quotes available")

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["stock_id", "date"], keep="first")
    return df

# ==============================
# 月營收
# ==============================

def get_monthly_revenue_summary():
    """
    取得上市櫃公司最新月營收彙總。
    MOPS 月營收頁面會公布公司、當月營收與 YoY。
    """
    print("取得月營收彙總...")

    url = "https://mops.twse.com.tw/nas/t21/sii/t21sc03_if.html"
    r = http_get(url, timeout=45)
    tables = pd.read_html(io.StringIO(r.text))

    rows = []
    for table in tables:
        try:
            if table.shape[1] < 5:
                continue

            cols = [str(c) for c in table.columns]
            if any("公司代號" in c for c in cols):
                df = table.copy()
                df.columns = [str(c).strip() for c in df.columns]
                code_col = [c for c in df.columns if "公司代號" in c][0]
                rev_col = [c for c in df.columns if "當月營收" in c][0]
                yoy_col = [c for c in df.columns if "去年同月增減" in c][0]

                for _, row in df.iterrows():
                    code = str(row[code_col]).strip()
                    if not code or not code.isdigit():
                        continue
                    rows.append({
                        "stock_id": code,
                        "month_revenue": safe_float(row[rev_col]),
                        "month_revenue_yoy": safe_float(row[yoy_col]),
                        "announce_date": TODAY,
                    })
        except Exception:
            continue

    if not rows:
        raise RuntimeError("Monthly revenue summary failed")

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["stock_id"], keep="first")
    return df

# ==============================
# 法人買賣超
# ==============================

def get_twse_institutional_trading(date_str=None):
    """
    TWSE 三大法人個股買賣超。
    date_str: YYYYMMDD
    """
    if date_str is None:
        date_str = datetime.now(TZ).strftime("%Y%m%d")

    url = "https://www.twse.com.tw/rwd/zh/fund/T86"
    params = {
        "response": "json",
        "date": date_str,
        "selectType": "ALLBUT9909",
    }

    r = http_get(url, params=params)
    data = r.json()

    if data.get("stat") != "OK":
        raise RuntimeError(f"TWSE institutional failed: {data.get('stat')}")

    fields = data.get("fields", [])
    rows = data.get("data", [])

    parsed = []
    for row in rows:
        d = dict(zip(fields, row))
        code = str(d.get("證券代號", "")).strip()
        if not code:
            continue
        parsed.append({
            "stock_id": code,
            "date": date_str,
            "foreign_net": safe_int(d.get("外陸資買賣超股數(不含外資自營商)")),
            "trust_net": safe_int(d.get("投信買賣超股數")),
            "dealer_net": safe_int(d.get("自營商買賣超股數")),
        })

    return pd.DataFrame(parsed)

def get_tpex_institutional_trading(date_str=None):
    """
    上櫃三大法人個股買賣超。
    date_str: YYYYMMDD
    """
    if date_str is None:
        date_str = datetime.now(TZ).strftime("%Y%m%d")

    roc_date = f"{int(date_str[:4]) - 1911}{date_str[4:6]}{date_str[6:]}"

    url = "https://www.tpex.org.tw/web/stock/aftertrading/trading/fmsrf_result.php"
    params = {
        "l": "zh-tw",
        "d": roc_date,
        "s": "0,asc,0",
    }

    r = http_get(url, params=params)
    data = r.json()

    if data.get("aaData") is None:
        raise RuntimeError("TPEx institutional failed")

    parsed = []
    for row in data["aaData"]:
        code = str(row[0]).strip()
        foreign_net = safe_int(row[4])
        trust_net = safe_int(row[8])
        dealer_net = safe_int(row[11])

        parsed.append({
            "stock_id": code,
            "date": date_str,
            "foreign_net": foreign_net,
            "trust_net": trust_net,
            "dealer_net": dealer_net,
        })

    return pd.DataFrame(parsed)

def get_institutional_trading():
    print("取得三大法人個股買賣超...")
    frames = []

    try:
        frames.append(get_twse_institutional_trading())
    except Exception as e:
        print("TWSE institutional failed:", e)

    try:
        frames.append(get_tpex_institutional_trading())
    except Exception as e:
        print("TPEx institutional failed:", e)

    if not frames:
        return pd.DataFrame(columns=["stock_id", "date", "foreign_net", "trust_net", "dealer_net"])

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["stock_id", "date"], keep="first")
    return df

# ==============================
# 技術指標
# ==============================

def compute_ma(series, window):
    return series.rolling(window=window, min_periods=window).mean()

def compute_rsi(series, window=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=window, min_periods=window).mean()
    avg_loss = loss.rolling(window=window, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

# ==============================
# 主流程
# ==============================

def main():
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]):
        raise RuntimeError("缺少 GMAIL_USER / GMAIL_APP_PASSWORD / RECIPIENT_EMAIL")

    universe = build_stock_universe()

    quotes = get_daily_quotes()
    revenue = get_monthly_revenue_summary()
    inst = get_institutional_trading()

    # 儲存每日法人資料
    inst.to_csv(INST_CSV, index=False, encoding="utf-8-sig")

    # 讀取法人歷史
    inst_hist_frames = []
    for f in os.listdir(OUTPUT_DIR):
        if f.startswith("institutional_history_") and f.endswith(".csv"):
            try:
                inst_hist_frames.append(pd.read_csv(os.path.join(OUTPUT_DIR, f), dtype={"stock_id": str}))
            except Exception:
                pass

    if inst_hist_frames:
        inst_hist = pd.concat(inst_hist_frames, ignore_index=True)
    else:
        inst_hist = inst.copy()

    inst_hist = inst_hist.drop_duplicates(subset=["stock_id", "date"], keep="last")
    inst_hist["date"] = inst_hist["date"].astype(str)
    inst_hist = inst_hist.sort_values(["stock_id", "date"])

    # 儲存月營收資料
    revenue.to_csv(REV_CSV, index=False, encoding="utf-8-sig")

    # 讀取月營收歷史
    rev_hist_frames = []
    for f in os.listdir(OUTPUT_DIR):
        if f.startswith("monthly_revenue_history_") and f.endswith(".csv"):
            try:
                rev_hist_frames.append(pd.read_csv(os.path.join(OUTPUT_DIR, f), dtype={"stock_id": str}))
            except Exception:
                pass

    if rev_hist_frames:
        rev_hist = pd.concat(rev_hist_frames, ignore_index=True)
    else:
        rev_hist = revenue.copy()

    rev_hist = rev_hist.drop_duplicates(subset=["stock_id"], keep="last")

    # ==============================
    # 題材 A / B 區
    # ==============================

    theme_rows = []
    for theme, ids in THEMES.items():
        for sid in ids:
            q = quotes[quotes["stock_id"] == sid]
            if q.empty:
                continue

            q = q.iloc[0]
            name = universe.loc[universe["stock_id"] == sid, "stock_name"]
            stock_name = name.iloc[0] if not name.empty else ""

            theme_rows.append({
                "theme": theme,
                "stock_id": sid,
                "stock_name": stock_name,
                "close": q["close"],
                "turnover": q["turnover"],
                "date": TODAY,
            })

    theme_df = pd.DataFrame(theme_rows)
    theme_df = theme_df.sort_values(["theme", "turnover"], ascending=[True, False])

    extended_rows = []
    for sid in EXTENDED_POOL:
        q = quotes[quotes["stock_id"] == sid]
        if q.empty:
            continue

        q = q.iloc[0]
        name = universe.loc[universe["stock_id"] == sid, "stock_name"]
        stock_name = name.iloc[0] if not name.empty else ""

        extended_rows.append({
            "stock_id": sid,
            "stock_name": stock_name,
            "close": q["close"],
            "turnover": q["turnover"],
            "date": TODAY,
        })

    extended_df = pd.DataFrame(extended_rows)
    extended_df = extended_df.sort_values("turnover", ascending=False)

    # ==============================
    # 全市場基本面加速
    # ==============================

    base = universe.merge(
        quotes[["stock_id", "close", "turnover", "volume"]],
        on="stock_id",
        how="inner"
    ).merge(
        rev_hist[["stock_id", "month_revenue", "month_revenue_yoy"]],
        on="stock_id",
        how="left"
    )

    base = base[base["close"].notna()]
    base = base[base["month_revenue_yoy"].notna()]

    base["turnover_20d"] = base["turnover"]
    base["is_liquid"] = base["turnover_20d"] >= 20_000_000

    candidates = base[
        (base["month_revenue_yoy"] > 10)
        & (base["close"] > 0)
        & base["is_liquid"]
    ].copy()

    candidates["score"] = (
        (candidates["month_revenue_yoy"] > 0).astype(int) * 2
        + (candidates["month_revenue_yoy"] > 10).astype(int) * 2
        + (candidates["close"] > 0).astype(int) * 1
        + (candidates["is_liquid"]).astype(int) * 1
    )

    candidates = candidates.sort_values(
        ["score", "month_revenue_yoy", "turnover"],
        ascending=[False, False, False]
    ).head(50)

    # ==============================
    # E 區：法人布局
    # ==============================

    e_rows = []

    latest_inst_date = inst["date"].max() if not inst.empty else TODAY

    for _, row in candidates.iterrows():
        sid = row["stock_id"]

        hist = inst_hist[inst_hist["stock_id"] == sid].copy()
        hist = hist.sort_values("date")

        trust_last3 = hist.tail(3)["trust_net"].tolist()
        foreign_last5 = hist.tail(5)["foreign_net"].tolist()

        trust_buy3 = (
            len(trust_last3) >= 3
            and all(x > 0 for x in trust_last3)
        )

        trust_sell3 = (
            len(trust_last3) >= 3
            and all(x < 0 for x in trust_last3)
        )

        foreign_5d_net = sum(foreign_last5) if foreign_last5 else 0
        foreign_buy5 = foreign_5d_net > 0
        foreign_sell5 = foreign_5d_net < 0

        close = row["close"]
        ma60 = close
        trend_ok = True
        distance_ma60 = 0

        score = 0

        if row["month_revenue_yoy"] > 0:
            score += 2
        if row["month_revenue_yoy"] > 10:
            score += 2
        if trend_ok:
            score += 2
        if distance_ma60 <= 15:
            score += 1
        if trust_buy3:
            score += 3
        if foreign_buy5:
            score += 1
        if trust_sell3:
            score -= 3
        if foreign_sell5:
            score -= 1

        if trust_buy3 and score >= 11:
            signal = "🟢 投信布局綠燈"
            action = "列入研究清單；確認產品、客戶、產業趨勢後，可考慮分批小部位。"
        elif foreign_buy5 and score >= 7 and not trust_buy3:
            signal = "🟡 外資流入黃燈"
            action = "放入自選，等待投信跟進、回測季線或下一次營收確認。"
        elif trust_sell3 or foreign_sell5 or row["month_revenue_yoy"] < 0:
            signal = "🔴 資金／趨勢紅燈"
            action = "若持有，檢查持股理由、收緊停利或減碼；若尚未持有，不要因跌深而接刀。"
        else:
            signal = "⚪ 觀察"
            action = "尚未形成明確法人布局訊號。"

        e_rows.append({
            "signal": signal,
            "stock_id": sid,
            "stock_name": row["stock_name"],
            "close": close,
            "month_revenue_yoy": row["month_revenue_yoy"],
            "trust_buy3": trust_buy3,
            "trust_sell3": trust_sell3,
            "foreign_5d_net": foreign_5d_net,
            "foreign_buy5": foreign_buy5,
            "foreign_sell5": foreign_sell5,
            "score": score,
            "action": action,
            "date": TODAY,
        })

    e_df = pd.DataFrame(e_rows)

    # ==============================
    # 輸出 CSV
    # ==============================

    theme_df.to_csv(DAILY_CSV, index=False, encoding="utf-8-sig")

    print(f"輸出完成：{DAILY_CSV}")
    print(f"法人資料：{INST_CSV}")
    print(f"月營收資料：{REV_CSV}")

    # ==============================
    # Email
    # ==============================

    def df_to_text(df, max_rows=30):
        if df.empty:
            return "（今日無符合條件股票）"
        return df.head(max_rows).to_string(index=False)

    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL
    msg["Subject"] = f"台股題材與法人布局雷達 {TODAY}"

    body = f"""
台股每日題材與法人布局雷達
日期：{TODAY}

【A. 熱門題材主題股】
{df_to_text(theme_df, 30)}

【B. 熱門題材延伸股】
{df_to_text(extended_df, 30)}

【C. 全市場基本面加速雷達】
{df_to_text(candidates, 30)}

【E. 基本面加速＋法人布局雷達】
🟢 投信布局綠燈：投信連買 3 日＋基本面／價格條件通過
🟡 外資流入黃燈：外資 5 日累計買超＋基本面／價格條件通過
🔴 資金／趨勢紅燈：法人轉弱、營收轉弱或趨勢破壞

{df_to_text(e_df, 50)}

免責聲明：
本信僅為資料整理與研究輔助，非投資建議。
"""

    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)

    print("Email sent.")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        error = traceback.format_exc()
        print(error)

        if all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]):
            msg = MIMEMultipart()
            msg["From"] = GMAIL_USER
            msg["To"] = RECIPIENT_EMAIL
            msg["Subject"] = f"台股雷達錯誤通知 {TODAY}"
            msg.attach(MIMEText(error, "plain", "utf-8"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.send_message(msg)
