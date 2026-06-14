#!/usr/bin/env python3
"""Vergelijk 3 dongles tegelijk (poort 1234/1235/1236), log naar CSV.
Elke ~2s: per dongle ruisvloer (mediaan) en piek boven ruis (DC uitgesloten)."""
import socket, struct, time, numpy as np, threading

FFT=1024; SR=3_200_000; CENTER=392_500_000
PORTS = {"SMArt+":1234, "SMArtbasic":1235, "v5":1236}
LOG = r"C:\Users\dayas\TetraPC\dongle_compare.csv"

def connect(port):
    s=socket.socket(); s.settimeout(5); s.connect(("127.0.0.1",port)); s.settimeout(2)
    try: s.recv(12)
    except: pass
    def cmd(c,p): s.sendall(struct.pack(">BI",c,p))
    cmd(0x01,CENTER); cmd(0x02,SR); cmd(0x08,0); cmd(0x03,1); cmd(0x04,300)
    return s

socks={n:connect(p) for n,p in PORTS.items()}
time.sleep(1)
win=np.hanning(FFT); wn=np.sum(win)/2; needed=FFT*2
state={n:{"base":-80,"peak":0} for n in PORTS}
running=True

def reader(name, s):
    buf=bytearray()
    while running:
        try: chunk=s.recv(65536)
        except: continue
        if not chunk: continue
        buf.extend(chunk)
        # alleen nieuwste frame verwerken voor lage CPU
        if len(buf)>=needed:
            nfr=len(buf)//needed; raw=bytes(buf[(nfr-1)*needed:nfr*needed]); del buf[:nfr*needed]
            iq=(np.frombuffer(raw,dtype=np.uint8).astype(np.float32)-127.5)/127.5
            x=iq[0::2]+1j*iq[1::2]
            fft=np.fft.fftshift(np.abs(np.fft.fft(x*win,FFT)))
            p=20*np.log10(fft/wn+1e-10); base=float(np.median(p))
            pp=p.copy(); c=FFT//2; pp[c-8:c+8]=-200
            state[name]={"base":base,"peak":float(np.max(pp)-base)}

threads=[threading.Thread(target=reader,args=(n,s),daemon=True) for n,s in socks.items()]
for t in threads: t.start()

import os
newfile = not os.path.exists(LOG)
with open(LOG,"a",encoding="utf-8") as f:
    if newfile:
        f.write("timestamp,"+",".join(f"{n}_base,{n}_peak" for n in PORTS)+"\n")
    print(f"Loggen naar {LOG} ... (Ctrl-C of sluit venster om te stoppen)")
    try:
        while True:
            time.sleep(2)
            ts=time.strftime("%H:%M:%S")
            row=",".join(f"{state[n]['base']:.1f},{state[n]['peak']:.1f}" for n in PORTS)
            f.write(f"{ts},{row}\n"); f.flush()
            print(f"{ts}  "+"  ".join(f"{n}: ruis {state[n]['base']:.1f} piek {state[n]['peak']:.1f}" for n in PORTS))
    except KeyboardInterrupt:
        running=False
