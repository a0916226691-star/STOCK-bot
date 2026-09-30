# -*- coding: utf-8 -*-
"""
台股主動資金雷達 v5.5 (雲端強固 + Google快訊整合版)
整合內容：
- v5.3 原始邏輯保留
- v5.4 反封鎖 Session + 雙源備援 + TPEx法人 + 交易日過濾
- v5.5 新增 Gmail IMAP 抓取 Google 快訊，整合進 8 大核心健檢
需求: pip install pandas numpy requests beautifulsoup4 lxml
"""

import os
import smtplib
import time
import random
import re
import traceback
import imaplib
import email
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import defaultdict

import numpy as np
import pandas as pd
import requests

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

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
MAX_HEDGE_5D_VOLUME_RATIO = 0.03
MAX_HISTORY_FILES = 90
HIGH_VOLATILITY_STOCKS = {"6919", "4763"}

# ==========================================================
# 1. 核心關注清單、題材與排除清單
# ==========================================================
FOCUS_STOCKS = ["2383","2368","6197","3293","4763","1808","6919","1503"]

FOCUS_PROFILES = {
    "2383": {"name": "台光電", "theme": "AI伺服器／高速CCL", "valuation": "合理偏高（高成長支撐）", "good_catalyst": "高速材料需求與產品組合", "risk_catalyst": "銅箔、玻纖布等原料成本波動", "rating": "持續研究"},
    "2368": {"name": "金像電", "theme": "AI伺服器／交換器PCB", "valuation": "合理區間", "good_catalyst": "高階伺服器與交換器PCB需求", "risk_catalyst": "產能擴充與客戶拉貨節奏", "rating": "持續研究"},
    "6197": {"name": "佳必琪", "theme": "AI高速傳輸線束", "valuation": "成長型估值", "good_catalyst": "高速線纜與伺服器需求", "risk_catalyst": "伺服器出貨節奏與競爭壓力", "rating": "持續研究"},
    "3293": {"name": "鈊象", "theme": "網路遊戲／授權", "valuation": "偏高，須持續追蹤", "good_catalyst": "海外授權與產品營運", "risk_catalyst": "海外法規與評價修正", "rating": "持續研究"},
    "4763": {"name": "材料-KY", "theme": "材料／絲束", "valuation": "需觀察成長持續性", "good_catalyst": "供需與擴產效益", "risk_catalyst": "營收趨勢、供需反轉與評價變化", "rating": "持續研究"},
    "1808": {"name": "潤隆", "theme": "營建／高股息", "valuation": "資產與現金流導向", "good_catalyst": "完工認列、現金流與股利政策", "risk_catalyst": "工程進度、政策與房市景氣", "rating": "持續研究"},
    "6919": {"name": "康霈", "theme": "生技新藥", "valuation": "高不確定性題材估值", "good_catalyst": "臨床、授權與研發進展", "risk_catalyst": "臨床結果與資金需求風險", "rating": "高風險研究"},
    "1503": {"name": "士電", "theme": "重電／變壓器／綠能", "valuation": "成長型估值", "good_catalyst": "電網投資、外銷訂單與產能", "risk_catalyst": "原物料、交期與評價修正", "rating": "持續研究"},
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

EXCLUDE_TOOL_STOCKS = {"2330","2454","2308","3711","2881","2882","2884","2886","2891","2892","2880","0050","0056","00878","006208","00919","00929"}

# ==========================================================
# 2. 共用強固防呆函式
# ==========================================================
def now_tw(): return datetime.now(TZ)
def today_str(): return now_tw().strftime("%Y-%m-%d")
def normalize_stock_id(value):
    text = str(value).strip()
    if text.endswith(".0"): text = text[:-2]
    return text.zfill(4) if text.isdigit() and len(text) < 4 else text

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

def get_recent_trading_dates(days=20):
    dates, cur = [], now_tw()
    while len(dates) < days:
        if cur.weekday() < 5:
            dates.append(cur.strftime("%Y%m%d"))
        cur -= timedelta(days=1)
    return dates

def request_get(url, params=None, timeout=30):
    last_error = None
    for attempt in range(4):
        try:
            response = SESSION.get(url, params=params, timeout=timeout)
            if response.status_code in (403, 429):
                wait = 30 + random.uniform(1,5)
                print(f"⚠️ 被風控 {response.status_code}，等待 {wait:.1f} 秒後重試...")
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response
        except Exception as error:
            last_error = error
            wait_seconds = (2 ** attempt) + random.uniform(0,1)
            print(f"連線警告：{url} 失敗，第 {attempt+1} 次重試前等待 {wait_seconds:.1f} 秒：{error}")
            time.sleep(wait_seconds)
    raise RuntimeError(f"連線失敗：{url}｜最後錯誤：{last_error}")

def request_json(url, params=None, timeout=30):
    response = request_get(url, params=params, timeout=timeout)
    if not response.text or len(response.text.strip()) < 10:
        raise RuntimeError(f"API 回傳內容完全空白：{url}")
    if response.text.strip().startswith("<"):
        print(f"❌ API 回傳 HTML 非 JSON，疑似被擋！網址：{url} 預覽：{response.text[:200]}")
        raise RuntimeError("回傳 HTML，可能被防火牆阻擋")
    try:
        return response.json()
    except Exception as e:
        print(f"❌ JSON 解析失敗！網址：{url} 預覽：{response.text[:300]}")
        raise RuntimeError(f"JSON 解析失敗: {e}")

def cleanup_old_files(prefix):
    try:
        files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith(prefix) and f.endswith(".csv")])
        if len(files) > MAX_HISTORY_FILES:
            for f in files[:-MAX_HISTORY_FILES]:
                os.remove(os.path.join(OUTPUT_DIR, f))
    except Exception: pass

