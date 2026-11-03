import requests, json, time
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"}
T=["0050.TW","0056.TW","00878.TW","00919.TW","2383.TW","2368.TW","6197.TW","3293.TWO","4763.TW","1808.TW","1503.TW","6919.TW","^TWII"]
out={}
for t in T:
    for host in ("query1","query2"):
        try:
            r=requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{t}",params={"range":"10y","interval":"1d","events":"div,split"},headers=UA,timeout=30)
            j=r.json()["chart"]["result"][0]
            out[t]={"ts":j["timestamp"],"close":j["indicators"]["quote"][0]["close"],"adj":j["indicators"]["adjclose"][0]["adjclose"],"events":j.get("events",{})}
            print(t,len(j["timestamp"]));break
        except Exception as e:
            print(t,host,"失敗",e)
    time.sleep(1)
json.dump(out,open("output/compare.json","w"))
