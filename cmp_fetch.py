import requests, json, time
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"}
T=["^TWII","0050.TW"]
out={}
for t in T:
    for host in ("query1","query2"):
        try:
            r=requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{t}",params={"period1":"820454400","period2":"1800000000","interval":"1d","events":"div,split"},headers=UA,timeout=30)
            j=r.json()["chart"]["result"][0]
            out[t]={"ts":j["timestamp"],"close":j["indicators"]["quote"][0]["close"],"adj":j["indicators"]["adjclose"][0]["adjclose"],"vol":j["indicators"]["quote"][0]["volume"],"events":j.get("events",{})}
            print(t,len(j["timestamp"]));break
        except Exception as e:
            print(t,host,"失敗",e)
    time.sleep(1)
json.dump(out,open("output/compare5.json","w"))
