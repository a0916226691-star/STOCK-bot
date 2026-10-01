# -*- coding: utf-8 -*-
"""
台股雷達 v7.2 - 完整改正版
修復：
1. 第一天0檔：DB天數<5天時，投信天數門檻自動降為1天 (啟動模式)
2. Turnover：備援線成交金額是千元，自動*1000
3. 舊CSV自動匯入：output/layout_institutional_history_*.csv 會自動匯入DB
4. 雙備援：TWSE openapi 空回應會切備援，不會再 JSONDecodeError
"""
import os, glob, sqlite3, time, traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import numpy as np
import pandas as pd
import requests

TZ = timezone(timedelta(hours=8))
OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)
DB_PATH = os.path.join(OUTPUT_DIR, "tw_radar.db")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.twse.com.tw/"
}

FOCUS_STOCKS = ["2383", "2368", "6197", "3293", "4763", "1808", "6919"]
THEMES = {
    "⭐ 個人重點關注焦點股": FOCUS_STOCKS,
    "AI 伺服器／ODM": ["2317", "2382", "3231", "6669", "6805"],
    "散熱": ["3017", "3324", "6205", "6131"],
    "PCB／CCL／載板": ["2383", "2368", "2385", "3037", "4967", "6274", "6197"],
}
THEME_MAP = {}
for n, c in THEMES.items():
    for x in c:
        THEME_MAP.setdefault(x, []).append(n)

EXCLUDE_TOOL_STOCKS = {"2330","2454","2308","3711","2881","2882","2884","2886","2891","2892","2880","0050","0056","00878","006208","00919","00929"}
TRUST_DAYS_WINDOW = 5
MIN_TRUST_BUY_DAYS = 3
MIN_DAILY_TURNOVER = 30_000_000

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
            txt=r.text.strip()
            if not txt or txt.startswith("<"):
                raise ValueError(f"Empty/HTML from {url}")
            return r
        except Exception as e:
            last=e
            time.sleep(2*(i+1))
    raise last
def get_recent_dates(days=15):
    now=datetime.now(TZ)
    return [(now-timedelta(days=i)).strftime("%Y%m%d") for i in range(days)]