# ==========================================================
# 3. 行情（雙源備援）
# ==========================================================
def _parse_twse_openapi(data):
    rows=[]
    for item in data:
        stock_id = normalize_stock_id(item.get("Code", ""))
        close = safe_float(item.get("ClosingPrice"))
        if not stock_id.isdigit() or len(stock_id)!=4 or pd.isna(close): continue
        rows.append({
            "stock_id": stock_id,
            "stock_name": str(item.get("Name","")).strip(),
            "market": "TWSE",
            "close": close,
            "change": safe_float(item.get("Change")),
            "volume": safe_int(item.get("TradeVolume")),
            "turnover": safe_float(item.get("TradeValue")),
        })
    return pd.DataFrame(rows)

def get_twse_quotes_primary():
    print("取得 TWSE 上市行情 (主源)...")
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    data = request_json(url)
    df = _parse_twse_openapi(data)
    if df.empty: raise RuntimeError("TWSE 主源解析後為空")
    print(f"TWSE 主源完成：{len(df)} 檔")
    return df

def get_twse_quotes_backup():
    print("取得 TWSE 上市行情 (備援 open_data)...")
    url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=open_data"
    data = request_json(url)
    df = _parse_twse_openapi(data)
    if df.empty: raise RuntimeError("TWSE 備援解析後為空")
    print(f"TWSE 備援完成：{len(df)} 檔")
    return df

def get_twse_quotes():
    try: return get_twse_quotes_primary()
    except Exception as e:
        print(f"主源失敗 {e}，切備援")
        return get_twse_quotes_backup()

def get_tpex_quotes():
    print("取得 TPEx 上櫃行情...")
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    data = request_json(url)
    rows=[]
    for item in data:
        stock_id = normalize_stock_id(item.get("SecuritiesCompanyCode") or item.get("SecuritiesCode") or item.get("Code") or "")
        close = safe_float(item.get("Close") or item.get("ClosingPrice"))
        if not stock_id.isdigit() or len(stock_id)!=4 or pd.isna(close): continue
        rows.append({
            "stock_id": stock_id,
            "stock_name": str(item.get("CompanyName") or item.get("SecuritiesName") or "").strip(),
            "market": "TPEx",
            "close": close,
            "change": safe_float(item.get("Change")),
            "volume": safe_int(item.get("Volume")),
            "turnover": safe_float(item.get("Amount")),
        })
    df = pd.DataFrame(rows)
    if df.empty: raise RuntimeError("TPEx 行情解析後為空")
    print(f"TPEx 行情完成：{len(df)} 檔")
    return df

def get_all_quotes():
    frames=[]
    for func in [get_twse_quotes, get_tpex_quotes]:
        try:
            df = func()
            if not df.empty: frames.append(df)
        except Exception as error:
            print(f"⚠️ 行情取得異常: {error}")
    if not frames: raise RuntimeError("❌ 上市與上櫃行情皆無法取得")
    quotes = pd.concat(frames, ignore_index=True)
    quotes["stock_id"] = quotes["stock_id"].map(normalize_stock_id)
    return quotes.drop_duplicates(subset=["stock_id"], keep="first")

# ==========================================================
# 4. 法人資料 (上市+上櫃)
# ==========================================================
def get_twse_institutional():
    print("取得 TWSE 法人資料...")
    url = "https://www.twse.com.tw/rwd/zh/fund/T86"
    data, used_date = None, None
    for date_code in get_recent_trading_dates(20):
        try:
            time.sleep(0.8)
            result = request_json(url, params={"response": "json", "date": date_code, "selectType": "ALLBUT0999"}, timeout=20)
            if result.get("stat") == "OK" and result.get("data"):
                data, used_date = result, date_code
                break
        except Exception: continue
    if data is None: raise RuntimeError("找不到可用的 TWSE 法人資料")
    fields, raw_rows = data.get("fields", []), data.get("data", [])
    hedge_col = next((c for c in fields if "自營商買賣超股數" in c and "避險" in c), None)
    prop_col = next((c for c in fields if "自營商買賣超股數" in c and "自行買賣" in c), None)
    fmt_date = f"{used_date[:4]}-{used_date[4:6]}-{used_date[6:]}"
    rows=[]
    for r in raw_rows:
        item=dict(zip(fields,r))
        sid=normalize_stock_id(item.get("證券代號",""))
        if not sid.isdigit() or len(sid)!=4: continue
        rows.append({
            "stock_id": sid, "date": fmt_date,
            "foreign_net": safe_int(item.get("外陸資買賣超股數(不含外資自營商)")),
            "trust_net": safe_int(item.get("投信買賣超股數")),
            "dealer_proprietary_net": safe_int(item.get(prop_col)) if prop_col else np.nan,
            "dealer_hedge_net": safe_int(item.get(hedge_col)) if hedge_col else np.nan,
            "hedge_data_available": hedge_col is not None
        })
    df=pd.DataFrame(rows)
    for c in ["foreign_net","trust_net","dealer_proprietary_net","dealer_hedge_net"]:
        df[c]=pd.to_numeric(df[c], errors="coerce")
    print(f"TWSE 法人完成：{len(df)} 檔，日期：{fmt_date}")
    return df

