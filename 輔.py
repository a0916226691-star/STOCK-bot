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
COST_PCT = 0.585      # 來回交易成本 %：手續費 0.1425% × 2 ＋ 證交稅 0.3%（未計券商折扣）
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


def read_tracking(path):
    """讀主程式的追蹤紀錄（tracking 表）；舊資料庫沒有這張表就回傳空表。"""
    conn = sqlite3.connect(path)
    try:
        has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracking'").fetchone()
        if not has:
            return pd.DataFrame()
        return pd.read_sql("SELECT * FROM tracking", conn, dtype={"stock_id": str})
    finally:
        conn.close()


# ───────────────────────── 追蹤交易成績 ─────────────────────────
def trade_report(tr, prices, cost):
    """tracking 表 → 每筆「買進→賣出」的實際損益統計（扣掉來回交易成本 cost %）。"""
    if tr is None or tr.empty:
        return ""
    last = prices.sort_values("date").groupby("stock_id")["close"].last() if not prices.empty else pd.Series(dtype=float)
    ov, st, rs = [], [], []
    for ver, g in tr.groupby("version"):
        bought = g[g["entry_price"].notna()]
        closed = bought[bought["status"] == "CLOSED"].copy()
        hold = bought[bought["status"] == "HOLD"].copy()
        unreal = (hold["stock_id"].map(last) / hold["entry_price"] - 1) * 100 if len(hold) else pd.Series(dtype=float)
        ov.append({"版本": ver, "追蹤過": len(g), "買進": len(bought), "買進率%": round(len(bought) / len(g) * 100, 1),
                   "觀察中": int((g["status"] == "WATCH").sum()), "放棄": int((g["status"] == "DROPPED").sum()),
                   "持有中": len(hold), "持有中平均未實現%": round(unreal.mean(), 2) if len(unreal.dropna()) else "-",
                   "已賣出": len(closed)})
        if closed.empty:
            continue
        closed["net"] = (closed["exit_price"] / closed["entry_price"] - 1) * 100 - cost
        w, l = closed.loc[closed["net"] > 0, "net"], closed.loc[closed["net"] <= 0, "net"]
        pf = round(w.mean() / abs(l.mean()), 2) if len(w) and len(l) and l.mean() != 0 else "-"
        st.append({"版本": ver, "已賣出": len(closed), "勝率%": round(len(w) / len(closed) * 100, 1),
                   "平均損益%": round(closed["net"].mean(), 2), "中位%": round(closed["net"].median(), 2),
                   "平均獲利%": round(w.mean(), 2) if len(w) else "-", "平均虧損%": round(l.mean(), 2) if len(l) else "-",
                   "盈虧比": pf, "最大虧損%": round(closed["net"].min(), 2),
                   "平均持有天": round(closed["days_held"].astype(float).mean(), 1),
                   "備註": "樣本少" if len(closed) < MIN_N else ""})
        closed["原因"] = closed["exit_reason"].astype(str).str.split("（").str[0]    # 去掉括號裡的數字，同一種原因歸在一起
        for reason, gg in closed.groupby("原因"):
            rs.append({"版本": ver, "賣出原因": reason, "筆數": len(gg), "平均損益%": round(gg["net"].mean(), 2),
                       "勝率%": round((gg["net"] > 0).mean() * 100, 1)})
    out = [f"【追蹤交易成績：從買進到賣出的實際損益（已扣來回交易成本 {cost:g}%）】",
           pd.DataFrame(ov).to_string(index=False), ""]
    if st:
        out += [pd.DataFrame(st).to_string(index=False), "", "賣出原因分布：", pd.DataFrame(rs).to_string(index=False), ""]
    else:
        out += ["還沒有任何一筆走完「買進→賣出」，請之後再看。", ""]
    return "\n".join(out)


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


def build_report(ev, args, multi_db, track_text=""):
    lines = ["═══ 台股雷達成績單 ═══",
             f"買進價：{'訊號隔天收盤' if args.entry == 'next' else '訊號當天收盤'}｜"
             f"{'連續出現全部計入' if args.all else '同一檔同一訊號連續出現只算第一次'}",
             "命中率：買進/觀察類＝後來上漲的比例；賣出類＝後來下跌的比例",
             "超額%：扣掉同期全市場平均報酬後的結果（賣出類已轉成「跌得比大盤多」為正）；"
             "正數才代表比隨便買有優勢", ""]
    if track_text:
        lines += [track_text]
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


