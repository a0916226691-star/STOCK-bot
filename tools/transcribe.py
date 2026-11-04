import sys, subprocess, os
from faster_whisper import WhisperModel
sid, url = sys.argv[1], sys.argv[2]
os.makedirs("out", exist_ok=True)
if "youtu" in url:
    subprocess.run(["yt-dlp","-f","bestaudio","-x","--audio-format","mp3","-o","a.%(ext)s",url],check=True); src="a.mp3"
else:
    import time
    for k in range(15):
        if subprocess.run(["curl","-sSL","--retry","5","-C","-","-o","a.mp4",url]).returncode==0: break
        time.sleep(5)
    src="a.mp4"
import numpy as np
raw=subprocess.run(["ffmpeg","-loglevel","error","-i",src,"-ac","1","-ar","16000","-f","s16le","-"],check=True,capture_output=True).stdout
audio=np.frombuffer(raw,np.int16).astype(np.float32)/32768
print("audio min",len(audio)/16000/60,flush=True)
m = WhisperModel("medium", device="cpu", compute_type="int8")
segs, info = m.transcribe(audio, language="zh", vad_filter=True, beam_size=1,
    initial_prompt="以下是台灣上市櫃公司法人說明會的繁體中文逐字稿，包含營收、毛利率、AI伺服器、客戶、產能等內容。")
with open(f"out/{sid}.txt","w") as f:
    for s in segs:
        line=f"[{int(s.start//60):02d}:{int(s.start%60):02d}] {s.text.strip()}"
        print(line, flush=True); f.write(line+"\n")