def get_tpex_institutional():
    print("取得 TPEx 上櫃法人資料...")
    rows=[]
    for date_code in get_recent_trading_dates(10):
        try:
            time.sleep(0.8)
            roc_year = int(date_code[:4]) - 1911
            roc_date = f"{roc_year}/{date_code[4:6]}/{date_code[6:]}"
            url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
            result = request_json(url, params={"l":"zh-tw","o":"json","se":"AL","t":"D","d":roc_date}, timeout=20)
            if not result.get("aaData"): continue
            for r in result["aaData"]:
                sid = normalize_stock_id(r[0])
                if not sid.isdigit() or len(sid)!=4: continue
                rows.append({
                    "stock_id": sid,
                    "date": f"{date_code[:4]}-{date_code[4:6]}-{date_code[6:]}",
                    "foreign_net": safe_int(r[2]),
                    "trust_net": safe_int(r[3]),
                    "dealer_hedge_net": safe_int(r[5]) if len(r)>5 else np.nan,
                    "hedge_data_available": True
                })
            if rows: break
        except Exception as e:
            print(f"TPEx {date_code} 失敗 {e}")
            continue
    if not rows:
        print("⚠️ TPEx 法人無資料，回傳空表")
        return pd.DataFrame()
    df=pd.DataFrame(rows)
    print(f"TPEx 法人完成：{len(df)} 檔")
    return df

def get_all_institutional():
    frames=[]
    try: frames.append(get_twse_institutional())
    except Exception as e: print(f"TWSE法人失敗 {e}")
    try:
        tpex = get_tpex_institutional()
        if not tpex.empty: frames.append(tpex)
    except Exception as e: print(f"TPEx法人失敗 {e}")
    if not frames: return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["stock_id","date"], keep="last")

# ==========================================================
# 5. 歷史與特徵
# ==========================================================
def load_history(prefix):
    filenames = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith(prefix) and f.endswith(".csv")])[-MAX_HISTORY_FILES:]
    frames=[]
    for fn in filenames:
        try:
            frame=pd.read_csv(os.path.join(OUTPUT_DIR, fn), dtype={"stock_id": str})
            if not frame.empty:
                frame["stock_id"]=frame["stock_id"].map(normalize_stock_id)
                frames.append(frame)
        except Exception: pass
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

def save_today_history(quotes, institutional, date_text):
    quotes.copy().assign(date=date_text).to_csv(os.path.join(OUTPUT_DIR, f"layout_price_history_{date_text}.csv"), index=False, encoding="utf-8-sig")
    if institutional is not None and not institutional.empty:
        institutional.to_csv(os.path.join(OUTPUT_DIR, f"layout_institutional_history_{date_text}.csv"), index=False, encoding="utf-8-sig")
    cleanup_old_files("layout_price_history_")
    cleanup_old_files("layout_institutional_history_")
    cleanup_old_files("layout_radar_")

def make_price_features(quotes, history, date_text):
    needed=["stock_id","stock_name","market","close","volume","turnover","date"]
    today=quotes.copy(); today["date"]=date_text
    full=today[needed].copy() if history is None or history.empty else pd.concat([history[needed], today[needed]], ignore_index=True)
    full["stock_id"]=full["stock_id"].map(normalize_stock_id)
    for col in ["close","volume","turnover"]: full[col]=pd.to_numeric(full[col], errors="coerce")
    full=full.drop_duplicates(subset=["stock_id","date"], keep="last").sort_values(["stock_id","date"])
    grouped=full.groupby("stock_id", group_keys=False)
    full["ma10"]=grouped["close"].transform(lambda x: x.rolling(10, min_periods=10).mean())
    full["ma20"]=grouped["close"].transform(lambda x: x.rolling(20, min_periods=20).mean())
    full["close_5d_ago"]=grouped["close"].transform(lambda x: x.shift(4))
    full["return_5d_pct"]=(full["close"]/full["close_5d_ago"]-1)*100
    full["high_10d"]=grouped["close"].transform(lambda x: x.rolling(10, min_periods=10).max())
    full["low_10d"]=grouped["close"].transform(lambda x: x.rolling(10, min_periods=10).min())
    full["range_10d_pct"]=(full["high_10d"]/full["low_10d"]-1)*100
    full["avg_volume_5d"]=grouped["volume"].transform(lambda x: x.shift(1).rolling(5, min_periods=5).mean())
    full["volume_ratio_5d"]=full["volume"]/full["avg_volume_5d"]
    full["distance_ma10_pct"]=(full["close"]/full["ma10"]-1)*100
    full["distance_ma20_pct"]=(full["close"]/full["ma20"]-1)*100
    return full.loc[full["date"]==date_text, ["stock_id","ma10","ma20","return_5d_pct","range_10d_pct","avg_volume_5d","volume_ratio_5d","distance_ma10_pct","distance_ma20_pct"]].copy()

