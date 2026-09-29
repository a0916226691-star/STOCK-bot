# -*- coding: utf-8 -*-
"""
台股「主動資金疑似布局」雷達 v4.5 8大核心持股完全版
特色：全市場 2,000 檔掃描 ＋ 智慧動態月度潛伏 ＋ 8大核心持股健檢 ＋ 終端機即時預覽
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

# 💡 8 大核心持股清單（已完整納入士電 1503）
FOCUS_STOCKS = ["2383", "2368", "6197", "3293", "4763", "1808", "6919", "1503"]

# 8 大核心持股的深度法人資料庫
FOCUS_PROFILES = {
    "2383": {"name": "台光電", "theme": "AI伺服器/高速CCL", "valuation": "合理偏高(高成長支撐)", "good_catalyst": "M8以上高速材料市占獨強", "risk_catalyst": "銅箔/玻纖布原料成本波動", "rating": "強力持有"},
    "2368": {"name": "金像電", "theme": "AI伺服器/交換器PCB", "valuation": "合理區間", "good_catalyst": "高階AI伺服器與交換器板需求強勁", "risk_catalyst": "產能擴充進度若遞延影響營收", "rating": "逢低加碼"},
    "6197": {"name": "佳必琪", "theme": "AI高速傳輸線束", "valuation": "成長型合理估值", "good_catalyst": "美系CSP大廠高速線纜拉貨", "risk_catalyst": "伺服器出貨節奏雜音", "rating": "區間操作"},
    "3293": {"name": "鈊象", "theme": "網路遊戲/高殖利率", "valuation": "偏高(高ROE與現金流保護)", "good_catalyst": "歐洲與美洲海外授權版圖擴大", "risk_catalyst": "海外博弈法規緊縮", "rating": "長期核心持有"},
    "4763": {"name": "材料-KY", "theme": "綠色環保絲束", "valuation": "評價修正中/本益比合理", "good_catalyst": "全球絲束供給緊俏、擴產效益", "risk_catalyst": "營收若月減需防成長神話破滅", "rating": "中立觀望"},
    "1808": {"name": "潤隆", "theme": "營建/高殖利率防禦", "valuation": "偏低(資產低估/高股息)", "good_catalyst": "完工案認列高峰、現金股利題材", "risk_catalyst": "房市政策打炒房、工程延宕", "rating": "逢低存股"},
    "6919": {"name": "康霈", "theme": "生技新藥/減脂醫美", "valuation": "題材面估值(未獲利)", "good_catalyst": "CBL-514二三期臨床與國際授權", "risk_catalyst": "生技臨床數據不如預期(高風險)", "rating": "高風險投機/嚴設停損"},
    "1503": {"name": "士電", "theme": "重電／變壓器／綠能", "valuation": "成長型合理估值", "good_catalyst": "台電強韌電網與北美大型變壓器外銷滿手", "risk_catalyst": "原物料銅價波動與本益比修正風險", "rating": "逢低加碼"},
}

# 全市場熱門題材對應庫（當雷達抓到新股票時自動對應）
THEME_PROFILES = {
    "AI 伺服器／ODM": {"valuation": "動態成長估值", "good": "受惠全球AI資本支出與CSP大廠拉貨", "risk": "供應鏈零組件短缺或毛利不如預期"},
    "散熱": {"valuation": "評價偏高但具潛力", "good": "水冷散熱(Liquid Cooling)滲透率加速", "risk": "技術迭代快、競爭對手多"},
    "PCB／CCL／載板": {"valuation": "合理區間", "good": "伺服器升級帶動高階板層數與單價提升", "risk": "原物料價格波動風險"},
    "機器人／智慧自動化": {"valuation": "題材本益比高", "good": "全球工業自動化與人形機器人長線趨勢", "risk": "實際營收貢獻尚需時間發酵"},
    "矽光子／CPO／光通訊": {"valuation": "高成長題材高估值", "good": "次世代高速傳輸技術突破點火", "risk": "技術商業化進度若延遲易回檔"},
}

THEMES = {
    "⭐ 個人重點關注焦點股": FOCUS_STOCKS,
    "AI 伺服器／ODM": ["2317", "2382", "3231", "6669", "6805"],
    "散熱": ["3017", "3324", "6205", "6131"],
    "PCB／CCL／載板": ["2383", "2368", "2385", "3037", "4967", "6274", "6197"],
    "機器人／智慧自動化": ["4588", "1590", "2359", "4566"],
    "矽光子／CPO／光通訊": ["3163", "3363", "4979", "3450", "6451"],
    "重電／綠能": ["1513", "1519", "1503", "6873"],
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
    for i in range(3):
        try:
            r=requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except: time.sleep(2*(i+1))
    raise RuntimeError(f"連線失敗 {url}")

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
    except: pass
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["stock_id"])

def get_twse_institutional():
    url="https://www.twse.com.tw/rwd/zh/fund/T86"
    data=None; used=None
    for d in get_recent_dates(15):
        try:
            j=request_get(url, params={"response":"json","date":d,"selectType":"ALLBUT0999"}, timeout=15).json()
            if j.get("stat")=="OK": data=j; used=d; break
        except: continue
    if not data: raise RuntimeError("無法人資料")
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
                    rows.append({"stock_id":sid,"month_revenue":safe_float(it.get(rev_col, np.nan)),"month_revenue_yoy":safe_float(it.get(yoy_col, np.nan)),"month_revenue_mom":safe_float(it.get(mom_col, np.nan)) if mom_col else np.nan,"announce_date":TODAY})
        except: pass
    df=pd.DataFrame(rows)
    return df.drop_duplicates(subset=["stock_id"]) if not df.empty else pd.DataFrame(columns=["stock_id","month_revenue","month_revenue_yoy","month_revenue_mom","announce_date"])

def load_history(prefix, limit=MAX_HISTORY_FILES):
    if not os.path.exists(OUTPUT_DIR): return pd.DataFrame()
    files=sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith(prefix) and f.endswith(".csv")])[-limit:]
    frames=[pd.read_csv(os.path.join(OUTPUT_DIR, fn), dtype={"stock_id":str}) for fn in files]
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
        gp_5=gp.tail(5)
        t_net_5=sum(gp_5["trust_net"]); f_net_5=sum(gp_5["foreign_net"]); h_abs_5=sum(abs(x) for x in gp_5["dealer_hedge_net"])
        t_days_5=sum(1 for x in gp_5["trust_net"] if x>0); f_days_5=sum(1 for x in gp_5["foreign_net"] if x>0)
        
        gp_20=gp.tail(20)
        actual_days = len(gp_20)
        t_net_20=sum(gp_20["trust_net"]); f_net_20=sum(gp_20["foreign_net"])
        t_days_20=sum(1 for x in gp_20["trust_net"] if x>0); f_days_20=sum(1 for x in gp_20["foreign_net"] if x>0)
        
        required_days = max(2, int(actual_days * 0.4))
        smart_accum_20d = actual_days >= 2 and ((t_days_20 >= required_days and t_net_20 > 0) or (f_days_20 >= required_days and f_net_20 > 0))

        avg=vol_map.get(sid, np.nan)
        t_ratio=np.nan if pd.isna(avg) or avg<=0 else t_net_5/(avg*5)
        f_ratio=np.nan if pd.isna(avg) or avg<=0 else f_net_5/(avg*5)
        h_ratio=np.nan if pd.isna(avg) or avg<=0 else h_abs_5/(avg*5)
        
        hedge=(not pd.isna(h_ratio) and h_ratio>=0.03) or (h_abs_5>=abs(t_net_5)+abs(f_net_5) and h_abs_5>0)
        trust_acc=t_days_5>=3 and t_net_5>0 and (pd.isna(t_ratio) or t_ratio>=MIN_TRUST_5D_VOLUME_RATIO)
        foreign_sup=f_days_5>=3 and f_net_5>0 and (pd.isna(f_ratio) or f_ratio>=MIN_FOREIGN_5D_VOLUME_RATIO)
        
        rows.append({
            "stock_id":sid, "trust_buy_days_5":t_days_5, "trust_5d_net":t_net_5,
            "foreign_buy_days_5":f_days_5, "foreign_5d_net":f_net_5, "dealer_hedge_5d_abs":h_abs_5,
            "trust_volume_ratio":t_ratio, "foreign_volume_ratio":f_ratio, "hedge_dominant":hedge,
            "trust_accumulation":trust_acc, "foreign_support":foreign_sup,
            "trust_20d_net":t_net_20, "foreign_20d_net":f_net_20, "smart_money_accum_20d":smart_accum_20d
        })
    return pd.DataFrame(rows)

def make_revenue_features(hist):
    if hist is None or hist.empty: return pd.DataFrame(columns=["stock_id","month_revenue","month_revenue_yoy","month_revenue_mom","fundamental_bad"])
    df=hist.copy().sort_values(["stock_id","announce_date"])
    rows=[]
    for sid, gp in df.groupby("stock_id"):
        gp=gp.drop_duplicates(subset=["announce_date"]).sort_values("announce_date")
        latest=gp.iloc[-1]; prev=gp.iloc[-2] if len(gp)>=2 else None
        yoy=safe_float(latest.get("month_revenue_yoy", np.nan)); mom=safe_float(latest.get("month_revenue_mom", np.nan))
        prev_yoy=safe_float(prev.get("month_revenue_yoy", np.nan)) if prev is not None else np.nan
        yoy_ch=(yoy-prev_yoy) if not pd.isna(yoy) and not pd.isna(prev_yoy) else np.nan
        bad=(not pd.isna(mom) and not pd.isna(yoy) and mom<=-20 and yoy<=0) or (not pd.isna(mom) and not pd.isna(yoy_ch) and mom<=-10 and yoy_ch<=-20)
        rows.append({"stock_id":sid,"month_revenue":safe_float(latest.get("month_revenue", np.nan)),"month_revenue_yoy":yoy,"month_revenue_mom":mom,"fundamental_bad":bool(bad)})
    return pd.DataFrame(rows)

def classify_stock(row):
    sid=row.get("stock_id","")
    if sid in EXCLUDE_TOOL_STOCKS: return "⚪ 排除：權值／ETF","權值/ETF",-5
    if bool(row.get("hedge_dominant",False)): return "🟠 排除：避險主導","避險高",-4
    
    ret5=row.get("return_5d_pct", np.nan); volr=row.get("volume_ratio_5d", np.nan); ma20=row.get("distance_ma20_pct", np.nan); bad=bool(row.get("fundamental_bad",False))
    smart_accum_20d = bool(row.get("smart_money_accum_20d", False))

    if not pd.isna(ret5) and ret5>10: return "🔴 排除：拉高／過熱","過熱",-3
    if not pd.isna(volr) and volr>2.0 and not pd.isna(ret5) and ret5>3: return "🔴 排除：拉高／過熱","爆量",-3
    if not pd.isna(ma20) and ma20<0 and bad: return "🔴 排除：跌破MA20＋基本面變壞","破線+基本面",-4
    if row.get("trust_5d_net",0)<0 and row.get("foreign_5d_net",0)<0 and row.get("trust_buy_days_5",0)==0: return "🔴 排除：法人轉賣","同賣",-3

    price_ok_monthly = (pd.isna(ret5) or -7 <= ret5 <= 8) and (pd.isna(row.get("range_10d_pct", np.nan)) or row.get("range_10d_pct") <= 20) and (pd.isna(ma20) or abs(ma20) <= 8)
    if smart_accum_20d and price_ok_monthly and not bad:
        return "🟣 月度主力默默吸籌（潛伏期）", "主力和法人持續默默買超+基期低", 11

    if not pd.isna(ma20) and ma20<0 and not bad and (row.get("trust_accumulation",False) or row.get("foreign_support",False)): return "🟡 籌碼尚在、價格轉弱","等站回",2
    
    price_ok=(pd.isna(ret5) or -7<=ret5<=6) and (pd.isna(row.get("range_10d_pct", np.nan)) or row.get("range_10d_pct")<=16) and (pd.isna(volr) or volr<=2.0) and (pd.isna(ma20) or abs(ma20)<=7)
    if row.get("turnover",0)>=MIN_DAILY_TURNOVER and row.get("trust_accumulation",False) and price_ok and not bad: return "🔵 主動資金疑似布局","吸籌",8
    if row.get("turnover",0)>=MIN_DAILY_TURNOVER and row.get("trust_accumulation",False) and not bad: return "🟢 吸籌延續／初步確認","吸籌延續",9
    if row.get("foreign_support",False): return "⚪ 待驗證：僅外資流入","外資單邊",1
    return "⚪ 不列入","未達標",0

def build_radar(quotes, pf, inf, rf):
    df=quotes.copy()
    for f in [pf, inf, rf]:
        if f is not None and not f.empty: df=df.merge(f, on="stock_id", how="left")
    
    must_cols = {
        "ma10": np.nan, "ma20": np.nan, "return_5d_pct": np.nan, "range_10d_pct": np.nan,
        "avg_volume_5d": np.nan, "volume_ratio_5d": np.nan, "distance_ma10_pct": np.nan, "distance_ma20_pct": np.nan,
        "trust_buy_days_5": 0, "trust_5d_net": 0, "foreign_5d_net": 0, "trust_volume_ratio": np.nan,
        "month_revenue": np.nan, "month_revenue_yoy": np.nan, "month_revenue_mom": np.nan,
        "hedge_dominant": False, "trust_accumulation": False, "foreign_support": False, 
        "smart_money_accum_20d": False, "trust_20d_net": 0, "foreign_20d_net": 0,
        "fundamental_bad": False
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
    df.loc[df["stock_id"].isin(FOCUS_STOCKS), "score"]+=10
    
    order={
        "🟣 月度主力默默吸籌（潛伏期）": 1,
        "🔵 主動資金疑似布局": 2,
        "🟢 吸籌延續／初步確認": 3,
        "🟡 籌碼尚在、價格轉弱": 4,
        "⚪ 待驗證：僅外資流入": 5,
        "🔴 排除：拉高／過熱": 6,
        "🔴 排除：跌破MA20＋基本面變壞": 7,
        "🔴 排除：法人轉賣": 8,
        "🟠 排除：避險主導": 9,
        "⚪ 排除：權值／ETF": 10,
        "⚪ 不列入": 99
    }
    df["sort_order"]=df["signal"].map(order).fillna(99)
    return df.sort_values(by=["sort_order","score","turnover"], ascending=[True,False,False]).drop(columns=["sort_order"])

def fmt_price(v): return "-" if pd.isna(v) else f"{float(v):.2f}"
def fmt_pct(v): return "累積中" if pd.isna(v) else f"{float(v):+.1f}%"
def fmt_shares(v):
    if pd.isna(v): return "-"
    v=int(v); return f"{v/1000:+.1f} 張" if abs(v)>=1000 else f"{v:+,} 股"

def make_focus_stock_report(radar_df):
    """專屬 8 大核心持股之估值、題材與法人評等報告"""
    lines = ["="*40, "⭐ 【8大核心持股法人級深度診斷報告】", "="*40]
    for sid in FOCUS_STOCKS:
        profile = FOCUS_PROFILES.get(sid, {})
        name = profile.get("name", sid)
        theme = profile.get("theme", "未分類")
        valuation = profile.get("valuation", "評估中")
        good = profile.get("good_catalyst", "-")
        risk = profile.get("risk_catalyst", "-")
        rating = profile.get("rating", "持有")
        
        match = radar_df[radar_df["stock_id"] == sid]
        if not match.empty:
            r = match.iloc[0]
            close = fmt_price(r.get("close"))
            signal = r.get("signal", "無訊號")
            t_net = fmt_shares(r.get("trust_5d_net", 0))
            yoy = fmt_pct(r.get("month_revenue_yoy", np.nan))
        else:
            close = "-"; signal = "無報價"; t_net = "-"; yoy = "-"

        lines.append(f"• {sid} {name} ｜ 產業：{theme}")
        lines.append(f"  [估值狀態] {valuation} ｜ 收盤：{close}")
        lines.append(f"  [核心題材] 好：{good} ｜ 風險：{risk}")
        lines.append(f"  [即時現況] 訊號：{signal} ｜ 投信5日：{t_net} ｜ 營收YoY：{yoy}")
        lines.append(f"  👉 【法人綜合評等】：💡 **{rating}**")
        lines.append("-" * 35)
    return "\n".join(lines)

def stock_lines_with_report(df, max_rows):
    """全市場新挖出來的潛力股，自動帶出估值與法人級題材報告"""
    if df is None or df.empty: return "（今日無股票符合）"
    lines=[]
    for _, r in df.head(max_rows).iterrows():
        sid = r.get('stock_id','')
        name = r.get('stock_name','')
        theme_str = r.get('theme','')
        tag=" ⭐" if sid in FOCUS_STOCKS else ""
        
        profile_info = {"valuation": "動態追蹤中", "good": "具備產業題材發酵潛力", "risk": "留意市場震盪與籌碼鬆動"}
        for t_key, t_val in THEME_PROFILES.items():
            if t_key in theme_str:
                profile_info = t_val
                break

        lines.append(f"{r.get('signal','')} {tag}｜{sid} {name}｜{theme_str}｜收盤 {fmt_price(r.get('close', np.nan))}")
        lines.append(f"  [量化法人報告] 估值：{profile_info['valuation']} ｜ 投信5日 {fmt_shares(r.get('trust_5d_net', 0))} ｜ 累計1月 {fmt_shares(r.get('trust_20d_net', 0))}")
        lines.append(f"  [題材與風險] 好：{profile_info['good']} ｜ 風險：{profile_info['risk']}")
        lines.append(f"  [技術與基本面] 5日漲幅 {fmt_pct(r.get('return_5d_pct', np.nan))} ｜ 營收YoY {fmt_pct(r.get('month_revenue_yoy', np.nan))}")
        lines.append(f"  判定原因：{r.get('reason','')}")
        lines.append("")
    return "\n".join(lines).rstrip()

def make_email_body(radar, today_str, exec_time):
    focus_report = make_focus_stock_report(radar)
    
    purple = radar[radar["signal"] == "🟣 月度主力默默吸籌（潛伏期）"]
    blue = radar[radar["signal"] == "🔵 主動資金疑似布局"]
    green = radar[radar["signal"] == "🟢 吸籌延續／初步確認"]
    yellow = radar[radar["signal"] == "🟡 籌碼尚在、價格轉弱"]
    red = radar[radar["signal"].str.startswith("🔴", na=False)]
    
    lines = [
        f"台股主動資金雷達與智慧法人報告 {today_str}",
        f"執行時間：{exec_time}",
        "",
        focus_report,
        "",
        "="*40,
        "📊 【全市場資金雷達掃描與智慧法人報告】",
        "="*40,
        f"🟣 月度潛伏（近1月主力默默吃貨） {len(purple)}", stock_lines_with_report(purple, 15),
        f"A. 🔵 短線布局 {len(blue)}", stock_lines_with_report(blue, 15), 
        f"B. 🟢 延續 {len(green)}", stock_lines_with_report(green, 15), 
        f"C. 🟡 轉弱 {len(yellow)}", stock_lines_with_report(yellow, 10), 
        f"D. 🔴 排除 {len(red)}", stock_lines_with_report(red, 15)
    ]
    return "\n".join(lines)

def send_email(sub, body):
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]): 
        print("💡 提示：尚未設定 Gmail 環境變數，故略過自動寄信。")
        return
    m=MIMEMultipart(); m["From"]=GMAIL_USER; m["To"]=RECIPIENT_EMAIL; m["Subject"]=sub; m.attach(MIMEText(body,"plain","utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com",465) as s: s.login(GMAIL_USER,GMAIL_APP_PASSWORD); s.send_message(m)

def main():
    NOW=datetime.now(TZ); TODAY=NOW.strftime("%Y-%m-%d")
    print(f"=== 開始執行台股主動資金雷達：{TODAY} ===")

    quotes=get_all_quotes()
    print(f"-> 成功抓取全市場報價，共計 {len(quotes)} 檔股票。")

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
    
    print("\n" + "=" * 50)
    print(body)
    print("=" * 50 + "\n")

    send_email(f"主動資金雷達與智慧法人報告｜{TODAY}", body)
    print("=== 執行完畢！完整 CSV 已存至 output/ 資料夾 ===")

if __name__=="__main__":
    try: main()
    except: traceback.print_exc(); raise
