# -*- coding: utf-8 -*-
"""
輔.py v7.0 - 驗證層 / 績效追蹤儀表板
====================================
用途：讀 主.py 產生的 output/tw_radar.db
回答 Perplexity 說你最缺的：藍/綠/紫燈到底有沒有優勢？

功能：
1. 統計 🔵🔵 藍燈 / 🟢 綠燈 發出後 5/10/20日 勝率與平均報酬
2. 列出你 7 檔焦點股的歷史訊號績效
3. 支援 5日 與 20日 投信持續買超 雙窗口對比

此檔建議覆蓋 輔.py，獨立執行，不影響主.py
"""
import os
import sqlite3
import pandas as pd

DB_PATH = "output/tw_radar.db"

def check_db():
    if not os.path.exists(DB_PATH):
        print(f"找不到 {DB_PATH}，請先跑主.py v7.0 一次")
        return False
    return True

def performance_summary():
    if not check_db(): return
    conn=sqlite3.connect(DB_PATH)
    try:
        perf=pd.read_sql("SELECT * FROM performance", conn, dtype={"stock_id":str})
        sig=pd.read_sql("SELECT * FROM signals", conn, dtype={"stock_id":str})
    except Exception as e:
        print(f"讀取失敗，可能是舊DB，請刪掉重建：{e}")
        conn.close(); return
    conn.close()

    if perf.empty:
        print("performance 還是空的，代表還沒有足夠的未來價格去回填。")
        print("你需要讓主.py連續跑至少 20 個交易日，或手動多跑幾天歷史價格。")
        print(f"目前 signals 有 {len(sig)} 筆訊號")
        return

    print(f"\n=== 驗證層報告 (共 {len(perf)} 筆已回填績效) ===\n")
    for signal_name in ["🔵 主動資金疑似布局", "🟢 吸籌延續／初步確認"]:
        sub=perf[perf["signal"]==signal_name]
        if sub.empty: continue
        print(f"{signal_name} : {len(sub)} 檔")
        for col in ["ret_5","ret_10","ret_20"]:
            valid=sub[col].dropna()
            if valid.empty: continue
            win=(valid>0).mean()*100
            avg=valid.mean()
            print(f"  {col}: 勝率 {win:.1f}% | 平均 {avg:+.2f}% | 中位 {valid.median():+.2f}%")
        print("")

    # 焦點股
    FOCUS=["2383","2368","6197","3293","4763","1808","6919"]
    focus_perf=perf[perf["stock_id"].isin(FOCUS)]
    if not focus_perf.empty:
        print("\n=== ⭐ 7檔焦點股績效 ===")
        print(focus_perf.sort_values(["stock_id","signal_date"]).to_string(index=False))
    else:
        print("\n焦點股目前還沒有回填績效")

    # 最近藍燈的 20日績效排名
    print("\n=== 最近藍燈 20日績效 Top10 ===")
    recent=perf.sort_values("signal_date", ascending=False).head(50)
    print(recent.sort_values("ret_20", ascending=False).head(10)[["signal_date","stock_id","signal","ret_5","ret_10","ret_20"]].to_string(index=False))

if __name__=="__main__":
    performance_summary()
