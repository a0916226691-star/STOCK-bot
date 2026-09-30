# -*- coding: utf-8 -*-
"""
台股主動資金雷達 v6.3 三區整合+歷史備援修正版
1區：持有8大 / 2區：抓到的長線🟣 / 3區：抓到的短線🟢🔵
修正：上市上櫃皆失敗時改用歷史備援，不再 exit 1
"""
import os, smtplib, time, random, re, html, traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from collections import defaultdict
import numpy as np, pandas as pd, requests

TZ = timezone(timedelta(hours=8))
OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "Accept": "application/json, text/plain, */*", "Accept-Language": "zh-TW,zh;q=0.9", "Referer": "https://www.twse.com.tw/zh/page/trading/fund/T86.html"}
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
    "2383": {"name": "台光電", "theme": "AI伺服器／高速CCL", "valuation": "合理偏高", "long_rating": "持續研究"},
    "2368": {"name": "金像電", "theme": "AI伺服器／交換器PCB", "valuation": "合理區間", "long_rating": "持續研究"},
    "6197": {"name": "佳必琪", "theme": "AI高速傳輸線束", "valuation": "成長型估值", "long_rating": "持續研究"},
    "3293": {"name": "鈊象", "theme": "網路遊戲／授權", "valuation": "偏高", "long_rating": "持續研究"},
    "4763": {"name": "材料-KY", "theme": "材料／絲束", "valuation": "需觀察", "long_rating": "持續研究"},
    "1808": {"name": "潤隆", "theme": "營建／高股息", "valuation": "資產導向", "long_rating": "持續研究"},
    "6919": {"name": "康霈", "theme": "生技新藥", "valuation": "高不確定性", "long_rating": "高風險研究"},
    "1503": {"name": "士電", "theme": "重電／變壓器", "valuation": "成長型估值", "long_rating": "持續研究"},
}
THEMES = {"AI伺服器／ODM": ["2317", "2382", "3231", "6669", "6805"], "散熱": ["3017", "3324", "6205", "6131"], "PCB／CCL／載板": ["2383", "2368", "2385", "3037", "4967", "6274", "6197"], "機器人／智慧自動化": ["4588", "1590", "2359", "4566"], "矽光子／CPO／光通訊": ["3163", "3363", "4979", "3450", "6451"], "重電／綠能": ["1513", "1519", "1503", "6873"]}
THEME_MAP = {}
for k, v in THEMES.items():
    for sid in v: THEME_MAP.setdefault(sid, []).append(k)
EXCLUDE_TOOL_STOCKS = {"2330", "2454", "2308", "3711", "2881", "2882", "2884", "2886", "2891", "2892", "2880", "0050", "0056", "00878", "006208", "00919", "00929"}

def now_tw(): return datetime.now(TZ)
def today_str(): return now_tw().strftime("%Y-%m-%d")
def _is_nan(value):
    try: return bool(pd.isna(value))
    except: return value is None
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
    except: return np.nan
def safe_int(value):
    n = safe_float(value); return np.nan if pd.isna(n) else int(n)
def safe_text(value):
    if _is_nan(value): return ""
    return html.escape(str(value).strip(), quote=True)
def sanitize_link(url):
    if not isinstance(url, str): return "#"
    url = url.strip()
    if url.startswith(("https://", "http://")): return html.escape(url, quote=True)
    return "#"
def is_market_trading_day(d):
    if d.weekday() >= 5: return False
    if d.strftime("%Y-%m-%d") in TW_MARKET_HOLIDAYS: return False
    return True
def get_recent_trading_dates(days=20):
    dates = []; cur = now_tw().date()
    while len(dates) < days:
        if is_market_trading_day(cur): dates.append(cur.strftime("%Y%m%d"))
        cur -= timedelta(days=1)
    return dates
def get_last_trading_date(): return get_recent_trading_dates(1)[0]
def request_get(url, params=None, timeout=30):
    last = None
    for i in range(4):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code in (403, 429):
                print(f"⚠️ 被風控 {r.status_code} 等待重試..."); time.sleep(30 + random.uniform(1,5)); continue
            r.raise_for_status(); return r
        except Exception as e:
            last = e; time.sleep((2**i) + random.uniform(0,1))
    raise RuntimeError(f"連線失敗：{url}｜{last}")
def request_json(url, params=None, timeout=30):
    r = request_get(url, params, timeout)
    if not r.text or len(r.text.strip()) < 10: raise RuntimeError("空白")
    if r.text.strip().startswith("<"): raise RuntimeError("回傳HTML被擋")
    return r.json()
def cleanup_old_files(prefix):
    try:
        files = sorted(f for f in os.listdir(OUTPUT_DIR) if f.startswith(prefix) and f.endswith(".csv"))
        if len(files) > MAX_HISTORY_FILES:
            for f in files[:-MAX_HISTORY_FILES]: os.remove(os.path.join(OUTPUT_DIR, f))
    except: pass