def make_institutional_features(history, price_features):
    output_columns=["stock_id","trust_buy_days_5","trust_5d_net","foreign_buy_days_5","foreign_5d_net","dealer_hedge_5d_abs","trust_volume_ratio","foreign_volume_ratio","hedge_volume_ratio","hedge_data_status","hedge_dominant","trust_accumulation","foreign_support","trust_20d_net","foreign_20d_net","trust_buy_days_20","foreign_buy_days_20","institutional_days_20","midterm_inflow_to_verify"]
    if history is None or history.empty: return pd.DataFrame(columns=output_columns)
    df=history.copy()
    df["stock_id"]=df["stock_id"].map(normalize_stock_id)
    df["date"]=df["date"].astype(str)
    for col in ["foreign_net","trust_net","dealer_hedge_net"]:
        if col not in df.columns: df[col]=np.nan
        df[col]=pd.to_numeric(df[col], errors="coerce")
    if "hedge_data_available" not in df.columns: df["hedge_data_available"]=df["dealer_hedge_net"].notna()
    df["hedge_data_available"]=df["hedge_data_available"].fillna(False).astype(bool)
    df["foreign_net"]=df["foreign_net"].fillna(0); df["trust_net"]=df["trust_net"].fillna(0)
    df=df.drop_duplicates(subset=["stock_id","date"], keep="last").sort_values(["stock_id","date"])
    volume_map=price_features.set_index("stock_id")["avg_volume_5d"].to_dict() if price_features is not None and not price_features.empty else {}
    rows=[]
    for stock_id, group in df.groupby("stock_id"):
        group=group.sort_values("date"); g5=group.tail(5); g20=group.tail(20)
        trust_5d=float(g5["trust_net"].sum()); foreign_5d=float(g5["foreign_net"].sum())
        trust_20d=float(g20["trust_net"].sum()); foreign_20d=float(g20["foreign_net"].sum())
        trust_buy_5=int((g5["trust_net"]>0).sum()); foreign_buy_5=int((g5["foreign_net"]>0).sum())
        trust_buy_20=int((g20["trust_net"]>0).sum()); inst_days_20=len(g20)
        hedge_available=bool(len(g5)>=5 and g5["hedge_data_available"].all() and g5["dealer_hedge_net"].notna().all())
        hedge_abs=float(g5["dealer_hedge_net"].abs().sum()) if hedge_available else np.nan
        hedge_status="available" if hedge_available else "unknown"
        avg_vol=volume_map.get(stock_id, np.nan)
        if pd.isna(avg_vol) or avg_vol<=0: trust_vr=foreign_vr=hedge_vr=np.nan
        else:
            five=avg_vol*5
            trust_vr=trust_5d/five; foreign_vr=foreign_5d/five
            hedge_vr=hedge_abs/five if hedge_available else np.nan
        hedge_dom=False
        if hedge_available:
            if not pd.isna(hedge_vr) and hedge_vr>=MAX_HEDGE_5D_VOLUME_RATIO: hedge_dom=True
            if hedge_abs>=abs(trust_5d)+abs(foreign_5d) and hedge_abs>0: hedge_dom=True
        trust_acc=(len(g5)>=5 and trust_buy_5>=3 and trust_5d>0 and (pd.isna(trust_vr) or trust_vr>=MIN_TRUST_5D_VOLUME_RATIO))
        foreign_sup=(len(g5)>=5 and foreign_buy_5>=3 and foreign_5d>0 and (pd.isna(foreign_vr) or foreign_vr>=MIN_FOREIGN_5D_VOLUME_RATIO))
        midterm=(inst_days_20>=10 and trust_buy_20>=max(5, int(inst_days_20*0.4)) and trust_20d>0)
        rows.append({"stock_id":stock_id,"trust_buy_days_5":trust_buy_5,"trust_5d_net":trust_5d,"foreign_buy_days_5":foreign_buy_5,"foreign_5d_net":foreign_5d,"dealer_hedge_5d_abs":hedge_abs,"trust_volume_ratio":trust_vr,"foreign_volume_ratio":foreign_vr,"hedge_volume_ratio":hedge_vr,"hedge_data_status":hedge_status,"hedge_dominant":hedge_dom,"trust_accumulation":trust_acc,"foreign_support":foreign_sup,"trust_20d_net":trust_20d,"foreign_20d_net":foreign_20d,"trust_buy_days_20":trust_buy_20,"foreign_buy_days_20":foreign_buy_5,"institutional_days_20":inst_days_20,"midterm_inflow_to_verify":midterm})
    return pd.DataFrame(rows, columns=output_columns)

