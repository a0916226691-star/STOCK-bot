import requests, time, csv, gzip
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"}
T=[l.strip() for l in open("ids.txt") if l.strip()]
w=csv.writer(gzip.open("output/adj_monthly.csv.gz","wt"));w.writerow(["t","ts","close","adj"])
ok=0
for n,t in enumerate(T):
    for host in ("query1","query2"):
        try:
            r=requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{t}",params={"period1":"1514764800","period2":"1800000000","interval":"1mo","events":"div,split"},headers=UA,timeout=30)
            j=r.json()["chart"]["result"][0]
            for a,b,c in zip(j["timestamp"],j["indicators"]["quote"][0]["close"],j["indicators"]["adjclose"][0]["adjclose"]):
                w.writerow([t,a,b,c])
            ok+=1;break
        except Exception as e:
            pass
    time.sleep(0.4)
    if n%200==0: print(n,ok,flush=True)
print("done",ok,len(T))
