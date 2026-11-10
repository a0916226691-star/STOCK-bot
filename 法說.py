"""持股法說會與財報收集（給 Claude 每晚分析「公司說的 vs 財報實際」用）。

流程：
1. 每晚持股檢查（長線.py --hold-watch 的 ir_watch）發現新的法說會、或影音上傳 → 寫進 ir/queue.json。
2. 這支程式（GitHub Actions：ir.yml，每晚跑）：
   - capture()：把 queue 裡的法說會簡報轉成文字、影音轉成逐字稿（faster-whisper），存到 ir/<代號>/。
   - fin_capture()：抓持股最新一季財報（FinMind：損益表、資產負債表、現金流量表），有新的一季就存檔並排進 queue。
3. Claude 的每晚排程讀 queue 裡「已收集、還沒分析」的項目，寫分析、寄信（mail.yml），再標成已分析。

YouTube 的影音（鈊象、潤隆）抓不到，只分析簡報；沒有影音的法說會也只有簡報。
"""
import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import requests

IR_DIR = "ir"
QUEUE = os.path.join(IR_DIR, "queue.json")
TW = timezone(timedelta(hours=8))
FINMIND = "https://api.finmindtrade.com/api/v4/data"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"}


def now_tw():
    return datetime.now(TW)


def load_queue():
    try:
        return json.load(open(QUEUE, encoding="utf-8"))
    except Exception:
        return []


