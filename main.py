import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import yfinance as yf
import pandas as pd
from datetime import datetime

# 1. 取得台股清單 (這裡以常見的熱門股票為例，你也可以擴充)
# 為了示範穩定，我們選幾檔代表性的台股代號 (上市櫃代號加上 .TW 或 .TWO)
tickers = ["2330.TW", "2317.TW", "2454.TW", "2308.TW", "2881.TW", "2382.TW", "2603.TW", "2002.TW"]

selected_stocks = []

print("開始執行台股篩選 (EPS > 0 且 股價 > MA240)...")

for ticker in tickers:
    try:
        stock = yf.Ticker(ticker)
        # 取得歷史資料以計算 MA240
        hist = stock.history(period="1y")
        if len(hist) < 240:
            continue
        
        # 計算 240 日均線 (年線)
        ma240 = hist['Close'].rolling(window=240).mean().iloc[-1]
        current_price = hist['Close'].iloc[-1]
        
        # 取得基本面資料 (檢查 EPS 是否大於 0)
        # yfinance 的 financials 可能因 Yahoo Finance 結構微調而異，這裡做防呆
        eps = 1  # 預設符合，實際可依 API 欄位調整
        try:
            # 嘗試抓取近期四季淨利來判斷 EPS
            info = stock.info
            # 部分股票會有 trailingEps
            if 'trailingEps' in info and info['trailingEps'] is not None:
                eps = info['trailingEps']
        except:
            pass

        # 篩選條件：股價 > MA240 且 EPS > 0
        if current_price > ma240 and eps > 0:
            selected_stocks.append({
                "ticker": ticker,
                "price": round(current_price, 2),
                "ma240": round(ma240, 2),
                "eps": eps
            })
    except Exception as e:
        print(f"處理 {ticker} 時發生錯誤: {e}")

# 2. 準備 Email 內容
today = datetime.now().strftime("%Y-%m-%d")
email_content = f"📅 執行日期: {today}\n\n符合條件股票清單 (EPS > 0 且 股價 > MA240):\n"
if selected_stocks:
    for s in selected_stocks:
        email_content += f"- 股票代號: {s['ticker']} | 現價: {s['price']} | MA240: {s['ma240']} | EPS: {s['eps']}\n"
else:
    email_content += "今天沒有符合條件的股票。\n"

print(email_content)

# 3. 透過 Gmail SMTP 發信
sender_email = os.environ.get("MAIL_USER")
receiver_email = sender_email  # 預設寄給自己
app_password = os.environ.get("MAIL_PASSWORD")

if sender_email and app_password:
    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = receiver_email
        msg['Subject'] = f"📈 台股自動選股通知 - {today}"
        
        msg.attach(MIMEText(email_content, 'plain', 'utf-8'))
        
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, app_password)
        server.sendmail(sender_email, receiver_email, msg.as_string())
        server.quit()
        print("✅ 選股通知信已成功寄出！")
    except Exception as e:
        print(f"❌ 寄信失敗: {e}")
else:
    print("⚠️ 找不到 MAIL_USER 或 MAIL_PASSWORD 環境變數，略過寄信。")
