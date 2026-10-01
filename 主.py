# -*- coding: utf-8 -*-
"""
台股雷達 v8.0 搭順風車版 - 買在爆發前，全市場掃描第一根

【用戶本意整理 - 照這個重寫】
1. 資金小，等沒用，誰資金大？外資、投信、主力、大戶
2. 我觀察他們，跟他們一起買、一起賣，不然被套
3. 為什麼買在還沒爆發前？就是一直買一直等，等一堆散戶衝進來買的時候，馬上拋出讓自己賺錢，別人被套在山上
4. 那7檔 2383 2368 6197 3293 4763 1808 6919 只是我要看的而已，誰買多少誰賣多少，不是焦點，不用加分
5. 真正的焦點是 全台股上市上櫃 有符合「剛好突破前高、剛突破長期盤整」第一根起漲的

【此版修正 - 詳細說明】
- 移除 FOCUS_STOCKS 加5分邏輯，改成 WATCHLIST 僅觀察，不影響排名
- 新增吸籌末端判斷：量還沒爆、價格還在盤整末端、接近20日高點
- 新增第一根起漲判斷：收盤價 / 20日高點 >0.97，代表快要突破
- 新增已爆發勿追：量比>2.5且5日漲>12% = 散戶衝進來的地方，應該是賣點不是買點
- 保留Perplexity建議：成交金額>=3000萬、避險排除、權值ETF排除、過熱排除、備援機制
- 保留土洋邏輯：土洋聯手會噴、先洋後土、洋買土賣觀察、洋賣土買接刀注意、雙殺先跑
- 保留5/10/20：MA5週線、MA10十日、MA20月線生命線，多頭排列5>10>20
- 崩潰修復：先DELETE再INSERT、空表保護、舊CSV容錯、TWSE openapi空回應切備援
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
# 只是觀察用，不加分，不當焦點
WATCHLIST = ["2383","2368","6197","3293","4763","1808","6919"]
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
            if not txt or txt.startswith("<"): raise ValueError("Empty/HTML")
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
    cur.execute("CREATE TABLE IF NOT EXISTS signals (signal_date TEXT, stock_id TEXT, stock_name TEXT, market TEXT, signal TEXT, reason TEXT, score INTEGER, close_at_signal REAL, trust_5d_net INTEGER, foreign_5d_net INTEGER, ma5 REAL, ma10 REAL, ma20 REAL, tech TEXT, PRIMARY KEY(signal_date, stock_id))")
    conn.commit(); conn.close()

def save_prices(df):
    if df is None or df.empty: return
    conn=sqlite3.connect(DB_PATH)
    try:
        for d in df["date"].dropna().unique():
            conn.execute("DELETE FROM prices WHERE date=?", (str(d),))
        df.to_sql("prices", conn, if_exists="append", index=False, method="multi")
        conn.commit()
    except Exception as e:
        print(f"save_prices fallback {e}")
        conn.rollback()
        for _, row in df.iterrows():
            try:
                conn.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?)",
                             (str(row.get("date")), str(row.get("stock_id")), str(row.get("stock_name","")), str(row.get("market","")), float(row["close"]) if pd.notna(row.get("close")) else None, int(row["volume"]) if pd.notna(row.get("volume")) else None, float(row["turnover"]) if pd.notna(row.get("turnover")) else None))
            except: pass
        conn.commit()
    finally: conn.close()

def save_institutional(df, market):
    if df is None or df.empty: return
    conn=sqlite3.connect(DB_PATH)
    try:
        for d in df["date"].dropna().unique():
            conn.execute("DELETE FROM institutional WHERE date=? AND market=?", (str(d), market))
        df["market"]=market
        df.to_sql("institutional", conn, if_exists="append", index=False, method="multi")
        conn.commit()
    except Exception as e:
        print(f"save_inst fallback {e}")
        conn.rollback()
        df["market"]=market
        for _, row in df.iterrows():
            try:
                conn.execute("INSERT OR REPLACE INTO institutional VALUES (?,?,?,?,?,?,?,?)",
                             (str(row["date"]), str(row["stock_id"]), int(row["foreign_net"]) if pd.notna(row["foreign_net"]) else 0, int(row["trust_net"]) if pd.notna(row["trust_net"]) else 0, int(row.get("dealer_prop",0)) if pd.notna(row.get("dealer_prop",0)) else 0, int(row.get("dealer_hedge",0)) if pd.notna(row.get("dealer_hedge",0)) else 0, int(row.get("dealer_total",0)) if pd.notna(row.get("dealer_total",0)) else 0, market))
            except: pass
        conn.commit()
    finally: conn.close()

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
    print("TWSE 皆失敗")
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
            rows.append({"date":get_today_str(),"stock_id":sid,"stock_name":str(it.get("CompanyName") or "").strip(),"market":"TPEx","close":close,"volume":safe_int(it.get("Volume")),"turnover":safe_float(it.get("Amount"))})
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
        print("法人無資料")
        return pd.DataFrame()
    fields=data["fields"]; raw=data["data"]
    fmt=f"{used[:4]}-{used[4:6]}-{used[6:]}"
    rows=[]
    for r in raw:
        it=dict(zip(fields,r))
        sid=normalize_stock_id(it.get("證券代號",""))
        if not sid.isdigit() or len(sid)!=4: continue
        rows.append({"date":fmt,"stock_id":sid,"foreign_net":safe_int(it.get("外陸資買賣超股數(不含外資自營商)")) or 0,"trust_net":safe_int(it.get("投信買賣超股數")) or 0,"dealer_prop":safe_int(it.get("自營商買賣超股數(自行買賣)")) or 0,"dealer_hedge":safe_int(it.get("自營商買賣超股數(避險)")) or 0,"dealer_total":safe_int(it.get("自營商買賣超股數")) or 0})
    print(f"TWSE法人 {len(rows)}")
    return pd.DataFrame(rows)

def get_tpex_institutional():
    try:
        data=request_get("https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading", timeout=30).json()
        rows=[]
        for it in data:
            sid=normalize_stock_id(it.get("SecuritiesCompanyCode") or "")
            if not sid.isdigit() or len(sid)!=4: continue
            rows.append({"date":get_today_str(),"stock_id":sid,"foreign_net":safe_int(it.get("Foreign") or 0) or 0,"trust_net":safe_int(it.get("InvestmentTrust") or 0) or 0,"dealer_prop":0,"dealer_hedge":0,"dealer_total":0})
        if rows: print(f"TPEx法人 {len(rows)}")
        return pd.DataFrame(rows)
    except: return pd.DataFrame()

def make_price_features(today_quotes, hist_prices):
    if today_quotes is None or today_quotes.empty:
        print("今日行情空")
        return pd.DataFrame()
    today=today_quotes.copy()
    full=today if hist_prices is None or hist_prices.empty else pd.concat([hist_prices, today], ignore_index=True)
    for c in ["close","volume","turnover"]:
        if c in full.columns: full[c]=pd.to_numeric(full[c], errors="coerce")
    full=full.drop_duplicates(subset=["stock_id","date"]).sort_values(["stock_id","date"])
    if full.empty: return pd.DataFrame()
    g=full.groupby("stock_id", group_keys=False)
    full["ma5"]=g["close"].transform(lambda x: x.rolling(5, min_periods=5).mean())
    full["ma10"]=g["close"].transform(lambda x: x.rolling(10, min_periods=10).mean())
    full["ma20"]=g["close"].transform(lambda x: x.rolling(20, min_periods=20).mean())
    full["close_5d_ago"]=g["close"].transform(lambda x: x.shift(4))
    full["return_5d"]=(full["close"]/full["close_5d_ago"]-1)*100
    full["high_10d"]=g["close"].transform(lambda x: x.rolling(10, min_periods=10).max())
    full["low_10d"]=g["close"].transform(lambda x: x.rolling(10, min_periods=10).min())
    full["high_20d"]=g["close"].transform(lambda x: x.rolling(20, min_periods=20).max())
    full["low_20d"]=g["close"].transform(lambda x: x.rolling(20, min_periods=20).min())
    full["range_10d"]=(full["high_10d"]/full["low_10d"]-1)*100
    full["range_20d"]=(full["high_20d"]/full["low_20d"]-1)*100
    full["avg_vol_5d"]=g["volume"].transform(lambda x: x.shift(1).rolling(5, min_periods=5).mean())
    full["avg_vol_20d"]=g["volume"].transform(lambda x: x.shift(1).rolling(20, min_periods=20).mean())
    full["vol_ratio_5d"]=full["volume"]/full["avg_vol_5d"]
    full["vol_ratio_20d"]=full["volume"]/full["avg_vol_20d"]
    full["dist_ma5"]=(full["close"]/full["ma5"]-1)*100
    full["dist_ma10"]=(full["close"]/full["ma10"]-1)*100
    full["dist_ma20"]=(full["close"]/full["ma20"]-1)*100
    full["break_high_20d_ratio"]=full["close"]/full["high_20d"]
    full["is_multi_up"]=(full["ma5"]>full["ma10"]) & (full["ma10"]>full["ma20"]) & (full["close"]>full["ma5"])
    full["above_ma20"]=full["close"]>full["ma20"]
    full["above_ma10"]=full["close"]>full["ma10"]
    # 吸籌末端：還在盤整，沒有爆大量，接近20日高點
    full["is_consolidation"]=full["range_20d"]<18  # 20日高低差<18%算盤整
    full["near_high"]=full["break_high_20d_ratio"]>=0.97  # 收盤價在20日高點97%以上，準備突破
    full["pre_breakout"]=full["is_consolidation"] & full["near_high"] & (full["vol_ratio_5d"]<2.0) & (full["return_5d"]<8)
    # 已爆發：量爆2.5倍以上且5日大漲>12%，散戶衝進來的地方，應該賣不是買
    full["exploded"]=(full["vol_ratio_5d"]>=2.5) & (full["return_5d"]>=12)
    full["overheat"]=(full["dist_ma20"]>12) | (full["dist_ma10"]>10) | (full["return_5d"]>15)
    today_str=get_today_str()
    out=full[full["date"]==today_str]
    print(f"price_features {len(out)} 吸籌末端 {sum(out['pre_breakout'])} 已爆發 {sum(out['exploded'])}")
    return out

def make_inst_features(inst_hist, price_feat):
    if inst_hist is None or inst_hist.empty:
        print("法人空")
        return pd.DataFrame()
    df=inst_hist.copy()
    for c in ["foreign_net","trust_net","dealer_hedge"]: df[c]=pd.to_numeric(df[c], errors="coerce").fillna(0)
    df=df.sort_values(["stock_id","date"])
    total_days=df["date"].nunique()
    is_bootstrap=total_days<5
    print(f"DB天數 {total_days} 啟動={is_bootstrap}")
    vol_map=price_feat.set_index("stock_id")["avg_vol_5d"].to_dict() if price_feat is not None and not price_feat.empty and "avg_vol_5d" in price_feat.columns else {}
    rows=[]
    for sid, gp in df.groupby("stock_id"):
        gp=gp.tail(5)
        t_net=sum(gp["trust_net"]); f_net=sum(gp["foreign_net"]); h_abs=sum(abs(x) for x in gp["dealer_hedge"])
        t_days=sum(1 for x in gp["trust_net"] if x>0)
        f_days=sum(1 for x in gp["foreign_net"] if x>0)
        recent=gp.tail(2)
        recent_t=sum(recent["trust_net"])
        early=gp.head(3) if len(gp)>=3 else gp
        early_f=sum(early["foreign_net"])
        is_yang_first=early_f>0 and recent_t>0 and f_net>0
        avg=vol_map.get(sid, np.nan)
        t_ratio=np.nan if pd.isna(avg) or avg<=0 else t_net/(avg*len(gp))
        h_ratio=np.nan if pd.isna(avg) or avg<=0 else h_abs/(avg*len(gp))
        hedge_dominant=(not pd.isna(h_ratio) and h_ratio>=0.03) or (h_abs>=abs(t_net)+abs(f_net) and h_abs>0)
        needed=1 if is_bootstrap else 2
        trust_acc=t_days>=needed and t_net>0 and not hedge_dominant
        foreign_acc=f_days>=needed and f_net>0 and abs(f_net)>=100000
        rows.append({
            "stock_id":sid,"trust_5d":t_net,"trust_days":t_days,"trust_acc":trust_acc,"trust_ratio":t_ratio,
            "foreign_5d":f_net,"foreign_days":f_days,"foreign_acc":foreign_acc,
            "hedge_dominant":hedge_dominant,"yang_first":is_yang_first
        })
    df_out=pd.DataFrame(rows)
    print(f"inst_features {len(df_out)} 避險排除 {sum(df_out['hedge_dominant'])}")
    return df_out

def classify(row):
    sid=str(row.get("stock_id",""))
    if sid in EXCLUDE_TOOL_STOCKS: return "⚪ 排除：權值／ETF","高權值/ETF",-5,""
    if bool(row.get("hedge_dominant",False)): return "🟠 排除：避險主導","避險量大",-4,""
    ret5=row.get("return_5d",np.nan); range20=row.get("range_20d",np.nan); vol5=row.get("vol_ratio_5d",np.nan)
    trust_acc=bool(row.get("trust_acc",False)); foreign_acc=bool(row.get("foreign_acc",False))
    trust_5d=row.get("trust_5d",0); foreign_5d=row.get("foreign_5d",0)
    yang_first=bool(row.get("yang_first",False))
    turnover=row.get("turnover",0)
    is_multi_up=bool(row.get("is_multi_up",False)); above_ma20=bool(row.get("above_ma20",False)); above_ma10=bool(row.get("above_ma10",False))
    overheat=bool(row.get("overheat",False)); exploded=bool(row.get("exploded",False)); pre_breakout=bool(row.get("pre_breakout",False))
    near_high=bool(row.get("near_high",False)); is_consolidation=bool(row.get("is_consolidation",False))
    ma5=row.get("ma5",np.nan); ma10=row.get("ma10",np.nan); ma20=row.get("ma20",np.nan)
    high20=row.get("high_20d",np.nan)
    tech_str=f"MA5:{ma5:.1f} MA10:{ma10:.1f} MA20:{ma20:.1f} 近20高:{high20:.1f}" if pd.notna(ma5) else ""

    # 先判斷已爆發，散戶衝進來的地方，應該賣不是買，搭順風車的人要下車了
    if exploded and not (trust_acc and foreign_acc):
        return "🔴 已爆發·勿追","量爆2.5倍+5日>12% 散戶衝進來的地方 搭車的要下車了",-3,tech_str
    if overheat and not (trust_acc and foreign_acc):
        if not pd.isna(ret5) and ret5>15:
            return "🔴 過熱·勿追","乖離過大等拉回",-3,tech_str

    # 雙殺一起賣，車子要翻了，快下車
    if trust_5d<0 and foreign_5d<0:
        if not above_ma10: return "🔴 雙殺破線·快下車","洋賣土賣+跌破MA10 大戶下車了 快跟著下車",-10,tech_str
        return "🔴 雙殺·快下車","洋賣土賣 一起賣 大戶下車了",-10,tech_str

    # 搭順風車核心邏輯：買在還沒爆發前
    # 最佳：吸籌末端+土洋同買+準備突破20日高點
    if trust_acc and foreign_acc and turnover>=MIN_DAILY_TURNOVER:
        if pre_breakout:
            return "🔵 吸籌末端·土洋同買·第一根","大戶吸籌末端+土洋同買+接近20日高點 快突破了 搭順風車上車點",10,tech_str
        if near_high and is_consolidation:
            return "🔵 吸籌中·土洋同買·快突破","盤整吸籌+土洋同買+近20日高點 準備第一根",9,tech_str
        if above_ma20 and is_multi_up:
            return "🔵 土洋同買·多頭","土洋同買+多頭排列 搭車中",8,tech_str
        if above_ma20:
            if abs(foreign_5d) > abs(trust_5d)*1.8:
                return "🟣 先洋後土·跟","外資先佈局投信後跟 大戶先上車了 跟著上",9,tech_str
            return "🔵 土洋同買·跟","土洋同買 跟著大戶",7,tech_str
        return "🟡 吸籌中·等站回","土洋同買但未站上月線 大戶還在吸 等站回MA20再跟",4,tech_str

    if foreign_acc and not trust_acc:
        if pre_breakout:
            return "🟣 吸籌末端·外資先上車","外資先吸籌+吸籌末端+快突破 大戶先上車了",8,tech_str
        if above_ma20 and is_multi_up:
            return "🟣 外資單邊·多頭跟","外資佈局+多頭排列 跟著大戶",6,tech_str
        if above_ma20:
            return "🟣 外資單邊·跟","外資佈局+站上月線 跟著",5,tech_str
        if trust_5d<0:
            return "⚪ 洋買土賣·觀察","外資買投信賣 大戶意見分歧 觀察",2,tech_str
        return "⚪ 洋買土賣·觀察","外資買投信賣 注意但不用先跑",2,tech_str

    if trust_acc and not foreign_acc:
        if pre_breakout:
            return "🔵 吸籌末端·投信先上車","投信先吸籌+吸籌末端+快突破 大戶先上車了",7,tech_str
        if above_ma20 and is_multi_up:
            return "🔵 投信單邊·多頭跟","投信佈局+多頭排列 跟著大戶",5,tech_str
        if above_ma20:
            return "🔵 投信單邊·跟","投信佈局+站上月線 跟著",4,tech_str
        if foreign_5d<0:
            return "🟡 洋賣土買·小心接刀","外資賣投信買 小心投信接刀 大戶不同調",3,tech_str
        return "🟡 投信吸籌·等站回","投信吸籌但未站上月線 等站回",3,tech_str

    return "⚪ 不列入","未達標",0,tech_str

def build_radar(quotes, price_feat, inst_feat):
    if quotes is None or quotes.empty:
        if price_feat is not None and not price_feat.empty:
            df=price_feat.copy()
        else:
            print("radar空")
            return pd.DataFrame()
    else:
        df=quotes.copy()
    if price_feat is not None and not price_feat.empty:
        keep=[c for c in price_feat.columns if c not in ["stock_name","market"]]
        if "date" in df.columns and "date" in price_feat.columns:
            df=df.merge(price_feat[keep], on=["stock_id","date"], how="left")
        else:
            df=df.merge(price_feat[keep], on="stock_id", how="left")
    if inst_feat is not None and not inst_feat.empty:
        df=df.merge(inst_feat, on="stock_id", how="left")
    for c,d in [("trust_5d",0),("trust_days",0),("trust_acc",False),("foreign_5d",0),("foreign_days",0),("foreign_acc",False),("yang_first",False),("hedge_dominant",False),("is_multi_up",False),("above_ma20",False),("above_ma10",False),("overheat",False),("exploded",False),("pre_breakout",False),("near_high",False),("is_consolidation",False),("stock_name",""),("market",""),("close",0),("turnover",0),("ma5",0),("ma10",0),("ma20",0),("vol_ratio_5d",0)]:
        if c not in df.columns: df[c]=d
        else:
            if c in ["trust_acc","foreign_acc","yang_first","hedge_dominant","is_multi_up","above_ma20","above_ma10","overheat","exploded","pre_breakout","near_high","is_consolidation"]: df[c]=df[c].fillna(False)
            elif c in ["stock_name","market"]: df[c]=df[c].fillna("")
            else: df[c]=df[c].fillna(0)
    if df.empty: return df
    cls=df.apply(classify, axis=1, result_type="expand")
    cls.columns=["signal","reason","base_score","tech"]
    df=pd.concat([df.reset_index(drop=True), cls.reset_index(drop=True)], axis=1)
    # 移除焦點加分，WATCHLIST只是觀察
    df["score"]=df["base_score"]
    order={"🔵 吸籌末端·土洋同買·第一根":1,"🔵 吸籌中·土洋同買·快突破":2,"🟣 先洋後土·跟":3,"🟣 吸籌末端·外資先上車":4,"🔵 吸籌末端·投信先上車":5,"🔵 土洋同買·多頭":6,"🔵 土洋同買·跟":7,"🟣 外資單邊·多頭跟":8,"🟣 外資單邊·跟":9,"🔵 投信單邊·多頭跟":10,"🔵 投信單邊·跟":11,"🟡 吸籌中·等站回":12,"🟡 投信吸籌·等站回":13,"⚪ 洋買土賣·觀察":14,"🟡 洋賣土買·小心接刀":15,"🔴 已爆發·勿追":20,"🔴 過熱·勿追":21,"🔴 雙殺·快下車":22,"🔴 雙殺破線·快下車":23,"🟠 排除：避險主導":24,"⚪ 排除：權值／ETF":25,"⚪ 不列入":99}
    df["sort_order"]=df["signal"].map(order).fillna(99)
    df=df.sort_values(["sort_order","score","turnover"], ascending=[True,False,False]).drop(columns=["sort_order"])
    print(f"build_radar 完成 {len(df)} 檔 吸籌末端 {sum(df['signal'].str.contains('吸籌末端'))} 檔")
    return df

def send_email(radar):
    import os, smtplib
    GMAIL_USER=os.getenv("GMAIL_USER"); GMAIL_APP_PASSWORD=os.getenv("GMAIL_APP_PASSWORD"); RECIPIENT_EMAIL=os.getenv("RECIPIENT_EMAIL")
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]): print("略過寄信"); return
    if radar is None or radar.empty: print("radar空不寄"); return
    today=get_today_str()
    def lines(sub_df, n):
        if sub_df is None or sub_df.empty: return "（無）"
        out=[]
        for _,r in sub_df.head(n).iterrows():
            try:
                out.append(f"{r.get('signal','')}｜{r.get('stock_id','')} {r.get('stock_name','')}｜{r.get('reason','')} 投信{int(r.get('trust_5d',0)/1000):+d}張 外資{int(r.get('foreign_5d',0)/1000):+d}張｜{r.get('tech','')} 收盤{float(r.get('close',0)):.1f} 量比{float(r.get('vol_ratio_5d',0)):.1f}")
            except: out.append(f"{r.get('stock_id','')}")
        return "\n".join(out) if out else "（無）"
    legend="""
