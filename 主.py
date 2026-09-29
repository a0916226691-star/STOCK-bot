# -*- coding: utf-8 -*-
"""
台股「主動資金疑似布局」雷達 v3.1 防呆版
修復 KeyError: 'month_revenue_yoy'
"""

import os, io, time, smtplib, traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import numpy as np
import pandas as pd
import requests

TZ = timezone(timedelta(hours=8))
OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def get_today_str():
    return datetime.now(TZ).strftime("%Y-%m-%d")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}

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

MIN_DAILY_TURNOVER = 30_000_000
MIN_TRUST_5D_VOLUME_RATIO = 0.01
MIN_FOREIGN_5D_VOLUME_RATIO = 0.03
MAX_5D_RETURN_FOR_ACCUMULATION = 6.0
MIN_5D_RETURN_FOR_ACCUMULATION = -7.0
MAX_10D_RANGE_FOR_ACCUMULATION = 16.0
MAX_VOLUME_SPIKE_RATIO = 2.0
MAX_5D_RETURN_FOR_GREEN = 10.0
MAX_DISTANCE_MA10_FOR_GREEN = 8.0
MAX_DISTANCE_MA20_FOR_BLUE = 7.0
FUNDAMENTAL_BAD_MOM = -20.0
FUNDAMENTAL_WARNING_MOM = -10.0
FUNDAMENTAL_BAD_YOY = 0.0
YOY_DROP_BAD_PP = -20.0
MAX_BLUE_EMAIL = 15
MAX_GREEN_EMAIL = 15
MAX_YELLOW_EMAIL = 10
MAX_RED_EMAIL = 15
MAX_HISTORY_FILES = 90

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

def find_first_column(cols, keys):
    cols=[str(c) for c in cols]
    for k in keys:
        for c in cols:
            if k in c: return c
    return None

def get_recent_dates(days=15):
    now=datetime.now(TZ)
    return [(now-timedelta(days=i)).strftime("%Y%m%d") for i in range(days)]

def get_twse_quotes():
    data=request_get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL").json()
    rows=[]
    for it in data:
        sid=normalize_stock_id(it.get("Code",""))
        if not sid.isdigit() or len(sid)!=4: continue
        close=safe_float(it.get("ClosingPrice"))
        if pd.isna(close): continue
        rows.append({"stock_id":sid,"stock_name":str(it.get("Name","")).strip(),"market":"TWSE","close":close,"change":safe_float(it.get("Change")),"volume":safe_int(it.get("TradeVolume")),"turnover":safe_float(it.get("TradeValue"))})
    return pd.DataFrame(rows)

def get_tpex_quotes():
    data=request_get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes").json()
    rows=[]
    for it in data:
        sid=normalize_stock_id(it.get("SecuritiesCompanyCode") or it.get("SecuritiesCode") or it.get("Code") or "")
        if not sid.isdigit() or len(sid)!=4: continue
        close=safe_float(it.get("Close") or it.get("ClosingPrice"))
        if pd.isna(close): continue
        rows.append({"stock_id":sid,"stock_name":str(it.get("CompanyName") or it.get("SecuritiesName") or "").strip(),"market":"TPEx","close":close,"change":safe_float(it.get("Change")),"volume":safe_int(it.get("Volume") or it.get("TradeVolume")),"turnover":safe_float(it.get("Amount") or it.get("TradeValue"))})
    return pd.DataFrame(rows)

def get_all_quotes():
    frames=[get_twse_quotes()]
    try: frames.append(get_tpex_quotes())
    except Exception as e: print(f"TPEx 暫不可用 {e}")
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["stock_id"])

def get_twse_institutional():
    url="https://www.twse.com.tw/rwd/zh/fund/T86"
    data=None; used=None
    for d in get_recent_dates(15):
        try:
            j=request_get(url, params={"response":"json","date":d,"selectType":"ALLBUT0999"}, timeout=15).json()
            if j.get("stat")=="OK": data=j; used=d; break
        except: continue
    if not data: raise RuntimeError("15日無法人")
    fields=data["fields"]; raw=data["data"]
    fmt=f"{used[:4]}-{used[4:6]}-{used[6:]}"
    rows=[]
    for r in raw:
        it=dict(zip(fields,r))
        sid=normalize_stock_id(it.get("證券代號",""))
        if not sid.isdigit() or len(sid)!=4: continue
        rows.append({"stock_id":sid,"date":fmt,"foreign_net":safe_int(it.get("外陸資買賣超股數(不含外資自營商)")),"trust_net":safe_int(it.get("投信買賣超股數")),"dealer_hedge_net":safe_int(it.get("自營商買賣超股數(避險)")) or 0})
    df=pd.DataFrame(rows)
    for c in ["foreign_net","trust_net","dealer_hedge_net"]: df[c]=pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df