def _parse_twse_openapi(data):
    rows = []
    for item in data:
        sid = normalize_stock_id(item.get("Code", "")); close = safe_float(item.get("ClosingPrice"))
        if not sid.isdigit() or len(sid)!= 4 or pd.isna(close): continue
        rows.append({"stock_id": sid, "stock_name": str(item.get("Name", "")).strip(), "market": "TWSE", "close": close, "change": safe_float(item.get("Change")), "volume": safe_int(item.get("TradeVolume")), "turnover": safe_float(item.get("TradeValue"))})
    return pd.DataFrame(rows)

def get_twse_quotes_primary():
    print("取得 TWSE 上市行情（主源 openapi）...")
    data = request_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    df = _parse_twse_openapi(data)
    if df.empty: raise RuntimeError("TWSE主源空")
    print(f"TWSE 主源完成：{len(df)} 檔"); return df

def get_tpex_quotes():
    print("取得 TPEx 上櫃行情...")
    data = request_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
    rows = []
    for item in data:
        sid = normalize_stock_id(item.get("SecuritiesCompanyCode") or item.get("SecuritiesCode") or "")
        close = safe_float(item.get("Close") or item.get("ClosingPrice"))
        if not sid.isdigit() or len(sid)!= 4 or pd.isna(close): continue
        rows.append({"stock_id": sid, "stock_name": str(item.get("CompanyName") or item.get("SecuritiesName") or "").strip(), "market": "TPEx", "close": close, "change": safe_float(item.get("Change")), "volume": safe_int(item.get("Volume")), "turnover": safe_float(item.get("Amount"))})
    df = pd.DataFrame(rows)
    if df.empty: raise RuntimeError("TPEx空")
    print(f"TPEx 完成：{len(df)} 檔"); return df

def get_latest_history_as_quotes():
    try:
        files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("layout_price_history_") and f.endswith(".csv")])
        if not files: return None
        latest = os.path.join(OUTPUT_DIR, files[-1])
        print(f"⚠️ 即時全失敗，啟用歷史備援：{latest}")
        df = pd.read_csv(latest, dtype={"stock_id": str})
        df["stock_id"] = df["stock_id"].map(normalize_stock_id)
        for col in ["stock_name", "market", "close", "volume", "turnover"]:
            if col not in df.columns: df[col] = np.nan
        return df[["stock_id", "stock_name", "market", "close", "volume", "turnover"]].drop_duplicates(subset=["stock_id"])
    except Exception as e:
        print(f"歷史備援失敗：{e}"); return None

def get_all_quotes():
    frames = []; errors = []
    for fn in [get_twse_quotes_primary, get_tpex_quotes]:
        try:
            df = fn()
            if df is not None and not df.empty: frames.append(df)
        except Exception as e:
            msg = f"{fn.__name__} 失敗：{e}"
            print(msg); print(traceback.format_exc()); errors.append(msg)
    if frames:
        q = pd.concat(frames, ignore_index=True)
        q["stock_id"] = q["stock_id"].map(normalize_stock_id)
        return q.drop_duplicates(subset=["stock_id"], keep="first")
    fb = get_latest_history_as_quotes()
    if fb is not None and not fb.empty:
        print(f"✅ 使用歷史備援 {len(fb)} 檔繼續跑，不中斷")
        return fb
    raise RuntimeError(f"上市上櫃皆失敗：{' | '.join(errors)}")

def get_twse_institutional():
    url = "https://www.twse.com.tw/rwd/zh/fund/T86"; data = None; used = None
    for d in get_recent_trading_dates(20):
        try:
            time.sleep(0.8); r = request_json(url, params={"response": "json", "date": d, "selectType": "ALLBUT0999"}, timeout=20)
            if r.get("stat") == "OK" and r.get("data"): data = r; used = d; break
        except: continue
    if data is None: raise RuntimeError("無TWSE法人")
    fields = data.get("fields", []); raw = data.get("data", [])
    hedge_col = next((c for c in fields if "自營商買賣超股數" in c and "避險" in c), None)
    prop_col = next((c for c in fields if "自營商買賣超股數" in c and "自行買賣" in c), None)
    fmt = f"{used[:4]}-{used[4:6]}-{used[6:]}"; rows = []
    for rr in raw:
        item = dict(zip(fields, rr)); sid = normalize_stock_id(item.get("證券代號", ""))
        if not sid.isdigit() or len(sid)!= 4: continue
        rows.append({"stock_id": sid, "date": fmt, "foreign_net": safe_int(item.get("外陸資買賣超股數(不含外資自營商)")), "trust_net": safe_int(item.get("投信買賣超股數")), "dealer_proprietary_net": safe_int(item.get(prop_col)) if prop_col else np.nan, "dealer_hedge_net": safe_int(item.get(hedge_col)) if hedge_col else np.nan, "hedge_data_available": hedge_col is not None})
    df = pd.DataFrame(rows)
    for c in ["foreign_net", "trust_net", "dealer_proprietary_net", "dealer_hedge_net"]: df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

