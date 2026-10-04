# -*- coding: utf-8 -*-
"""
鎖倉.py - 法人鎖倉雷達

要找的股票：法人（外資＋投信）一直買、很少賣，就算大盤恐慌也沒有跑，資金越堆越高、鎖在裡面不動。
（例子：台光電——原本不被看好，後來被需要、股價漲了，法人不但沒跑還加碼。）

怎麼量（最近 WINDOW 個交易日，預設 60）
  1. 買超日占比：法人合計淨買超的日子占幾成（越高越好）
  2. 大賣日：單日法人淨賣超占當天成交量 DUMP_VOL_PCT% 以上的天數（越少越好）
  3. 累積買超占成交量：這段期間法人淨買了多少，相對於同期總成交量的 %（越高代表越重倉）
  4. 恐慌日守住：全市場最弱的那幾天（平均跌幅最大），法人有沒有跟著賣（守住的比例越高越好）
  5. 還沒走：最近 5 天法人合計不是淨賣，而且股價還站在月線上
  6. 流動性：平均成交金額夠大，進出才不會卡住

退出（法人開始賣就走）：連續 3 天法人淨賣超／近 5 日合計淨賣超／單日大賣／保底停損

用法
    python 鎖倉.py              # 算榜單、更新追蹤、寄信（前提：主.py 已經先收好資料）
    python 鎖倉.py --no-email   # 不寄信
注意：這是用「收盤後的資料」做的模擬記錄，不會真的下單；過去的行為不代表未來。
"""
import argparse
import importlib
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) or ".")
R = importlib.import_module("主")          # 重用主程式的資料收集、資料庫與寄信

# ── 參數（想調鬆或調嚴，改這裡）──────────────────────────────
WINDOW = int(os.getenv("LOCK_WINDOW", "60"))          # 觀察期（交易日）
MIN_DAYS = int(os.getenv("LOCK_MIN_DAYS", "30"))      # 至少要有幾天法人資料才評分
MIN_BUY_RATIO = 0.60        # 買超日占比下限
MAX_DUMP_DAYS = 1           # 大賣日最多幾天
DUMP_VOL_PCT = 5.0          # 單日淨賣超占當天成交量 ≥ 此 % ＝ 大賣日
MIN_CUM_PCT = 3.0           # 累積買超占成交量下限（%）
MIN_PANIC_HOLD = 0.70       # 恐慌日守住比例下限（有足夠的恐慌日才檢查）
WEAK_DAY_RATIO = 0.15       # 最弱的 15% 交易日當作「恐慌日」（至少 3 天）
WEAK_DAY_MAX_RET = -0.5     # 而且當天全市場平均要跌超過 0.5% 才算
HELD_TOLERANCE = 0.02       # 恐慌日法人賣超不超過成交量 2%，仍算「守住」
MIN_TURNOVER = R.MIN_DAILY_TURNOVER    # 平均成交金額下限（元）
TOP_N = int(os.getenv("LOCK_TOP_N", "30"))            # 信裡最多列幾檔
# 退出
EXIT_SELL_STREAK = 3        # 連續幾天法人淨賣超就走
SAFETY_STOP_PCT = 10.0      # 保底停損：比進榜價跌超過此 % 就走
COOLDOWN_DAYS = 10          # 出榜後幾個日曆天內不重複進榜

LOCK_COLS = ["stock_id", "stock_name", "first_in", "entry_price", "status", "last_date",
             "out_date", "out_price", "out_reason"]


# ───────────────────────── 特徵 ─────────────────────────
def load_data(window):
    cal = int(window * 1.8) + 15
    inst = R.load_table("institutional", cal)
    px = R.load_table("prices", cal)
    return inst, px


