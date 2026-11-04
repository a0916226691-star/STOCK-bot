# FinMind 續抓：MODE=fund → 合約負債、應收帳款（資產負債表）、營業費用、營收（損益表）→ output/fund.csv
#                MODE=margin → 每日融資（買進、賣出、現償、餘額）→ output/margin.csv
import requests,time,os,csv
U="https://api.finmindtrade.com/api/v4/data"
MODE=os.environ["MODE"]
ids=[l.strip() for l in open("tools/inv_ids.txt") if l.strip()]
out=f"output/{MODE}.csv";os.makedirs("output",exist_ok=True)
done=set()
if os.path.exists(out):
    for r in csv.reader(open(out)): done.add(r[0])
w=open(out,"a",newline="");cw=csv.writer(w)
t0=time.time();n=0;shown=set()
def get(ds,sid):
    while True:
        try:
            r=requests.get(U,params={"dataset":ds,"data_id":sid,"start_date":"2018-01-01"},timeout=90);j=r.json()
            if r.status_code==200: return j.get("data",[])
            print(sid,ds,r.status_code,j.get("msg"),flush=True)
        except Exception as e: print(sid,ds,e,flush=True)
        if (time.time()-t0)/60>320: return None
        time.sleep(300)
def keep_fund(ds,x):
    t=x["type"]
    if ds=="TaiwanStockBalanceSheet": return ("ontract" in t or "Receivable" in t) and "_per" not in t
    return t in ("OperatingExpenses","Revenue")
for sid in ids:
    if sid in done: continue
    if (time.time()-t0)/60>320: break
    rows=[];ok=True
    if MODE=="fund":
        for ds in ("TaiwanStockBalanceSheet","TaiwanStockFinancialStatements"):
            d=get(ds,sid)
            if d is None: ok=False;break
            if ds not in shown and d:
                print("types",ds,sorted({x["type"] for x in d if "ontract" in x["type"] or "Receiv" in x["type"] or "xpense" in x["type"]}),flush=True);shown.add(ds)
            rows+=[(sid,x["date"],x["type"],x["value"]) for x in d if keep_fund(ds,x)]
    else:
        d=get("TaiwanStockMarginPurchaseShortSale",sid)
        if d is None: ok=False
        else: rows=[(sid,x["date"],x["MarginPurchaseBuy"],x["MarginPurchaseSell"],x["MarginPurchaseCashRepayment"],x["MarginPurchaseTodayBalance"]) for x in d]
    if not ok: break
    if not rows: rows=[(sid,"","none")]
    cw.writerows(rows);w.flush();n+=1
    if n%50==0: print(n,len(done)+n,round((time.time()-t0)/60),flush=True)
print("this run",n,"total",len(done)+n,"of",len(ids))