def get_monthly_revenue():
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
                code_col=find_first_column(table.columns, ["公司代號","代號"])
                yoy_col=find_first_column(table.columns, ["去年同月增減","YoY"])
                mom_col=find_first_column(table.columns, ["上月比較增減","MoM"])
                rev_col=find_first_column(table.columns, ["當月營收","本月營收"])
                if not code_col or not yoy_col: continue
                for _, it in table.iterrows():
                    sid=normalize_stock_id(it.get(code_col,""))
                    if not sid.isdigit() or len(sid)!=4: continue
                    rows.append({"stock_id":sid,"month_revenue":safe_float(it.get(rev_col, np.nan)) if rev_col else np.nan,"month_revenue_yoy":safe_float(it.get(yoy_col, np.nan)),"month_revenue_mom":safe_float(it.get(mom_col, np.nan)) if mom_col else np.nan,"announce_date":TODAY})
        except Exception as e: print(f"MOPS失敗 {e}")
    df=pd.DataFrame(rows)
    if df.empty:
        print("月營收無資料")
        return pd.DataFrame(columns=["stock_id","month_revenue","month_revenue_yoy","month_revenue_mom","announce_date"])
    return df.drop_duplicates(subset=["stock_id"])

def load_history(prefix, limit=MAX_HISTORY_FILES):
    if not os.path.exists(OUTPUT_DIR): return pd.DataFrame()
    files=sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith(prefix) and f.endswith(".csv")])[-limit:]
    frames=[]
    for fn in files:
        try:
            df=pd.read_csv(os.path.join(OUTPUT_DIR, fn), dtype={"stock_id":str})
            if not df.empty:
                df["stock_id"]=df["stock_id"].map(normalize_stock_id)
                frames.append(df)
        except: pass
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

def save_today_history(quotes, inst, rev, today_str):
    quotes.copy().assign(date=today_str).to_csv(os.path.join(OUTPUT_DIR, f"layout_price_history_{today_str}.csv"), index=False, encoding="utf-8-sig")
    if inst is not None and not inst.empty: inst.to_csv(os.path.join(OUTPUT_DIR, f"layout_institutional_history_{today_str}.csv"), index=False, encoding="utf-8-sig")
    if rev is not None and not rev.empty: rev.to_csv(os.path.join(OUTPUT_DIR, f"layout_revenue_history_{today_str}.csv"), index=False, encoding="utf-8-sig")

def make_price_features(quotes, hist, today_str):
    today_df=quotes.copy(); today_df["date"]=today_str
    full=today_df if hist is None or hist.empty else pd.concat([hist, today_df], ignore_index=True)
    for c in ["close","volume","turnover"]: full[c]=pd.to_numeric(full[c], errors="coerce")
    full=full.drop_duplicates(subset=["stock_id","date"]).sort_values(["stock_id","date"])
    g=full.groupby("stock_id", group_keys=False)
    full["ma10"]=g["close"].transform(lambda x: x.rolling(10, min_periods=10).mean())
    full["ma20"]=g["close"].transform(lambda x: x.rolling(20, min_periods=20).mean())
    full["close_5d_ago"]=g["close"].transform(lambda x: x.shift(4))
    full["return_5d_pct"]=(full["close"]/full["close_5d_ago"]-1)*100
    full["range_10d_pct"]=(g["close"].transform(lambda x: x.rolling(10).max())/g["close"].transform(lambda x: x.rolling(10).min())-1)*100
    full["avg_volume_5d"]=g["volume"].transform(lambda x: x.shift(1).rolling(5, min_periods=5).mean())
    full["volume_ratio_5d"]=full["volume"]/full["avg_volume_5d"]
    full["distance_ma10_pct"]=(full["close"]/full["ma10"]-1)*100
    full["distance_ma20_pct"]=(full["close"]/full["ma20"]-1)*100
    return full[full["date"]==today_str][["stock_id","ma10","ma20","return_5d_pct","range_10d_pct","avg_volume_5d","volume_ratio_5d","distance_ma10_pct","distance_ma20_pct"]]