def init_db():
    conn=sqlite3.connect(DB_PATH)
    cur=conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS prices (date TEXT, stock_id TEXT, stock_name TEXT, market TEXT, close REAL, volume INTEGER, turnover REAL, PRIMARY KEY(date, stock_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS institutional (date TEXT, stock_id TEXT, foreign_net INTEGER, trust_net INTEGER, dealer_prop INTEGER, dealer_hedge INTEGER, dealer_total INTEGER, market TEXT, PRIMARY KEY(date, stock_id, market))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS revenue (announce_date TEXT, stock_id TEXT, revenue REAL, yoy REAL, mom REAL, PRIMARY KEY(announce_date, stock_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS signals (signal_date TEXT, stock_id TEXT, stock_name TEXT, market TEXT, signal TEXT, reason TEXT, score INTEGER, close_at_signal REAL, trust_5d_net INTEGER, trust_buy_days INTEGER, return_5d REAL, range_10d REAL, dist_ma20 REAL, PRIMARY KEY(signal_date, stock_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS performance (signal_date TEXT, stock_id TEXT, signal TEXT, close_0 REAL, close_5 REAL, ret_5 REAL, close_10 REAL, ret_10 REAL, close_20 REAL, ret_20 REAL, computed_date TEXT, PRIMARY KEY(signal_date, stock_id))""")
    conn.commit(); conn.close()
    # 自動匯入舊CSV
    try:
        files=glob.glob(os.path.join(OUTPUT_DIR,"layout_institutional_history_*.csv"))
        if files:
            print(f"發現舊CSV {len(files)}個，自動匯入DB...")
            conn=sqlite3.connect(DB_PATH)
            all_df=[]
            for f in files[-30:]:
                try:
                    df=pd.read_csv(f, dtype={"stock_id":str})
                    if "trust_net" in df.columns and "date" in df.columns:
                        for c in ["dealer_prop","dealer_hedge","dealer_total","dealer_proprietary_net","dealer_hedge_net"]:
                            if c not in df.columns:
                                df[c]=0
                        if "dealer_prop" not in df.columns and "dealer_proprietary_net" in df.columns:
                            df["dealer_prop"]=df["dealer_proprietary_net"]
                        if "dealer_hedge" not in df.columns and "dealer_hedge_net" in df.columns:
                            df["dealer_hedge"]=df["dealer_hedge_net"]
                        if "dealer_total" not in df.columns and "dealer_net" in df.columns:
                            df["dealer_total"]=df["dealer_net"]
                        tmp=df[["date","stock_id","foreign_net","trust_net","dealer_prop","dealer_hedge","dealer_total"]].copy()
                        tmp["market"]="TWSE"
                        all_df.append(tmp)
                except: pass
            if all_df:
                big=pd.concat(all_df, ignore_index=True)
                big.to_sql("institutional", conn, if_exists="append", index=False, method="multi")
                conn.execute("DELETE FROM institutional WHERE rowid NOT IN (SELECT MIN(rowid) FROM institutional GROUP BY date, stock_id, market)")
                conn.commit()
                print(f"已匯入 {len(big)} 筆舊法人，DB現在有歷史了")
            conn.close()
    except Exception as e:
        print(f"舊CSV匯入跳過 {e}")

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

def load_price_history(days=90):
    conn=sqlite3.connect(DB_PATH)
    try:
        df=pd.read_sql(f"SELECT * FROM prices WHERE date >= date('now','-{days} days','localtime')", conn, dtype={"stock_id":str})
    except:
        df=pd.DataFrame()
    conn.close()
    return df

def load_inst_history(days=90):
    conn=sqlite3.connect(DB_PATH)
    try:
        df=pd.read_sql(f"SELECT * FROM institutional WHERE date >= date('now','-{days} days','localtime')", conn, dtype={"stock_id":str})
    except:
        df=pd.DataFrame()
    conn.close()
    return df

# 雙備援行情
def get_twse_quotes():
    print("取得 TWSE 上市行情 [主線]...")
    try:
        r=request_get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=30)
        data=r.json()
        if isinstance(data, list) and len(data)>0:
            rows=[]
            for it in data:
                sid=normalize_stock_id(it.get("Code",""))
                if not sid.isdigit() or len(sid)!=4: continue
                close=safe_float(it.get("ClosingPrice"))
                if pd.isna(close): continue
                rows.append({"date":get_today_str(),"stock_id":sid,"stock_name":str(it.get("Name","")).strip(),"market":"TWSE","close":close,"volume":safe_int(it.get("TradeVolume")),"turnover":safe_float(it.get("TradeValue"))})
            if rows:
                print(f"TWSE 主線成功 {len(rows)} 筆")
                return pd.DataFrame(rows)
    except Exception as e:
        print(f"TWSE 主線失敗切備援：{e}")

    print("TWSE 走備援線...")
    for d in get_recent_dates(15):
        try:
            url=f"https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json&date={d}"
            r=request_get(url, timeout=30)
            j=r.json()
            if j.get("stat")!="OK": continue
            fields=j.get("fields",[]); raw=j.get("data",[])
            rows=[]
            for row in raw:
                it=dict(zip(fields,row))
                sid=normalize_stock_id(it.get("證券代號",""))
                if not sid.isdigit() or len(sid)!=4: continue
                close=safe_float(it.get("收盤價"))
                if pd.isna(close): continue
                vol=safe_int(it.get("成交股數"))
                turn=safe_float(it.get("成交金額"))
                if not pd.isna(turn) and turn < 10_000_000:  # 千元轉元
                    turn*=1000
                rows.append({"date":get_today_str(),"stock_id":sid,"stock_name":str(it.get("證券名稱","")).strip(),"market":"TWSE","close":close,"volume":vol,"turnover":turn})
            if rows:
                print(f"TWSE 備援成功 {d} {len(rows)} 筆")
                return pd.DataFrame(rows)
        except:
            continue
    raise RuntimeError("TWSE 上市行情主+備皆失敗")

def get_tpex_quotes():
    print("取得 TPEx 上櫃行情...")
    try:
        data=request_get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=30).json()
        rows=[]
        for it in data:
            sid=normalize_stock_id(it.get("SecuritiesCompanyCode") or it.get("SecuritiesCode") or it.get("Code") or "")
            if not sid.isdigit() or len(sid)!=4: continue
            close=safe_float(it.get("Close") or it.get("ClosingPrice"))
            if pd.isna(close): continue
            rows.append({"date":get_today_str(),"stock_id":sid,"stock_name":str(it.get("CompanyName") or it.get("SecuritiesName") or "").strip(),"market":"TPEx","close":close,"volume":safe_int(it.get("Volume") or it.get("TradeVolume")),"turnover":safe_float(it.get("Amount") or it.get("TradeValue"))})
        if rows:
            print(f"TPEx {len(rows)} 筆")
            return pd.DataFrame(rows)
    except Exception as e:
        print(f"TPEx 失敗略過 {e}")
    return pd.DataFrame()

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
        rows.append({"date":fmt,"stock_id":sid,"foreign_net":safe_int(it.get("外陸資買賣超股數(不含外資自營商)")) or 0,"trust_net":safe_int(it.get("投信買賣超股數")) or 0,"dealer_prop":safe_int(it.get("自營商買賣超股數(自行買賣)")) or 0,"dealer_hedge":safe_int(it.get("自營商買賣超股數(避險)")) or 0,"dealer_total":safe_int(it.get("自營商買賣超股數")) or 0})
    return pd.DataFrame(rows)

def get_tpex_institutional():
    print("取得 TPEx 上櫃三大法人...")
    try:
        data=request_get("https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading", timeout=30).json()
        rows=[]
        for it in data:
            sid=normalize_stock_id(it.get("SecuritiesCompanyCode") or it.get("StockID") or it.get("Code") or "")
            if not sid.isdigit() or len(sid)!=4: continue
            rows.append({"date":get_today_str(),"stock_id":sid,"foreign_net":safe_int(it.get("Foreign") or it.get("外資買賣超股數") or 0) or 0,"trust_net":safe_int(it.get("InvestmentTrust") or it.get("投信買賣超股數") or 0) or 0,"dealer_prop":safe_int(it.get("Dealer") or 0) or 0,"dealer_hedge":0,"dealer_total":safe_int(it.get("Dealer") or 0) or 0})
        print(f"TPEx 法人 {len(rows)} 筆")
        return pd.DataFrame(rows)
    except Exception as e:
        print(f"TPEx法人暫不可用：{e}")
        return pd.DataFrame()

def get_monthly_revenue():
    print("取得月營收...")
    urls=["https://mops.twse.com.tw/nas/t21/sii/t21sc03_if.html","https://mops.twse.com.tw/nas/t21/otc/t21sc03_if.html"]
    rows=[]; TODAY=get_today_str()
    for url in urls:
        try:
            resp=request_get(url, timeout=45)
            try: text=resp.content.decode('big5', errors='ignore')
            except: text=resp.text
            import io
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
    total_days=df["date"].nunique()
    is_bootstrap = total_days < 5
    print(f"DB歷史天數 {total_days} 天，{'啟動模式門檻1天' if is_bootstrap else '正常模式門檻3天'}")
    vol_map=price_feat.set_index("stock_id")["avg_vol_5d"].to_dict() if not price_feat.empty else {}
    rows=[]
    for sid, gp in df.groupby("stock_id"):
        gp=gp.tail(TRUST_DAYS_WINDOW)
        t_net=sum(gp["trust_net"]); f_net=sum(gp["foreign_net"]); h_abs=sum(abs(x) for x in gp["dealer_hedge"])
        t_days=sum(1 for x in gp["trust_net"] if x>0)
        available=len(gp)
        needed = 1 if is_bootstrap else min(MIN_TRUST_BUY_DAYS, available)
        avg=vol_map.get(sid, np.nan)
        t_ratio=np.nan if pd.isna(avg) or avg<=0 else t_net/(avg*available)
        h_ratio=np.nan if pd.isna(avg) or avg<=0 else h_abs/(avg*available)
        hedge=(not pd.isna(h_ratio) and h_ratio>=0.03) or (h_abs>=abs(t_net)+abs(f_net) and h_abs>0)
        trust_acc=t_days>=needed and t_net>0 and (pd.isna(t_ratio) or t_ratio>=0.01 or is_bootstrap)
        rows.append({"stock_id":sid,"trust_5d":t_net,"trust_days":t_days,"foreign_5d":f_net,"hedge_abs":h_abs,"trust_vol_ratio":t_ratio,"hedge_dominant":hedge,"trust_acc":trust_acc})
    return pd.DataFrame(rows)

def classify(row):
    sid=row["stock_id"]
    if sid in EXCLUDE_TOOL_STOCKS: return "⚪ 排除：權值／ETF","高權值",-5
    if bool(row.get("hedge_dominant",False)): return "🟠 排除：避險主導","避險高",-4
    ret5=row.get("return_5d",np.nan); volr=row.get("vol_ratio",np.nan); ma20=row.get("dist_ma20",np.nan); ma10=row.get("dist_ma10",np.nan)
    trust_acc=bool(row.get("trust_acc",False))
    if not pd.isna(ret5) and ret5>15: return "🔴 排除：過熱","5日>15%",-3
    if not pd.isna(volr) and volr>3.0 and not pd.isna(ret5) and ret5>5: return "🔴 排除：過熱","爆量",-3
    if row.get("trust_5d",0)<0 and row.get("foreign_5d",0)<0 and row.get("trust_days",0)==0: return "🔴 排除：法人轉賣","同賣",-3
    if not pd.isna(ma20) and ma20<0 and trust_acc: return "🟡 籌碼尚在、價格轉弱","等站回MA20",2
    price_ok=(pd.isna(ret5) or (-10<=ret5<=10)) and (pd.isna(row.get("range_10d",np.nan)) or row.get("range_10d")<=25) and (pd.isna(volr) or volr<=3.0) and (pd.isna(ma20) or abs(ma20)<=15)
    if row.get("turnover",0)>=MIN_DAILY_TURNOVER and trust_acc and price_ok: return "🔵 主動資金疑似布局","投信盤整吸籌",8
    if row.get("turnover",0)>=MIN_DAILY_TURNOVER and trust_acc and not pd.isna(ma10) and 0<=ma10<=12: return "🟢 吸籌延續／初步確認","站上MA10",9
    if row.get("trust_days",0)>=1 and row.get("trust_5d",0)>0: return "⚪ 待驗證：投信單邊","投信1日買",1
    return "⚪ 不列入","未達標",0

def build_radar(quotes, price_feat, inst_feat):
    df=quotes.copy()
    if not price_feat.empty: df=df.merge(price_feat, on="stock_id", how="left", suffixes=("","_pf"))
    if not inst_feat.empty: df=df.merge(inst_feat, on="stock_id", how="left")
    for c,d in [("trust_5d",0),("trust_days",0),("hedge_dominant",False),("trust_acc",False)]:
        if c not in df.columns: df[c]=d
        else: df[c]=df[c].fillna(d)
    cls=df.apply(classify, axis=1, result_type="expand")
    cls.columns=["signal","reason","base_score"]
    df=pd.concat([df, cls], axis=1)
    df["score"]=df["base_score"] + (df["trust_days"]>=3).astype(int)*2 + (df["trust_days"]>=1).astype(int)*1 + df["stock_id"].isin(FOCUS_STOCKS).astype(int)*5
    order={"🔵 主動資金疑似布局":1,"🟢 吸籌延續／初步確認":2,"🟡 籌碼尚在、價格轉弱":3,"⚪ 待驗證：投信單邊":4,"🔴 排除：過熱":5,"🔴 排除：法人轉賣":6,"🟠 排除：避險主導":8,"⚪ 排除：權值／ETF":9,"⚪ 不列入":99}
    df["sort_order"]=df["signal"].map(order).fillna(99)
    return df.sort_values(["sort_order","score","turnover"], ascending=[True,False,False]).drop(columns=["sort_order"])

def update_performance():
    conn=sqlite3.connect(DB_PATH)
    try:
        signals=pd.read_sql("SELECT * FROM signals WHERE signal IN ('🔵 主動資金疑似布局','🟢 吸籌延續／初步確認')", conn, dtype={"stock_id":str})
        prices=pd.read_sql("SELECT date, stock_id, close FROM prices", conn, dtype={"stock_id":str})
    except:
        conn.close(); return
    conn.close()
    if signals.empty: return
    prices["date"]=pd.to_datetime(prices["date"]); signals["signal_date"]=pd.to_datetime(signals["signal_date"])
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
        perf_rows.append({"signal_date":sdate.strftime("%Y-%m-%d"),"stock_id":sid,"signal":sig["signal"],"close_0":close0,"close_5":c5,"ret_5":r5,"close_10":c10,"ret_10":r10,"close_20":c20,"ret_20":r20,"computed_date":get_today_str()})
    if perf_rows:
        conn=sqlite3.connect(DB_PATH)
        pd.DataFrame(perf_rows).to_sql("performance", conn, if_exists="replace", index=False)
        conn.commit(); conn.close()

def send_email(radar):
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]): print("略過寄信"); return
    today=get_today_str()
    def fmt(v): return "-" if pd.isna(v) else f"{float(v):.2f}"
    def lines(sub_df, n):
        if sub_df.empty: return "（無）"
        out=[]
        for _,r in sub_df.head(n).iterrows():
            tag=" ⭐" if r["stock_id"] in FOCUS_STOCKS else ""
            theme="/".join(THEME_MAP.get(r["stock_id"],[]))
            out.append(f"{r['signal']}{tag}｜{r['stock_id']} {r['stock_name']} {theme}｜收盤 {fmt(r['close'])} 投信{int(r.get('trust_5d',0)/1000):+d}張 {int(r.get('trust_days',0))}日")
        return "\n".join(out)
    blue=radar[radar["signal"]=="🔵 主動資金疑似布局"]; green=radar[radar["signal"]=="🟢 吸籌延續／初步確認"]; yellow=radar[radar["signal"]=="🟡 籌碼尚在、價格轉弱"]; focus=radar[radar["stock_id"].isin(FOCUS_STOCKS)]
    body=f"台股雷達 v7.2 {today} {datetime.now(TZ).strftime('%H:%M')}\n\n⭐ 焦點7檔\n{lines(focus,10)}\n\n━━━━━━━━━━━━\nA. 🔵 布局 {len(blue)}\n{lines(blue,30)}\n\nB. 🟢 延續 {len(green)}\n{lines(green,20)}\n"
    msg=MIMEMultipart(); msg["From"]=GMAIL_USER; msg["To"]=RECIPIENT_EMAIL; msg["Subject"]=f"雷達 v7.2｜{today}"; msg.attach(MIMEText(body,"plain","utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com",465) as s: s.login(GMAIL_USER,GMAIL_APP_PASSWORD); s.send_message(msg)

def main():
    init_db()
    print(f"=== v7.2 開始 {datetime.now(TZ)} ===")
    quotes=pd.concat([get_twse_quotes(), get_tpex_quotes()], ignore_index=True).drop_duplicates(subset=["stock_id"])
    tw_inst=get_twse_institutional()
    tp_inst=get_tpex_institutional()
    rev=get_monthly_revenue()
    save_prices(quotes); save_institutional(tw_inst, "TWSE")
    if not tp_inst.empty: save_institutional(tp_inst, "TPEx")
    save_revenue(rev)
    hist_price=load_price_history(90); hist_inst=load_inst_history(90)
    pf=make_price_features(quotes, hist_price)
    all_inst=pd.concat([hist_inst, tw_inst, tp_inst], ignore_index=True) if not hist_inst.empty else pd.concat([tw_inst, tp_inst], ignore_index=True)
    inf=make_inst_features(all_inst, pf)
    radar=build_radar(quotes, pf, inf)
    today=get_today_str()
    sig_df=radar[["stock_id"]].copy()
    sig_df["signal_date"]=today; sig_df["stock_name"]=radar["stock_name"]; sig_df["market"]=radar["market"]; sig_df["signal"]=radar["signal"]; sig_df["reason"]=radar["reason"]; sig_df["score"]=radar["score"]; sig_df["close_at_signal"]=radar["close"]; sig_df["trust_5d_net"]=radar.get("trust_5d",0); sig_df["trust_buy_days"]=radar.get("trust_days",0); sig_df["return_5d"]=radar.get("return_5d",np.nan); sig_df["range_10d"]=radar.get("range_10d",np.nan); sig_df["dist_ma20"]=radar.get("dist_ma20",np.nan)
    conn=sqlite3.connect(DB_PATH); sig_df.to_sql("signals", conn, if_exists="append", index=False); conn.commit(); conn.close()
    radar.to_csv(os.path.join(OUTPUT_DIR, f"radar_{today}.csv"), index=False, encoding="utf-8-sig")
    update_performance(); send_email(radar)
    print("=== v7.2 完成 ===")

if __name__=="__main__":
    try: main()
    except Exception: traceback.print_exc(); raise