def get_tpex_institutional():
    rows = []
    for d in get_recent_trading_dates(10):
        try:
            time.sleep(0.8); roc = int(d[:4]) - 1911; roc_date = f"{roc}/{d[4:6]}/{d[6:]}"
            url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
            r = request_json(url, params={"l": "zh-tw", "o": "json", "se": "AL", "t": "D", "d": roc_date}, timeout=20)
            if not r.get("aaData"): continue
            for rr in r["aaData"]:
                sid = normalize_stock_id(rr[0])
                if not sid.isdigit() or len(sid)!= 4: continue
                rows.append({"stock_id": sid, "date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "foreign_net": safe_int(rr[2]), "trust_net": safe_int(rr[3]), "dealer_hedge_net": safe_int(rr[5]) if len(rr) > 5 else np.nan, "hedge_data_available": True})
            if rows: break
        except: continue
    return pd.DataFrame(rows) if rows else pd.DataFrame()

def get_all_institutional():
    frames = []
    for fn in [get_twse_institutional, get_tpex_institutional]:
        try:
            df = fn()
            if not df.empty: frames.append(df)
        except Exception as e: print(f"法人 {fn.__name__} 失敗：{e}")
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["stock_id", "date"], keep="last") if frames else pd.DataFrame()

def load_history(prefix):
    files = sorted(f for f in os.listdir(OUTPUT_DIR) if f.startswith(prefix) and f.endswith(".csv"))[-MAX_HISTORY_FILES:]
    frames = []
    for fn in files:
        try:
            df = pd.read_csv(os.path.join(OUTPUT_DIR, fn), dtype={"stock_id": str})
            if not df.empty: df["stock_id"] = df["stock_id"].map(normalize_stock_id); frames.append(df)
        except: pass
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

def save_history(quotes, institutional, data_date):
    quotes.copy().assign(date=data_date).to_csv(os.path.join(OUTPUT_DIR, f"layout_price_history_{data_date}.csv"), index=False, encoding="utf-8-sig")
    if institutional is not None and not institutional.empty:
        institutional.to_csv(os.path.join(OUTPUT_DIR, f"layout_institutional_history_{data_date}.csv"), index=False, encoding="utf-8-sig")
    cleanup_old_files("layout_price_history_"); cleanup_old_files("layout_institutional_history_"); cleanup_old_files("layout_radar_")

def make_price_features(quotes, history, data_date):
    cols = ["stock_id", "stock_name", "market", "close", "volume", "turnover", "date"]
    today = quotes.copy(); today["date"] = data_date
    full = today[cols].copy() if history is None or history.empty else pd.concat([history[cols], today[cols]], ignore_index=True)
    full["stock_id"] = full["stock_id"].map(normalize_stock_id)
    for c in ["close", "volume", "turnover"]: full[c] = pd.to_numeric(full[c], errors="coerce")
    full = full.drop_duplicates(subset=["stock_id", "date"], keep="last").sort_values(["stock_id", "date"])
    g = full.groupby("stock_id", group_keys=False)
    full["ma10"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    full["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    full["close_5d_ago"] = g["close"].transform(lambda s: s.shift(4))
    full["return_5d_pct"] = (full["close"] / full["close_5d_ago"] - 1) * 100
    full["high_10d"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).max())
    full["low_10d"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).min())
    full["range_10d_pct"] = (full["high_10d"] / full["low_10d"] - 1) * 100
    full["avg_volume_5d"] = g["volume"].transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    full["volume_ratio_5d"] = full["volume"] / full["avg_volume_5d"]
    full["distance_ma10_pct"] = (full["close"] / full["ma10"] - 1) * 100
    full["distance_ma20_pct"] = (full["close"] / full["ma20"] - 1) * 100
    full["history_days"] = g["date"].transform("count")
    full["history_ready"] = full["history_days"] >= MIN_HISTORY_DAYS_FOR_SIGNAL
    out = ["stock_id", "ma10", "ma20", "return_5d_pct", "range_10d_pct", "avg_volume_5d", "volume_ratio_5d", "distance_ma10_pct", "distance_ma20_pct", "history_days", "history_ready"]
    return full.loc[full["date"] == data_date, out].copy()

def make_institutional_features(history, price_features):
    out_cols = ["stock_id", "trust_buy_days_5", "trust_5d_net", "foreign_buy_days_5", "foreign_5d_net", "dealer_hedge_5d_abs", "trust_volume_ratio", "foreign_volume_ratio", "hedge_volume_ratio", "hedge_data_status", "hedge_dominant", "trust_accumulation", "foreign_support", "trust_20d_net", "foreign_20d_net", "trust_buy_days_20", "foreign_buy_days_20", "institutional_days_20", "midterm_inflow_to_verify"]
    if history is None or history.empty: return pd.DataFrame(columns=out_cols)
    df = history.copy(); df["stock_id"] = df["stock_id"].map(normalize_stock_id); df["date"] = df["date"].astype(str)
    for c in ["foreign_net", "trust_net", "dealer_hedge_net"]:
        if c not in df.columns: df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "hedge_data_available" not in df.columns: df["hedge_data_available"] = df["dealer_hedge_net"].notna()
    df["hedge_data_available"] = df["hedge_data_available"].fillna(False).astype(bool)
    df["foreign_net"] = df["foreign_net"].fillna(0); df["trust_net"] = df["trust_net"].fillna(0)
    df = df.drop_duplicates(subset=["stock_id", "date"], keep="last").sort_values(["stock_id", "date"])
    vol_map = {} if price_features is None or price_features.empty else price_features.set_index("stock_id")["avg_volume_5d"].to_dict()
    rows = []
    for sid, group in df.groupby("stock_id"):
        group = group.sort_values("date"); g5 = group.tail(5); g20 = group.tail(20)
        t5 = float(g5["trust_net"].sum()); f5 = float(g5["foreign_net"].sum()); t20 = float(g20["trust_net"].sum()); f20 = float(g20["foreign_net"].sum())
        tb5 = int((g5["trust_net"] > 0).sum()); fb5 = int((g5["foreign_net"] > 0).sum()); tb20 = int((g20["trust_net"] > 0).sum()); fb20 = int((g20["foreign_net"] > 0).sum())
        hedge_avail = bool(len(g5) >= 5 and g5["hedge_data_available"].all() and g5["dealer_hedge_net"].notna().all())
        hedge_abs = float(g5["dealer_hedge_net"].abs().sum()) if hedge_avail else np.nan
        avg = vol_map.get(sid, np.nan)
        if pd.isna(avg) or avg <= 0: tvr = np.nan; fvr = np.nan; hvr = np.nan
        else: tvr = t5 / (avg * 5); fvr = f5 / (avg * 5); hvr = hedge_abs / (avg * 5) if hedge_avail else np.nan
        status = "資料不足"; dom = False
        if hedge_avail:
            if pd.isna(hvr): status = "資料不足"
            elif hvr >= HEDGE_DOMINANT_RATIO: status = "避險主導"; dom = True
            elif hvr >= HEDGE_WATCH_RATIO: status = "注意"
            else: status = "正常"
        trust_acc = bool(len(g5) >= 5 and tb5 >= 3 and t5 > 0 and (pd.isna(tvr) or tvr >= MIN_TRUST_5D_VOLUME_RATIO))
        foreign_sup = bool(len(g5) >= 5 and fb5 >= 3 and f5 > 0 and (pd.isna(fvr) or fvr >= MIN_FOREIGN_5D_VOLUME_RATIO))
        mid = bool(len(g20) >= 10 and tb20 >= max(5, int(len(g20) * 0.4)) and t20 > 0)
        rows.append({"stock_id": sid, "trust_buy_days_5": tb5, "trust_5d_net": t5, "foreign_buy_days_5": fb5, "foreign_5d_net": f5, "dealer_hedge_5d_abs": hedge_abs, "trust_volume_ratio": tvr, "foreign_volume_ratio": fvr, "hedge_volume_ratio": hvr, "hedge_data_status": status, "hedge_dominant": dom, "trust_accumulation": trust_acc, "foreign_support": foreign_sup, "trust_20d_net": t20, "foreign_20d_net": f20, "trust_buy_days_20": tb20, "foreign_buy_days_20": fb20, "institutional_days_20": len(g20), "midterm_inflow_to_verify": mid})
    return pd.DataFrame(rows, columns=out_cols)

def get_rss_urls_from_env():
    raw = os.getenv("GOOGLE_ALERTS_RSS") or os.getenv("GOOGLE_ALERTS_RSS_URLS") or ""
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip().startswith(("https://", "http://"))]
def parse_rss_date(t):
    m = re.search(r"<pubDate>(.*?)</pubDate>|<published>(.*?)</published>|<updated>(.*?)</updated>", t, re.DOTALL | re.IGNORECASE)
    if not m: return None
    raw = next((x for x in m.groups() if x), None)
    if not raw: return None
    try: return parsedate_to_datetime(raw)
    except: return None