def build_features(inst, px, window=WINDOW):
    """回傳 (每檔股票的特徵表, 資料日, 恐慌日清單, 窗口日期清單)。"""
    if inst is None or inst.empty or px is None or px.empty:
        return pd.DataFrame(), None, [], []
    px = px.copy()
    for c in ("close", "volume", "turnover"):
        px[c] = pd.to_numeric(px[c], errors="coerce")
    px = px.dropna(subset=["close"]).drop_duplicates(["date", "stock_id"], keep="last")
    close = px.pivot(index="date", columns="stock_id", values="close").sort_index()
    dates = list(close.index)
    data_date = dates[-1]
    win_dates = dates[-window:]

    # 全市場每天的平均漲跌（%）→ 找出最弱的幾天
    mkt = (close.pct_change().clip(-0.1, 0.1).mean(axis=1) * 100).reindex(win_dates)
    n_weak = max(3, int(round(len(win_dates) * WEAK_DAY_RATIO)))
    weak_dates = [d for d in mkt.sort_values().index[:n_weak] if mkt[d] <= WEAK_DAY_MAX_RET]

    it = inst.copy()
    for c in ("foreign_net", "trust_net"):
        it[c] = pd.to_numeric(it[c], errors="coerce").fillna(0)
    it["net"] = it["foreign_net"] + it["trust_net"]
    it = it.groupby(["date", "stock_id"], as_index=False)["net"].sum()
    d = it.merge(px[["date", "stock_id", "stock_name", "volume", "turnover"]], on=["date", "stock_id"], how="inner")
    d = d[d["date"].isin(win_dates) & (d["volume"] > 0)].sort_values(["stock_id", "date"]).copy()
    if d.empty:
        return pd.DataFrame(), data_date, weak_dates, win_dates

    d["buy"] = d["net"] > 0
    d["dump"] = (d["net"] < 0) & (-d["net"] >= DUMP_VOL_PCT / 100 * d["volume"])
    d["weak"] = d["date"].isin(weak_dates)
    d["held_weak"] = d["weak"] & (d["net"] >= -HELD_TOLERANCE * d["volume"])
    g = d.groupby("stock_id")
    f = pd.DataFrame({
        "stock_name": g["stock_name"].last(),
        "n_days": g.size(),
        "buy_days": g["buy"].sum(),
        "dump_days": g["dump"].sum(),
        "cum_net": g["net"].sum(),
        "cum_vol": g["volume"].sum(),
        "weak_days": g["weak"].sum(),
        "held_weak": g["held_weak"].sum(),
        "avg_turnover": g["turnover"].mean(),
        "last_date": g["date"].max(),
    })
    last5 = g.tail(5).groupby("stock_id")["net"].sum()
    last3 = g.tail(EXIT_SELL_STREAK).groupby("stock_id")["net"].apply(lambda s: bool(len(s) == EXIT_SELL_STREAK and (s < 0).all()))
    today = d[d["date"] == data_date].set_index("stock_id")
    f["net5"] = last5
    f["sell_streak"] = last3.reindex(f.index).fillna(False)
    f["dump_today"] = today["dump"].reindex(f.index).fillna(False).astype(bool)
    f["today_sell_pct"] = (-today["net"] / today["volume"] * 100).reindex(f.index)
    f["buy_ratio"] = f["buy_days"] / f["n_days"]
    f["cum_pct"] = f["cum_net"] / f["cum_vol"] * 100
    f["panic_hold"] = (f["held_weak"] / f["weak_days"]).where(f["weak_days"] >= 3)

    last_close = close.iloc[-1]
    f["close"] = last_close.reindex(f.index)
    f["ma20"] = close.tail(20).mean().reindex(f.index) if len(close) >= 20 else np.nan
    f["ret20"] = ((close.iloc[-1] / close.iloc[-21] - 1) * 100).reindex(f.index) if len(close) > 20 else np.nan
    f = f[f["last_date"] == data_date]                  # 今天沒資料的（停牌、下市）不評分
    return f, data_date, weak_dates, win_dates


