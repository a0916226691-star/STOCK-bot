import requests,json
U="https://api.finmindtrade.com/api/v4/data"
for ds,extra in (("TaiwanStockBalanceSheet",{}),("TaiwanStockFinancialStatements",{}),("TaiwanStockHoldingSharesPer",{})):
    try:
        r=requests.get(U,params={"dataset":ds,"data_id":"2383","start_date":"2019-01-01",**extra},timeout=60)
        j=r.json();d=j.get("data",[])
        print(ds,r.status_code,j.get("msg"),len(d))
        if d:
            types=sorted({x.get("type") or x.get("HoldingSharesLevel") for x in d})
            print("  dates",d[0]["date"],d[-1]["date"]," types:",[t for t in types if t and any(k in str(t) for k in ("nventor","Cost","1,000,001","more","400,001","Gross","Revenue"))][:20])
    except Exception as e: print(ds,"ERR",e)
for u in ["https://www.tdcc.com.tw/portal/zh/smWeb/qryStock","https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"]:
    try: r=requests.get(u,timeout=30);print(u,r.status_code,len(r.text))
    except Exception as e: print(u,e)