# ───────────────────────── A／B 兩套比較（看每天信裡實際推薦的股票，後來表現如何） ─────────────────────────
AB_LABELS = {"A候選": "A 候選｜快突破／剛突破的第一根（信裡最上面的名單）",
             "A買進": "A 買進｜A 套模擬買進（已突破、條件齊全）",
             "B可買": "B 可買｜精簡信的「可買」",
             "B觀察": "B 觀察｜精簡信的「觀察」"}


def ab_report(path, since=None, until=None, cost=COST_PCT, csv_path=None):
    """每天信裡推薦的股票（picks 表），從「第一次出現」那天持有到統計截止日，算報酬、勝率、超額、最大回檔。"""
    if not os.path.exists(path):
        return f"找不到資料庫：{path}"
    conn = sqlite3.connect(path)
    try:
        has = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='picks'").fetchone()
        if not has:
            return "資料庫裡還沒有 picks 表（新版主程式跑過一次才會建立）。"
        picks = pd.read_sql("SELECT * FROM picks", conn, dtype={"stock_id": str})
        prices = pd.read_sql("SELECT date, stock_id, close FROM prices", conn, dtype={"stock_id": str})
    finally:
        conn.close()
    if picks.empty:
        return "picks 表還是空的，等主程式多跑幾天。"
    if since:
        picks = picks[picks["date"] >= since]
    if until:
        picks = picks[picks["date"] <= until]
    px = prices.dropna(subset=["close"]).drop_duplicates(["date", "stock_id"], keep="last") \
               .pivot(index="date", columns="stock_id", values="close").sort_index()
    if picks.empty or px.empty:
        return "指定區間內沒有推薦紀錄。"
    last = min(until, px.index.max()) if until else px.index.max()
    first = picks["date"].min()
    days = [d for d in px.index if first <= d <= last]
    picks = picks.sort_values("date").drop_duplicates(["grp", "stock_id"], keep="first")   # 同一檔同一組只算第一次出現

    recs = []
    for _, p in picks.iterrows():
        sid = p["stock_id"]
        if sid not in px.columns:
            continue
        later = [d for d in px.index if p["date"] < d <= last]
        for mode in ("same", "next"):
            if mode == "same":
                e_date, e_px = p["date"], float(p["close"])
            else:
                if not later:
                    continue
                e_date = later[0]
                e_px = px.at[e_date, sid]
            end_px = px.at[last, sid] if last in px.index else np.nan
            if e_date >= last or pd.isna(e_px) or pd.isna(end_px) or e_px <= 0:
                continue
            seg = px.loc[e_date:last, sid].dropna()
            row_e, row_l = px.loc[e_date], px.loc[last]
            m = ((row_l / row_e - 1) * 100).replace([np.inf, -np.inf], np.nan).dropna()
            mkt = float(m.mean()) if len(m) else np.nan
            ret = (end_px / e_px - 1) * 100 - cost
            recs.append({"grp": p["grp"], "mode": mode, "stock_id": sid, "stock_name": p["stock_name"],
                         "first_date": p["date"], "entry_date": e_date, "entry": e_px, "end": float(end_px),
                         "ret_net": ret, "excess": ret - mkt if pd.notna(mkt) else np.nan,
                         "maxdd": (seg.min() / e_px - 1) * 100})
    out = [f"══════ 台股雷達 A／B 一週比較 ══════",
           f"推薦期間 {first}～{last}（共 {len(days)} 個交易日，統計到 {last} 收盤）",
           f"報酬 = 從第一次出現持有到 {last} 收盤，已扣來回成本 {cost}%；超額 = 報酬 − 同期全市場平均",
           "同一檔同一組只算第一次出現；訊號當天收盤進場＝最理想，隔天收盤進場＝較貼近你早上看信才買的情況", ""]
    if not recs:
        out.append("目前還沒有「出現之後又過了至少一個交易日」的推薦，無法計算。")
        return "\n".join(out)
    df = pd.DataFrame(recs)
    for mode, title in (("same", "【訊號當天收盤進場】"), ("next", "【訊號隔天收盤進場】")):
        sub = df[df["mode"] == mode]
        out.append(title)
        out.append(f"{'組別':<6}{'檔數':>4}{'平均%':>8}{'中位%':>8}{'勝率%':>7}{'超額%':>8}{'最差%':>8}{'平均回檔%':>10}  備註")
        for g in AB_LABELS:
            x = sub[sub["grp"] == g]
            if x.empty:
                out.append(f"{g:<6}{0:>4}  （沒有資料）")
                continue
            out.append(f"{g:<6}{len(x):>4}{x['ret_net'].mean():>8.2f}{x['ret_net'].median():>8.2f}"
                       f"{(x['ret_net'] > 0).mean() * 100:>7.0f}{x['excess'].mean():>8.2f}{x['ret_net'].min():>8.2f}"
                       f"{x['maxdd'].mean():>10.2f}  {'樣本少' if len(x) < MIN_N else ''}")
        out.append("")
    both = set(df[df["grp"].str.startswith("A")]["stock_id"]) & set(df[df["grp"].str.startswith("B")]["stock_id"])
    out.append(f"A、B 兩套都推薦過的股票：{len(both)} 檔")
    same = df[df["mode"] == "same"]
    for g in AB_LABELS:
        x = same[same["grp"] == g].sort_values("ret_net", ascending=False)
        if len(x):
            top = "、".join(f"{r.stock_name}{r.ret_net:+.1f}%" for r in x.head(3).itertuples())
            bot = "、".join(f"{r.stock_name}{r.ret_net:+.1f}%" for r in x.tail(3).iloc[::-1].itertuples())
            out.append(f"{g} 最好：{top}｜最差：{bot}")
    out += ["", "怎麼看：先看「超額%」與「勝率%」，再看「平均回檔%」（越接近 0 代表上車後越少被洗）。",
            f"樣本少於 {MIN_N} 檔只能當參考，不要只憑一週的結果就決定放棄哪一套。過去表現不代表未來。"]
    if csv_path:
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        out.append(f"已存 {csv_path}")
    return "\n".join(out)


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
    ap.add_argument("--ab", action="store_true", help="A／B 兩套比較（每天信裡實際推薦的股票後來表現如何）")
    ap.add_argument("--since", metavar="YYYY-MM-DD", help="--ab 的起始日（預設從第一筆推薦開始）")
    ap.add_argument("--until", metavar="YYYY-MM-DD", help="--ab 的截止日（預設到最新資料）")
    ap.add_argument("--cost", type=float, default=COST_PCT,
                    help=f"來回交易成本 %%（預設 {COST_PCT}：手續費買賣各0.1425%%＋證交稅0.3%%，有券商折扣可調低）")
    args = ap.parse_args(argv)
    args._csv_df = None

    if args.ab:
        text = ab_report((args.db or [DEFAULT_DB])[0].split("=", 1)[-1], args.since, args.until, args.cost, args.csv)
        print(text)
        if args.email:
            send_email("台股雷達 A／B 一週比較報告", text)
        return None

    specs = args.db or [DEFAULT_DB]
    frames, track_texts = [], []
    for spec in specs:
        name, path = spec.split("=", 1) if "=" in spec else ("", spec)
        prefix = (name or os.path.basename(os.path.dirname(os.path.abspath(path))) or "db") + ":"
        sig, prices = read_db(path)
        ev = build_events(sig, prices, 1 if args.entry == "next" else 0, dedup=not args.all)
        tr = read_tracking(path)
        if len(specs) > 1:
            ev["version"] = prefix + ev["version"].astype(str)
            if not tr.empty:
                tr["version"] = prefix + tr["version"].astype(str)
        frames.append(ev)
        track_texts.append(trade_report(tr, prices, args.cost))
    ev = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    report = build_report(ev, args, len(specs) > 1, "\n".join(t for t in track_texts if t))
    print(report)
    if args.csv and args._csv_df is not None:
        args._csv_df.to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"已存 {args.csv}")
    if args.email:
        send_email("台股雷達成績單", report)
    return ev


if __name__ == "__main__":
    main()
