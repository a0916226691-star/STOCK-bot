# radar_engine.py - 輔的實驗室，以後加燈號都只改這支
import pandas as pd

FOCUS_STOCKS = {
    "2330": {"name": "台積電"}, "2317": {"name": "鴻海"},
    "2383": {"name": "台光電"}, "3037": {"name": "欣興"},
    "3034": {"name": "聯詠"}, "2454": {"name": "聯發科"},
    "2603": {"name": "長榮"}, "2609": {"name": "陽明"},
}

def build_radar(quotes_df, inst_df=None):
    # 這裡放你原本判斷 🟢🔵🟣🟡 的邏輯
    # 我先幫你留一個最小可跑版，你原本的邏輯貼回來就行
    if quotes_df.empty:
        return pd.DataFrame()
    quotes_df["signal"] = "🟢 吸籌延續／初步確認"
    return quotes_df

def make_html_email_body(radar_df, date_str):
    # 三區排版搬來這裡
    if radar_df.empty:
        return f"<h3>{date_str} 今日無資料，沿用歷史備援</h3>"
    return f"<h3>{date_str} 雷達日報</h3>" + radar_df.to_html(index=False)
