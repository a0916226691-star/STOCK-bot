import smtplib
import os
import requests
import pandas as pd
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import yfinance as yf
from datetime import datetime

# ==========================================
# 1. 長線價值選股邏輯 (原有邏輯)
# ==========================================
tickers = ["2330.TW", "2317.TW", "2454.TW", "2308.TW", "2881.TW", "2382.TW", "2603.TW", "2002.TW"]
selected_stocks = []

print("開始執行台股長線篩選 (EPS > 0 且 股價 > MA240)...")

for ticker in tickers:
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1y")
        if len(hist) < 240:
            continue
        
        ma240 = hist['Close'].rolling(window=240).mean().iloc[-1]
        current_price = hist['Close'].iloc[-1]
        
        eps = 1  
        try:
            info = stock.info
            if 'trailingEps' in info and info['trailingEps'] is not None:
                eps = info['trailingEps']
        except:
            pass

        if current_price > ma240 and eps > 0:
            selected_stocks.append({
                "ticker": ticker,
                "price": round(current_price, 2),
                "ma240": round(ma240, 2),
                "eps": eps
            })
    except Exception as e:
        print(f"處理 {ticker} 時發生錯誤: {e}")


# ==========================================
# 2. 短線三大法人籌碼追蹤邏輯 (新加入邏輯)
# ==========================================
print("開始抓取證交所三大法人買賣超資料...")
institutional_summary = "【三大法人短線動能觀察】\n"
url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

try:
    response = requests.get(url, headers=headers)
    data = response.json()
    
    if data.get("stat") == "OK":
        fields = data.get("fields")
        rows = data.get("data")
        df_chips = pd.DataFrame(rows, columns=fields)
        
        institutional_summary += "今日法人買賣超前幾大代表性股票摘要：\n"
        for index, row in df_chips.head(5).iterrows():
            code = row[0]
            name = row[1]
            institutional_summary += f"- 代號: {code} {name}\n"
    else:
        institutional_summary += "今日非交易日或無法取得法人資料。\n"
except Exception as e:
    institutional_summary += f"抓取法人籌碼發生錯誤: {e}\n"


# ==========================================
# 3. 組合 Email 內容並發送
# ==========================================
today = datetime.now().strftime("%Y-%m-%d")
email_content = f"📅 執行日期: {today}\n\n"

# 長線選股結果
email_content += "==============================\n"
email_content += "📈 長線價值選股 (EPS > 0 且 股價 > MA240):\n"
if selected_stocks:
    for s in selected_stocks:
        email_content += f"- 股票代號: {s['ticker']} | 現價: {s['price']} | MA240: {s['ma240']} | EPS: {s['eps']}\n"
else:
    email_content += "今天沒有符合條件的長線股票。\n"

email_content += "\n==============================\n"
email_content += institutional_summary

print(email_content)

# 發送 Gmail SMTP
sender_email = os.environ.get("MAIL_USER")
receiver_email = sender_email  
app_password = os.environ.get("MAIL_PASSWORD")

if sender_email and app_password:
    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = receiver_email
        msg['Subject'] = f"📊 台股自動選股與籌碼機器人報告 - {today}"
        
        msg.attach(MIMEText(email_content, 'plain', 'utf-8'))
        
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, app_password)
        server.sendmail(sender_email, receiver_email, msg.as_string())
        server.quit()
        print("✅ 整合後的選股與籌碼通知信已成功寄出！")
    except Exception as e:
        print(f"❌ 寄信失敗: {e}")
else:
    print("⚠️ 找不到 MAIL_USER 或 MAIL_PASSWORD 環境變數，略過寄信。")