def make_institutional_features(hist, price_features):
    if hist is None or hist.empty: return pd.DataFrame()
    df=hist.copy()
    for c in ["foreign_net","trust_net","dealer_hedge_net"]: df[c]=pd.to_numeric(df[c], errors="coerce").fillna(0)
    df=df.drop_duplicates(subset=["stock_id","date"]).sort_values(["stock_id","date"])
    vol_map=price_features.set_index("stock_id")["avg_volume_5d"].to_dict() if price_features is not None and not price_features.empty else {}
    rows=[]
    for sid, gp in df.groupby("stock_id"):
        gp=gp.tail(5)
        t_net=sum(gp["trust_net"]); f_net=sum(gp["foreign_net"]); h_abs=sum(abs(x) for x in gp["dealer_hedge_net"])
        t_days=sum(1 for x in gp["trust_net"] if x>0); f_days=sum(1 for x in gp["foreign_net"] if x>0)
        avg=vol_map.get(sid, np.nan)
        t_ratio=np.nan if pd.isna(avg) or avg<=0 else t_net/(avg*5)
        f_ratio=np.nan if pd.isna(avg) or avg<=0 else f_net/(avg*5)
        h_ratio=np.nan if pd.isna(avg) or avg<=0 else h_abs/(avg*5)
        hedge=(not pd.isna(h_ratio) and h_ratio>=0.03) or (h_abs>=abs(t_net)+abs(f_net) and h_abs>0)
        trust_acc=t_days>=3 and t_net>0 and (pd.isna(t_ratio) or t_ratio>=MIN_TRUST_5D_VOLUME_RATIO)
        foreign_sup=f_days>=3 and f_net>0 and (pd.isna(f_ratio) or f_ratio>=MIN_FOREIGN_5D_VOLUME_RATIO)
        rows.append({"stock_id":sid,"trust_buy_days_5":t_days,"trust_5d_net":t_net,"foreign_buy_days_5":f_days,"foreign_5d_net":f_net,"dealer_hedge_5d_abs":h_abs,"trust_volume_ratio":t_ratio,"foreign_volume_ratio":f_ratio,"hedge_dominant":hedge,"trust_accumulation":trust_acc,"foreign_support":foreign_sup})
    return pd.DataFrame(rows)

def make_revenue_features(hist):
    if hist is None or hist.empty:
        return pd.DataFrame(columns=["stock_id","month_revenue","month_revenue_yoy","month_revenue_mom","fundamental_bad","fundamental_note"])
    df=hist.copy().sort_values(["stock_id","announce_date"])
    rows=[]
    for sid, gp in df.groupby("stock_id"):
        gp=gp.drop_duplicates(subset=["announce_date"]).sort_values("announce_date")
        latest=gp.iloc[-1]; prev=gp.iloc[-2] if len(gp)>=2 else None
        yoy=safe_float(latest.get("month_revenue_yoy", np.nan)); mom=safe_float(latest.get("month_revenue_mom", np.nan))
        prev_yoy=safe_float(prev.get("month_revenue_yoy", np.nan)) if prev is not None else np.nan
        yoy_ch=(yoy-prev_yoy) if not pd.isna(yoy) and not pd.isna(prev_yoy) else np.nan
        bad=(not pd.isna(mom) and not pd.isna(yoy) and mom<=-20 and yoy<=0) or (not pd.isna(mom) and not pd.isna(yoy_ch) and mom<=-10 and yoy_ch<=-20)
        rows.append({"stock_id":sid,"month_revenue":safe_float(latest.get("month_revenue", np.nan)),"month_revenue_yoy":yoy,"month_revenue_mom":mom,"fundamental_bad":bool(bad),"fundamental_note":"轉弱" if bad else "正常"})
    return pd.DataFrame(rows)

