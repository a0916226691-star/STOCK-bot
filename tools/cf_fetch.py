# FinMind 現金流量表（累計）：營業現金流、稅前淨利、購置廠房設備 → output/cf_<part>.csv（續抓）
import requests,time,os,csv
U="https://api.finmindtrade.com/api/v4/data"
PART=int(os.environ["PART"]); NP=int(os.environ["NP"])
ids=[l.strip() for l in open("tools/inv_ids.txt") if l.strip()][PART::NP]
out=f"output/cf_{PART}.csv";os.makedirs("output",exist_ok=True)
done=set()
if os.path.exists(out):
    for r in csv.reader(open(out)): done.add(r[0])
w=open(out,"a",newline="");cw=csv.writer(w)
KEEP={"CashFlowsFromOperatingActivities","NetIncomeBeforeTax","PropertyAndPlantAndEquipment"}
t0=time.time();n=0
for sid in ids:
    if sid in done: continue
    if (time.time()-t0)/60>320: break
    while True:
        try:
            r=requests.get(U,params={"dataset":"TaiwanStockCashFlowsStatement","data_id":sid,"start_date":"2018-01-01"},timeout=90)
            if r.status_code==200: d=r.json().get("data",[]);break
            print(sid,r.status_code,flush=True)
        except Exception as e: print(sid,e,flush=True)
        time.sleep(300)
    rows=[(sid,x["date"],x["type"],x["value"]) for x in d if x["type"] in KEEP] or [(sid,"","none","")]
    cw.writerows(rows);w.flush();n+=1
    if n%50==0: print(n,round((time.time()-t0)/60),flush=True)
print("this run",n,"total",len(done)+n,"of",len(ids))
