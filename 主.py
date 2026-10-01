# -*- coding: utf-8 -*-
"""
台股「主動資金疑似布局」雷達 v7.0 - 符合 Perplexity 7層架構建議
================================================================
改動對應：
資料層：TWSE + TPEx 官方 API 雙市場
法人層：外資、投信、自營自行、避險分開存 (TWSE T86 + TPEx 3法人)
儲存層：SQLite 單一資料庫 (取代散落 CSV)
規則層：投信 5/20日持續買超 + 盤整 + 量縮 + MA (可切換 5/20)
驗證層：訊號後 5/10/20日績效自動回填與追蹤
報告層：CSV + HTML Email
維護層：py_compile 安全 + 防呆 + GitHub Actions 友善

此檔建議覆蓋 主.py
"""

import os
import io
import time
import sqlite3
import smtplib
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import numpy as np
import pandas as pd
import requests

# ============ 0. 基本設定 ============
TZ = timezone(timedelta(hours=8))
OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)
DB_PATH = os.path.join(OUTPUT_DIR, "tw_radar.db")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"}

# 你的 7 檔焦點 - 主.py 也會置頂
FOCUS_STOCKS = ["2383", "2368", "6197", "3293", "4763", "1808", "6919"]

THEMES = {
    "⭐ 個人重點關注焦點股": FOCUS_STOCKS,
    "AI 伺服器／ODM": ["2317", "2382", "3231", "6669", "6805"],
    "散熱": ["3017", "3324", "6205", "6131"],
    "PCB／CCL／載板": ["2383", "2368", "2385", "3037", "4967", "6274", "6197"],
}
THEME_MAP = {}
for name, codes in THEMES.items():
    for c in codes:
        THEME_MAP.setdefault(c, []).append(name)

EXCLUDE_TOOL_STOCKS = {
    "2330", "2454", "2308", "3711",
    "2881", "2882", "2884", "2886", "2891", "2892", "2880",
    "0050", "0056", "00878", "006208", "00919", "00929",
}

# 規則層參數 - 可依需求切 5/20
TRUST_DAYS_WINDOW = 5  # Perplexity建議你同時看 5/20，這裡預設5，輔.py會同時統計20
MIN_TRUST_BUY_DAYS = 3
MIN_DAILY_TURNOVER = 30_000_000
MIN_TRUST_5D_VOLUME_RATIO = 0.01
MAX_5D_RETURN = 6.0
MIN_5D_RETURN = -7.0
MAX_10D_RANGE = 16.0
MAX_VOLUME_SPIKE = 2.0
MAX_DISTANCE_MA20_BLUE = 7.0
MAX_DISTANCE_MA10_GREEN = 8.0

# ============ 1. 工具 ============
def get_today_str(): return datetime.now(TZ).strftime("%Y-%m-%d")

def safe_float(v):
    if v is None: return np.nan
    try:
        t=str(v).strip()
        if t in ["","--","---","nan","NaN","None","null","—"]: return np.nan
        t=t.replace(",","").replace("%","").replace("＋","+").replace("－","-").replace("—","-").replace("–","-")
        if t.startswith("+"): t=t[1:]
        return float(t)
    except: return np.nan

def safe_int(v):
    n=safe_float(v)
    return np.nan if pd.isna(n) else int(n)

def normalize_stock_id(v):
    t=str(v).strip()
    if t.endswith(".0"): t=t[:-2]
    return t.zfill(4) if t.isdigit() and len(t)<4 else t

def request_get(url, params=None, timeout=30):
    last=None
    for i in range(3):
        try:
            r=requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last=e; time.sleep(2*(i+1))
    raise last

def get_recent_dates(days=15):
    now=datetime.now(TZ)
    return [(now-timedelta(days=i)).strftime("%Y%m%d") for i in range(days)]