def save_queue(q):
    os.makedirs(IR_DIR, exist_ok=True)
    json.dump(q, open(QUEUE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def enqueue(item):
    """加一筆（同一檔、同一天、同一種只留一筆；影音晚上傳就更新那一筆並重新收集）。"""
    q = load_queue()
    for x in q:
        if x["sid"] == item["sid"] and x["date"] == item["date"] and x["kind"] == item["kind"]:
            if item.get("video") and item["video"] != x.get("video"):
                x.update(item)
                x["status"] = "new"
                save_queue(q)
            return
    item.setdefault("status", "new")
    item["added"] = now_tw().strftime("%Y-%m-%d %H:%M")
    q.append(item)
    save_queue(q)


def capture_ir(x):
    """簡報 PDF → 文字；irconference 影音 → 逐字稿。"""
    if not x.get("pdf") and not x.get("video"):                  # 沒附連結：到公開資訊觀測站查這場的簡報、影音
        try:
            import 長線
            ir = 長線.ir_latest(x["sid"])
            if ir and ir["date"] == x["date"]:
                x.update({k: ir[k] for k in ("pdf", "video", "summary", "place")})
        except Exception as e:
            print(f"{x['sid']} 查法說會連結失敗：{e}")
    d = os.path.join(IR_DIR, x["sid"])
    os.makedirs(d, exist_ok=True)
    base = os.path.join(d, x["date"])
    files = []
    if x.get("pdf"):
        try:
            subprocess.run(["curl", "-sSL", "--retry", "5", "-A", UA["User-Agent"], "-o", "/tmp/ir.pdf", x["pdf"]], check=True)
            subprocess.run(["pdftotext", "-layout", "/tmp/ir.pdf", base + "_簡報.txt"], check=True)
            files.append(base + "_簡報.txt")
        except Exception as e:
            print(f"{x['sid']} 簡報抓不到：{e}")
    v = x.get("video") or ""
    if v and "youtu" not in v:
        try:
            for _ in range(15):                                  # 大檔常斷線：續傳
                r = subprocess.run(["curl", "-sSL", "-C", "-", "--retry", "5", "-o", "/tmp/ir.mp4", v])
                if r.returncode == 0:
                    break
                time.sleep(10)
            transcribe("/tmp/ir.mp4", base + "_逐字稿.txt")
            files.append(base + "_逐字稿.txt")
        except Exception as e:
            print(f"{x['sid']} 影音轉逐字稿失敗：{e}")
    elif v:
        x["note"] = "影音在 YouTube，抓不到，只有簡報"
    return files


def transcribe(src, out):
    import numpy as np
    from faster_whisper import WhisperModel
    p = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                       capture_output=True, check=True)
    audio = np.frombuffer(p.stdout, np.int16).astype(np.float32) / 32768.0
    m = WhisperModel("medium", device="cpu", compute_type="int8")
    segs, _ = m.transcribe(audio, language="zh", vad_filter=True, beam_size=1,
                           initial_prompt="以下是台灣上市櫃公司法人說明會的繁體中文逐字稿，包含營收、毛利率、AI伺服器、客戶、產能、展望等內容。")
    with open(out, "w", encoding="utf-8") as f:
        for s in segs:
            f.write(f"[{int(s.start // 60):02d}:{int(s.start % 60):02d}] {s.text.strip()}\n")


def finmind(ds, sid, start):
    for _ in range(3):
        try:
            r = requests.get(FINMIND, params={"dataset": ds, "data_id": sid, "start_date": start}, timeout=60)
            if r.status_code == 200:
                return r.json().get("data", [])
            print(f"{sid} {ds} {r.status_code}")
        except Exception as e:
            print(f"{sid} {ds} {e}")
        time.sleep(30)
    return []


def fin_capture(ids, names):
    """持股最新一季財報：有新的一季 → 存 ir/<代號>/財報_<季>.csv（近 9 季、損益＋資產負債＋現金流量的各項目），排進 queue。"""
    start = f"{now_tw().year - 3}-01-01"
    for sid in ids:
        rows = []
        for ds, tag in (("TaiwanStockFinancialStatements", "損益"), ("TaiwanStockBalanceSheet", "資產負債"),
                        ("TaiwanStockCashFlowsStatement", "現金流量")):
            rows += [(tag, x["date"], x["type"], x["value"]) for x in finmind(ds, sid, start)
                     if not str(x["type"]).endswith("_per")]
            time.sleep(2)
        if not rows:
            continue
        last = max(r[1] for r in rows)
        q = f"{last[:4]}Q{(int(last[5:7]) - 1) // 3 + 1}"
        path = os.path.join(IR_DIR, sid, f"財報_{q}.csv")
        if os.path.exists(path):
            continue
        dates = sorted({r[1] for r in rows})[-9:]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("報表,項目," + ",".join(dates) + "\n")
            keys = sorted({(r[0], r[2]) for r in rows})
            val = {(r[0], r[2], r[1]): r[3] for r in rows}
            for tag, t in keys:
                f.write(f"{tag},{t}," + ",".join(str(val.get((tag, t, d), "")) for d in dates) + "\n")
        print(f"{sid} 新的一季財報：{q} → {path}")
        enqueue({"sid": sid, "name": names.get(sid, sid), "date": last, "kind": "財報", "quarter": q,
                 "files": [path], "status": "captured"})


def capture():
    q = load_queue()
    for x in q:
        if x["kind"] == "法說" and x["status"] == "new":
            print(f"收集 {x['name']}({x['sid']}) {x['date']} 法說會")
            x["files"] = capture_ir(x)
            x["status"] = "captured" if x["files"] else "failed"
            save_queue(q)


def holdings():
    import 主 as R
    R.LONG_HOLDINGS.clear()
    R.load_holdings()
    return [s for s in R.LONG_HOLDINGS if not s.startswith("00")]


def names_of(ids):
    import sqlite3
    try:
        c = sqlite3.connect(os.path.join("output", "longterm.db"))
        return dict(c.execute("SELECT stock_id, name FROM revenue GROUP BY stock_id HAVING month = MAX(month)").fetchall())
    except Exception:
        return {}


def send(path):
    import 主 as R
    body = open(path, encoding="utf-8").read()
    first = body.strip().splitlines()[0].lstrip("# ").strip()
    R.send_email(first or "持股法說會／財報分析", body)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", action="store_true", help="收集法說會簡報、逐字稿和新的財報")
    ap.add_argument("--send", help="寄出一份分析（檔案路徑）")
    a = ap.parse_args()
    if a.send:
        send(a.send)
    elif a.capture:
        capture()
        ids = holdings()
        fin_capture(ids, names_of(ids))