# ==========================================================
# 6. Google 快訊整合 (v5.5 新增)
# ==========================================================
# v5.5.1 修補：只替換 fetch_google_alerts 函式即可
import imaplib
import email
from collections import defaultdict
import re

def fetch_google_alerts(days=3, max_mails=10):
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD]):
        print("未設定 Gmail，跳過 Google 快訊")
        return {}
    alerts_by_stock = defaultdict(list)
    risk_keywords = ["資安","入侵","重訊","重大","減資","違約","處置","警示","下市","搜索","檢調","火災","裁罰"]

    try:
        print(f"連線 Gmail IMAP: {GMAIL_USER}")
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("INBOX")

        # 改用更寬鬆的搜尋：先抓全部，再手動過濾，避免 SINCE 語系問題
        # 先試精準搜尋，失敗就 fallback 到全部
        search_queries = [
            '(FROM "googlealerts-noreply@google.com")',
            'FROM "googlealerts"',
            'SUBJECT "Google"',
            'SUBJECT "快訊"',
        ]
        mail_ids = []
        for q in search_queries:
            try:
                status, data = mail.search(None, q)
                if status == "OK" and data[0]:
                    mail_ids = data[0].split()
                    if mail_ids:
                        print(f"用條件 {q} 找到 {len(mail_ids)} 封")
                        break
            except Exception as e:
                print(f"搜尋 {q} 失敗 {e}")
                continue

        if not mail_ids:
            print("完全找不到 Google 快訊郵件，請確認 Gmail 有收到")
            mail.logout()
            return {}

        # 只取最近 10 封，避免抓太多
        mail_ids = mail_ids[-max_mails:]
        print(f"開始解析最近 {len(mail_ids)} 封...")

        for mid in reversed(mail_ids):
            _, msg_data = mail.fetch(mid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject",""))))
            print(f"解析郵件: {subject[:60]}")

            body_html = ""
            body_text = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    try:
                        payload = part.get_payload(decode=True)
                        if not payload: continue
                        charset = part.get_content_charset() or "utf-8"
                        decoded = payload.decode(charset, errors="ignore")
                        if ctype == "text/html":
                            body_html = decoded
                        elif ctype == "text/plain":
                            body_text = decoded
                    except:
                        continue
            else:
                try:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body_html = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
                except:
                    body_html = str(msg.get_payload())

            combined = body_html + chr(10) + body_text

            if not combined:
                continue

            # Debug: 印前 300 字看格式
            # print(f"內文預覽: {combined[:300]}")

            links = []
            if HAS_BS4:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(combined, "html.parser")
                for a in soup.find_all("a", href=True):
                    title = a.get_text(strip=True)
                    href = a["href"]
                    # 放寬過濾：只要不是取消訂閱都留
                    if len(title) < 6: continue
                    if "取消訂閱" in title or "Unsubscribe" in title: continue
                    if "google.com/alerts/feeds" in href: continue
                    # Google 會包一層 google.com/url?q=實際網址，要解開
                    if "google.com/url" in href:
                        m = re.search(r"[?&]q=([^&]+)", href)
                        if m:
                            import urllib.parse
                            href = urllib.parse.unquote(m.group(1))
                    links.append((title, href))
            else:
                for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{6,})</a>', combined):
                    href, title = m.group(1), m.group(2).strip()
                    links.append((title, href))

            print(f"  -> 抓到 {len(links)} 個連結")

            for title, href in links:
                for sid in FOCUS_STOCKS:
                    sname = FOCUS_PROFILES.get(sid, {}).get("name","")
                    if sid in title or (sname and sname in title):
                        level = "高風險" if any(k in title for k in risk_keywords) else "中性"
                        alerts_by_stock[sid].append({"title": title, "link": href, "level": level})
                        print(f"    命中 {sid} {sname}: {title[:40]}")

        mail.logout()
    except Exception as e:
        print(f"⚠️ Google 快訊抓取失敗: {e}")
        traceback.print_exc()
        return {}

    # 去重
    for sid in list(alerts_by_stock.keys()):
        seen=set(); uniq=[]
        for item in alerts_by_stock[sid]:
            if item["title"] not in seen:
                uniq.append(item); seen.add(item["title"])
        alerts_by_stock[sid]=uniq[:5]

    if not alerts_by_stock:
        print("⚠️ 解析完成但沒有命中 8 大核心股，可能是快訊關鍵字不是代號+名稱")
        print("建議：去 google.com/alerts 把關鍵字改成 \"3293 鈊象\" 這種格式")
    
    print(f"Google 快訊分類完成：{ {k:len(v) for k,v in alerts_by_stock.items()} }")
    return alerts_by_stock