def criteria(f):
    """每檔是否達到「鎖倉」標準；回傳 (是否通過, 沒通過的原因)。"""
    why = []
    if f["n_days"] < MIN_DAYS:
        why.append(f"法人資料只有 {int(f['n_days'])} 天（至少 {MIN_DAYS}）")
    if f["buy_ratio"] < MIN_BUY_RATIO:
        why.append(f"買超日只占 {f['buy_ratio'] * 100:.0f}%")
    if f["dump_days"] > MAX_DUMP_DAYS:
        why.append(f"大賣日 {int(f['dump_days'])} 天")
    if f["cum_pct"] < MIN_CUM_PCT:
        why.append(f"累積買超只占成交量 {f['cum_pct']:.1f}%")
    if pd.notna(f["panic_hold"]) and f["panic_hold"] < MIN_PANIC_HOLD:
        why.append(f"恐慌日只守住 {f['panic_hold'] * 100:.0f}%")
    if f["net5"] < 0:
        why.append("近 5 日法人淨賣超")
    if pd.notna(f["ma20"]) and f["close"] < f["ma20"]:
        why.append("股價跌破月線")
    if f["avg_turnover"] < MIN_TURNOVER:
        why.append("成交金額太小")
    return not why, why


def score_of(f):
    ph = f["panic_hold"] if pd.notna(f["panic_hold"]) else 0.5
    return round(35 * min(f["buy_ratio"] / 0.8, 1) + 30 * min(max(f["cum_pct"], 0) / 10, 1)
                 + 20 * ph + 15 * (1 - min(f["dump_days"] / 3, 1)), 1)


def exit_reason(f, entry_price=None):
    """法人開始賣就走：回傳退出原因，沒有則 None。"""
    if entry_price and f["close"] <= entry_price * (1 - SAFETY_STOP_PCT / 100):
        return f"跌破保底停損 {SAFETY_STOP_PCT:g}%"
    if bool(f["dump_today"]):
        return f"今天法人大賣（占成交量 {f['today_sell_pct']:.0f}%）"
    if bool(f["sell_streak"]):
        return f"法人連 {EXIT_SELL_STREAK} 天淨賣超"
    if f["net5"] < 0:
        return "近 5 日法人合計淨賣超"
    return None


# ───────────────────────── 追蹤（進榜 → 在榜 → 出榜） ─────────────────────────
def init_tables():
    with R.db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS lock_track (stock_id TEXT, stock_name TEXT, first_in TEXT, "
                     "entry_price REAL, status TEXT, last_date TEXT, out_date TEXT, out_price REAL, "
                     "out_reason TEXT, PRIMARY KEY(stock_id, first_in))")


def update_track(feats, passed, data_date):
    """每天更新一次；同一天重跑不會重複計算。回傳 (紀錄, 事件)。"""
    with R.db() as conn:
        tr = pd.read_sql("SELECT * FROM lock_track", conn, dtype={"stock_id": str})
    tr = tr.astype(object).where(tr.notna(), None) if not tr.empty else tr
    recs = tr.to_dict("records") if not tr.empty else []
    ev = {"new": [], "out": []}
    open_ids = set()
    for t in recs:
        if t["status"] != "IN":
            continue
        sid = t["stock_id"]
        open_ids.add(sid)
        if str(t.get("last_date") or "") >= data_date or sid not in feats.index:
            continue
        f = feats.loc[sid]
        why = exit_reason(f, t["entry_price"])
        if why:
            t.update(status="OUT", out_date=data_date, out_price=float(f["close"]), out_reason=why)
            ev["out"].append(t)
        t["last_date"] = data_date
    today = datetime.strptime(data_date, "%Y-%m-%d")
    cooling = {t["stock_id"] for t in recs if t.get("out_date")
               and (today - datetime.strptime(str(t["out_date"]), "%Y-%m-%d")).days < COOLDOWN_DAYS}
    for sid in passed:
        if sid in open_ids or sid in cooling or exit_reason(feats.loc[sid]):
            continue
        f = feats.loc[sid]
        t = {"stock_id": sid, "stock_name": f["stock_name"], "first_in": data_date, "entry_price": float(f["close"]),
             "status": "IN", "last_date": data_date, "out_date": None, "out_price": None, "out_reason": None}
        recs.append(t)
        ev["new"].append(t)
    if recs:
        R.upsert("lock_track", pd.DataFrame(recs, columns=LOCK_COLS), LOCK_COLS)
    return recs, ev


# ───────────────────────── 信件 ─────────────────────────
def fmt_px(x):
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def zhang(shares):
    return f"{int(round(float(shares) / 1000)):+,d}"


