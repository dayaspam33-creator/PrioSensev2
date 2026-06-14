#!/usr/bin/env python3
"""Vergelijk 2 dongles tegelijk via rtl_tcp op poort 1234 en 1235.
Leest beide ~20s, berekent ruisvloer en piek op de C2000 downlink (390-395)."""
import socket, struct, time, numpy as np

FFT = 1024
SR  = 3_200_000
CENTER = 392_500_000
DUR = 20.0   # seconden meten

def connect(port):
    s = socket.socket(); s.settimeout(5); s.connect(("127.0.0.1", port))
    s.settimeout(2)
    try: s.recv(12)
    except: pass
    def cmd(c, p): s.sendall(struct.pack(">BI", c, p))
    cmd(0x01, CENTER); cmd(0x02, SR); cmd(0x08, 0)
    cmd(0x03, 1); cmd(0x04, 300)   # manual gain 30 dB
    return s

def measure(s):
    buf = bytearray(); needed = FFT*2
    bases, peaks = [], []
    win = np.hanning(FFT); wn = np.sum(win)/2
    t0 = time.time()
    while time.time() - t0 < DUR:
        try: chunk = s.recv(65536)
        except: break
        if not chunk: break
        buf.extend(chunk)
        while len(buf) >= needed:
            raw = buf[:needed]; del buf[:needed]
            iq = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32)-127.5)/127.5
            x = iq[0::2] + 1j*iq[1::2]
            fft = np.fft.fftshift(np.abs(np.fft.fft(x*win, FFT)))
            p = 20*np.log10(fft/wn + 1e-10)
            base = np.median(p)
            # DC-piek rond center (midden ±8 bins) uitsluiten van de piek-meting
            pp = p.copy(); c = FFT//2; pp[c-8:c+8] = -200
            peak = np.max(pp) - base
            bases.append(base); peaks.append(peak)
    return bases, peaks

print("Verbinden met beide dongles...")
s0 = connect(1234); s1 = connect(1235)
time.sleep(1)
print(f"Meten gedurende {DUR:.0f}s op {CENTER/1e6:.1f} MHz, gain 30...\n")

# Afwisselend lezen voor gelijktijdigheid
import threading
res = {}
def run(name, s): res[name] = measure(s)
t0 = threading.Thread(target=run, args=("Dongle A (dev0/1234)", s0))
t1 = threading.Thread(target=run, args=("Dongle B (dev1/1235)", s1))
t0.start(); t1.start(); t0.join(); t1.join()

for name in ["Dongle A (dev0/1234)", "Dongle B (dev1/1235)"]:
    b, p = res[name]
    if not b:
        print(f"{name}: geen data"); continue
    print(f"{name}:  n={len(b)}")
    print(f"   ruisvloer (mediaan): {np.mean(b):.1f} dBFS")
    print(f"   piek boven ruis: gem {np.mean(p):.1f}  max {np.max(p):.1f} dB\n")
s0.close(); s1.close()