# ==========================================================
# 7. 分類與雷達
# ==========================================================
def classify_stock(row):
    stock_id=row["stock_id"]
    if stock_id in EXCLUDE_TOOL_STOCKS: return "⚪ 排除：權值／ETF／工具股","高權值或工具型流量，不納入雷達。",-5
    if bool(row.get("hedge_dominant", False)): return "🟠 排除：避險流量主導","自營商避險流量偏高，不當作方向性買盤。",-4
    return_5d=row.get("return_5d_pct", np.nan); range_10d=row.get("range_10d_pct", np.nan); volume_ratio=row.get("volume_ratio_5d", np.nan); distance_ma10=row.get("distance_ma10_pct", np.nan); distance_ma20=row.get("distance_ma20_pct", np.nan)
    trust_5d_net=float(row.get("trust_5d_net",0) or 0); foreign_5d_net=float(row.get("foreign_5d_net",0) or 0); trust_buy_days_5=int(row.get("trust_buy_days_5",0) or 0)
    trust_accumulation=bool(row.get("trust_accumulation",False)); foreign_support=bool(row.get("foreign_support",False)); midterm_inflow=bool(row.get("midterm_inflow_to_verify",False))
    turnover=safe_float(row.get("turnover", np.nan)); liquid=not pd.isna(turnover) and turnover>=MIN_DAILY_TURNOVER
    overheat_limit = 18 if stock_id in HIGH_VOLATILITY_STOCKS else 10
    if not pd.isna(return_5d) and return_5d > overheat_limit: return "🔴 排除：拉高／過熱",f"5日漲幅超過{overheat_limit}%，不符合盤整吸籌。",-3
    if not pd.isna(volume_ratio) and volume_ratio>2.0 and not pd.isna(return_5d) and return_5d>3: return "🔴 排除：拉高／過熱","爆量且股價走強，不符合盤整吸籌。",-3
    if trust_5d_net<0 and foreign_5d_net<0 and trust_buy_days_5==0: return "🔴 排除：法人轉賣","投信與外資近5日同步偏賣。",-3
    if not pd.isna(distance_ma20) and distance_ma20<0 and (trust_accumulation or foreign_support or midterm_inflow): return "🟡 籌碼尚在、價格轉弱","跌破MA20月線；等待重新站回，不加碼。",2
    if not pd.isna(distance_ma10) and distance_ma10<0 and trust_5d_net<0: return "🟡 籌碼鬆動, 跌破MA10","股價跌破MA10，且投信近5日偏賣。",1
    price_ok=(pd.isna(return_5d) or -7<=return_5d<=6) and (pd.isna(range_10d) or range_10d<=16) and (pd.isna(volume_ratio) or volume_ratio<=2.0) and (pd.isna(distance_ma20) or abs(distance_ma20)<=7)
    midterm_price_ok=(pd.isna(return_5d) or -7<=return_5d<=8) and (pd.isna(range_10d) or range_10d<=20) and (pd.isna(distance_ma20) or abs(distance_ma20)<=8)
    green_price_confirmation=not pd.isna(distance_ma10) and 0<=distance_ma10<=8 and (pd.isna(return_5d) or 0<return_5d<=10)
    if liquid and trust_accumulation and green_price_confirmation: return "🟢 吸籌延續／初步確認","投信持續買超後，股價溫和站上MA10。",9
    if liquid and trust_accumulation and price_ok: return "🔵 主動資金疑似布局","投信5日持續買超、價格仍盤整，未見避險主導。",8
    if liquid and midterm_inflow and midterm_price_ok: return "🟣 中期資金流入待驗證","投信中期買盤具持續性，仍需研究基本面與消息。",7
    if foreign_support and not trust_accumulation: return "⚪ 待驗證：僅外資流入","外資流入未獲投信確認，可能是被動流量。",1
    return "⚪ 不列入","未同時符合投信吸籌與價格型態條件。",0

