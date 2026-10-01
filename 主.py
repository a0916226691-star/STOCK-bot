# -*- coding: utf-8 -*-
"""
台股雷達 v7.4 - 修復 KeyError: 'stock_name'
原因：當TWSE+TPEx當天都抓不到，或舊DB匯入失敗導致quotes空，radar就沒有stock_name欄位
修正：
1. quotes若空，自動用DB最後一天的價格當備援
2. build_radar 保證保留 stock_name
3. signals 寫入前檢查欄位是否存在，避免KeyError
4. 舊CSV匯入容錯，失敗就跳過不影響主流程
"""
import os, glob, sqlite3, time
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

HEADERS = {"User-Agent":"Mozilla/5.0 Chrome/124.0","Referer":"https://www.twse.com.tw/"}
FOCUS_STOCKS = ["2383","2368","6197","3293","4763","1808","6919"]
EXCLUDE_TOOL_STOCKS={"2330","2454","2308","3711","2881","2882","2884","2886","2891","2892","2880","0050","0056","00878","006208","00919","00929"}
MIN_DAILY_TURNOVER=30_000_000

def get_today_str(): return datetime.now(TZ).strftime("%Y-%m-%d")
def safe_float(v):
    try:
        t=str(v).strip().replace(",","").replace("＋","+").replace("－","-")
        if t.startswith("+"): t=t[1:]
        if t in ["","--","---"]: return np.nan
        return float(t)
    except: return np.nan
def safe_int(v):
    n=safe_float(v); return np.nan if pd.isna(n) else int(n)
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
            if not txt or txt.startswith("<"): raise ValueError("Empty")
            return r
        except Exception as e: last=e; time.sleep(2*(i+1))
    raise last
def get_recent_dates(days=15):
    now=datetime.now(TZ); return [(now-timedelta(days=i)).strftime("%Y%m%d") for i in range(days)]