def fetch_google_alerts_rss():
    urls = get_rss_urls_from_env()
    if not urls: return {}
    alerts = defaultdict(list)
    junk = ["同學會", "爆料", "散戶", "發言", "聊天", "閒聊", "心情", "權證", "猜測", "看多", "看空"]
    risk = ["資安", "入侵", "重訊", "重大", "減資", "違約", "處置", "警示", "下市", "搜索", "檢調", "火災", "裁罰", "重罰"]
    for rss_url in urls:
        try:
            r = request_get(rss_url, timeout=20)
            items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL | re.IGNORECASE)
            for it in items:
                tm = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", it, re.DOTALL | re.IGNORECASE)
                lm = re.search(r"<link>(.*?)</link>", it, re.DOTALL | re.IGNORECASE)
                if not tm: continue
                title = (tm.group(1) or tm.group(2) or "").strip()
                title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
                link = lm.group(1).strip() if lm else ""
                pub = parse_rss_date(it)
                if len(title) < 4: continue
                if any(k in title for k in junk): continue
                for sid in FOCUS_STOCKS:
                    name = FOCUS_PROFILES.get(sid, {}).get("name", "")
                    if name and name in title:
                        level = "高風險" if any(k in title for k in risk) else "中性"
                        alerts[sid].append({"title": title, "link": link, "level": level, "published": pub})
        except Exception as e: print(f"RSS失敗：{e}"); continue
    for sid in list(alerts.keys()):
        seen = set(); uniq = []
        for x in alerts[sid]:
            if x["title"] not in seen: uniq.append(x); seen.add(x["title"])
        uniq.sort(key=lambda x: x["published"].timestamp() if x.get("published") else 0, reverse=True)
        alerts[sid] = uniq[:2]
    return alerts