def classify_stock(row):
    sid=row.get("stock_id","")
    if sid in EXCLUDE_TOOL_STOCKS: return "⚪ 排除：權值／ETF","權值/ETF",-5
    if bool(row.get("hedge_dominant",False)): return "🟠 排除：避險主導","避險高",-4
    ret5=row.get("return_5d_pct", np.nan); volr=row.get("volume_ratio_5d", np.nan); ma20=row.get("distance_ma20_pct", np.nan); ma10=row.get("distance_ma10_pct", np.nan); bad=bool(row.get("fundamental_bad",False))
    if not pd.isna(ret5) and ret5>10: return "🔴 排除：拉高／過熱","過熱",-3
    if not pd.isna(volr) and volr>2.0 and not pd.isna(ret5) and ret5>3: return "🔴 排除：拉高／過熱","爆量",-3
    if not pd.isna(ma20) and ma20<0 and bad: return "🔴 排除：跌破MA20＋基本面變壞","破線+基本面",-4
    if row.get("trust_5d_net",0)<0 and row.get("foreign_5d_net",0)<0 and row.get("trust_buy_days_5",0)==0: return "🔴 排除：法人轉賣","同賣",-3
    if not pd.isna(ma20) and ma20<0 and not bad and (row.get("trust_accumulation",False) or row.get("foreign_support",False)): return "🟡 籌碼尚在、價格轉弱","等站回",2
    price_ok=(pd.isna(ret5) or -7<=ret5<=6) and (pd.isna(row.get("range_10d_pct", np.nan)) or row.get("range_10d_pct")<=16) and (pd.isna(volr) or volr<=2.0) and (pd.isna(ma20) or abs(ma20)<=7)
    if row.get("turnover",0)>=MIN_DAILY_TURNOVER and row.get("trust_accumulation",False) and price_ok and not bad: return "🔵 主動資金疑似布局","吸籌",8
    if row.get("turnover",0)>=MIN_DAILY_TURNOVER and row.get("trust_accumulation",False) and not pd.isna(ma10) and 0<=ma10<=8 and not bad: return "🟢 吸籌延續／初步確認","站上MA10",9
    if row.get("foreign_support",False): return "⚪ 待驗證：僅外資流入","外資單邊",1
    return "⚪ 不列入","未達標",0

def build_radar(quotes, pf, inf, rf):
    df=quotes.copy()
    for f in [pf, inf, rf]:
        if f is not None and not f.empty: df=df.merge(f, on="stock_id", how="left")
    # 防呆：確保所有後面會用到的欄位都存在
    must_cols = {
        "ma10": np.nan, "ma20": np.nan, "return_5d_pct": np.nan, "range_10d_pct": np.nan,
        "avg_volume_5d": np.nan, "volume_ratio_5d": np.nan, "distance_ma10_pct": np.nan, "distance_ma20_pct": np.nan,
        "trust_buy_days_5": 0, "trust_5d_net": 0, "foreign_5d_net": 0, "trust_volume_ratio": np.nan,
        "month_revenue": np.nan, "month_revenue_yoy": np.nan, "month_revenue_mom": np.nan,
        "hedge_dominant": False, "trust_accumulation": False, "foreign_support": False, "fundamental_bad": False, "fundamental_note": "待驗證"
    }
    for c, d in must_cols.items():
        if c not in df.columns: df[c]=d
        else: df[c]=df[c].fillna(d) if not isinstance(d, bool) else df[c].fillna(d)

    df["theme"]=df["stock_id"].map(lambda x: "/".join(THEME_MAP.get(x, [])))
    cls=df.apply(classify_stock, axis=1, result_type="expand")
    cls.columns=["signal","reason","base_score"]
    df=pd.concat([df, cls], axis=1)
    df["score"]=df["base_score"]
    df.loc[df["trust_buy_days_5"]>=4, "score"]+=2
    df.loc[df["stock_id"].isin(FOCUS_STOCKS), "score"]+=5
    order={"🔵 主動資金疑似布局":1,"🟢 吸籌延續／初步確認":2,"🟡 籌碼尚在、價格轉弱":3,"⚪ 待驗證：僅外資流入":4,"🔴 排除：拉高／過熱":5,"🔴 排除：跌破MA20＋基本面變壞":6,"🔴 排除：法人轉賣":7,"🟠 排除：避險主導":8,"⚪ 排除：權值／ETF":9,"⚪ 不列入":99}
    df["sort_order"]=df["signal"].map(order).fillna(99)
    return df.sort_values(by=["sort_order","score","turnover"], ascending=[True,False,False]).drop(columns=["sort_order"])

def fmt_price(v): return "-" if pd.isna(v) else f"{float(v):.2f}"
def fmt_pct(v): return "累積中" if pd.isna(v) else f"{float(v):+.1f}%"
def fmt_ratio(v): return "累積中" if pd.isna(v) else f"{float(v)*100:.2f}%"
def fmt_shares(v):
    if pd.isna(v): return "-"
    v=int(v); return f"{v/1000:+.1f} 張" if abs(v)>=1000 else f"{v:+,} 股"