def build_radar(quotes, price_features, institutional_features):
    df=quotes.copy()
    for feature_df in [price_features, institutional_features]:
        if feature_df is not None and not feature_df.empty:
            df=df.merge(feature_df, on="stock_id", how="left")
    defaults={"ma10":np.nan,"ma20":np.nan,"return_5d_pct":np.nan,"range_10d_pct":np.nan,"avg_volume_5d":np.nan,"volume_ratio_5d":np.nan,"distance_ma10_pct":np.nan,"distance_ma20_pct":np.nan,"trust_buy_days_5":0,"trust_5d_net":0,"foreign_buy_days_5":0,"foreign_5d_net":0,"trust_volume_ratio":np.nan,"foreign_volume_ratio":np.nan,"hedge_data_status":"unknown","hedge_dominant":False,"trust_accumulation":False,"foreign_support":False,"trust_20d_net":0,"foreign_20d_net":0,"trust_buy_days_20":0,"foreign_buy_days_20":0,"institutional_days_20":0,"midterm_inflow_to_verify":False}
    for col, default_val in defaults.items():
        if col not in df.columns: df[col]=default_val
        elif isinstance(default_val, bool): df[col]=df[col].fillna(default_val).astype(bool)
        else: df[col]=df[col].fillna(default_val)
    df["theme"]=df["stock_id"].map(lambda sid: "／".join(THEME_MAP.get(sid, [])))
    df["is_focus_stock"]=df["stock_id"].isin(FOCUS_STOCKS)
    classified=df.apply(classify_stock, axis=1, result_type="expand"); classified.columns=["signal","reason","base_score"]
    df=pd.concat([df, classified], axis=1); df["score"]=df["base_score"]
    df.loc[df["trust_buy_days_5"]>=4, "score"]+=2
    df.loc[df["foreign_support"]==True, "score"]+=1
    signal_order={"🟢 吸籌延續／初步確認":1,"🔵 主動資金疑似布局":2,"🟣 中期資金流入待驗證":3,"🟡 籌碼尚在、價格轉弱":4,"🟡 籌碼鬆動, 跌破MA10":5,"⚪ 待驗證：僅外資流入":6,"🔴 排除：拉高／過熱":7,"🔴 排除：法人轉賣":8,"🟠 排除：避險流量主導":9,"⚪ 排除：權值／ETF／工具股":10,"⚪ 不列入":99}
    df["sort_order"]=df["signal"].map(signal_order).fillna(99)
    return df.sort_values(["sort_order","score","turnover"], ascending=[True, False, False], na_position="last").drop(columns=["sort_order"])

# ==========================================================
# 8. 報告與寄信
# ==========================================================
def fmt_price(v): return "-" if pd.isna(v) else f"{float(v):.2f}"
def fmt_pct(v): return "累積中" if pd.isna(v) else f"{float(v):+.1f}%"
def fmt_ratio(v): return "待驗證" if pd.isna(v) else f"{float(v)*100:.2f}%"
def fmt_shares(v):
    if pd.isna(v): return "-"
    v=int(v)
    return f"{v/1000:+.1f} 張" if abs(v)>=1000 else f"{v:+,} 股"

def make_focus_report(radar, alerts_dict=None):
    if alerts_dict is None: alerts_dict={}
    lines=["="*54, "⭐ 8大核心持股研究健檢 + Google快訊", "="*54]
    for stock_id in FOCUS_STOCKS:
        profile=FOCUS_PROFILES.get(stock_id, {})
        name=profile.get("name", stock_id)
        theme=profile.get("theme","未分類")
        valuation=profile.get("valuation","-")
        good_catalyst=profile.get("good_catalyst","-")
        risk_catalyst=profile.get("risk_catalyst","-")
        rating=profile.get("rating","持續研究")
        match=radar[radar["stock_id"]==stock_id]
        if match.empty:
            lines.append(f"• {stock_id} {name}｜未取得當日行情")
            lines.append("-"*54)
            continue
        row=match.iloc[0]
        lines.append(f"• {stock_id} {name}｜產業：{theme}")
        lines.append(f"  收盤：{fmt_price(row.get('close', np.nan))}｜月線乖離：{fmt_pct(row.get('distance_ma20_pct', np.nan))}")
        lines.append(f"  估值備忘：{valuation}")
        lines.append(f"  題材：{good_catalyst}")
        lines.append(f"  風險：{risk_catalyst}")
        lines.append(f"  即時雷達：{row.get('signal','-')}｜投信5日：{fmt_shares(row.get('trust_5d_net',np.nan))}")
        alerts = alerts_dict.get(stock_id, [])
        if alerts:
            lines.append(f"  📰 Google快訊 [{len(alerts)}則]：")
            for al in alerts:
                icon = "⚠️" if al["level"]=="高風險" else "•"
                lines.append(f"  {icon} [{al['level']}] {al['title'][:50]}")
                if al["level"]=="高風險":
                    lines.append(f"    風控提醒：{al['title'][:40]}... 需確認重訊/資安")
                lines.append(f"    連結：{al['link'][:80]}")
        else:
            lines.append(f"  📰 Google快訊：近2日無新快訊")
        lines.append(f"  研究標籤：{rating}")
        lines.append("-"*54)
    return "\n".join(lines)