def fetch_google_alerts():
    try:
        rss = fetch_google_alerts_rss()
        if rss: return rss
    except Exception as e: print(f"RSS模組異常：{e}")
    return {}

def classify_stock(row):
    sid = row["stock_id"]
    if sid in EXCLUDE_TOOL_STOCKS: return "⚪ 排除：權值／ETF／工具股", "高權值不納入", -5
    if not bool(row.get("history_ready", False)): return "⚪ 歷史資料不足", f"未滿{MIN_HISTORY_DAYS_FOR_SIGNAL}日", 0
    if bool(row.get("hedge_dominant", False)): return "🟠 排除：避險流量主導", "避險偏高", -4
    ret = row.get("return_5d_pct", np.nan); rng = row.get("range_10d_pct", np.nan); vr = row.get("volume_ratio_5d", np.nan)
    dma10 = row.get("distance_ma10_pct", np.nan); dma20 = row.get("distance_ma20_pct", np.nan)
    t5 = float(row.get("trust_5d_net", 0) or 0); f5 = float(row.get("foreign_5d_net", 0) or 0); tb5 = int(row.get("trust_buy_days_5", 0) or 0)
    t_acc = bool(row.get("trust_accumulation", False)); f_sup = bool(row.get("foreign_support", False)); mid = bool(row.get("midterm_inflow_to_verify", False))
    turnover = safe_float(row.get("turnover", np.nan)); liquid = not pd.isna(turnover) and turnover >= MIN_DAILY_TURNOVER
    overheat = 18 if sid in HIGH_VOLATILITY_STOCKS else 10
    if not pd.isna(ret) and ret > overheat: return "🔴 排除：拉高／過熱", f"5日>{overheat}%", -3
    if not pd.isna(vr) and vr > 2.0 and not pd.isna(ret) and ret > 3: return "🔴 排除：拉高／過熱", "爆量走強", -3
    if t5 < 0 and f5 < 0 and tb5 == 0: return "🔴 排除：法人轉賣", "投信外資同步賣", -3
    if not pd.isna(dma20) and dma20 < 0 and (t_acc or f_sup or mid): return "🟡 籌碼尚在、價格轉弱", "跌破MA20", 2
    if not pd.isna(dma10) and dma10 < 0 and t5 < 0: return "🟡 籌碼鬆動、跌破MA10", "跌破MA10且投信賣", 1
    price_ok = (pd.isna(ret) or -7 <= ret <= 6) and (pd.isna(rng) or rng <= 16) and (pd.isna(vr) or vr <= 2.0) and (pd.isna(dma20) or abs(dma20) <= 7)
    mid_ok = (pd.isna(ret) or -7 <= ret <= 8) and (pd.isna(rng) or rng <= 20) and (pd.isna(dma20) or abs(dma20) <= 8)
    green_ok = not pd.isna(dma10) and 0 <= dma10 <= 8 and (pd.isna(ret) or 0 < ret <= 10)
    if liquid and t_acc and green_ok: return "🟢 吸籌延續／初步確認", "站上MA10", 9
    if liquid and t_acc and price_ok: return "🔵 主動資金疑似布局", "投信持續買超盤整", 8
    if liquid and mid and mid_ok: return "🟣 中期資金流入待驗證", "投信中期買盤持續", 7
    if f_sup and not t_acc: return "⚪ 待驗證：僅外資流入", "外資流入未獲投信確認", 1
    return "⚪ 不列入", "未符合吸籌條件", 0

