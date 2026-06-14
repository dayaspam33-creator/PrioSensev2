#!/usr/bin/env python3
"""Meet één dongle op poort 1234, ~15s, ruisvloer + piek (DC uitgesloten)."""
import socket, struct, time, numpy as np
FFT=1024; SR=3_200_000; CENTER=392_500_000; DUR=15.0
s=socket.socket(); s.settimeout(5); s.connect(("127.0.0.1",1234)); s.settimeout(2)
try: s.recv(12)
except: pass
def cmd(c,p): s.sendall(struct.pack(">BI",c,p))
cmd(0x01,CENTER); cmd(0x02,SR); cmd(0x08,0); cmd(0x03,1); cmd(0x04,300)
time.sleep(1)
buf=bytearray(); needed=FFT*2; win=np.hanning(FFT); wn=np.sum(win)/2
bases,peaks=[],[]; t0=time.time()
while time.time()-t0<DUR:
    try: chunk=s.recv(65536)
    except: break
    if not chunk: break
    buf.extend(chunk)
    while len(buf)>=needed:
        raw=buf[:needed]; del buf[:needed]
        iq=(np.frombuffer(raw,dtype=np.uint8).astype(np.float32)-127.5)/127.5
        x=iq[0::2]+1j*iq[1::2]
        fft=np.fft.fftshift(np.abs(np.fft.fft(x*win,FFT)))
        p=20*np.log10(fft/wn+1e-10); base=np.median(p)
        pp=p.copy(); c=FFT//2; pp[c-8:c+8]=-200
        bases.append(base); peaks.append(np.max(pp)-base)
s.close()
print(f"SMArt basic (alleen): n={len(bases)}")
print(f"  ruisvloer (mediaan): {np.mean(bases):.1f} dBFS")
print(f"  piek boven ruis: gem {np.mean(peaks):.1f}  max {np.max(peaks):.1f} dB")