def stock_text(sid, f, t=None, data_date=None):
    s = (f"{f['stock_name']}({sid})｜收盤 {fmt_px(f['close'])}")
    if t and t.get("entry_price"):
        days = (datetime.strptime(data_date, "%Y-%m-%d") - datetime.strptime(t["first_in"], "%Y-%m-%d")).days
        s += f"｜進榜價 {fmt_px(t['entry_price'])}（{(f['close'] / t['entry_price'] - 1) * 100:+.1f}%，進榜 {days} 天）"
    ph = f"守住 {int(f['held_weak'])}/{int(f['weak_days'])}" if pd.notna(f["panic_hold"]) else "期間沒有恐慌日"
    return (f"{s}\n   鎖倉分數 {score_of(f):g}｜買超日 {int(f['buy_days'])}/{int(f['n_days'])}天（{f['buy_ratio'] * 100:.0f}%）"
            f"｜累積買超占成交量 {f['cum_pct']:.1f}%\n   大賣日 {int(f['dump_days'])} 天｜恐慌日{ph}"
            f"｜近5日法人 {zhang(f['net5'])}張")


def holdings_text(feats, passed_set, holdings):
    if not holdings:
        return ""
    out = []
    for sid, cost in holdings.items():
        if sid not in feats.index:
            out.append(f"❔ {sid}｜今天沒有資料")
            continue
        f = feats.loc[sid]
        why = exit_reason(f, cost)
        pnl = f"｜買進 {fmt_px(cost)}（{(f['close'] / cost - 1) * 100:+.1f}%）" if cost else ""
        if why:
            tag = f"🔴 法人在走：{why}"
        elif sid in passed_set:
            tag = "🟢 鎖倉中"
        else:
            ok, miss = criteria(f)
            tag = "🟡 法人沒在賣，但鎖倉條件沒達標：" + "、".join(miss[:3])
        out.append(f"{tag}｜{f['stock_name']}({sid})｜收盤 {fmt_px(f['close'])}{pnl}\n   近5日法人 {zhang(f['net5'])}張｜累積買超占成交量 {f['cum_pct']:.1f}%")
    return "\n".join(out)


LEGEND = """【怎麼看】
鎖倉＝法人（外資＋投信）一直買、很少賣，連大盤恐慌時也沒跑，資金越堆越高。
買超日：最近 """ + f"{WINDOW}" + """ 個交易日裡，法人合計淨買超的日子。
累積買超占成交量：這段期間法人淨買的股數，占同期總成交量的 %，越高代表越重倉。
大賣日：單日法人淨賣超占當天成交量 """ + f"{DUMP_VOL_PCT:g}" + """% 以上。
恐慌日：全市場平均跌最兇的那幾天；「守住」＝那天法人沒有賣（或只賣很少）。
進榜條件：買超日≥""" + f"{MIN_BUY_RATIO * 100:.0f}" + """%、大賣日≤""" + f"{MAX_DUMP_DAYS}" + """天、累積買超占成交量≥""" + f"{MIN_CUM_PCT:g}" + """%、恐慌日守住≥""" + f"{MIN_PANIC_HOLD * 100:.0f}" + """%、近5日沒有淨賣超、股價在月線上。
出榜（該走）：法人連""" + f"{EXIT_SELL_STREAK}" + """天淨賣超／近5日合計淨賣超／單日大賣／比進榜價跌超過""" + f"{SAFETY_STOP_PCT:g}" + """%。
這是收盤後資料做的模擬記錄，不會真的下單；過去的行為不代表未來，法人也可能突然翻掉。"""


