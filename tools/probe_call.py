import requests,re
s=requests.Session();H={"User-Agent":"Mozilla/5.0"}
r=s.get("https://www.tdcc.com.tw/portal/zh/smWeb/qryStock",headers=H,timeout=30)
print(r.status_code,len(r.text))
ds=re.findall(r'<option[^>]*value="(\d{8})"',r.text);print("可選日期",len(ds),ds[:3],ds[-3:])
tok=re.findall(r'name="SYNCHRONIZER_TOKEN"\s+value="([^"]+)"',r.text) or re.findall(r'SYNCHRONIZER_TOKEN[^>]*value="([^"]+)"',r.text)
print("token",bool(tok)); print(r.text[:400])
if tok and ds:
    d={"SYNCHRONIZER_TOKEN":tok[0],"SYNCHRONIZER_URI":"/portal/zh/smWeb/qryStock","method":"submit","firDate":ds[-1],"scaDate":ds[-1],"sqlMethod":"StockNo","stockNo":"2383","stockName":""}
    r2=s.post("https://www.tdcc.com.tw/portal/zh/smWeb/qryStock",data=d,headers=H,timeout=30)
    t=re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",r2.text));i=t.find("1,000,001");print(r2.status_code,t[i-300:i+200])
