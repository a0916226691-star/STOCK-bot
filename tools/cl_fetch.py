# FinMind：資產負債表裡的合約負債（季），存成 output/cl.csv（續抓）
import requests,time,os,csv
U="https://api.finmindtrade.com/api/v4/data"
ids=[l.strip() for l in open("tools/inv_ids.txt") if l.strip()]
out="output/cl.csv";os.makedirs("output",exist_ok=True)
done=set()
if os.path.exists(out):
    for r in csv.reader(open(out)): done.add(r[0])
w=open(out,"a",newline="");cw=csv.writer(w)
t0=time.time();n=0;shown=False
def get(sid):
    while True:
        r=requests.get(U,params={"dataset":"TaiwanStockBalanceSheet","data_id":sid,"start_date":"2018-01-01"},timeout=60)
        j=r.json()
        if r.status_code==200: return j.get("data",[])
        print(sid,r.status_code,j.get("msg"),flush=True)
        if (time.time()-t0)/60>320: return None
        time.sleep(300)
for sid in ids:
    if sid in done: continue
    if (time.time()-t0)/60>320: break
    d=get(sid)
    if d is None: break
    if not shown and d:
        print("types:",sorted({x["type"] for x in d if "ontract" in x["type"] or "Liabilit" in x["type"]}),flush=True);shown=True
    rows=[(sid,x["date"],x["type"],x["value"]) for x in d if "ontract" in x["type"] and "per" not in x["type"]]
    if not rows: rows=[(sid,"","none","")]
    cw.writerows(rows);w.flush();n+=1
    if n%50==0: print(n,len(done)+n,round((time.time()-t0)/60),flush=True)
print("this run",n,"total",len(done)+n,"of",len(ids))
