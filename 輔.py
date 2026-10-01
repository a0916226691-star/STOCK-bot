# -*- coding: utf-8 -*-
"""
evaluate.py - 台股雷達成績單（取代 輔.py）

回答一個問題：每一種訊號發出之後，5／10／20 個交易日實際表現如何？不同版本誰比較好？

做法
  - 直接用資料庫裡的「訊號紀錄」＋「歷史收盤價」計算，不需要 performance 表，也不會動到資料庫。
  - 買進類（🔵🟣）：看後來有沒有漲；賣出類（🔴）：看後來有沒有跌。🟡⚪ 歸在「觀察/等待」。
  - 每筆都扣掉「同一段期間全市場平均漲跌」，才看得出是選股有效，還是剛好遇到多頭。
  - 同一檔股票連續好幾天出現同一個訊號，只算第一次（避免同一件事被重複計算、灌高樣本數）。
  - 版本來源：signal_log 表（主程式用環境變數 RADAR_VERSION 標記版本）；
    沒有 signal_log 時退回讀舊的 signals 表，版本記為「舊紀錄」。

用法
    python evaluate.py                                  # 讀 output/tw_radar.db
    python evaluate.py --db 主=output/tw_radar.db --db 輔=output_aux/tw_radar.db
    python evaluate.py --detail                         # 另外列出每一種訊號的成績
    python evaluate.py --entry next                     # 以「訊號隔天收盤」當買進價（較貼近實際，預設是當天收盤）
    python evaluate.py --all                            # 連續出現也全部計入（不去重）
    python evaluate.py --csv result.csv --email         # 存 CSV、寄信（用 GMAIL_USER / GMAIL_APP_PASSWORD / RECIPIENT_EMAIL）

注意：過去表現不代表未來；樣本少於 30 筆的數字只能當參考，不要據此調整規則。
"""
import argparse
import os
import smtplib
import sqlite3
from email.mime.text import MIMEText

import numpy as np
import pandas as pd

HORIZONS = (5, 10, 20)
MIN_N = 30            # 樣本少於此數就標註「樣本少」
DEDUP_GAP = 5         # 同一檔同一訊號，間隔超過幾個交易日才算「新的一次」
DEFAULT_DB = os.path.join(os.getenv("RADAR_OUTPUT_DIR", "output"), "tw_radar.db")
BUY, SELL, WATCH = "買進", "賣出/避開", "觀察/等待"


def group_of(label):
    """訊號名稱 → 類別；排除與不列入回傳 None（不計成績）。"""
    label = str(label)
    if "不列入" in label or "排除" in label:
        return None
    if label.startswith("🔴"):
        return SELL
    if label.startswith(("🔵", "🟣")):
        return BUY
    return WATCH


# ───────────────────────── 讀資料 ─────────────────────────
def read_db(path):
    if not os.path.exists(path):
        raise SystemExit(f"找不到資料庫：{path}")
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "prices" not in tables:
            raise SystemExit(f"{path} 裡沒有 prices 表，請先跑過主程式")
        prices = pd.read_sql("SELECT date, stock_id, close FROM prices", conn, dtype={"stock_id": str})
        parts = []
        if "signal_log" in tables:
            parts.append(pd.read_sql("SELECT signal_date, stock_id, version, signal FROM signal_log",
                                     conn, dtype={"stock_id": str}))
        if "signals" in tables:
            old = pd.read_sql("SELECT signal_date, stock_id, signal FROM signals", conn, dtype={"stock_id": str})
            if parts and not parts[0].empty:      # 只補 signal_log 開始之前的舊紀錄，避免重複
                old = old[old["signal_date"] < parts[0]["signal_date"].min()]
            old["version"] = "舊紀錄"
            parts.append(old)
    finally:
        conn.close()
    sig = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["signal_date", "stock_id", "version", "signal"])
    return sig, prices


# ───────────────────────── 算報酬 ─────────────────────────
def build_events(sig, prices, entry_offset, dedup):
    """每筆訊號配上 5/10/20 日報酬(%) 與同期全市場平均報酬(%)。"""
    cols = ["version", "signal", "group", "signal_date", "stock_id"] + \
           [f"{p}_{n}" for n in HORIZONS for p in ("ret", "mkt")]
    if sig.empty or prices.empty:
        return pd.DataFrame(columns=cols)

    px = prices.pivot_table(index="date", columns="stock_id", values="close", aggfunc="last").sort_index()
    cnt = px.notna().sum(axis=1)
    px = px[cnt >= 0.5 * cnt.median()]            # 剔除只抓到一部分股票的殘缺日，避免交易日算錯
    cal = px.index

    ev = sig.copy()
    ev["group"] = ev["signal"].map(group_of)
    ev = ev[ev["group"].notna() & ev["signal_date"].isin(cal)].copy()
    if ev.empty:
        return pd.DataFrame(columns=cols)
    ev["pos"] = cal.get_indexer(ev["signal_date"])

    if dedup:
        ev = ev.sort_values(["version", "signal", "stock_id", "pos"])
        prev = ev.groupby(["version", "signal", "stock_id"])["pos"].shift(1)
        ev = ev[prev.isna() | (ev["pos"] - prev > DEDUP_GAP)]

    col_idx = px.columns.get_indexer(ev["stock_id"])
    ok = col_idx >= 0
    for n in HORIZONS:
        r = px.shift(-(entry_offset + n)) / px.shift(-entry_offset) - 1
        arr = np.full(len(ev), np.nan)
        arr[ok] = r.to_numpy()[ev["pos"].to_numpy()[ok], col_idx[ok]]
        ev[f"ret_{n}"] = arr * 100
        ev[f"mkt_{n}"] = r.mean(axis=1).to_numpy()[ev["pos"].to_numpy()] * 100
    return ev[cols].reset_index(drop=True)