def build_radar(quotes, price_features, institutional_features):
    df = quotes.copy()
    for fd in [price_features, institutional_features]:
        if fd is not None and not fd.empty: df = df.merge(fd, on="stock_id", how="left")
    defaults = {"ma10": np.nan, "ma20": np.nan, "return_5d_pct": np.nan, "range_10d_pct": np.nan, "avg_volume_5d": np.nan, "volume_ratio_5d": np.nan, "distance_ma10_pct": np.nan, "distance_ma20_pct": np.nan, "history_days": 0, "history_ready": False, "trust_buy_days_5": 0, "trust_5d_net": 0, "foreign_buy_days_5": 0, "foreign_5d_net": 0, "trust_volume_ratio": np.nan, "foreign_volume_ratio": np.nan, "hedge_volume_ratio": np.nan, "hedge_data_status": "資料不足", "hedge_dominant": False, "trust_accumulation": False, "foreign_support": False, "trust_20d_net": 0, "foreign_20d_net": 0, "trust_buy_days_20": 0, "foreign_buy_days_20": 0, "institutional_days_20": 0, "midterm_inflow_to_verify": False}
    for c, d in defaults.items():
        if c not in df.columns: df[c] = d
        elif isinstance(d, bool): df[c] = df[c].fillna(d).astype(bool)
        else: df[c] = df[c].fillna(d)
    df["theme"] = df["stock_id"].map(lambda s: "／".join(THEME_MAP.get(s, [])))
    df["is_focus_stock"] = df["stock_id"].isin(FOCUS_STOCKS)
    cls = df.apply(classify_stock, axis=1, result_type="expand"); cls.columns = ["signal", "reason", "base_score"]
    df = pd.concat([df, cls], axis=1); df["score"] = df["base_score"]
    df.loc[df["trust_buy_days_5"] >= 4, "score"] += 2
    df.loc[df["foreign_support"] == True, "score"] += 1
    order = {"🟢 吸籌延續／初步確認": 1, "🔵 主動資金疑似布局": 2, "🟣 中期資金流入待驗證": 3, "🟡 籌碼尚在、價格轉弱": 4, "🟡 籌碼鬆動、跌破MA10": 5, "⚪ 待驗證：僅外資流入": 6, "🔴 排除：拉高／過熱": 7, "🔴 排除：法人轉賣": 8, "🟠 排除：避險流量主導": 9, "⚪ 排除：權值／ETF／工具股": 10, "⚪ 歷史資料不足": 11, "⚪ 不列入": 99}
    df["sort_order"] = df["signal"].map(order).fillna(99)
    return df.sort_values(["sort_order", "score", "turnover"], ascending=[True, False, False], na_position="last").drop(columns=["sort_order"])

def fmt_price(v):
    if _is_nan(v): return "-"
    try:
        fv = safe_float(v)
        if pd.isna(fv): return safe_text(v)
        return f"{fv:,.2f}"
    except: return safe_text(v)
def fmt_pct(v, empty="-"):
    if _is_nan(v): return empty
    try: return f"{float(v):+.2f}%"
    except: return safe_text(v)
def fmt_shares(v):
    if _is_nan(v): return "-"
    try:
        lots = float(v) / 1000
        if lots == 0: return "0 張"
        if abs(lots) >= 100: return f"{lots:+,.0f} 張"
        return f"{lots:+,.1f} 張"
    except: return safe_text(v)
def get_signal_color(s):
    t = str(s)
    if "🟢" in t: return "#2e7d32"
    if "🔵" in t: return "#1565c0"
    if "🟣" in t: return "#6a1b9a"
    if "🟡" in t: return "#f57c00"
    if "🔴" in t: return "#c62828"
    if "🟠" in t: return "#ef6c00"
    return "#555"
def get_long_rating_color(r):
    t = str(r).upper()
    if "A" in t: return "#2e7d32"
    if "高風險" in t or "D" in t: return "#c62828"
    return "#666"
def get_alerts_for_stock(alerts_dict, sid):
    if not alerts_dict: return []
    norm = normalize_stock_id(sid)
    for k in [norm, str(sid).strip(), str(int(norm)) if norm.isdigit() else ""]:
        if k and alerts_dict.get(k): return alerts_dict.get(k)
    return []

def make_html_holdings_zone(radar):
    radar_map = {}
    if radar is not None and not radar.empty:
        rc = radar.copy(); rc["stock_id"] = rc["stock_id"].map(normalize_stock_id)
        rc = rc.drop_duplicates(subset=["stock_id"], keep="first")
        radar_map = {r["stock_id"]: r for _, r in rc.iterrows()}
    html = """<div style="margin-bottom:25px; border:2px solid #2e7d32; border-radius:10px; padding:15px; background:#f9fdf9;"><h3 style="color:#2e7d32; border-bottom:2px solid #2e7d32; padding-bottom:8px; margin-top:0;">📌 第一區：目前持有 - 8大核心持股</h3>"""
    for raw in FOCUS_STOCKS:
        sid = normalize_stock_id(raw); prof = FOCUS_PROFILES.get(sid, {})
        name = prof.get("name", sid); theme = prof.get("theme", "未分類"); val = prof.get("valuation", "尚未建檔"); rating = prof.get("long_rating", "持續研究")
        row = radar_map.get(sid)
        if row is not None: close = fmt_price(row.get("close")); sig = row.get("signal", "-"); sig_c = get_signal_color(sig); trust = fmt_shares(row.get("trust_5d_net")); foreign = fmt_shares(row.get("foreign_5d_net")); reason = safe_text(row.get("reason", ""))
        else: close = "-"; sig = "未取得"; sig_c = "#777"; trust = "-"; foreign = "-"; reason = "無行情"
        html += f"""<div style="background:#fff; border-left:5px solid #2e7d32; padding:10px 12px; margin-bottom:10px; border-radius:4px;"><b>{safe_text(sid)} {safe_text(name)}</b> <span style="font-size:11px; background:#e8f5e9; padding:2px 6px; border-radius:10px; color:{get_long_rating_color(rating)};">{safe_text(rating)}</span> <span style="font-size:11px; color:#666;">{safe_text(theme)} / {safe_text(val)}</span><br><span style="font-size:13px;"><b>收盤：</b>{close} | <b style="color:{sig_c};">{safe_text(sig)}</b> | 投信5日:{trust} 外資5日:{foreign}</span><br><span style="font-size:12px; color:#666;">{reason}</span></div>"""
    html += "</div>"; return html