# ============ 2. SQLite 儲存層 ============
def init_db():
    conn=sqlite3.connect(DB_PATH)
    cur=conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS prices (
        date TEXT, stock_id TEXT, stock_name TEXT, market TEXT,
        close REAL, volume INTEGER, turnover REAL,
        PRIMARY KEY(date, stock_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS institutional (
        date TEXT, stock_id TEXT,
        foreign_net INTEGER, trust_net INTEGER,
        dealer_prop INTEGER, dealer_hedge INTEGER, dealer_total INTEGER,
        market TEXT,
        PRIMARY KEY(date, stock_id, market))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS revenue (
        announce_date TEXT, stock_id TEXT,
        revenue REAL, yoy REAL, mom REAL,
        PRIMARY KEY(announce_date, stock_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS signals (
        signal_date TEXT, stock_id TEXT,
        stock_name TEXT, market TEXT, signal TEXT, reason TEXT, score INTEGER,
        close_at_signal REAL,
        trust_5d_net INTEGER, trust_buy_days INTEGER,
        return_5d REAL, range_10d REAL, dist_ma20 REAL,
        PRIMARY KEY(signal_date, stock_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS performance (
        signal_date TEXT, stock_id TEXT, signal TEXT,
        close_0 REAL, close_5 REAL, ret_5 REAL,
        close_10 REAL, ret_10 REAL, close_20 REAL, ret_20 REAL,
        computed_date TEXT,
        PRIMARY KEY(signal_date, stock_id))""")
    conn.commit(); conn.close()

def save_prices(df):
    if df.empty: return
    conn=sqlite3.connect(DB_PATH)
    df.to_sql("prices", conn, if_exists="append", index=False, method="multi")
    conn.execute("DELETE FROM prices WHERE rowid NOT IN (SELECT MIN(rowid) FROM prices GROUP BY date, stock_id)")
    conn.commit(); conn.close()

def save_institutional(df, market):
    if df.empty: return
    df["market"]=market
    conn=sqlite3.connect(DB_PATH)
    df.to_sql("institutional", conn, if_exists="append", index=False, method="multi")
    conn.execute("DELETE FROM institutional WHERE rowid NOT IN (SELECT MIN(rowid) FROM institutional GROUP BY date, stock_id, market)")
    conn.commit(); conn.close()

def save_revenue(df):
    if df.empty: return
    conn=sqlite3.connect(DB_PATH)
    df.to_sql("revenue", conn, if_exists="append", index=False, method="multi")
    conn.commit(); conn.close()

def load_price_history(days=60):
    conn=sqlite3.connect(DB_PATH)
    try:
        df=pd.read_sql(f"SELECT * FROM prices WHERE date >= date('now','-{days} days','localtime')", conn, dtype={"stock_id":str})
    except: df=pd.DataFrame()
    conn.close()
    return df

def load_inst_history(days=60):
    conn=sqlite3.connect(DB_PATH)
    try:
        df=pd.read_sql(f"SELECT * FROM institutional WHERE date >= date('now','-{days} days','localtime')", conn, dtype={"stock_id":str})
    except: df=pd.DataFrame()
    conn.close()
    return df

# ============ 3. 資料層 - 官方 API ============
def get_twse_quotes():
    print("取得 TWSE 上市行情...")
    data=request_get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL").json()
    rows=[]
    for it in data:
        sid=normalize_stock_id(it.get("Code",""))
        if not sid.isdigit() or len(sid)!=4: continue
        close=safe_float(it.get("ClosingPrice"))
        if pd.isna(close): continue
        rows.append({"date":get_today_str(),"stock_id":sid,"stock_name":str(it.get("Name","")).strip(),"market":"TWSE","close":close,"volume":safe_int(it.get("TradeVolume")),"turnover":safe_float(it.get("TradeValue"))})
    df=pd.DataFrame(rows)
    print(f"TWSE {len(df)} 筆")
    return df

def get_tpex_quotes():
    print("取得 TPEx 上櫃行情...")
    data=request_get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes").json()
    rows=[]
    for it in data:
        sid=normalize_stock_id(it.get("SecuritiesCompanyCode") or it.get("SecuritiesCode") or it.get("Code") or "")
        if not sid.isdigit() or len(sid)!=4: continue
        close=safe_float(it.get("Close") or it.get("ClosingPrice"))
        if pd.isna(close): continue
        rows.append({"date":get_today_str(),"stock_id":sid,"stock_name":str(it.get("CompanyName") or it.get("SecuritiesName") or "").strip(),"market":"TPEx","close":close,"volume":safe_int(it.get("Volume") or it.get("TradeVolume")),"turnover":safe_float(it.get("Amount") or it.get("TradeValue"))})
    df=pd.DataFrame(rows)
    print(f"TPEx {len(df)} 筆")
    return df

def get_twse_institutional():
    print("取得 TWSE 三大法人...")
    url="https://www.twse.com.tw/rwd/zh/fund/T86"
    data=None; used=None
    for d in get_recent_dates(15):
        try:
            j=request_get(url, params={"response":"json","date":d,"selectType":"ALLBUT0999"}, timeout=15).json()
            if j.get("stat")=="OK": data=j; used=d; break
        except: continue
    if not data: raise RuntimeError("TWSE法人 15日無資料")
    fields=data["fields"]; raw=data["data"]
    fmt=f"{used[:4]}-{used[4:6]}-{used[6:]}"
    rows=[]
    for r in raw:
        it=dict(zip(fields,r))
        sid=normalize_stock_id(it.get("證券代號",""))
        if not sid.isdigit() or len(sid)!=4: continue
        rows.append({"date":fmt,"stock_id":sid,
                     "foreign_net":safe_int(it.get("外陸資買賣超股數(不含外資自營商)")) or 0,
                     "trust_net":safe_int(it.get("投信買賣超股數")) or 0,
                     "dealer_prop":safe_int(it.get("自營商買賣超股數(自行買賣)")) or 0,
                     "dealer_hedge":safe_int(it.get("自營商買賣超股數(避險)")) or 0,
                     "dealer_total":safe_int(it.get("自營商買賣超股數")) or 0})
    return pd.DataFrame(rows)

def get_tpex_institutional():
    print("取得 TPEx 上櫃三大法人...")
    # 官方：tpex_3insti_daily_trading
    try:
        data=request_get("https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading").json()
        rows=[]
        # TPEx格式可能為 list of dict
        for it in data:
            sid=normalize_stock_id(it.get("SecuritiesCompanyCode") or it.get("StockID") or it.get("Code") or "")
            if not sid.isdigit() or len(sid)!=4: continue
            # 欄位名在不同時期可能不同，盡量相容
            foreign=safe_int(it.get("Foreign") or it.get("ForeignInvestorBuySell") or it.get("外資買賣超股數") or 0) or 0
            trust=safe_int(it.get("InvestmentTrust") or it.get("投信買賣超") or it.get("投信買賣超股數") or 0) or 0
            dealer=safe_int(it.get("Dealer") or it.get("自營商買賣超") or 0) or 0
            rows.append({"date":get_today_str(),"stock_id":sid,
                         "foreign_net":foreign,"trust_net":trust,
                         "dealer_prop":dealer,"dealer_hedge":0,"dealer_total":dealer})
        df=pd.DataFrame(rows)
        print(f"TPEx 法人 {len(df)} 筆")
        return df
    except Exception as e:
        print(f"TPEx法人暫不可用，今日先用上市：{e}")
        return pd.DataFrame(columns=["date","stock_id","foreign_net","trust_net","dealer_prop","dealer_hedge","dealer_total"])

def get_monthly_revenue():
    print("取得月營收...")
    urls=["https://mops.twse.com.tw/nas/t21/sii/t21sc03_if.html","https://mops.twse.com.tw/nas/t21/otc/t21sc03_if.html"]
    rows=[]; TODAY=get_today_str()
    for url in urls:
        try:
            resp=request_get(url, timeout=45)
            try: text=resp.content.decode('big5', errors='ignore')
            except: text=resp.text
            tables=pd.read_html(io.StringIO(text))
            for table in tables:
                if table.empty or len(table.columns)<3: continue
                table.columns=[str(c).replace("\n","").strip() for c in table.columns]
                def find_col(keys):
                    for k in keys:
                        for c in table.columns:
                            if k in c: return c
                    return None
                code_col=find_col(["公司代號","代號"])
                yoy_col=find_col(["去年同月增減","YoY"])
                mom_col=find_col(["上月比較增減","MoM"])
                rev_col=find_col(["當月營收","本月營收"])
                if not code_col or not yoy_col: continue
                for _, it in table.iterrows():
                    sid=normalize_stock_id(it.get(code_col,""))
                    if not sid.isdigit() or len(sid)!=4: continue
                    rows.append({"announce_date":TODAY,"stock_id":sid,"revenue":safe_float(it.get(rev_col, np.nan)) if rev_col else np.nan,"yoy":safe_float(it.get(yoy_col, np.nan)),"mom":safe_float(it.get(mom_col, np.nan)) if mom_col else np.nan})
        except Exception as e: print(f"MOPS失敗 {e}")
    return pd.DataFrame(rows).drop_duplicates(subset=["stock_id"])

# ============ 4. 特徵與規則層 ============
def make_price_features(today_quotes, hist_prices):
    today=today_quotes.copy()
    full=today if hist_prices is None or hist_prices.empty else pd.concat([hist_prices, today], ignore_index=True)
    for c in ["close","volume","turnover"]: full[c]=pd.to_numeric(full[c], errors="coerce")
    full=full.drop_duplicates(subset=["stock_id","date"]).sort_values(["stock_id","date"])
    g=full.groupby("stock_id", group_keys=False)
    full["ma10"]=g["close"].transform(lambda x: x.rolling(10, min_periods=10).mean())
    full["ma20"]=g["close"].transform(lambda x: x.rolling(20, min_periods=20).mean())
    full["close_5d_ago"]=g["close"].transform(lambda x: x.shift(4))
    full["return_5d"]=(full["close"]/full["close_5d_ago"]-1)*100
    full["high_10d"]=g["close"].transform(lambda x: x.rolling(10, min_periods=10).max())
    full["low_10d"]=g["close"].transform(lambda x: x.rolling(10, min_periods=10).min())
    full["range_10d"]=(full["high_10d"]/full["low_10d"]-1)*100
    full["avg_vol_5d"]=g["volume"].transform(lambda x: x.shift(1).rolling(5, min_periods=5).mean())
    full["vol_ratio"]=full["volume"]/full["avg_vol_5d"]
    full["dist_ma10"]=(full["close"]/full["ma10"]-1)*100
    full["dist_ma20"]=(full["close"]/full["ma20"]-1)*100
    today_str=get_today_str()
    return full[full["date"]==today_str]

def make_inst_features(inst_hist, price_feat):
    if inst_hist.empty: return pd.DataFrame()
    df=inst_hist.copy()
    for c in ["foreign_net","trust_net","dealer_hedge"]: df[c]=pd.to_numeric(df[c], errors="coerce").fillna(0)
    df=df.sort_values(["stock_id","date"])
    vol_map=price_feat.set_index("stock_id")["avg_vol_5d"].to_dict() if not price_feat.empty else {}
    rows=[]
    for sid, gp in df.groupby("stock_id"):
        gp=gp.tail(TRUST_DAYS_WINDOW)
        t_net=sum(gp["trust_net"]); f_net=sum(gp["foreign_net"]); h_abs=sum(abs(x) for x in gp["dealer_hedge"])
        t_days=sum(1 for x in gp["trust_net"] if x>0)
        avg=vol_map.get(sid, np.nan)
        t_ratio=np.nan if pd.isna(avg) or avg<=0 else t_net/(avg*TRUST_DAYS_WINDOW)
        h_ratio=np.nan if pd.isna(avg) or avg<=0 else h_abs/(avg*TRUST_DAYS_WINDOW)
        hedge=(not pd.isna(h_ratio) and h_ratio>=0.03) or (h_abs>=abs(t_net)+abs(f_net) and h_abs>0)
        trust_acc=t_days>=MIN_TRUST_BUY_DAYS and t_net>0 and (pd.isna(t_ratio) or t_ratio>=MIN_TRUST_5D_VOLUME_RATIO)
        rows.append({"stock_id":sid,"trust_5d":t_net,"trust_days":t_days,"foreign_5d":f_net,"hedge_abs":h_abs,"trust_vol_ratio":t_ratio,"hedge_dominant":hedge,"trust_acc":trust_acc})
    return pd.DataFrame(rows)

def classify(row):
    sid=row["stock_id"]
    if sid in EXCLUDE_TOOL_STOCKS: return "⚪ 排除：權值／ETF","高權值",-5
    if bool(row.get("hedge_dominant",False)): return "🟠 排除：避險主導","避險高",-4
    ret5=row.get("return_5d",np.nan); volr=row.get("vol_ratio",np.nan); ma20=row.get("dist_ma20",np.nan); ma10=row.get("dist_ma10",np.nan)
    trust_acc=bool(row.get("trust_acc",False))
    if not pd.isna(ret5) and (ret5>10 or (not pd.isna(volr) and volr>MAX_VOLUME_SPIKE and ret5>3)): return "🔴 排除：過熱","過熱",-3
    if not pd.isna(ma20) and ma20<0 and not trust_acc: pass # 讓黃燈處理
    if row.get("trust_5d",0)<0 and row.get("foreign_5d",0)<0: return "🔴 排除：法人轉賣","同賣",-3
    if not pd.isna(ma20) and ma20<0 and trust_acc: return "🟡 籌碼尚在、價格轉弱","等站回MA20",2
    price_ok=(pd.isna(ret5) or (MIN_5D_RETURN<=ret5<=MAX_5D_RETURN)) and (pd.isna(row.get("range_10d",np.nan)) or row.get("range_10d")<=MAX_10D_RANGE) and (pd.isna(volr) or volr<=MAX_VOLUME_SPIKE) and (pd.isna(ma20) or abs(ma20)<=MAX_DISTANCE_MA20_BLUE)
    if row.get("turnover",0)>=MIN_DAILY_TURNOVER and trust_acc and price_ok: return "🔵 主動資金疑似布局","投信盤整吸籌",8
    if row.get("turnover",0)>=MIN_DAILY_TURNOVER and trust_acc and not pd.isna(ma10) and 0<=ma10<=MAX_DISTANCE_MA10_GREEN: return "🟢 吸籌延續／初步確認","站上MA10",9
    return "⚪ 不列入","未達標",0

# ============ 5. 驗證層 - 輔.py 的核心 ============
def update_performance():
    """每日跑完後，回填之前訊號的 5/10/20日績效 - 這是 Perplexity 說你最缺的"""
    conn=sqlite3.connect(DB_PATH)
    signals=pd.read_sql("SELECT * FROM signals WHERE signal IN ('🔵 主動資金疑似布局','🟢 吸籌延續／初步確認')", conn, dtype={"stock_id":str})
    if signals.empty:
        conn.close(); return
    prices=pd.read_sql("SELECT date, stock_id, close FROM prices", conn, dtype={"stock_id":str})
    conn.close()
    prices["date"]=pd.to_datetime(prices["date"])
    signals["signal_date"]=pd.to_datetime(signals["signal_date"])
    perf_rows=[]
    for _, sig in signals.iterrows():
        sid=sig["stock_id"]; sdate=sig["signal_date"]; close0=sig["close_at_signal"]
        future=prices[(prices["stock_id"]==sid) & (prices["date"]>sdate)].sort_values("date")
        if future.empty: continue
        def get_ret(n):
            if len(future)>=n:
                c=future.iloc[n-1]["close"]
                return c, (c/close0-1)*100 if close0 else np.nan
            return np.nan, np.nan
        c5,r5=get_ret(5); c10,r10=get_ret(10); c20,r20=get_ret(20)
        perf_rows.append({"signal_date":sdate.strftime("%Y-%m-%d"),"stock_id":sid,"signal":sig["signal"],
                          "close_0":close0,"close_5":c5,"ret_5":r5,"close_10":c10,"ret_10":r10,"close_20":c20,"ret_20":r20,
                          "computed_date":get_today_str()})
    if perf_rows:
        conn=sqlite3.connect(DB_PATH)
        pd.DataFrame(perf_rows).to_sql("performance", conn, if_exists="replace", index=False)
        conn.commit(); conn.close()
        print(f"驗證層更新 {len(perf_rows)} 筆績效")

# ============ 6. 報告層 ============
def build_radar(quotes, price_feat, inst_feat):
    df=quotes.copy()
    if not price_feat.empty: df=df.merge(price_feat, on="stock_id", how="left", suffixes=("","_pf"))
    if not inst_feat.empty: df=df.merge(inst_feat, on="stock_id", how="left")
    # 防呆
    for c,d in [("trust_5d",0),("trust_days",0),("hedge_dominant",False),("trust_acc",False)]:
        if c not in df.columns: df[c]=d
        else: df[c]=df[c].fillna(d)
    cls=df.apply(classify, axis=1, result_type="expand")
    cls.columns=["signal","reason","base_score"]
    df=pd.concat([df, cls], axis=1)
    df["score"]=df["base_score"] + (df["trust_days"]>=4).astype(int)*2 + df["stock_id"].isin(FOCUS_STOCKS).astype(int)*5
    order={"🔵 主動資金疑似布局":1,"🟢 吸籌延續／初步確認":2,"🟡 籌碼尚在、價格轉弱":3,"🔴 排除：過熱":5,"🔴 排除：法人轉賣":6,"🟠 排除：避險主導":8,"⚪ 排除：權值／ETF":9,"⚪ 不列入":99}
    df["sort_order"]=df["signal"].map(order).fillna(99)
    return df.sort_values(["sort_order","score","turnover"], ascending=[True,False,False]).drop(columns=["sort_order"])

def send_email(radar):
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]):
        print("略過寄信"); return
    today=get_today_str()
    def fmt(v): return "-" if pd.isna(v) else f"{float(v):.2f}"
    def fmt_pct(v): return "累積中" if pd.isna(v) else f"{float(v):+.1f}%"
    def lines(sub_df, n):
        if sub_df.empty: return "（無）"
        out=[]
        for _,r in sub_df.head(n).iterrows():
            tag=" ⭐" if r["stock_id"] in FOCUS_STOCKS else ""
            theme="/".join(THEME_MAP.get(r["stock_id"],[]))
            out.append(f"{r['signal']}{tag}｜{r['stock_id']} {r['stock_name']} {theme}｜{r['market']}｜收盤 {fmt(r['close'])}")
            out.append(f"投信{int(r.get('trust_5d',0)/1000):+d}張({int(r.get('trust_days',0))}日)｜5日 {fmt_pct(r.get('return_5d',np.nan))}｜距MA20 {fmt_pct(r.get('dist_ma20',np.nan))}｜{r['reason']}\n")
        return "\n".join(out)
    blue=radar[radar["signal"]=="🔵 主動資金疑似布局"]
    green=radar[radar["signal"]=="🟢 吸籌延續／初步確認"]
    yellow=radar[radar["signal"]=="🟡 籌碼尚在、價格轉弱"]
    focus=radar[radar["stock_id"].isin(FOCUS_STOCKS)]

    body=f"""台股主動資金雷達 v7.0 {today}
執行 {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} | DB: {DB_PATH}

⭐ 焦點7檔
{lines(focus, 10)}

━━━━━━━━━━━━
A. 🔵 布局 {len(blue)}
━━━━━━━━━━━━
{lines(blue, 15)}

━━━━━━━━━━━━
B. 🟢 延續 {len(green)}
━━━━━━━━━━━━
{lines(green, 15)}

━━━━━━━━━━━━
C. 🟡 轉弱 {len(yellow)}
━━━━━━━━━━━━
{lines(yellow, 10)}

驗證層已更新至 performance 表，請用 輔.py 查看勝率。
"""
    msg=MIMEMultipart(); msg["From"]=GMAIL_USER; msg["To"]=RECIPIENT_EMAIL; msg["Subject"]=f"主動資金雷達 v7.0｜{today}"; msg.attach(MIMEText(body,"plain","utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com",465) as s: s.login(GMAIL_USER,GMAIL_APP_PASSWORD); s.send_message(msg)
    print("Email已寄")

# ============ 7. 主流程 ============
def main():
    init_db()
    print(f"=== v7.0 開始 {datetime.now(TZ)} ===")
    # 資料層
    quotes=pd.concat([get_twse_quotes(), get_tpex_quotes()], ignore_index=True).drop_duplicates(subset=["stock_id"])
    tw_inst=get_twse_institutional()
    tp_inst=get_tpex_institutional()
    rev=get_monthly_revenue()

    # 儲存層
    save_prices(quotes)
    save_institutional(tw_inst, "TWSE")
    if not tp_inst.empty: save_institutional(tp_inst, "TPEx")
    save_revenue(rev)

    # 讀歷史做特徵
    hist_price=load_price_history(90)
    hist_inst=load_inst_history(90)
    pf=make_price_features(quotes, hist_price)
    inf=make_inst_features(pd.concat([hist_inst, tw_inst, tp_inst], ignore_index=True) if not hist_inst.empty else pd.concat([tw_inst, tp_inst], ignore_index=True), pf)

    radar=build_radar(quotes, pf, inf)

    # 存訊號
    today=get_today_str()
    sig_df=radar[["stock_id"]].copy()
    sig_df["signal_date"]=today; sig_df["stock_name"]=radar["stock_name"]; sig_df["market"]=radar["market"]
    sig_df["signal"]=radar["signal"]; sig_df["reason"]=radar["reason"]; sig_df["score"]=radar["score"]
    sig_df["close_at_signal"]=radar["close"]; sig_df["trust_5d_net"]=radar.get("trust_5d",0); sig_df["trust_buy_days"]=radar.get("trust_days",0)
    sig_df["return_5d"]=radar.get("return_5d",np.nan); sig_df["range_10d"]=radar.get("range_10d",np.nan); sig_df["dist_ma20"]=radar.get("dist_ma20",np.nan)
    conn=sqlite3.connect(DB_PATH)
    sig_df.to_sql("signals", conn, if_exists="append", index=False)
    conn.commit(); conn.close()

    # 報告 + 驗證
    radar.to_csv(os.path.join(OUTPUT_DIR, f"radar_{today}.csv"), index=False, encoding="utf-8-sig")
    update_performance()
    send_email(radar)
    print("=== v7.0 完成 ===")

if __name__=="__main__":
    try: main()
    except Exception:
        traceback.print_exc()
        raise
