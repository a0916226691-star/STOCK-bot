import requests, json, urllib.parse, time
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"}
q=urllib.parse.quote("台郡 6269")
tests={
 "google_rss":f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
 "cnyes_search":"https://api.cnyes.com/media/api/v1/search?q=%E5%8F%B0%E9%83%A1&limit=5",
 "cnyes_tw":"https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit=5",
 "cnyes_stock":"https://ess.api.cnyes.com/ess/api/v1/news/keyword?q=6269&limit=5&page=1",
 "yahoo_page":"https://tw.stock.yahoo.com/quote/6269/news",
 "yahoo_rss":"https://tw.stock.yahoo.com/rss?s=6269",
 "mops_news":"https://mopsov.twse.com.tw/mops/web/ajax_t05st01?encodeURIComponent=1&step=1&firstin=1&off=1&co_id=6269&year=115",
 "mops_today":"https://mopsov.twse.com.tw/mops/web/t05sr01_1",
}
out={}
for k,u in tests.items():
    try:
        r=requests.get(u,headers=UA,timeout=20); out[k]={"status":r.status_code,"len":len(r.text),"head":r.text[:1500]}
    except Exception as e: out[k]={"err":str(e)}
    time.sleep(1)
open("output/newstest.json","w",encoding="utf-8").write(json.dumps(out,ensure_ascii=False,indent=1))