# ───────────────────────── 統計 ─────────────────────────
def summarize(ev, by):
    rows = []
    for keys, g in ev.groupby(by, sort=True):
        keys = keys if isinstance(keys, tuple) else (keys,)
        label = dict(zip(by, keys))
        sign = -1 if label["group"] == SELL else 1       # 賣出類：跌才算對
        for n in HORIZONS:
            r = g[f"ret_{n}"].dropna()
            if r.empty:
                continue
            excess = (r - g.loc[r.index, f"mkt_{n}"]) * sign
            hit = (r > 0).mean() if sign == 1 else (r < 0).mean()
            rows.append({**label, "持有": f"{n}日", "樣本": len(r),
                         "命中率%": round(hit * 100, 1), "平均%": round(r.mean(), 2),
                         "中位%": round(r.median(), 2), "最差10%": round(r.quantile(0.1), 2),
                         "超額%": round(excess.mean(), 2),
                         "備註": "樣本少" if len(r) < MIN_N else ""})
    return pd.DataFrame(rows)


def build_report(ev, args, multi_db):
    lines = ["═══ 台股雷達成績單 ═══",
             f"買進價：{'訊號隔天收盤' if args.entry == 'next' else '訊號當天收盤'}｜"
             f"{'連續出現全部計入' if args.all else '同一檔同一訊號連續出現只算第一次'}",
             "命中率：買進/觀察類＝後來上漲的比例；賣出類＝後來下跌的比例",
             "超額%：扣掉同期全市場平均報酬後的結果（賣出類已轉成「跌得比大盤多」為正）；"
             "正數才代表比隨便買有優勢", ""]
    if ev.empty:
        lines.append("目前沒有可以計算的訊號。可能原因：還沒有訊號紀錄，或訊號日期在價格資料之外。")
        return "\n".join(lines)

    ev = ev.copy()
    ev["version"] = ev["version"].astype(str)
    tot = len(ev)
    mature = " / ".join(f"{n}日 {int(ev[f'ret_{n}'].notna().sum())}" for n in HORIZONS)
    first, last = ev["signal_date"].min(), ev["signal_date"].max()
    lines += [f"訊號期間 {first} ～ {last}｜共 {tot} 筆（已滿期可計算：{mature}）", ""]
    if ev["ret_5"].notna().sum() == 0:
        lines.append("還沒有任何訊號滿 5 個交易日，請之後再看。")
        return "\n".join(lines)

    summ = summarize(ev, ["version", "group"])
    summ = summ.rename(columns={"version": "版本", "group": "類別"})
    lines += ["【各版本 × 類別】", summ.to_string(index=False), ""]
    if args.detail:
        det = summarize(ev, ["version", "group", "signal"]).rename(
            columns={"version": "版本", "group": "類別", "signal": "訊號"})
        lines += ["【各訊號明細】", det.to_string(index=False), ""]
    lines.append(f"（樣本少於 {MIN_N} 筆者標註「樣本少」，只能參考；過去表現不代表未來。）")
    report_df = summarize(ev, ["version", "group", "signal"])
    args._csv_df = report_df
    return "\n".join(lines)


def send_email(subject, body):
    user, pwd, to = (os.getenv(k) for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "RECIPIENT_EMAIL"))
    if not (user and pwd and to):
        print("未設定 Gmail 環境變數，略過寄信")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pwd)
            s.send_message(msg)
        print("成績單已寄出")
    except Exception as e:
        print(f"寄信失敗：{e}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="台股雷達成績單")
    ap.add_argument("--db", action="append", metavar="[名稱=]路徑",
                    help=f"資料庫，可重複指定來比較不同資料庫（預設 {DEFAULT_DB}）")
    ap.add_argument("--entry", choices=["same", "next"], default="same", help="買進價：訊號當天收盤(same)或隔天收盤(next)")
    ap.add_argument("--all", action="store_true", help="不去重，連續出現的訊號全部計入")
    ap.add_argument("--detail", action="store_true", help="另外列出每一種訊號的明細")
    ap.add_argument("--csv", metavar="檔名", help="把明細存成 CSV")
    ap.add_argument("--email", action="store_true", help="把成績單寄到信箱")
    args = ap.parse_args(argv)
    args._csv_df = None

    specs = args.db or [DEFAULT_DB]
    frames = []
    for spec in specs:
        name, path = spec.split("=", 1) if "=" in spec else ("", spec)
        sig, prices = read_db(path)
        ev = build_events(sig, prices, 1 if args.entry == "next" else 0, dedup=not args.all)
        if len(specs) > 1:
            ev["version"] = (name or os.path.basename(os.path.dirname(os.path.abspath(path))) or "db") + ":" + ev["version"].astype(str)
        frames.append(ev)
    ev = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    report = build_report(ev, args, len(specs) > 1)
    print(report)
    if args.csv and args._csv_df is not None:
        args._csv_df.to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"已存 {args.csv}")
    if args.email:
        send_email("台股雷達成績單", report)
    return ev


if __name__ == "__main__":
    main()