def stock_lines(df, max_rows):
    if df is None or df.empty: return "（今日無股票）"
    lines=[]
    for _, r in df.head(max_rows).iterrows():
        tag=" ⭐" if r.get('stock_id','') in FOCUS_STOCKS else ""
        theme=f"｜{r.get('theme','')}" if r.get('theme','') else ""
        lines.append(f"{r.get('signal','')} {tag}｜{r.get('stock_id','')} {r.get('stock_name','')}{theme}｜{r.get('market','')}｜收盤 {fmt_price(r.get('close', np.nan))}")
        lines.append(f"投信5日 {fmt_shares(r.get('trust_5d_net', 0))}（{int(r.get('trust_buy_days_5',0))}日買）｜相對量 {fmt_ratio(r.get('trust_volume_ratio', np.nan))}｜外資5日 {fmt_shares(r.get('foreign_5d_net',0))}")
        lines.append(f"5日 {fmt_pct(r.get('return_5d_pct', np.nan))}｜振幅 {fmt_pct(r.get('range_10d_pct', np.nan))}｜距MA20 {fmt_pct(r.get('distance_ma20_pct', np.nan))}｜YoY {fmt_pct(r.get('month_revenue_yoy', np.nan))}")
        lines.append(f"判定：{r.get('reason','')}")
        lines.append("")
    return "\n".join(lines).rstrip()

def make_email_body(radar, today_str, exec_time):
    focus_df=radar[radar["stock_id"].isin(FOCUS_STOCKS)]
    blue=radar[radar["signal"]=="🔵 主動資金疑似布局"]; green=radar[radar["signal"]=="🟢 吸籌延續／初步確認"]; yellow=radar[radar["signal"]=="🟡 籌碼尚在、價格轉弱"]; red=radar[radar["signal"].str.startswith("🔴", na=False)]
    lines=[f"台股主動資金雷達 {today_str}",f"執行 {exec_time}","","⭐ 關注7檔"]
    if not focus_df.empty:
        for _, r in focus_df.iterrows():
            lines.append(f"• {r.get('stock_id')} {r.get('stock_name')} {r.get('theme','')} ｜ {r.get('signal')} ｜ 收盤 {fmt_price(r.get('close'))} ｜ 投信 {fmt_shares(r.get('trust_5d_net'))}")
    else: lines.append("（無資料）")
    lines+=["",f"A. 🔵 布局 {len(blue)}", stock_lines(blue,15), f"B. 🟢 延續 {len(green)}", stock_lines(green,15), f"C. 🟡 轉弱 {len(yellow)}", stock_lines(yellow,10), f"D. 🔴 排除 {len(red)}", stock_lines(red,15)]
    return "\n".join(lines)

def send_email(sub, body):
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]): print("略過寄信"); return
    m=MIMEMultipart(); m["From"]=GMAIL_USER; m["To"]=RECIPIENT_EMAIL; m["Subject"]=sub; m.attach(MIMEText(body,"plain","utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com",465) as s: s.login(GMAIL_USER,GMAIL_APP_PASSWORD); s.send_message(m)

def main():
    NOW=datetime.now(TZ); TODAY=NOW.strftime("%Y-%m-%d")
    quotes=get_all_quotes()
    try: inst=get_twse_institutional()
    except: inst=pd.DataFrame()
    rev=get_monthly_revenue()
    ph=load_history("layout_price_history_"); ih=load_history("layout_institutional_history_"); rh=load_history("layout_revenue_history_")
    if not inst.empty: ih=pd.concat([ih,inst], ignore_index=True)
    if not rev.empty: rh=pd.concat([rh,rev], ignore_index=True)
    pf=make_price_features(quotes, ph, TODAY); inf=make_institutional_features(ih, pf); rf=make_revenue_features(rh)
    radar=build_radar(quotes, pf, inf, rf)
    save_today_history(quotes, inst, rev, TODAY)
    radar.to_csv(os.path.join(OUTPUT_DIR, f"layout_radar_{TODAY}.csv"), index=False, encoding="utf-8-sig")
    body=make_email_body(radar, TODAY, NOW.strftime('%Y-%m-%d %H:%M'))
    send_email(f"主動資金雷達｜{TODAY}", body)

if __name__=="__main__":
    try: main()
    except: traceback.print_exc(); raise
