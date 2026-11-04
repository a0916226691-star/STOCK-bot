import re, requests
H={"User-Agent":"Mozilla/5.0"}
ids=["2383","2368","6197","3293","4763","1808","1503"]
for sid in ids:
    for url in ["https://mopsov.twse.com.tw/mops/web/ajax_t100sb07_1","https://mops.twse.com.tw/mops/web/ajax_t100sb07_1"]:
        try:
            r=requests.post(url,data={"encodeURIComponent":1,"step":1,"firstin":1,"off":1,"queryName":"co_id","inpuType":"co_id","TYPEK":"all","co_id":sid},headers=H,timeout=30)
            t=r.text
            print("=====",sid,url,r.status_code,len(t))
            print(re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," | ",t))[:1500])
            print("LINKS:",re.findall(r'(?:href|src)=["\']([^"\']+)',t)[:40])
            break
        except Exception as e: print(sid,url,e)
