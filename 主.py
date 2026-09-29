# -*- coding: utf-8 -*-
"""
台股「主動資金疑似布局」雷達（修正版 v3 焦點追蹤）
- 修復 safe_float 負號
- 法人回補 15 天
- 量比用 shift(1)
- FOCUS + THEMES 真正啟用
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

# 你的 7 檔焦點
FOCUS_STOCKS = ["2383", "2368", "6197", "3293", "4763", "1808", "6919"]

THEMES = {
    "⭐ 個人重點關注焦點股": FOCUS_STOCKS,
    "AI 伺服器／ODM": ["2317", "2382", "3231", "6669", "6805"],
    "散熱": ["3017", "3324", "6205", "6131"],
    "PCB／CCL／載板": ["2383", "2368", "2385", "3037", "4967", "6274", "6197"],
}

# 反向索引：stock_id -> theme
THEME_MAP = {}
for theme_name, codes in THEMES.items():
    for c in codes:
        THEME_MAP.setdefault(c, []).append(theme_name)

# 權值/ETF 排除 - 把你 THEMES 要追的拿掉，不然永遠被排除
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

def safe_float(value):
    if value is None: return np.nan
    try:
        text = str(value).strip()
        if text == "" or text in ["--", "---", "nan", "NaN", "None", "null", "—"]: return np.nan
        text = text.replace(",", "").replace("%", "").replace("＋", "+").replace("－", "-").replace("—", "-").replace("–", "-")
        if text.startswith("+"): text = text[1:]
        return float(text)
    except: return np.nan

def safe_int(value):
    n = safe_float(value)
    return np.nan if pd.isna(n) else int(n)

def normalize_stock_id(value):
    t = str(value).strip()
    if t.endswith(".0"): t = t[:-2]
    return t.zfill(4) if t.isdigit() and len(t) < 4 else t

def request_get(url, params=None, timeout=30):
    last = None
    for i in range(3):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(2*(i+1))
    raise last

def find_first_column(columns, keywords):
    cols = [str(c) for c in columns]
    for k in keywords:
        for c in cols:
            if k in c: return c
    return None

def get_recent_dates(days=15):
    now = datetime.now(TZ)
    return [(now - timedelta(days=i)).strftime("%Y%m%d") for i in range(days)]

def get_twse_quotes():
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    data = request_get(url).json()
    rows = []
    for item in data:
        sid = normalize_stock_id(item.get("Code", ""))
        if not sid.isdigit() or len(sid)!=4: continue
        close = safe_float(item.get("ClosingPrice"))
        if pd.isna(close): continue
        rows.append({"stock_id": sid, "stock_name": str(item.get("Name","")).strip(), "market":"TWSE", "close":close, "change":safe_float(item.get("Change")), "volume":safe_int(item.get("TradeVolume")), "turnover":safe_float(item.get("TradeValue"))})
    return pd.DataFrame(rows)

def get_tpex_quotes():
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    data = request_get(url).json()
    rows=[]
    for item in data:
        sid = normalize_stock_id(item.get("SecuritiesCompanyCode") or item.get("SecuritiesCode") or item.get("Code") or "")
        if not sid.isdigit() or len(sid)!=4: continue
        close = safe_float(item.get("Close") or item.get("ClosingPrice"))
        if pd.isna(close): continue
        rows.append({"stock_id":sid, "stock_name":str(item.get("CompanyName") or item.get("SecuritiesName") or "").strip(), "market":"TPEx", "close":close, "change":safe_float(item.get("Change")), "volume":safe_int(item.get("Volume") or item.get("TradeVolume")), "turnover":safe_float(item.get("Amount") or item.get("TradeValue"))})
    return pd.DataFrame(rows)

def get_all_quotes():
    frames=[get_twse_quotes()]
    try: frames.append(get_tpex_quotes())
    except Exception as e: print(f"TPEx 暫時不可用：{e}")
    q = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["stock_id"])
    return q

def get_twse_institutional():
    url="https://www.twse.com.tw/rwd/zh/fund/T86"
    data=None; used=None
    for d in get_recent_dates(15):
        try:
            j = request_get(url, params={"response":"json","date":d,"selectType":"ALLBUT0999"}, timeout=15).json()
            if j.get("stat")=="OK": data=j; used=d; break
        except: continue
    if not data: raise RuntimeError("15日內無法人資料")
    fields=data["fields"]; rows_raw=data["data"]
    fmt=f"{used[:4]}-{used[4:6]}-{used[6:]}"
    rows=[]
    for r in rows_raw:
        item=dict(zip(fields,r))
        sid=normalize_stock_id(item.get("證券代號",""))
        if not sid.isdigit() or len(sid)!=4: continue
        rows.append({"stock_id":sid,"date":fmt,"foreign_net":safe_int(item.get("外陸資買賣超股數(不含外資自營商)")),"trust_net":safe_int(item.get("投信買賣超股數")),"dealer_proprietary_net":safe_int(item.get("自營商買賣超股數(自行買賣)")) or 0,"dealer_hedge_net":safe_int(item.get("自營商買賣超股數(避險)")) or 0,"dealer_net":safe_int(item.get("自營商買賣超股數")) or 0})
    df=pd.DataFrame(rows)
    for c in ["foreign_net","trust_net","dealer_proprietary_net","dealer_hedge_net","dealer_net"]: df[c]=pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df

def get_monthly_revenue():
    urls=["https://mops.twse.com.tw/nas/t21/sii/t21sc03_if.html","https://mops.twse.com.tw/nas/t21/otc/t21sc03_if.html"]
    rows=[]; TODAY=get_today_str()
    for url in urls:
        try:
            resp=request_get(url, timeout=45)
            # 自動判斷 big5 / utf8
            try: text=resp.content.decode('big5', errors='ignore') if 'big5' in resp.apparent_encoding.lower() or len(resp.content)>0 else resp.text
            except: text=resp.text
            tables=pd.read_html(io.StringIO(text))
            for table in tables:
                if table.empty or len(table.columns)<3: continue
                table.columns=[str(c).replace("\n","").strip() for c in table.columns]
                code_col=find_first_column(table.columns, ["公司代號","代號"])
                rev_col=find_first_column(table.columns, ["當月營收","本月營收"])
                yoy_col=find_first_column(table.columns, ["去年同月增減(%)","YoY"])
                mom_col=find_first_column(table.columns, ["上月比較增減(%)","MoM"])
                if not code_col or not yoy_col: continue
                for _, it in table.iterrows():
                    sid=normalize_stock_id(it.get(code_col,""))
                    if not sid.isdigit() or len(sid)!=4: continue
                    rows.append({"stock_id":sid,"month_revenue":safe_float(it.get(rev_col, np.nan)) if rev_col else np.nan,"month_revenue_yoy":safe_float(it.get(yoy_col, np.nan)),"month_revenue_mom":safe_float(it.get(mom_col, np.nan)) if mom_col else np.nan,"announce_date":TODAY})
        except Exception as e: print(f"MOPS失敗 {url} {e}")
    df=pd.DataFrame(rows)
    if df.empty: return pd.DataFrame(columns=["stock_id","month_revenue","month_revenue_yoy","month_revenue_mom","announce_date"])
    return df.drop_duplicates(subset=["stock_id"], keep="last")

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

def save_today_history(quotes, institutional_df, revenue_df, today_str):
    quotes.copy().assign(date=today_str).to_csv(os.path.join(OUTPUT_DIR, f"layout_price_history_{today_str}.csv"), index=False, encoding="utf-8-sig")
    if institutional_df is not None and not institutional_df.empty:
        institutional_df.to_csv(os.path.join(OUTPUT_DIR, f"layout_institutional_history_{today_str}.csv"), index=False, encoding="utf-8-sig")
    if revenue_df is not None and not revenue_df.empty:
        revenue_df.to_csv(os.path.join(OUTPUT_DIR, f"layout_revenue_history_{today_str}.csv"), index=False, encoding="utf-8-sig")

def make_price_features(quotes, price_history, today_str):
    today_df=quotes.copy(); today_df["date"]=today_str
    full = today_df if price_history is None or price_history.empty else pd.concat([price_history, today_df], ignore_index=True)
    full["stock_id"]=full["stock_id"].map(normalize_stock_id)
    for c in ["close","volume","turnover"]: full[c]=pd.to_numeric(full[c], errors="coerce")
    full=full.drop_duplicates(subset=["stock_id","date"]).sort_values(["stock_id","date"])
    g=full.groupby("stock_id", group_keys=False)
    full["ma10"]=g["close"].transform(lambda x: x.rolling(10, min_periods=10).mean())
    full["ma20"]=g["close"].transform(lambda x: x.rolling(20, min_periods=20).mean())
    full["close_5d_ago"]=g["close"].transform(lambda x: x.shift(4))
    full["close_10d_ago"]=g["close"].transform(lambda x: x.shift(9))
    full["return_5d_pct"]=(full["close"]/full["close_5d_ago"]-1)*100
    full["range_10d_pct"]=(g["close"].transform(lambda x: x.rolling(10).max())/g["close"].transform(lambda x: x.rolling(10).min())-1)*100
    full["avg_volume_5d"]=g["volume"].transform(lambda x: x.shift(1).rolling(5, min_periods=5).mean())
    full["volume_ratio_5d"]=full["volume"]/full["avg_volume_5d"]
    full["distance_ma10_pct"]=(full["close"]/full["ma10"]-1)*100
    full["distance_ma20_pct"]=(full["close"]/full["ma20"]-1)*100
    return full[full["date"]==today_str][["stock_id","ma10","ma20","return_5d_pct","range_10d_pct","avg_volume_5d","volume_ratio_5d","distance_ma10_pct","distance_ma20_pct"]]

def make_institutional_features(inst_history, price_features):
    if inst_history is None or inst_history.empty: return pd.DataFrame(columns=["stock_id","trust_buy_days_5","trust_5d_net","foreign_buy_days_5","foreign_5d_net","dealer_hedge_5d_abs","trust_volume_ratio","foreign_volume_ratio","hedge_dominant","trust_accumulation","foreign_support"])
    df=inst_history.copy()
    for c in ["foreign_net","trust_net","dealer_hedge_net"]: df[c]=pd.to_numeric(df[c], errors="coerce").fillna(0)
    df=df.drop_duplicates(subset=["stock_id","date"]).sort_values(["stock_id","date"])
    vol_map=price_features.set_index("stock_id")["avg_volume_5d"].to_dict() if price_features is not None and not price_features.empty else {}
    rows=[]
    for sid, gp in df.groupby("stock_id"):
        gp=gp.tail(5)
        t_net=sum(gp["trust_net"]); f_net=sum(gp["foreign_net"]); h_abs=sum(abs(x) for x in gp["dealer_hedge_net"])
        t_days=sum(1 for x in gp["trust_net"] if x>0); f_days=sum(1 for x in gp["foreign_net"] if x>0)
        avg=vol_map.get(sid, np.nan)
        t_ratio = np.nan if pd.isna(avg) or avg<=0 else t_net/(avg*5)
        f_ratio = np.nan if pd.isna(avg) or avg<=0 else f_net/(avg*5)
        h_ratio = np.nan if pd.isna(avg) or avg<=0 else h_abs/(avg*5)
        hedge = (not pd.isna(h_ratio) and h_ratio>=0.03) or (h_abs >= abs(t_net)+abs(f_net) and h_abs>0)
        trust_acc = t_days>=3 and t_net>0 and (pd.isna(t_ratio) or t_ratio>=MIN_TRUST_5D_VOLUME_RATIO)
        foreign_sup = f_days>=3 and f_net>0 and (pd.isna(f_ratio) or f_ratio>=MIN_FOREIGN_5D_VOLUME_RATIO)
        rows.append({"stock_id":sid,"trust_buy_days_5":t_days,"trust_5d_net":t_net,"foreign_buy_days_5":f_days,"foreign_5d_net":f_net,"dealer_hedge_5d_abs":h_abs,"trust_volume_ratio":t_ratio,"foreign_volume_ratio":f_ratio,"hedge_volume_ratio":h_ratio,"hedge_dominant":hedge,"trust_accumulation":trust_acc,"foreign_support":foreign_sup})
    return pd.DataFrame(rows)

def make_revenue_features(rev_history):
    if rev_history is None or rev_history.empty: return pd.DataFrame(columns=["stock_id","month_revenue_yoy","month_revenue_mom","fundamental_bad"])
    df=rev_history.copy().sort_values(["stock_id","announce_date"])
    rows=[]
    for sid, gp in df.groupby("stock_id"):
        gp=gp.drop_duplicates(subset=["announce_date"]).sort_values("announce_date")
        latest=gp.iloc[-1]; prev=gp.iloc[-2] if len(gp)>=2 else None
        yoy=latest["month_revenue_yoy"]; mom=latest["month_revenue_mom"]
        prev_yoy=prev["month_revenue_yoy"] if prev is not None else np.nan
        yoy_ch=(yoy-prev_yoy) if not pd.isna(yoy) and not pd.isna(prev_yoy) else np.nan
        bad=(not pd.isna(mom) and not pd.isna(yoy) and mom<=-20 and yoy<=0) or (not pd.isna(mom) and not pd.isna(yoy_ch) and mom<=-10 and yoy_ch<=-20)
        rows.append({"stock_id":sid,"month_revenue":latest["month_revenue"],"month_revenue_yoy":yoy,"month_revenue_mom":mom,"previous_yoy":prev_yoy,"yoy_change_pp":yoy_ch,"fundamental_bad":bool(bad),"fundamental_note":"MoM+YoY轉弱" if bad else "正常"})
    return pd.DataFrame(rows)

def classify_stock(row):
    sid=row["stock_id"]
    if sid in EXCLUDE_TOOL_STOCKS: return "⚪ 排除：權值／ETF","權值/ETF",-5
    if bool(row.get("hedge_dominant",False)): return "🟠 排除：避險主導","避險流量高",-4
    ret5=row.get("return_5d_pct", np.nan); volr=row.get("volume_ratio_5d", np.nan); ma20=row.get("distance_ma20_pct", np.nan); ma10=row.get("distance_ma10_pct", np.nan); bad=bool(row.get("fundamental_bad",False))
    if not pd.isna(ret5) and ret5>MAX_5D_RETURN_FOR_GREEN: return "🔴 排除：拉高／過熱","短線過熱",-3
    if not pd.isna(volr) and volr>MAX_VOLUME_SPIKE_RATIO and not pd.isna(ret5) and ret5>3: return "🔴 排除：拉高／過熱","爆量拉高",-3
    if not pd.isna(ma20) and ma20<0 and bad: return "🔴 排除：跌破MA20＋基本面變壞","破線+基本面差",-4
    if row.get("trust_5d_net",0)<0 and row.get("foreign_5d_net",0)<0 and row.get("trust_buy_days_5",0)==0: return "🔴 排除：法人轉賣","投信外資同賣",-3
    if not pd.isna(ma20) and ma20<0 and not bad and (row.get("trust_accumulation",False) or row.get("foreign_support",False)): return "🟡 籌碼尚在、價格轉弱","等待站回MA20",2
    price_ok=(pd.isna(ret5) or (-7<=ret5<=6)) and (pd.isna(row.get("range_10d_pct", np.nan)) or row.get("range_10d_pct")<=16) and (pd.isna(volr) or volr<=2.0) and (pd.isna(ma20) or abs(ma20)<=7)
    if row.get("turnover",0)>=MIN_DAILY_TURNOVER and row.get("trust_accumulation",False) and price_ok and not bad: return "🔵 主動資金疑似布局","投信盤整吸籌",8
    if row.get("turnover",0)>=MIN_DAILY_TURNOVER and row.get("trust_accumulation",False) and not pd.isna(ma10) and 0<=ma10<=8 and not bad: return "🟢 吸籌延續／初步確認","站上MA10",9
    if row.get("foreign_support",False): return "⚪ 待驗證：僅外資流入","外資單邊",1
    return "⚪ 不列入","未達標",0

def build_radar(quotes, price_features, inst_features, revenue_features):
    df=quotes.copy()
    for f in [price_features, inst_features, revenue_features]:
        if f is not None and not f.empty: df=df.merge(f, on="stock_id", how="left")
    for col, d in [("trust_buy_days_5",0),("trust_5d_net",0),("foreign_5d_net",0),("hedge_dominant",False),("trust_accumulation",False),("foreign_support",False),("fundamental_bad",False)]:
        if col not in df.columns: df[col]=d
        else: df[col]=df[col].fillna(d)
    df["theme"] = df["stock_id"].map(lambda x: "/".join(THEME_MAP.get(x, [])))
    classified=df.apply(classify_stock, axis=1, result_type="expand")
    classified.columns=["signal","reason","base_score"]
    df=pd.concat([df, classified], axis=1)
    df["score"]=df["base_score"]
    df.loc[df["trust_buy_days_5"]>=4, "score"]+=2
    df.loc[df["foreign_support"]==True, "score"]+=1
    df.loc[df["stock_id"].isin(FOCUS_STOCKS), "score"]+=5
    order={"🔵 主動資金疑似布局":1,"🟢 吸籌延續／初步確認":2,"🟡 籌碼尚在、價格轉弱":3,"⚪ 待驗證：僅外資流入":4,"🔴 排除：拉高／過熱":5,"🔴 排除：跌破MA20＋基本面變壞":6,"🔴 排除：法人轉賣":7,"🟠 排除：避險主導":8,"⚪ 排除：權值／ETF":9,"⚪ 排除：權值／避險工具":9,"⚪ 不列入":99}
    df["sort_order"]=df["signal"].map(order).fillna(99)
    return df.sort_values(by=["sort_order","score","turnover"], ascending=[True,False,False], na_position="last").drop(columns=["sort_order"])

def format_price(v): return "-" if pd.isna(v) else f"{float(v):.2f}"
def format_pct(v): return "累積中" if pd.isna(v) else f"{float(v):+.1f}%"
def format_ratio(v): return "累積中" if pd.isna(v) else f"{float(v)*100:.2f}%"
def format_shares(v):
    if pd.isna(v): return "-"
    v=int(v)
    return f"{v/1000:+.1f} 張" if abs(v)>=1000 else f"{v:+,} 股"

def stock_lines(df, max_rows):
    if df is None or df.empty: return "（今日無股票）"
    lines=[]
    for _, r in df.head(max_rows).iterrows():
        tag=" ⭐【關注】" if r['stock_id'] in FOCUS_STOCKS else ""
        theme=f"｜{r['theme']}" if r.get('theme') else ""
        lines.append(f"{r['signal']}{tag}｜{r['stock_id']} {r['stock_name']}{theme}｜{r['market']}｜收盤 {format_price(r['close'])}")
        lines.append(f"投信5日 {format_shares(r['trust_5d_net'])}（{int(r['trust_buy_days_5'])}日買）｜投信相對量 {format_ratio(r['trust_volume_ratio'])}｜外資5日 {format_shares(r['foreign_5d_net'])}")
        lines.append(f"5日 {format_pct(r['return_5d_pct'])}｜10日振幅 {format_pct(r['range_10d_pct'])}｜距MA20 {format_pct(r['distance_ma20_pct'])}｜YoY {format_pct(r['month_revenue_yoy'])}")
        lines.append(f"判定：{r['reason']}")
        lines.append("")
    return "\n".join(lines).rstrip()

def make_email_body(radar, today_str, exec_time_str):
    focus_df=radar[radar["stock_id"].isin(FOCUS_STOCKS)]
    blue=radar[radar["signal"]=="🔵 主動資金疑似布局"]
    green=radar[radar["signal"]=="🟢 吸籌延續／初步確認"]
    yellow=radar[radar["signal"]=="🟡 籌碼尚在、價格轉弱"]
    red=radar[radar["signal"].str.startswith("🔴", na=False)]
    lines=[f"台股主動資金雷達 {today_str}","執行："+exec_time_str,"","━━━━━━━━━━━━━━━━━━━━","⭐ 指定關注 7 檔即時狀態","━━━━━━━━━━━━━━━━━━━━"]
    if not focus_df.empty:
        for _, r in focus_df.iterrows():
            lines.append(f"• {r['stock_id']} {r['stock_name']} {r.get('theme','')} ｜ {r['signal']} ｜ 收盤 {format_price(r['close'])} ｜ 投信 {format_shares(r['trust_5d_net'])} ｜ {r['reason']}")
    else: lines.append("（無資料）")
    lines+=["","━━━━━━━━━━━━━━━━━━━━",f"A. 🔵 布局（{len(blue)}）","━━━━━━━━━━━━━━━━━━━━",stock_lines(blue,15),"","━━━━━━━━━━━━━━━━━━━━",f"B. 🟢 延續（{len(green)}）","━━━━━━━━━━━━━━━━━━━━",stock_lines(green,15),"","━━━━━━━━━━━━━━━━━━━━",f"C. 🟡 轉弱（{len(yellow)}）","━━━━━━━━━━━━━━━━━━━━",stock_lines(yellow,10),"","━━━━━━━━━━━━━━━━━━━━",f"D. 🔴 排除（{len(red)}）","━━━━━━━━━━━━━━━━━━━━",stock_lines(red,15)]
    return "\n".join(lines)

def send_email(subject, body):
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]): print("略過寄信"); return
    m=MIMEMultipart(); m["From"]=GMAIL_USER; m["To"]=RECIPIENT_EMAIL; m["Subject"]=subject; m.attach(MIMEText(body,"plain","utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com",465) as s: s.login(GMAIL_USER,GMAIL_APP_PASSWORD); s.send_message(m)

def main():
    NOW=datetime.now(TZ); TODAY=NOW.strftime("%Y-%m-%d")
    print(f"========== 開始 {NOW} ==========")
    quotes=get_all_quotes()
    try: inst=get_twse_institutional()
    except Exception as e: print(e); inst=pd.DataFrame(columns=["stock_id","date","foreign_net","trust_net","dealer_hedge_net"])
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
    print("完成")

if __name__=="__main__":
    try: main()
    except Exception:
        traceback.print_exc()
        try: send_email(f"【錯誤】雷達｜{get_today_str()}", traceback.format_exc())
        except: pass
        raise
