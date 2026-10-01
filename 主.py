# 主.py v6.3.1 - 冷凍版
# 修正：上市上櫃皆失敗不會 exit(1)，會自動讀 output 備援 + 會叫輔的
import os
import glob
import pandas as pd
import requests
from datetime import datetime

# === 關鍵：叫輔的 ===
import radar_engine as engine

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

def safe_text(v):
    return str(v).strip() if pd.notna(v) else ""

def normalize_stock_id(v):
    s = str(v).strip()
    return s.zfill(4) if s.isdigit() else s

def load_latest_backup(prefix):
    files = sorted(glob.glob(f"{OUTPUT_DIR}/{prefix}_*.csv"))
    if not files:
        return None, None
    latest = files[-1]
    print(f"[備援] 沿用 {latest}")
    return pd.read_csv(latest, dtype={"stock_id": str}), latest

def get_all_quotes():
    """ v6.3 核心修正：抓不到就不死，改讀備援 """
    quotes = pd.DataFrame()
    try:
        print("[主] 抓取 TWSE...")
        r1 = requests.get(TWSE_URL, timeout=20)
        r1.raise_for_status()
        # 你原本解析 TWSE 的邏輯放這裡...
        # 這裡先模擬成功解析
    except Exception as e:
        print(f"[主] TWSE 失敗: {e}")

    try:
        print("[主] 抓取 TPEx...")
        r2 = requests.get(TPEX_URL, timeout=20)
        r2.raise_for_status()
        # 你原本解析 TPEx 的邏輯放這裡...
    except Exception as e:
        print(f"[主] TPEx 失敗: {e}")

    # 如果兩個都沒抓到，v6.2 會直接 exit(1)，v6.3 改成讀備援
    if quotes.empty:
        print("[主] 上市上櫃皆失敗，啟動 v6.3 備援機制")
        backup, _ = load_latest_backup("layout_price_history")
        if backup is not None and not backup.empty:
            return backup
        else:
            print("[主] 連備援都沒有，回傳空表讓 Email 顯示備援訊息")
            return pd.DataFrame()

    return quotes

def get_all_institutional():
    # 你原本抓法人 + 備援的邏輯
    # 同樣邏輯：抓不到就讀 output/layout_institutional_*.csv
    return pd.DataFrame()

def main():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"===== {today} 開始執行 主.py v6.3.1 =====")

    # 1. 主的只負責抓 (有備援)
    quotes_df = get_all_quotes()
    inst_df = get_all_institutional()

    # 2. 主的呼叫輔的 - 這就是你要的「跑輔的代碼」
    # 你以後改 radar_engine.py，這裡就會自動用新的
    print("[主] 呼叫輔的 radar_engine.py 開始算燈號...")
    radar_df = engine.build_radar(quotes_df, inst_df)
    html_body = engine.make_html_email_body(radar_df, today)

    # 3. 主的負責存檔 + 寄信 (這段永遠不動)
    if not quotes_df.empty:
        quotes_df.to_csv(f"{OUTPUT_DIR}/layout_price_history_{today}.csv", index=False, encoding='utf-8-sig')
    if not radar_df.empty:
        radar_df.to_csv(f"{OUTPUT_DIR}/layout_radar_{today}.csv", index=False, encoding='utf-8-sig')

    # send_email(html_body) # 你原本的寄信函式
    print(f"[主] 完成， radar {len(radar_df)} 檔")
    print("主的已跑完，輔的也跟著跑完了")

if __name__ == "__main__":
    main()
