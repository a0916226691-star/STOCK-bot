# 集保股權分散表：近一年每週，千張以上（第 15 級）持股比例、人數，續抓
import requests,re,time,os,csv
H={"User-Agent":"Mozilla/5.0"};URL="https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"
ids=[l.strip() for l in open("tools/tdcc_ids.txt") if l.strip()]
out="output/tdcc.csv";done=set()
if os.path.exists(out): done={(r[0],r[1]) for r in csv.reader(open(out))}
w=open(out,"a",newline="");cw=csv.writer(w);t0=time.time()
s=requests.Session()
def page():
    r=s.get(URL,headers=H,timeout=30)
    return re.findall(r'<option[^>]*value="(\d{8})"',r.text),re.findall(r'name="SYNCHRONIZER_TOKEN"\s+value="([^"]+)"',r.text)
ds,tok=page();print("weeks",len(ds),flush=True);n=0
for sid in ids:
    for d in ds:
        if (sid,d) in done: continue
        if (time.time()-t0)/60>320: break
        for k in range(3):
            try:
                _,tok=page()
                r=s.post(URL,data={"SYNCHRONIZER_TOKEN":tok[0],"SYNCHRONIZER_URI":"/portal/zh/smWeb/qryStock","method":"submit","firDate":d,"scaDate":d,"sqlMethod":"StockNo","stockNo":sid,"stockName":""},headers=H,timeout=30)
                t=re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",r.text))
                m=re.search(r"15 1,000,001以上 ([\d,]+) ([\d,]+) ([\d.]+)",t)
                cw.writerow([sid,d,m.group(1).replace(",","") if m else "",m.group(3) if m else ""]);n+=1;break
            except Exception as e:
                time.sleep(5);s=requests.Session()
        time.sleep(0.3)
    w.flush()
    if n and n%500<len(ds): print(sid,n,round((time.time()-t0)/60),flush=True)
print("done",n)