【搭順風車邏輯 - 買在還沒爆發前】
🔵 吸籌末端·土洋同買·第一根：大戶吸籌末端+土洋同買+接近20日高點97%以上+量還沒爆<2倍 = 第一根起漲 搭車上車點 勝率最高
🔵 吸籌中·土洋同買·快突破：盤整吸籌+土洋同買+近高點 準備突破
🟣 先洋後土·跟：外資先佈局投信後跟 大戶先上車了 跟著上
🟣/🔵 吸籌末端·外資/投信先上車：單邊大戶先吸籌+吸籌末端+快突破
🟡 吸籌中·等站回：大戶還在吸但未站上月線 等站回MA20再跟
⚪ 洋買土賣·觀察：大戶意見分歧 觀察就好
🟡 洋賣土買·小心接刀：大戶不同調 小心接刀
🔴 已爆發·勿追/過熱·勿追：量爆2.5倍+5日>12% 散戶衝進來的地方 搭車的人要下車了 不是買點
🔴 雙殺·快下車：洋賣土賣 大戶下車了 快跟著下車 不然被套
WATCHLIST觀察名單：2383 2368 6197 3293 4763 1808 6919 只是觀察誰買多少誰賣多少，不加分
技術：MA5=週線 MA10=10日 MA20=月線(生命線) 多頭排列=5>10>20 盤整=20日高低差<18% 接近高點=收盤>20日高點97%
"""
    # 主雷達：全市場第一根
    best=radar[radar["signal"].str.contains("吸籌末端·土洋同買·第一根|吸籌中·土洋同買·快突破|先洋後土|吸籌末端", na=False)]
    follow=radar[radar["signal"].str.contains("土洋同買·|外資單邊·跟|投信單邊·跟", na=False)]
    danger=radar[radar["signal"].str.contains("已爆發|過熱|雙殺", na=False)].head(15)
    watch=radar[radar["stock_id"].isin(WATCHLIST)]

    body=f"""台股雷達 v8.0 搭順風車版 {today} - 買在還沒爆發前
{legend}
━━━━━━━━━━━━
🔵🟣 吸籌末端·第一根 {len(best)} 檔 (全市場掃描 第一根起漲 搭車上車點)
{lines(best,30)}