def build_email(data_date, feats, scores, passed, recs, ev, holdings, weak_dates, n_days_win):
    line = "━━━━━━━━━━━━"
    by_id = {t["stock_id"]: t for t in recs if t["status"] == "IN"}
    new_ids = {t["stock_id"] for t in ev["new"]}
    stay = [sid for sid in scores.index if sid in by_id and sid not in new_ids][:TOP_N]
    parts = [f"法人鎖倉雷達｜資料日 {data_date}",
             f"觀察期 {n_days_win} 個交易日｜期間恐慌日 {len(weak_dates)} 天｜目前達標 {len(passed)} 檔｜追蹤中 {len(by_id)} 檔", ""]
    if holdings:
        parts += [line, "💼 我持有的股票（鎖倉狀態）", holdings_text(feats, set(passed), holdings), ""]
    parts += [line, f"🆕 今天新進榜 {len(ev['new'])} 檔（法人鎖倉成形）"]
    parts += [stock_text(t["stock_id"], feats.loc[t["stock_id"]], t, data_date) for t in ev["new"][:TOP_N]] or ["（今天沒有）"]
    parts += ["", line, f"🔒 持續在榜 {len(stay)} 檔（法人還鎖著，依鎖倉分數排序）"]
    parts += [stock_text(sid, feats.loc[sid], by_id[sid], data_date) for sid in stay] or ["（沒有）"]
    parts += ["", line, f"🚪 今天出榜 {len(ev['out'])} 檔（法人開始走）"]
    parts += [f"{t['stock_name']}({t['stock_id']})｜進榜價 {fmt_px(t['entry_price'])} → 現價 {fmt_px(t['out_price'])}"
              f"（{(t['out_price'] / t['entry_price'] - 1) * 100:+.1f}%）｜原因：{t['out_reason']}" for t in ev["out"]] or ["（今天沒有）"]
    parts += ["", line, LEGEND]
    return "\n".join(parts) + "\n"


# ───────────────────────── 主流程 ─────────────────────────
def run(send_mail=True):
    init_tables()
    inst, px = load_data(WINDOW)
    feats, data_date, weak_dates, win_dates = build_features(inst, px, WINDOW)
    if feats.empty:
        print("沒有可用的法人／價格資料，結束")
        raise SystemExit(1)
    ok = {}
    for sid, f in feats.iterrows():
        ok[sid] = criteria(f)[0]
    passed_ids = [sid for sid, v in ok.items() if v]
    scores = pd.Series({sid: score_of(feats.loc[sid]) for sid in passed_ids}, dtype=float).sort_values(ascending=False)
    passed = list(scores.index)
    print(f"資料日 {data_date}｜觀察 {len(win_dates)} 個交易日｜恐慌日 {len(weak_dates)} 天 {weak_dates}")
    print(f"有評分的股票 {len(feats)} 檔（法人資料≥{MIN_DAYS}天的 {int((feats['n_days'] >= MIN_DAYS).sum())} 檔）｜達標 {len(passed)} 檔")
    if (feats["n_days"] >= MIN_DAYS).sum() == 0:
        print(f"⚠ 沒有任何股票的法人資料達到 {MIN_DAYS} 天，請先回補歷史資料（--backfill）")
    recs, ev = update_track(feats, passed, data_date)
    print(f"追蹤：今天新進榜 {len(ev['new'])}｜出榜 {len(ev['out'])}｜在榜 {sum(1 for t in recs if t['status'] == 'IN')}")
    try:
        out = feats.assign(passed=pd.Series(ok), score=scores).sort_values("score", ascending=False)
        out.to_csv(os.path.join(R.OUTPUT_DIR, f"lock_{data_date}.csv"), encoding="utf-8-sig")
    except OSError as e:
        print(f"寫 CSV 失敗：{e}")
    holdings = R.load_holdings()
    body = build_email(data_date, feats, scores, passed, recs, ev, holdings, weak_dates, len(win_dates))
    print("\n===== 信件內容 =====\n" + body)
    if send_mail:
        n_in = sum(1 for t in recs if t["status"] == "IN")
        subject = f"🔒鎖倉雷達 {int(data_date[5:7])}/{data_date[8:]}｜新進{len(ev['new'])} 在榜{n_in} 出榜{len(ev['out'])}"
        R.send_email(subject, body)


def main():
    ap = argparse.ArgumentParser(description="法人鎖倉雷達")
    ap.add_argument("--no-email", action="store_true", help="不寄信")
    args = ap.parse_args()
    run(send_mail=not args.no_email)


if __name__ == "__main__":
    main()
