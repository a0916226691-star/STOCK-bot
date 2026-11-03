"""長期歷史資料（研究、回測用）：上市＋上櫃每日股價、法人買賣超，存在 output/history/，每個月一個壓縮檔。

主資料庫 tw_radar.db 只留最近 300 天（GitHub 單檔上限 100MB），更早的資料放這裡：
  output/history/prices_YYYY-MM.csv.gz   date, stock_id, stock_name, market, open, high, low, close, volume, turnover
  output/history/inst_YYYY-MM.csv.gz     date, stock_id, market, foreign_net, trust_net, dealer_total

用法：
  python 歷史.py --seed                 把資料庫裡現有的資料全部存進歷史檔（第一次用）
  python 歷史.py --years 2021 2022      回補指定年份（每個交易日抓上市、上櫃的股價和法人）
每天的主程式會自動把最近 10 天、以及要從資料庫刪掉的舊資料存進來（export_months）。
"""
import argparse
import os
import time
from datetime import date, timedelta

import pandas as pd

import importlib.util

_spec = importlib.util.spec_from_file_location("radar_main", os.path.join(os.path.dirname(os.path.abspath(__file__)), "主.py"))
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

HIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "history")
P_COLS = ["date", "stock_id", "stock_name", "market", "open", "high", "low", "close", "volume", "turnover"]
I_COLS = ["date", "stock_id", "market", "foreign_net", "trust_net", "dealer_total"]


def _path(kind, month):
    return os.path.join(HIST_DIR, f"{kind}_{month}.csv.gz")


def _merge_write(kind, month, df, cols):
    """把 df 併進這個月的檔案（同一天同一檔以新的為準）。內容沒變就不重寫，避免 git 每天多一份。"""
    os.makedirs(HIST_DIR, exist_ok=True)
    df = df[cols].copy()
    df["stock_id"] = df["stock_id"].astype(str)
    p = _path(kind, month)
    if os.path.exists(p):
        old = pd.read_csv(p, dtype={"stock_id": str})
        new = pd.concat([old, df]).drop_duplicates(["date", "stock_id"], keep="last")
        new = new.sort_values(["date", "stock_id"]).reset_index(drop=True)
        if len(new) == len(old) and new.equals(old.sort_values(["date", "stock_id"]).reset_index(drop=True)):
            return 0
    else:
        new = df.drop_duplicates(["date", "stock_id"], keep="last").sort_values(["date", "stock_id"])
    new.to_csv(p, index=False, compression={"method": "gzip", "mtime": 0})
    return len(new)


def export_months(months=None, only_missing=False):
    """把資料庫裡指定月份（None＝全部）的股價、法人存進歷史檔。only_missing：檔案已經有了就跳過。"""
    with R.db() as c:
        px = pd.read_sql("SELECT * FROM prices", c, dtype={"stock_id": str})
        ins = pd.read_sql("SELECT * FROM institutional", c, dtype={"stock_id": str})
    for col in P_COLS:
        if col not in px.columns:
            px[col] = None
    out = 0
    for kind, df, cols in (("prices", px, P_COLS), ("inst", ins, I_COLS)):
        if df.empty:
            continue
        df["m"] = df["date"].str[:7]
        for m, g in df.groupby("m"):
            if months is not None and m not in months:
                continue
            if only_missing and os.path.exists(_path(kind, m)):
                continue
            out += _merge_write(kind, m, g, cols) > 0
    return out


def daily_export():
    """主程式每天呼叫：最近 10 天的月份更新一次；資料庫最舊的月份如果還沒存過也存一次（之後會被刪掉）。"""
    recent = {(R.now_tw() - timedelta(days=k)).strftime("%Y-%m") for k in range(0, 11)}
    n = export_months(recent)
    n += export_months(None, only_missing=True)
    print(f"歷史資料：更新 {n} 個月份檔")


def _have(kind, month):
    p = _path(kind, month)
    if not os.path.exists(p):
        return set()
    return set(pd.read_csv(p, usecols=["date", "market"]).groupby(["date", "market"]).size().index)


def backfill_years(years, budget_min=270):
    """逐日回補：上市 MI_INDEX＋T86、上櫃行情＋三大法人。每抓完一個月存一次，時間到就停（下次接著補）。"""
    t0 = time.time()
    today = R.now_tw().date()
    for y in years:
        d = date(y, 1, 1)
        while d.year == y and d <= today:
            m = d.strftime("%Y-%m")
            hp, hi = _have("prices", m), _have("inst", m)
            P, I = [], []
            while d.strftime("%Y-%m") == m and d <= today:
                if d.weekday() < 5:
                    iso, ds = d.strftime("%Y-%m-%d"), d.strftime("%Y%m%d")
                    for mk, fq, fi in (("TWSE", R.fetch_twse_quotes, R.fetch_twse_inst),
                                       ("TPEx", R.fetch_tpex_quotes_hist, R.fetch_tpex_inst_hist)):
                        if (iso, mk) not in hp:
                            try:
                                P.append(fq(ds))
                            except Exception as e:
                                print(f"  {iso} {mk} 行情失敗：{e}")
                            time.sleep(2.5)
                        if (iso, mk) not in hi:
                            try:
                                I.append(fi(ds))
                            except Exception as e:
                                print(f"  {iso} {mk} 法人失敗：{e}")
                            time.sleep(2.5)
                d += timedelta(days=1)
            np_ = _merge_write("prices", m, pd.concat(P), P_COLS) if P and not pd.concat(P).empty else 0
            ni = _merge_write("inst", m, pd.concat(I), I_COLS) if I and not pd.concat(I).empty else 0
            print(f"{m}：股價 {np_} 筆、法人 {ni} 筆（{(time.time() - t0) / 60:.0f} 分鐘）", flush=True)
            if (time.time() - t0) / 60 > budget_min:
                print("時間到，先存檔，下次接著補")
                return


def main():
    ap = argparse.ArgumentParser(description="長期歷史資料")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--years", nargs="*", type=int, default=[])
    ap.add_argument("--budget", type=int, default=270)
    a = ap.parse_args()
    if a.seed:
        print(f"從資料庫存進歷史檔：{export_months(None)} 個檔")
    if a.years:
        backfill_years(a.years, a.budget)


if __name__ == "__main__":
    main()