━━━━━━━━━━━━
🔵🟣 跟著大戶 {len(follow)} 檔
{lines(follow,20)}

━━━━━━━━━━━━
🔴 已爆發·快下車 {len(danger)} 檔 (散戶衝進來的地方 要下車了)
{lines(danger,15)}

━━━━━━━━━━━━
👀 WATCHLIST觀察名單 7檔 (2383 2368 6197 3293 4763 1808 6919) - 只看誰買多少誰賣多少 不加分
{lines(watch,10)}
"""

    msg=MIMEMultipart(); msg["From"]=GMAIL_USER; msg["To"]=RECIPIENT_EMAIL; msg["Subject"]=f"雷達 v8.0搭順風車·第一根｜{today}"; msg.attach(MIMEText(body,"plain","utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com",465) as s: s.login(GMAIL_USER,GMAIL_APP_PASSWORD); s.send_message(msg)
    print("Email已寄 v8.0")

def main():
    init_db()
    print(f"=== v8.0 搭順風車版開始 {get_today_str()} ===")
    tw_quotes=get_twse_quotes(); tp_quotes=get_tpex_quotes()
    quotes=pd.concat([tw_quotes, tp_quotes], ignore_index=True) if not (tw_quotes.empty and tp_quotes.empty) else pd.DataFrame()
    if not quotes.empty:
        quotes=quotes.drop_duplicates(subset=["stock_id"])
        print(f"quotes {len(quotes)}")
    else:
        hist=load_price_history(5)
        if not hist.empty:
            last_date=hist["date"].max()
            quotes=hist[hist["date"]==last_date].copy()
            quotes["date"]=get_today_str()
            print(f"用DB備援 {last_date} {len(quotes)}")
        else:
            quotes=pd.DataFrame(columns=["date","stock_id","stock_name","market","close","volume","turnover"])
    tw_inst=get_twse_institutional(); tp_inst=get_tpex_institutional()
    save_prices(quotes)
    if not tw_inst.empty: save_institutional(tw_inst, "TWSE")
    if not tp_inst.empty: save_institutional(tp_inst, "TPEx")
    hist_price=load_price_history(90); hist_inst=load_inst_history(90)
    pf=make_price_features(quotes, hist_price)
    frames=[x for x in [hist_inst, tw_inst, tp_inst] if x is not None and not x.empty]
    all_inst=pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    inf=make_inst_features(all_inst, pf)
    radar=build_radar(quotes, pf, inf)
    today=get_today_str()
    try:
        conn=sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM signals WHERE signal_date=?", (today,))
        conn.commit(); conn.close()
    except: pass
    if radar is not None and not radar.empty:
        sig_df=pd.DataFrame()
        sig_df["stock_id"]=radar["stock_id"]
        sig_df["signal_date"]=today
        sig_df["stock_name"]=radar["stock_name"] if "stock_name" in radar.columns else ""
        sig_df["market"]=radar["market"] if "market" in radar.columns else ""
        sig_df["signal"]=radar["signal"] if "signal" in radar.columns else ""
        sig_df["reason"]=radar["reason"] if "reason" in radar.columns else ""
        sig_df["score"]=radar["score"] if "score" in radar.columns else 0
        sig_df["close_at_signal"]=radar["close"] if "close" in radar.columns else 0
        sig_df["trust_5d_net"]=radar["trust_5d"] if "trust_5d" in radar.columns else 0
        sig_df["foreign_5d_net"]=radar["foreign_5d"] if "foreign_5d" in radar.columns else 0
        sig_df["ma5"]=radar["ma5"] if "ma5" in radar.columns else 0
        sig_df["ma10"]=radar["ma10"] if "ma10" in radar.columns else 0
        sig_df["ma20"]=radar["ma20"] if "ma20" in radar.columns else 0
        sig_df["tech"]=radar["tech"] if "tech" in radar.columns else ""
        try:
            conn=sqlite3.connect(DB_PATH)
            sig_df.to_sql("signals", conn, if_exists="append", index=False)
            conn.commit(); conn.close()
            print(f"signals寫入 {len(sig_df)}")
        except Exception as e: print(f"signals失敗 {e}")
        try: radar.to_csv(os.path.join(OUTPUT_DIR, f"radar_{today}.csv"), index=False, encoding="utf-8-sig")
        except Exception as e: print(f"csv失敗 {e}")
        send_email(radar)
    else:
        print("radar空")
    print("=== v8.0 完成 ===")

if __name__=="__main__": main()