def init_db():
    conn=sqlite3.connect(DB_PATH)
    cur=conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS prices (date TEXT, stock_id TEXT, stock_name TEXT, market TEXT, close REAL, volume INTEGER, turnover REAL, PRIMARY KEY(date, stock_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS institutional (date TEXT, stock_id TEXT, foreign_net INTEGER, trust_net INTEGER, dealer_prop INTEGER, dealer_hedge INTEGER, dealer_total INTEGER, market TEXT, PRIMARY KEY(date, stock_id, market))")
    cur.execute("CREATE TABLE IF NOT EXISTS signals (signal_date TEXT, stock_id TEXT, stock_name TEXT, market TEXT, signal TEXT, reason TEXT, score INTEGER, close_at_signal REAL, trust_5d_net INTEGER, trust_buy_days INTEGER, PRIMARY KEY(signal_date, stock_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS performance (signal_date TEXT, stock_id TEXT, signal TEXT, close_0 REAL, close_5 REAL, ret_5 REAL, close_10 REAL, ret_10 REAL, close_20 REAL, ret_20 REAL, computed_date TEXT, PRIMARY KEY(signal_date, stock_id))")
    conn.commit(); conn.close()
    # 舊CSV匯入 - 容錯版，不影響主流程
    try:
        files=glob.glob(os.path.join(OUTPUT_DIR,"layout_institutional_history_*.csv"))
        if files:
            print(f"[init] 發現舊CSV {len(files)}個，嘗試匯入...")
            conn=sqlite3.connect(DB_PATH)
            imported=0
            for f in files[-30:]:
                try:
                    df=pd.read_csv(f, dtype={"stock_id":str})
                    if "trust_net" not in df.columns or "date" not in df.columns: continue
                    need=["date","stock_id","foreign_net","trust_net"]
                    if not all(c in df.columns for c in need): continue
                    for c in ["dealer_prop","dealer_hedge","dealer_total"]:
                        if c not in df.columns: df[c]=0
                    tmp=df[["date","stock_id","foreign_net","trust_net","dealer_prop","dealer_hedge","dealer_total"]].copy()
                    tmp["market"]="TWSE"
                    for d in tmp["date"].unique():
                        try: conn.execute("DELETE FROM institutional WHERE date=? AND market='TWSE'", (d,))
                        except: pass
                    tmp.to_sql("institutional", conn, if_exists="append", index=False, method="multi")
                    imported+=len(tmp)
                except Exception as e:
                    print(f"[init] 舊檔 {f} 跳過 {e}")
                    continue
            conn.commit(); conn.close()
            print(f"[init] 舊CSV匯入完成 {imported} 筆")
    except Exception as e:
        print(f"[init] 舊CSV匯入整體跳過 {e}")

def save_prices(df):
    if df is None or df.empty: 
        print("save_prices 空，跳過")
        return
    # 保證必要欄位
    for c in ["date","stock_id","stock_name","market","close","volume","turnover"]:
        if c not in df.columns: df[c]=None
    conn=sqlite3.connect(DB_PATH)
    try:
        for d in df["date"].dropna().unique():
            conn.execute("DELETE FROM prices WHERE date=?", (d,))
        df.to_sql("prices", conn, if_exists="append", index=False, method="multi")
        conn.commit()
        print(f"save_prices {len(df)} 筆")
    except Exception as e:
        print(f"save_prices 錯誤改用逐筆 REPLACE {e}")
        try:
            conn.rollback()
            for _, row in df.iterrows():
                conn.execute("INSERT OR REPLACE INTO prices (date, stock_id, stock_name, market, close, volume, turnover) VALUES (?,?,?,?,?,?,?)",
                             (str(row.get("date")), str(row.get("stock_id")), str(row.get("stock_name","")), str(row.get("market","")), float(row["close"]) if pd.notna(row.get("close")) else None, int(row["volume"]) if pd.notna(row.get("volume")) else None, float(row["turnover"]) if pd.notna(row.get("turnover")) else None))
            conn.commit()
        except Exception as e2:
            print(f"逐筆也失敗 {e2}")
    finally:
        conn.close()

def save_institutional(df, market):
    if df is None or df.empty: return
    for c in ["date","stock_id","foreign_net","trust_net","dealer_prop","dealer_hedge","dealer_total"]:
        if c not in df.columns: df[c]=0
    conn=sqlite3.connect(DB_PATH)
    try:
        for d in df["date"].dropna().unique():
            conn.execute("DELETE FROM institutional WHERE date=? AND market=?", (str(d), market))
        df["market"]=market
        df.to_sql("institutional", conn, if_exists="append", index=False, method="multi")
        conn.commit()
    except Exception as e:
        print(f"save_inst {market} 錯誤改用REPLACE {e}")
        try:
            conn.rollback()
            df["market"]=market
            for _, row in df.iterrows():
                conn.execute("INSERT OR REPLACE INTO institutional (date, stock_id, foreign_net, trust_net, dealer_prop, dealer_hedge, dealer_total, market) VALUES (?,?,?,?,?,?,?,?)",
                             (str(row["date"]), str(row["stock_id"]), int(row["foreign_net"]) if pd.notna(row["foreign_net"]) else 0, int(row["trust_net"]) if pd.notna(row["trust_net"]) else 0, int(row["dealer_prop"]) if pd.notna(row.get("dealer_prop")) else 0, int(row["dealer_hedge"]) if pd.notna(row.get("dealer_hedge")) else 0, int(row["dealer_total"]) if pd.notna(row.get("dealer_total")) else 0, market))
            conn.commit()
        except Exception as e2:
            print(e2)
    finally:
        conn.close()

def load_price_history(days=90):
    conn=sqlite3.connect(DB_PATH)
    try: df=pd.read_sql(f"SELECT * FROM prices WHERE date >= date('now','-{days} days','localtime')", conn, dtype={"stock_id":str})
    except: df=pd.DataFrame()
    conn.close(); return df

def load_inst_history(days=90):
    conn=sqlite3.connect(DB_PATH)
    try: df=pd.read_sql(f"SELECT * FROM institutional WHERE date >= date('now','-{days} days','localtime')", conn, dtype={"stock_id":str})
    except: df=pd.DataFrame()
    conn.close(); return df

def get_twse_quotes():
    print("TWSE 主線...")
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
                print(f"TWSE 主線 {len(rows)}")
                return pd.DataFrame(rows)
    except Exception as e: print(f"主線失敗 {e}")
    print("TWSE 備援...")
    for d in get_recent_dates(15):
        try:
            url=f"https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json&date={d}"
            r=request_get(url, timeout=30); j=r.json()
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
                if not pd.isna(turn) and turn < 10000000: turn*=1000
                rows.append({"date":get_today_str(),"stock_id":sid,"stock_name":str(it.get("證券名稱","")).strip(),"market":"TWSE","close":close,"volume":vol,"turnover":turn})
            if rows: 
                print(f"備援 {d} {len(rows)}")
                return pd.DataFrame(rows)
        except: continue
    print("TWSE 皆失敗，回空")
    return pd.DataFrame()

def get_tpex_quotes():
    print("TPEx...")
    try:
        data=request_get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=30).json()
        rows=[]
        for it in data:
            sid=normalize_stock_id(it.get("SecuritiesCompanyCode") or it.get("Code") or "")
            if not sid.isdigit() or len(sid)!=4: continue
            close=safe_float(it.get("Close") or it.get("ClosingPrice"))
            if pd.isna(close): continue
            rows.append({"date":get_today_str(),"stock_id":sid,"stock_name":str(it.get("CompanyName") or it.get("SecuritiesName") or "").strip(),"market":"TPEx","close":close,"volume":safe_int(it.get("Volume")),"turnover":safe_float(it.get("Amount"))})
        if rows: 
            print(f"TPEx {len(rows)}")
            return pd.DataFrame(rows)
    except Exception as e: print(f"TPEx失敗 {e}")
    return pd.DataFrame()

def get_twse_institutional():
    url="https://www.twse.com.tw/rwd/zh/fund/T86"
    data=None; used=None
    for d in get_recent_dates(15):
        try:
            j=request_get(url, params={"response":"json","date":d,"selectType":"ALLBUT0999"}, timeout=15).json()
            if j.get("stat")=="OK": data=j; used=d; break
        except: continue
    if not data: 
        print("法人無資料，回空")
        return pd.DataFrame()
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
    try:
        data=request_get("https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading", timeout=30).json()
        rows=[]
        for it in data:
            sid=normalize_stock_id(it.get("SecuritiesCompanyCode") or "")
            if not sid.isdigit() or len(sid)!=4: continue
            rows.append({"date":get_today_str(),"stock_id":sid,"foreign_net":safe_int(it.get("Foreign") or 0) or 0,"trust_net":safe_int(it.get("InvestmentTrust") or 0) or 0,"dealer_prop":0,"dealer_hedge":0,"dealer_total":0})
        return pd.DataFrame(rows)
    except: return pd.DataFrame()

def make_price_features(today_quotes, hist_prices):
    if today_quotes is None or today_quotes.empty:
        print("make_price_features today空，回空")
        return pd.DataFrame()
    today=today_quotes.copy()
    full=today if hist_prices is None or hist_prices.empty else pd.concat([hist_prices, today], ignore_index=True)
    for c in ["close","volume","turnover"]: 
        if c in full.columns: full[c]=pd.to_numeric(full[c], errors="coerce")
    full=full.drop_duplicates(subset=["stock_id","date"]).sort_values(["stock_id","date"])
    if full.empty: return pd.DataFrame()
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
    out=full[full["date"]==today_str]
    print(f"price_features {len(out)}")
    return out

def make_inst_features(inst_hist, price_feat):
    if inst_hist is None or inst_hist.empty: 
        print("inst_hist 空")
        return pd.DataFrame()
    df=inst_hist.copy()
    for c in ["foreign_net","trust_net"]: df[c]=pd.to_numeric(df[c], errors="coerce").fillna(0)
    df=df.sort_values(["stock_id","date"])
    total_days=df["date"].nunique()
    is_bootstrap = total_days < 5
    print(f"DB天數 {total_days} 啟動模式={is_bootstrap}")
    rows=[]
    for sid, gp in df.groupby("stock_id"):
        gp=gp.tail(5)
        t_net=sum(gp["trust_net"]); f_net=sum(gp["foreign_net"])
        t_days=sum(1 for x in gp["trust_net"] if x>0)
        needed = 1 if is_bootstrap else 3
        trust_acc=t_days>=needed and t_net>0
        rows.append({"stock_id":sid,"trust_5d":t_net,"trust_days":t_days,"foreign_5d":f_net,"trust_acc":trust_acc})
    return pd.DataFrame(rows)

def classify(row):
    sid=str(row.get("stock_id",""))
    if sid in EXCLUDE_TOOL_STOCKS: return "⚪ 排除：權值／ETF","高權值",-5
    ret5=row.get("return_5d",np.nan)
    trust_acc=bool(row.get("trust_acc",False))
    if not pd.isna(ret5) and ret5>15: return "🔴 排除：過熱","過熱",-3
    if trust_acc and row.get("turnover",0)>=30000000:
        return "🔵 主動資金疑似布局","投信吸籌",8
    if trust_acc:
        return "⚪ 待驗證：投信單邊","投信1日買",1
    return "⚪ 不列入","未達標",0

def build_radar(quotes, price_feat, inst_feat):
    if quotes is None or quotes.empty:
        print("build_radar quotes空，用price_feat當底")
        if price_feat is not None and not price_feat.empty:
            df=price_feat.copy()
        else:
            print("兩者皆空，回空radar")
            return pd.DataFrame(columns=["stock_id","stock_name","market","close","volume","turnover","signal","reason","score"])
    else:
        df=quotes.copy()
    if price_feat is not None and not price_feat.empty:
        # 避免重複欄位衝突
        keep_cols=[c for c in price_feat.columns if c not in ["stock_name","market"]]
        df=df.merge(price_feat[keep_cols], on=["stock_id","date"], how="left") if "date" in df.columns and "date" in price_feat.columns else df.merge(price_feat[keep_cols], on="stock_id", how="left")
    if inst_feat is not None and not inst_feat.empty:
        df=df.merge(inst_feat, on="stock_id", how="left")
    for c,d in [("trust_5d",0),("trust_days",0),("trust_acc",False),("stock_name",""),("market",""),("close",0),("volume",0),("turnover",0)]:
        if c not in df.columns: df[c]=d
        else: df[c]=df[c].fillna(d) if c not in ["stock_name","market"] else df[c].fillna("")
    if df.empty:
        return df
    cls=df.apply(classify, axis=1, result_type="expand")
    cls.columns=["signal","reason","base_score"]
    df=pd.concat([df.reset_index(drop=True), cls.reset_index(drop=True)], axis=1)
    df["score"]=df["base_score"] + df["stock_id"].isin(FOCUS_STOCKS).astype(int)*5
    order={"🔵 主動資金疑似布局":1,"⚪ 待驗證：投信單邊":4,"⚪ 不列入":99}
    df["sort_order"]=df["signal"].map(order).fillna(99)
    df=df.sort_values(["sort_order","score"], ascending=[True,False]).drop(columns=["sort_order"])
    print(f"build_radar 完成 {len(df)} 檔，藍 {len(df[df['signal'].str.contains('布局')])}")
    return df

def send_email(radar):
    import os, smtplib
    GMAIL_USER=os.getenv("GMAIL_USER"); GMAIL_APP_PASSWORD=os.getenv("GMAIL_APP_PASSWORD"); RECIPIENT_EMAIL=os.getenv("RECIPIENT_EMAIL")
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]): print("略過寄信"); return
    if radar is None or radar.empty:
        print("radar空，不寄信")
        return
    today=get_today_str()
    def lines(sub_df, n):
        if sub_df is None or sub_df.empty: return "（無）"
        out=[]
        for _,r in sub_df.head(n).iterrows():
            try:
                sname=r.get("stock_name","")
                out.append(f"{r.get('signal','')}｜{r.get('stock_id','')} {sname} 投信{int(r.get('trust_5d',0)/1000):+d}張")
            except: 
                out.append(f"{r.get('stock_id','')}")
        return "\n".join(out) if out else "（無）"
    blue=radar[radar["signal"]=="🔵 主動資金疑似布局"] if "signal" in radar.columns else pd.DataFrame()
    focus=radar[radar["stock_id"].isin(FOCUS_STOCKS)] if "stock_id" in radar.columns else pd.DataFrame()
    body=f"台股雷達 v7.4 {today}\n\n⭐ 焦點\n{lines(focus,10)}\n\n🔵 布局 {len(blue)}\n{lines(blue,30)}\n"
    msg=MIMEMultipart(); msg["From"]=GMAIL_USER; msg["To"]=RECIPIENT_EMAIL; msg["Subject"]=f"雷達 v7.4｜{today}"; msg.attach(MIMEText(body,"plain","utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com",465) as s: s.login(GMAIL_USER,GMAIL_APP_PASSWORD); s.send_message(msg)
    print("Email已寄")

def main():
    init_db()
    print(f"=== v7.4 開始 {datetime.now(TZ)} ===")
    tw_quotes=get_twse_quotes()
    tp_quotes=get_tpex_quotes()
    quotes=pd.concat([tw_quotes, tp_quotes], ignore_index=True) if not (tw_quotes.empty and tp_quotes.empty) else pd.DataFrame()
    if not quotes.empty:
        quotes=quotes.drop_duplicates(subset=["stock_id"])
        print(f"quotes 合併 {len(quotes)}")
    else:
        print("當天行情皆空，嘗試用DB最後一天當備援")
        hist=load_price_history(5)
        if not hist.empty:
            last_date=hist["date"].max()
            quotes=hist[hist["date"]==last_date].copy()
            quotes["date"]=get_today_str()
            print(f"用DB備援 {last_date} {len(quotes)} 筆")
        else:
            print("DB也空，今天無法產生radar")
            quotes=pd.DataFrame(columns=["date","stock_id","stock_name","market","close","volume","turnover"])

    tw_inst=get_twse_institutional()
    tp_inst=get_tpex_institutional()

    save_prices(quotes)
    if not tw_inst.empty: save_institutional(tw_inst, "TWSE")
    if not tp_inst.empty: save_institutional(tp_inst, "TPEx")

    hist_price=load_price_history(90)
    hist_inst=load_inst_history(90)

    pf=make_price_features(quotes, hist_price)
    # all institutional
    frames=[x for x in [hist_inst, tw_inst, tp_inst] if x is not None and not x.empty]
    all_inst=pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    inf=make_inst_features(all_inst, pf)

    radar=build_radar(quotes, pf, inf)

    today=get_today_str()
    # signals 先刪再寫，容錯
    try:
        conn=sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM signals WHERE signal_date=?", (today,))
        conn.commit(); conn.close()
    except: pass

    if radar is not None and not radar.empty:
        sig_df=pd.DataFrame()
        sig_df["stock_id"]=radar["stock_id"] if "stock_id" in radar.columns else []
        sig_df["signal_date"]=today
        sig_df["stock_name"]=radar["stock_name"] if "stock_name" in radar.columns else ""
        sig_df["market"]=radar["market"] if "market" in radar.columns else ""
        sig_df["signal"]=radar["signal"] if "signal" in radar.columns else ""
        sig_df["reason"]=radar["reason"] if "reason" in radar.columns else ""
        sig_df["score"]=radar["score"] if "score" in radar.columns else 0
        sig_df["close_at_signal"]=radar["close"] if "close" in radar.columns else 0
        sig_df["trust_5d_net"]=radar["trust_5d"] if "trust_5d" in radar.columns else 0
        sig_df["trust_buy_days"]=radar["trust_days"] if "trust_days" in radar.columns else 0
        try:
            conn=sqlite3.connect(DB_PATH)
            sig_df.to_sql("signals", conn, if_exists="append", index=False)
            conn.commit(); conn.close()
            print(f"signals 寫入 {len(sig_df)}")
        except Exception as e:
            print(f"signals寫入失敗 {e}")
        try:
            radar.to_csv(os.path.join(OUTPUT_DIR, f"radar_{today}.csv"), index=False, encoding="utf-8-sig")
        except Exception as e:
            print(f"csv寫入失敗 {e}")
        send_email(radar)
    else:
        print("radar空，不寫signals與csv")

    print("=== v7.4 完成 ===")

if __name__=="__main__": main()