def make_html_long_capture_zone(radar, alerts_dict=None, maximum=20):
    if alerts_dict is None: alerts_dict = {}
    if radar is None or radar.empty: return """<div style="border:1px solid #ce93d8; padding:15px; border-radius:8px; margin-bottom:25px;"><h3 style="color:#6a1b9a;">🟣 第二區：抓到的長線</h3><p style="color:#888;">今日無長線候選</p></div>"""
    frame = radar[radar["signal"] == "🟣 中期資金流入待驗證"].copy()
    frame = frame[~frame["is_focus_stock"]]
    if frame.empty: return """<div style="border:1px solid #ce93d8; padding:15px; border-radius:8px; margin-bottom:25px;"><h3 style="color:#6a1b9a;">🟣 第二區：抓到的長線 - 中期資金流入待驗證</h3><p style="color:#888;">今日無長線候選（已排除持有）</p></div>"""
    h = """<div style="margin-bottom:25px; border:1px solid #ce93d8; border-radius:8px; padding:15px; background:#fdf2ff;"><h3 style="color:#6a1b9a; border-bottom:2px solid #6a1b9a; padding-bottom:8px; margin-top:0;">🟣 第二區：抓到的長線 - 中期資金流入待驗證（非持有）</h3><div style="overflow-x:auto;"><table style="width:100%; border-collapse:collapse; font-size:13px; background:#fff;"><thead><tr style="background:#f3e5f5; text-align:left;"><th style="padding:8px; border:1px solid #ddd;">標的</th><th style="padding:8px; border:1px solid #ddd;">法人20日</th><th style="padding:8px; border:1px solid #ddd;">技術</th><th style="padding:8px; border:1px solid #ddd;">判定</th></tr></thead><tbody>"""
    for _, row in frame.head(maximum).iterrows():
        sid = normalize_stock_id(row.get("stock_id", "")); sname = safe_text(row.get("stock_name", ""))
        alerts = get_alerts_for_stock(alerts_dict, sid); alert_html = ""
        if alerts: alert_html = "<div style='margin-top:4px; font-size:11px; background:#f3e5f5; padding:3px 5px; border-radius:3px;'>" + "<br>".join([f"• {safe_text(a.get('title',''))}" for a in alerts[:1]]) + "</div>"
        h += f"""<tr><td style="padding:8px; border:1px solid #ddd;"><b>{safe_text(sid)}</b> {sname}<br><span style="color:#6a1b9a; font-weight:bold;">🟣 中期流入</span></td><td style="padding:8px; border:1px solid #ddd;">投信20日:{fmt_shares(row.get("trust_20d_net"))}<br>買超天數:{int(row.get("trust_buy_days_20",0))}天</td><td style="padding:8px; border:1px solid #ddd;">距MA20:{fmt_pct(row.get("distance_ma20_pct"))}<br>5日:{fmt_pct(row.get("return_5d_pct"))}</td><td style="padding:8px; border:1px solid #ddd; font-size:12px;">{safe_text(row.get("reason",""))}{alert_html}</td></tr>"""
    h += "</tbody></table></div></div>"; return h

def make_html_short_capture_zone(radar, alerts_dict=None, maximum=20):
    if alerts_dict is None: alerts_dict = {}
    if radar is None or radar.empty: return """<div style="border:1px solid #90caf9; padding:15px; border-radius:8px; margin-bottom:25px;"><h3 style="color:#1565c0;">⚡ 第三區：抓到的短線</h3><p style="color:#888;">今日無短線候選</p></div>"""
    frame = radar[radar["signal"].isin(["🟢 吸籌延續／初步確認", "🔵 主動資金疑似布局"])].copy()
    frame = frame[~frame["is_focus_stock"]]
    if frame.empty: return """<div style="border:1px solid #90caf9; padding:15px; border-radius:8px; margin-bottom:25px;"><h3 style="color:#1565c0;">⚡ 第三區：抓到的短線 - 吸籌/布局（非持有）</h3><p style="color:#888;">今日無短線候選（已排除持有）</p></div>"""
    h = """<div style="margin-bottom:25px; border:1px solid #90caf9; border-radius:8px; padding:15px; background:#f5f9ff;"><h3 style="color:#1565c0; border-bottom:2px solid #1565c0; padding-bottom:8px; margin-top:0;">⚡ 第三區：抓到的短線 - 吸籌延續/疑似布局（非持有）</h3><div style="overflow-x:auto;"><table style="width:100%; border-collapse:collapse; font-size:13px; background:#fff;"><thead><tr style="background:#e3f2fd; text-align:left;"><th style="padding:8px; border:1px solid #ddd;">訊號/標的</th><th style="padding:8px; border:1px solid #ddd;">法人5日</th><th style="padding:8px; border:1px solid #ddd;">技術</th><th style="padding:8px; border:1px solid #ddd;">判定+快訊</th></tr></thead><tbody>"""
    for _, row in frame.head(maximum).iterrows():
        sid = normalize_stock_id(row.get("stock_id", "")); sname = safe_text(row.get("stock_name", "")); sig = row.get("signal", "-")
        alerts = get_alerts_for_stock(alerts_dict, sid); alert_html = ""
        if alerts:
            alert_html = "<div style='margin-top:4px; background:#e3f2fd; padding:3px 5px; border-radius:3px; font-size:11px;'>"
            for a in alerts[:2]: alert_html += f"• {safe_text(a.get('title',''))}<br>"
            alert_html += "</div>"
        h += f"""<tr><td style="padding:8px; border:1px solid #ddd;"><span style="color:{get_signal_color(sig)}; font-weight:bold;">{safe_text(sig)}</span><br><b>{safe_text(sid)}</b> {sname}</td><td style="padding:8px; border:1px solid #ddd;">投信:{fmt_shares(row.get("trust_5d_net"))}<br>外資:{fmt_shares(row.get("foreign_5d_net"))}<br>避險:{safe_text(row.get("hedge_data_status"))}</td><td style="padding:8px; border:1px solid #ddd;">5日:{fmt_pct(row.get("return_5d_pct"))}<br>距MA20:{fmt_pct(row.get("distance_ma20_pct"))}<br>量比:{fmt_pct((row.get("volume_ratio_5d",np.nan)-1)*100 if not _is_nan(row.get("volume_ratio_5d",np.nan)) else np.nan)}</td><td style="padding:8px; border:1px solid #ddd; font-size:12px;">{safe_text(row.get("reason",""))}{alert_html}</td></tr>"""
    h += "</tbody></table></div></div>"; return h

