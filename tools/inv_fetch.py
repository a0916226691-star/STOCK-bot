# FinMind：存貨、營業成本、營收、毛利（季），存成 output/inv.csv（續抓）
import requests,time,os,csv,sys
U="https://api.finmindtrade.com/api/v4/data"
ids=[l.strip() for l in open("tools/inv_ids.txt") if l.strip()]
out="output/inv.csv";os.makedirs("output",exist_ok=True)
done=set()
if os.path.exists(out):
    for r in csv.reader(open(out)): done.add(r[0])
w=open(out,"a",newline="");cw=csv.writer(w)
t0=time.time();n=0
KEEP={"Inventories","CostOfGoodsSold","Revenue","GrossProfit"}
def get(ds,sid):
    while True:
        r=requests.get(U,params={"dataset":ds,"data_id":sid,"start_date":"2018-01-01"},timeout=60)
        j=r.json()
        if r.status_code==200: return j.get("data",[])
        print(sid,ds,r.status_code,j.get("msg"),flush=True)
        if (time.time()-t0)/60>320: return None
        time.sleep(300)
for sid in ids:
    if sid in done: continue
    if (time.time()-t0)/60>320: break
    rows=[]
    for ds in ("TaiwanStockBalanceSheet","TaiwanStockFinancialStatements"):
        d=get(ds,sid)
        if d is None: break
        rows+=[(sid,x["date"],x["type"],x["value"]) for x in d if x["type"] in KEEP]
    else:
        if not rows: rows=[(sid,"","none","")]
        cw.writerows(rows);w.flush();n+=1
        if n%25==0: print(n,len(done)+n,round((time.time()-t0)/60),flush=True)
print("this run",n,"total",len(done)+n,"of",len(ids))