def stock_lines(frame, maximum):
    if frame is None or frame.empty: return "（今日無股票）"
    lines=[]
    for _, row in frame.head(maximum).iterrows():
        focus_mark=" ⭐" if bool(row.get("is_focus_stock",False)) else ""
        hedge_text=fmt_ratio(row.get("hedge_volume_ratio",np.nan)) if row.get("hedge_data_status")=="available" else "資料未取得"
        lines.append(f"{row['signal']}{focus_mark}｜{row['stock_id']} {row['stock_name']}｜{row['market']}｜收盤 {fmt_price(row['close'])}｜{row.get('theme','')}")
        lines.append(f"  投信5日 {fmt_shares(row.get('trust_5d_net',np.nan))}（{int(row.get('trust_buy_days_5',0) or 0)}日買）｜投信20日 {fmt_shares(row.get('trust_20d_net',np.nan))}｜外資5日 {fmt_shares(row.get('foreign_5d_net',np.nan))}")
        lines.append(f"  5日 {fmt_pct(row.get('return_5d_pct',np.nan))}｜10日振幅 {fmt_pct(row.get('range_10d_pct',np.nan))}｜距MA20 {fmt_pct(row.get('distance_ma20_pct',np.nan))}｜避險相對量 {hedge_text}")
        lines.append(f"  判定：{row['reason']}\n")
    return "\n".join(lines).rstrip()

def make_email_body(radar, date_text, execution_time, alerts_dict=None):
    focus_report=make_focus_report(radar, alerts_dict)
    purple=radar[radar["signal"]=="🟣 中期資金流入待驗證"]
    blue=radar[radar["signal"]=="🔵 主動資金疑似布局"]
    green=radar[radar["signal"]=="🟢 吸籌延續／初步確認"]
    yellow=radar[radar["signal"].str.startswith("🟡", na=False)]
    red=radar[radar["signal"].str.startswith("🔴", na=False)]
    orange=radar[radar["signal"]=="🟠 排除：避險流量主導"]
    lines=[
        "台股主動資金雷達與風控觀察報告 v5.5",
        f"日期：{date_text}",
        f"執行時間：{execution_time}（台灣時間）",
        "",
        focus_report,
        "",
        "="*54, f"🟢 吸籌延續／初步確認｜{len(green)} 檔", "="*54, stock_lines(green, 10),
        "", "="*54, f"🔵 主動資金疑似布局｜{len(blue)} 檔", "="*54, stock_lines(blue, 10),
        "", "="*54, f"🟣 中期資金流入待驗證｜{len(purple)} 檔", "="*54, stock_lines(purple, 10),
        "", "="*54, f"🟡 轉弱／籌碼鬆動｜{len(yellow)} 檔", "="*54, stock_lines(yellow, 10),
        "", "="*54, f"🔴 排除｜{len(red)} 檔", "="*54, stock_lines(red, 15),
        "", "="*54, f"🟠 避險主導｜{len(orange)} 檔", "="*54, stock_lines(orange, 10),
        "", "提醒：本報告為公開資料研究工具 + Google快訊整合，不構成投資建議。",
    ]
    return "\n".join(lines)

def send_email(subject, body):
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]):
        print("未設定 Gmail Secrets，略過寄信；CSV 仍會正常產生。")
        return
    message=MIMEMultipart(); message["From"]=GMAIL_USER; message["To"]=RECIPIENT_EMAIL; message["Subject"]=subject
    message.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD); server.send_message(message)
    print("Email 寄送完成。")

def main():
    now=now_tw(); date_text=now.strftime("%Y-%m-%d")
    print("="*60); print(f"開始執行台股主動資金雷達 v5.5：{date_text}"); print("="*60)
    try:
        quotes=get_all_quotes(); print(f"成功取得行情：{len(quotes)} 檔。")
    except Exception as error:
        print(f"❌ 無法取得任何市場行情，終止本次執行: {error}"); raise
    try:
        institutional_today=get_all_institutional()
    except Exception as error:
        print(f"⚠️ 法人資料取得失敗，今天略過法人計算：{error}"); institutional_today=pd.DataFrame()

    # Google 快訊 (v5.5 新增)
    try:
        alerts_dict = fetch_google_alerts(days=2, max_mails=5)
    except Exception as e:
        print(f"快訊模組異常 {e}"); alerts_dict={}

    price_history=load_history("layout_price_history_")
    institutional_history=load_history("layout_institutional_history_")
    if not institutional_today.empty:
        institutional_history=pd.concat([institutional_history, institutional_today], ignore_index=True)

    price_features=make_price_features(quotes, price_history, date_text)
    institutional_features=make_institutional_features(institutional_history, price_features)
    radar=build_radar(quotes, price_features, institutional_features)

    save_today_history(quotes, institutional_today, date_text)
    radar_path=os.path.join(OUTPUT_DIR, f"layout_radar_{date_text}.csv")
    radar.to_csv(radar_path, index=False, encoding="utf-8-sig")
    print(f"雷達 CSV 已輸出：{radar_path}")

    body=make_email_body(radar, date_text, now.strftime("%Y-%m-%d %H:%M"), alerts_dict)
    print("\n"+"="*60); print(body); print("="*60+"\n")
    send_email(f"主動資金雷達 v5.5｜{date_text}", body)
    print("執行完成。")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        error_text=traceback.format_exc()
        print("\n程式發生錯誤："); print(error_text)
        try: send_email(f"【錯誤】主動資金雷達 v5.5｜{today_str()}", error_text)
        except Exception: pass
        raise