def make_html_email_body(radar, data_date, execution_time, alerts_dict=None):
    if alerts_dict is None: alerts_dict = {}
    zone1 = make_html_holdings_zone(radar)
    zone2 = make_html_long_capture_zone(radar, alerts_dict, 20)
    zone3 = make_html_short_capture_zone(radar, alerts_dict, 20)
    total = len(radar) if radar is not None else 0
    gc = bc = pc = 0
    if radar is not None and not radar.empty:
        gc = int((radar["signal"] == "🟢 吸籌延續／初步確認").sum()); bc = int((radar["signal"] == "🔵 主動資金疑似布局").sum()); pc = int((radar["signal"] == "🟣 中期資金流入待驗證").sum())
    return f"""<html><head><meta charset="utf-8"></head><body style="font-family:Arial, Microsoft JhengHei; color:#333; background:#f9f9f9; padding:20px;"><div style="max-width:960px; margin:auto; background:#fff; padding:25px; border-radius:8px;"><h2 style="color:#1565c0; border-bottom:3px solid #1565c0;">📈 台股主動資金雷達 v6.3 三區版</h2><p style="font-size:13px; color:#666;"><b>資料日期：</b>{safe_text(data_date)} | <b>執行：</b>{safe_text(execution_time)} | 掃描:{total}檔 | 🟢{gc} 🔵{bc} 🟣{pc}</p>{zone1}{zone2}{zone3}<hr><p style="font-size:11px; color:#888; text-align:center;">第一區為持有8檔，第二區為抓到的長線🟣，第三區為抓到的短線🟢🔵，已排除持有。僅供研究非買賣建議。</p></div></body></html>"""

def send_email(subject, html_body):
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]): print("未設Gmail，略過寄信"); return
    msg = MIMEMultipart("alternative"); msg["From"] = GMAIL_USER; msg["To"] = RECIPIENT_EMAIL; msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD); s.send_message(msg)
    print("Email寄送完成")

def main():
    now = now_tw(); exec_time = now.strftime("%Y-%m-%d %H:%M")
    print(f"開始 v6.3 三區版：{exec_time}")
    quotes = get_all_quotes()
    inst = pd.DataFrame()
    try: inst = get_all_institutional()
    except Exception as e: print(f"法人失敗：{e}")
    alerts = fetch_google_alerts()
    if inst is not None and not inst.empty and "date" in inst.columns: data_date = str(inst["date"].max())
    else: ld = get_last_trading_date(); data_date = f"{ld[:4]}-{ld[4:6]}-{ld[6:]}"
    print(f"資料日期：{data_date}")
    ph = load_history("layout_price_history_"); ih = load_history("layout_institutional_history_")
    if inst is not None and not inst.empty: ih = pd.concat([ih, inst], ignore_index=True)
    pf = make_price_features(quotes, ph, data_date)
    inf = make_institutional_features(ih, pf)
    radar = build_radar(quotes, pf, inf)
    save_history(quotes, inst, data_date)
    radar.to_csv(os.path.join(OUTPUT_DIR, f"layout_radar_{data_date}.csv"), index=False, encoding="utf-8-sig")
    html_body = make_html_email_body(radar, data_date, exec_time, alerts)
    send_email(f"主動資金雷達 v6.3 三區｜{data_date}", html_body)
    print("完成")

if __name__ == "__main__":
    try: main()
    except Exception:
        err = traceback.format_exc(); print(err)
        try: send_email(f"【錯誤】v6.3 {today_str()}", f"<pre>{safe_text(err)}</pre>")
        except: pass
        raise
