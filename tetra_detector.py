#!/usr/bin/env python3
"""
PrioSense v1
PyQt6 · Dark UI · 3 signaalbalken · Slot-tracking · Auto-reconnect
"""

from collections import deque
import numpy as np
import socket, struct, subprocess, threading, time, sys, os, wave, math, platform
from datetime import datetime
import matplotlib
matplotlib.use("QtAgg")
import matplotlib.pyplot as plt
import matplotlib.animation as _mpl_anim
import matplotlib.gridspec as _mpl_gs
from matplotlib.widgets import Slider as _MplSlider, Button as _MplButton

# ── Geluid ────────────────────────────────────────────────────────────────────
def _sound_dir():
    """Naast de exe bij frozen, naast het script bij development."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

_BEEP_WAV   = os.path.join(_sound_dir(), "_beep.wav")
_SIREN_WAV  = os.path.join(_sound_dir(), "_siren.wav")

def _make_beep_wav(path, freq=900, duration=0.3, volume=0.05, rate=44100):
    n = int(rate * duration)
    with wave.open(path, "w") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(rate)
        f.writeframes(b"".join(
            struct.pack("<h", int(32767 * volume * math.sin(2*math.pi*freq*i/rate)))
            for i in range(n)))

def _make_siren_wav(path, volume=0.08, rate=44100):
    """Sirene: 2x sweep van 800 Hz naar 1400 Hz en terug (0.4s per sweep)."""
    frames = []
    sweep_n = int(rate * 0.4)
    for _ in range(2):
        # Omhoog: 800 → 1400 Hz
        for i in range(sweep_n):
            f = 800 + (1400 - 800) * i / sweep_n
            frames.append(struct.pack("<h", int(32767 * volume * math.sin(2*math.pi*f*i/rate))))
        # Omlaag: 1400 → 800 Hz
        for i in range(sweep_n):
            f = 1400 - (1400 - 800) * i / sweep_n
            frames.append(struct.pack("<h", int(32767 * volume * math.sin(2*math.pi*f*i/rate))))
    with wave.open(path, "w") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(rate)
        f.writeframes(b"".join(frames))

_make_beep_wav(_BEEP_WAV)
if not os.path.exists(_SIREN_WAV):
    _make_siren_wav(_SIREN_WAV)

try:
    import winsound as _ws
    def _play_beep():
        _ws.PlaySound(_BEEP_WAV, _ws.SND_FILENAME)
    def _play_siren():
        _ws.PlaySound(_SIREN_WAV, _ws.SND_FILENAME)
except ImportError:
    def _play_beep():
        if sys.platform == "darwin":
            os.system(f"afplay '{_BEEP_WAV}'")
        else:
            os.system(f"aplay '{_BEEP_WAV}' 2>/dev/null || paplay '{_BEEP_WAV}' 2>/dev/null")
    def _play_siren():
        if sys.platform == "darwin":
            os.system(f"afplay '{_SIREN_WAV}'")
        else:
            os.system(f"aplay '{_SIREN_WAV}' 2>/dev/null || paplay '{_SIREN_WAV}' 2>/dev/null")
            if os.path.exists(_WASTED_WAV):
                os.system(f"aplay '{_WASTED_WAV}' 2>/dev/null || paplay '{_WASTED_WAV}' 2>/dev/null")

# ── Instellingen ──────────────────────────────────────────────────────────────
DEFAULT_CENTER   = 382_500_000
SAMPLE_RATE      = 3_200_000
FFT_SIZE         = 1024
WFALL_ROWS       = 100
THRESHOLD_SOFT   = 30
THRESHOLD_HARD   = 10
GAIN_DB          = 40
TCP_HOST         = "127.0.0.1"
TCP_PORT         = 1234
DEVICE_IDX       = 0

# Verbindingsmodus: "PC" (rtl_tcp.exe poort 1234) of "Android" (marto poort 14423)
CONNECTION_MODES = {
    "PC":      1234,
    "Android": 14423,
}
LOG_COOLDOWN     = 10.0
N_SEGS           = 10
DB_PER_BLOCK     = 4.5   # voor absolute schaal (niet meer gebruikt in bars)
DB_PER_BLOCK_REL = 2.8   # relatieve schaal: 25 dB boven slot_floor = 10 segmenten
HANG_TIME        = 4.0
N_SMOOTH         = 15
DECAY_DB_S       = 9.0
BASELINE_FREEZE  = 20.0   # vaste freeze-grens, los van drempel
SLOT_FLOOR       = 20.0   # minimum dB om in balk te tonen, los van drempel

_SEARCH_PATHS = [
    r"C:\Users\dayas\Desktop\sdrsharp-x64\rtl_tcp.exe",
    r"C:\Program Files\rtl-sdr\rtl_tcp.exe",
    "rtl_tcp", "rtl_tcp.exe",
]
RTL_TCP_PATH = next((p for p in _SEARCH_PATHS if os.path.exists(p)), _SEARCH_PATHS[-1])
# Vaste log-locatie — werkt ook correct vanuit PyInstaller exe
if getattr(sys, 'frozen', False):
    _LOG_DIR  = os.path.dirname(sys.executable)
else:
    _LOG_DIR  = os.path.dirname(os.path.abspath(__file__))

LOG_PATH         = os.path.join(_LOG_DIR, "detections.csv")
DEBUG_LOG_PATH   = os.path.join(_LOG_DIR, "debug_log.csv")
FREQ_MEMORY_PATH = os.path.join(_LOG_DIR, "freq_memory.json")

TETRA_FREQS = [
    380.150, 380.400, 380.650, 380.900,
    381.150, 381.400, 381.650, 381.900,
    382.150, 382.400, 382.650, 382.900,
    383.150, 383.400, 383.650, 383.900,
    384.150, 384.400, 384.650, 384.900,
]

def send_cmd(sock, cmd, param):
    sock.sendall(struct.pack(">BI", cmd, param))


# ── Detector ──────────────────────────────────────────────────────────────────
class TcpDetector:
    WARMUP = 150

    def __init__(self):
        self.center_freq   = DEFAULT_CENTER
        self.gain_db       = GAIN_DB
        self.auto_gain     = False
        self.freqs         = self._calc_freqs(DEFAULT_CENTER)
        self.power         = np.full(FFT_SIZE, -80.0)
        self.baseline      = None
        self.wfall         = np.full((WFALL_ROWS, FFT_SIZE), -80.0)
        self.threshold        = float(THRESHOLD_SOFT)
        self.hard_threshold   = 40.0
        self.muted            = False
        self._last_siren_time = 0.0
        self.alarm         = False
        self.alarm_level   = 0
        self.alarm_freq    = 0.0
        self.alarm_db      = 0.0
        self._alarm_until  = 0.0
        self.running       = False
        self._lock         = threading.Lock()
        self._sock         = None
        self._proc         = None
        self._last_beep        = 0.0
        self._beeping          = False
        self._log_cooldown     = {}
        self._prev_alarm_level = 0
        self._last_red_beep    = 0.0
        self.mode_name     = "Standaard"
        self.status        = "Opstarten…"
        self.n_frames      = 0
        self.slot_floor = SLOT_FLOOR
        self.hang_time  = HANG_TIME
        self.slots = [
            {"freq": None, "db": 0.0, "hang_until": 0.0, "decay_t": 0.0}
            for _ in range(3)
        ]
        self._alarm_cleared_at = 0.0
        self._ch_history  = {}
        self.raw_peaks    = {}
        self.debug_logging    = False
        self._last_debug_log  = 0.0
        # AGR
        self.agr_enabled      = True
        self.agr_active       = False
        self._agr_orig_gain   = None
        self._agr_clear_time  = 0.0
        # Verbindingsmodus
        self.connection_mode  = "PC"   # "PC" of "Android"
        self._tcp_port        = TCP_PORT
        self.android_host     = "192.168.0.144"  # IP van de telefoon
        # Frequentie geheugen
        self._freq_history    = deque()   # (timestamp, freq, db)
        self.known_freqs      = self._load_known_freqs()

    def _calc_freqs(self, center_hz):
        return np.linspace((center_hz - SAMPLE_RATE/2) / 1e6,
                           (center_hz + SAMPLE_RATE/2) / 1e6, FFT_SIZE)

    def _drain(self, pipe):
        try:
            for line in pipe:
                t = line.decode(errors="replace").rstrip()
                if t: print(f"[rtl_tcp] {t}")
        except Exception:
            pass

    def _connect(self):
        self._tcp_port = CONNECTION_MODES.get(self.connection_mode, TCP_PORT)
        host = self.android_host if self.connection_mode == "Android" else TCP_HOST
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(5)
        self._sock.connect((host, self._tcp_port))
        self._sock.settimeout(2)
        try: self._sock.recv(12)
        except Exception: pass
        send_cmd(self._sock, 0x01, self.center_freq)
        send_cmd(self._sock, 0x02, SAMPLE_RATE)
        send_cmd(self._sock, 0x03, 0 if self.auto_gain else 1)
        if not self.auto_gain:
            send_cmd(self._sock, 0x04, int(self.gain_db * 10))

    def _try_reconnect(self):
        attempts = 0
        if self.running:
            _show_toast("PrioSense ⚠️", "Dongle verbinding verbroken — opnieuw verbinden...")
        while self.running:
            attempts += 1
            self.status = f"Herverbinden… poging {attempts}"
            try:
                if self._sock:
                    try: self._sock.close()
                    except: pass
                self._connect()
                with self._lock:
                    self.baseline = None
                    self.n_frames = 0
                self.status = "Herverbonden!"
                if self.running:
                    _show_toast("PrioSense ✅", "Dongle verbonden")
                print(f"Herverbonden na {attempts} poging(en)")
                return True
            except Exception as e:
                print(f"Herverbinden mislukt (poging {attempts}): {e}")
                if self.running:
                    time.sleep(3)
        return False

    def start(self):
        port = CONNECTION_MODES.get(self.connection_mode, TCP_PORT)
        if self.connection_mode == "Android":
            # Android modus: RTL TCP Android app draait al op de telefoon
            print(f"Android modus — verbinden op {TCP_HOST}:{port}")
        else:
            # PC modus: rtl_tcp.exe opstarten
            if sys.platform != "win32":
                os.system("pkill rtl_tcp 2>/dev/null")
                time.sleep(0.5)
            if os.path.exists(RTL_TCP_PATH):
                print(f"rtl_tcp starten: {RTL_TCP_PATH}")
                _flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
                self._proc = subprocess.Popen(
                    [RTL_TCP_PATH, "-a", TCP_HOST, "-p", str(port),
                     "-d", str(DEVICE_IDX), "-f", str(self.center_freq),
                     "-s", str(SAMPLE_RATE)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=_flags)
                threading.Thread(target=self._drain, args=(self._proc.stdout,),
                                 daemon=True).start()
                time.sleep(3.0)
            else:
                print(f"rtl_tcp niet gevonden — probeer {TCP_HOST}:{port}")
        try:
            self._connect()
        except Exception as e:
            raise RuntimeError(f"Kan geen verbinding maken op {TCP_HOST}:{port}.\n\n"
                               f"Modus: {self.connection_mode}\n\n"
                               f"{'Controleer of de dongle is aangesloten en rtl_tcp draait.' if self.connection_mode == 'PC' else 'Controleer of RTL TCP Android actief is en op START gedrukt.'}\n\nFout: {e}")
        self.running = True
        self._log_session("SESSION_START")
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._log_session("SESSION_STOP")
        self.running = False
        try: self._sock.close()
        except Exception: pass
        if self._proc: self._proc.terminate()

    def set_gain(self, gain_db, auto=None):
        if auto is not None:
            self.auto_gain = auto
        self.gain_db = gain_db
        if self._sock:
            try:
                if self.auto_gain:
                    send_cmd(self._sock, 0x03, 0)
                else:
                    send_cmd(self._sock, 0x03, 1)
                    send_cmd(self._sock, 0x04, int(gain_db * 10))
            except Exception: pass

    def set_center_freq(self, mhz):
        hz = int(round(mhz * 1e6))
        self.center_freq = hz
        new_freqs = self._calc_freqs(hz)
        if self._sock:
            try: send_cmd(self._sock, 0x01, hz)
            except Exception: pass
        with self._lock:
            self.freqs       = new_freqs
            self.baseline    = None
            self.n_frames    = 0
            self._ch_history = {}
            for s in self.slots:
                s["freq"] = None; s["db"] = 0.0

    def reset_baseline(self):
        with self._lock:
            self.baseline    = None
            self.n_frames    = 0
            self._ch_history = {}
            for s in self.slots:
                s["freq"] = None; s["db"] = 0.0

    def _load_known_freqs(self):
        try:
            if os.path.exists(FREQ_MEMORY_PATH):
                import json
                with open(FREQ_MEMORY_PATH, "r", encoding="utf-8") as f:
                    return set(json.load(f))
        except Exception: pass
        return set()

    def save_known_freq(self, freq):
        """Sla frequentie op als bevestigd hulpdienst kanaal."""
        self.known_freqs.add(round(freq, 3))
        try:
            import json
            with open(FREQ_MEMORY_PATH, "w", encoding="utf-8") as f:
                json.dump(sorted(self.known_freqs), f)
        except Exception: pass

    def get_best_freq_last_2min(self):
        """Geeft de sterkste frequentie van de laatste 2 minuten terug."""
        cutoff = time.time() - 120.0
        recent = [(db, freq) for (ts, freq, db) in self._freq_history if ts >= cutoff]
        if not recent:
            return None
        return max(recent)[1]

    def _ensure_csv(self):
        try:
            if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0:
                with open(LOG_PATH, "w", encoding="utf-8") as f:
                    f.write("timestamp,type,frequentie_mhz,db,modus,slot\n")
        except Exception:
            pass

    def _log(self, freq, db, slot_num=0):
        now = time.time()
        key = (round(freq, 3), slot_num)
        if now - self._log_cooldown.get(key, 0) < LOG_COOLDOWN:
            return
        self._log_cooldown[key] = now
        try:
            self._ensure_csv()
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{ts},DETECTIE,{freq:.3f},{db:.1f},{self.mode_name},slot{slot_num+1}\n")
        except Exception:
            pass

    def _log_police(self):
        try:
            self._ensure_csv()
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._lock:
                actief = [(i, s["freq"], s["db"])
                          for i, s in enumerate(self.slots)
                          if s["freq"] is not None and s["db"] > 0]
                mode = self.mode_name
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                if actief:
                    for i, freq, db in actief:
                        f.write(f"{ts},POLITIE,{freq:.3f},{db:.1f},{mode},slot{i+1}\n")
                else:
                    f.write(f"{ts},POLITIE,geen_signaal,0,{mode},-\n")
        except Exception:
            pass

    def _log_session(self, event):
        try:
            self._ensure_csv()
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{ts},{event},-,-,-,-\n")
        except Exception:
            pass

    def _beep(self):
        if self.muted: return
        if self._beeping: return
        if time.time() - self._last_beep < 1.0: return
        self._last_beep = time.time()
        self._beeping   = True
        def _do():
            try: _play_beep()
            except Exception: pass
            self._beeping = False
        threading.Thread(target=_do, daemon=True).start()

    def _loop(self):
        buf = bytearray(); needed = FFT_SIZE * 2
        while self.running:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    if not self._try_reconnect(): break
                    buf = bytearray(); continue
                buf.extend(chunk)
                while len(buf) >= needed:
                    raw     = buf[:needed]; del buf[:needed]
                    iq      = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5) / 127.5
                    samples = iq[0::2] + 1j * iq[1::2]
                    fft     = np.fft.fftshift(np.abs(np.fft.fft(samples, FFT_SIZE)))
                    power   = 20 * np.log10(fft / FFT_SIZE + 1e-10)
                    self.n_frames += 1
                    with self._lock:
                        self.wfall    = np.roll(self.wfall, 1, axis=0)
                        self.wfall[0] = power
                        self.power    = power
                        if self.n_frames < self.WARMUP:
                            alpha = 0.05
                            if self.baseline is None:
                                self.baseline = power.copy()
                            else:
                                self.baseline = (1 - alpha) * self.baseline + alpha * power
                            pct = int(100 * self.n_frames / self.WARMUP)
                            self.status      = f"Baseline opbouwen  {pct}%"
                            self.alarm       = False
                            self.alarm_level = 0
                        else:
                            self.status  = "Scannen"
                            diff         = power - self.baseline
                            now          = time.time()
                            hard_thr     = self.hard_threshold
                            ch_db    = {}  # smoothed → balkjes & slots
                            raw_ch   = {}  # ongefilterd → alarm & beep
                            for cf in TETRA_FREQS:
                                if self.freqs[0] <= cf <= self.freqs[-1]:
                                    idx = int(np.argmin(np.abs(self.freqs - cf)))
                                    raw_db = float(diff[idx])
                                    raw_ch[cf] = raw_db
                                    if cf not in self._ch_history:
                                        self._ch_history[cf] = deque(maxlen=N_SMOOTH)
                                    self._ch_history[cf].append(raw_db)
                                    ch_db[cf] = sum(self._ch_history[cf]) / len(self._ch_history[cf])
                            # Peak-hold voor raw data venster (elke FFT-frame bijgewerkt)
                            for cf, rdb in raw_ch.items():
                                pk_db, pk_exp = self.raw_peaks.get(cf, (rdb, now + 3.0))
                                if rdb >= pk_db or now > pk_exp:
                                    self.raw_peaks[cf] = (rdb, now + 3.0)
                                else:
                                    self.raw_peaks[cf] = (pk_db, pk_exp)
                            # Baseline freeze op basis van smoothed waarden
                            best_db = max(ch_db.values(), default=0.0)
                            if self.alarm_level == 0:
                                if self._alarm_cleared_at == 0.0:
                                    self._alarm_cleared_at = now
                                silent_secs = now - self._alarm_cleared_at
                                if best_db < BASELINE_FREEZE or silent_secs > 10.0:
                                    # Na 10s rust: sneller bijwerken om bevroren baseline te corrigeren
                                    alpha = 0.003 if silent_secs <= 10.0 else min(0.05, 0.003 + (silent_secs - 10.0) * 0.002)
                                    self.baseline = (1 - alpha) * self.baseline + alpha * power
                            else:
                                self._alarm_cleared_at = 0.0
                                if best_db < BASELINE_FREEZE:
                                    alpha = 0.003
                                    self.baseline = (1 - alpha) * self.baseline + alpha * power
                            # Balkjes en slots op basis van RUWE waarden
                            active   = {cf: db for cf, db in raw_ch.items() if db > self.slot_floor}
                            assigned = set()
                            for slot in self.slots:
                                if slot["freq"] in active:
                                    slot["db"]         = max(slot["db"], active[slot["freq"]])
                                    slot["hang_until"] = now + self.hang_time
                                    slot["decay_t"]    = 0.0
                                    assigned.add(slot["freq"])
                            for cf, db in sorted(active.items(), key=lambda x: -x[1]):
                                if cf in assigned: continue
                                for slot in self.slots:
                                    if slot["freq"] is None or now > slot["hang_until"]:
                                        slot["freq"]       = cf
                                        slot["db"]         = db
                                        slot["hang_until"] = now + self.hang_time
                                        slot["decay_t"]    = 0.0
                                        assigned.add(cf)
                                        break
                            for slot in self.slots:
                                if slot["freq"] is not None and now > slot["hang_until"]:
                                    if slot["decay_t"] == 0.0:
                                        slot["decay_t"] = now
                                    dt = now - slot["decay_t"]
                                    slot["db"]      = max(0.0, slot["db"] - DECAY_DB_S * dt)
                                    slot["decay_t"] = now
                                    if slot["db"] <= 0:
                                        slot["freq"] = None
                                elif slot["freq"] is not None:
                                    slot["decay_t"] = 0.0
                            # Alarm & beep op basis van RUWE waarden (geen smoothing)
                            best_raw_db   = max(raw_ch.values(), default=0.0)
                            best_raw_freq = max(raw_ch, key=raw_ch.get) if raw_ch else 0.0

                            # Frequentie geschiedenis bijhouden (2 min venster)
                            if best_raw_db > self.slot_floor:
                                self._freq_history.append((now, best_raw_freq, best_raw_db))
                                while self._freq_history and now - self._freq_history[0][0] > 120.0:
                                    self._freq_history.popleft()

                            if best_raw_db > hard_thr:
                                self.alarm = True; self.alarm_level = 2
                                self.alarm_freq = best_raw_freq; self.alarm_db = best_raw_db
                                self._alarm_until = now + 2.0
                                best_slot = next((i for i, s in enumerate(self.slots) if s["freq"] == best_raw_freq), 0)
                                self._log(best_raw_freq, best_raw_db, best_slot)
                                if now - self._last_red_beep >= 1.0:
                                    self._beep()
                                    self._last_red_beep = now
                            elif best_raw_db > self.threshold:
                                self.alarm = True; self.alarm_level = 1
                                self.alarm_freq = best_raw_freq; self.alarm_db = best_raw_db
                                self._alarm_until = now + 2.0
                                if self._prev_alarm_level == 0:
                                    self._beep()
                            elif now >= self._alarm_until:
                                self.alarm = False; self.alarm_level = 0
                                self._last_red_beep = 0.0

                            # Sirene met harde 60s cooldown
                            if self.alarm_level == 2 and not self.muted:
                                if now - self._last_siren_time >= 60.0:
                                    self._last_siren_time = now
                                    threading.Thread(target=_play_siren, daemon=True).start()

                            # AGR — automatische gain reductie bij hard alarm
                            if self.agr_enabled:
                                if self.alarm_level == 2 and not self.agr_active:
                                    self._agr_orig_gain = self.gain_db
                                    new_gain = max(10.0, self.gain_db - 15.0)
                                    self.gain_db = new_gain
                                    try:
                                        send_cmd(self._sock, 0x03, 1)
                                        send_cmd(self._sock, 0x04, int(new_gain * 10))
                                    except Exception: pass
                                    self.agr_active = True
                                    self._agr_clear_time = 0.0
                                elif self.alarm_level == 0 and self.agr_active:
                                    if self._agr_clear_time == 0.0:
                                        self._agr_clear_time = now
                                    elif now - self._agr_clear_time >= 5.0:
                                        if self._agr_orig_gain is not None:
                                            self.gain_db = self._agr_orig_gain
                                            try:
                                                send_cmd(self._sock, 0x03, 1)
                                                send_cmd(self._sock, 0x04, int(self._agr_orig_gain * 10))
                                            except Exception: pass
                                        self.agr_active = False
                                        self._agr_orig_gain = None
                                        self._agr_clear_time = 0.0
                                elif self.alarm_level > 0:
                                    self._agr_clear_time = 0.0

                            self._prev_alarm_level = self.alarm_level

                            # Debug logging (elke 2s als ingeschakeld)
                            if self.debug_logging and now - self._last_debug_log >= 2.0:
                                self._last_debug_log = now
                                try:
                                    write_header = not os.path.exists(DEBUG_LOG_PATH)
                                    with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                                        if write_header:
                                            f.write("timestamp,gain_db,alarm_level,baseline_avg," +
                                                    ",".join(f"ch_{cf:.3f}" for cf in sorted(raw_ch)) + "\n")
                                        baseline_avg = float(np.mean(self.baseline)) if self.baseline is not None else 0.0
                                        ch_vals = ",".join(f"{raw_ch[cf]:.1f}" for cf in sorted(raw_ch))
                                        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                                        f.write(f"{ts},{self.gain_db},{self.alarm_level},{baseline_avg:.1f},{ch_vals}\n")
                                except Exception:
                                    pass

            except socket.timeout:
                continue
            except Exception as e:
                print(f"Lus fout: {e}")
                if not self._try_reconnect(): break
                buf = bytearray()


# ══════════════════════════════════════════════════════════════════════════════
#  Klassieke matplotlib weergave
# ══════════════════════════════════════════════════════════════════════════════
_CLASSIC_ANIS = []   # voorkomt garbage collection van animatie

def open_classic_view(det):
    global _CLASSIC_ANIS
    for a in list(_CLASSIC_ANIS):
        try: plt.close(a._fig)
        except Exception: pass
    _CLASSIC_ANIS.clear()

    BG = "#0d1117"; PANEL = "#161b22"; CYAN = "#58d9f0"
    RED = "#ff4444"; GREEN = "#44dd88"; ORANGE = "#f0a500"
    WHITE = "#e6edf3"; GRAY = "#444c56"; ALARM_BG = "#3d0000"

    fig = plt.figure(figsize=(16, 10), facecolor=BG)
    fig.canvas.manager.set_window_title("PrioSense — Klassiek")

    # Linker kolom: spectrum + meters via GridSpec
    gs = _mpl_gs.GridSpec(1, 1, figure=fig,
                          left=0.05, right=0.68, top=0.97, bottom=0.06)
    gs_l = _mpl_gs.GridSpecFromSubplotSpec(2, 1, subplot_spec=gs[0],
                                           height_ratios=[2.5, 2.0], hspace=0.42)
    ax_spec  = fig.add_subplot(gs_l[0])
    ax_meter = fig.add_subplot(gs_l[1])

    for ax in [ax_spec, ax_meter]:
        ax.set_facecolor(PANEL)
        ax.tick_params(colors=WHITE, labelsize=8)
        for sp in ax.spines.values(): sp.set_color(GRAY)

    # Rechter kolom: widgets via add_axes (vaste posities → klikbaar)
    RX = 0.725   # links van rechterkolom
    RW = 0.255   # breedte rechterkolom
    ax_alrm  = fig.add_axes([RX, 0.800, RW, 0.165])
    ax_thr   = fig.add_axes([RX, 0.696, RW, 0.068])
    ax_gain  = fig.add_axes([RX, 0.590, RW, 0.068])
    ax_freq  = fig.add_axes([RX, 0.484, RW, 0.068])
    ax_auto  = fig.add_axes([RX, 0.378, RW, 0.062])
    ax_reset = fig.add_axes([RX, 0.272, RW, 0.062])
    ax_info  = fig.add_axes([RX, 0.155, RW, 0.062])
    ax_time  = fig.add_axes([RX, 0.068, RW, 0.062])

    for ax in [ax_alrm, ax_info, ax_time]:
        ax.set_facecolor(BG)
        for sp in ax.spines.values(): sp.set_visible(False)
        ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)

    # Verberg de navigatie-toolbar zodat hij slider-clicks niet blokkeert
    try:
        fig.canvas.manager.toolbar.setVisible(False)
    except Exception:
        pass

    # Spectrum
    with det._lock:
        _f = det.freqs.copy(); _p = det.power.copy()
    line_pwr,  = ax_spec.plot(_f, _p, color=CYAN,   lw=0.9, label="Vermogen")
    line_base, = ax_spec.plot(_f, _p, color=ORANGE, lw=0.8, ls="--", alpha=0.6, label="Baseline")
    ax_spec.set_xlim(_f[0], _f[-1]); ax_spec.set_ylim(-90, -10)
    ax_spec.set_xlabel("Frequentie (MHz)", color=WHITE, fontsize=8)
    ax_spec.set_ylabel("dBm", color=WHITE, fontsize=8)
    ax_spec.set_title(f"Spectrum  ({_f[0]:.1f}–{_f[-1]:.1f} MHz)", color=WHITE, fontsize=10)
    ax_spec.grid(True, alpha=0.12, color=WHITE)
    ax_spec.legend(facecolor=BG, labelcolor=WHITE, fontsize=7, loc="upper right")
    for cf in TETRA_FREQS:
        ax_spec.axvline(cf, color=CYAN, alpha=0.15, lw=0.5)

    # LED meters (24 segmenten, 4 kleuren)
    N_CL = 24; SEG_MAX = 48.0; SEG_DB = SEG_MAX / N_CL
    def seg_off(i):
        if i >= 20: return "#3a0000"
        if i >= 15: return "#3a1800"
        if i >= 8:  return "#1e2a00"
        return "#003316"
    def seg_on(i):
        if i >= 20: return "#ff3333"
        if i >= 15: return "#ff8800"
        if i >= 8:  return "#aaee00"
        return "#22ee66"

    ax_meter.set_facecolor("#0a0f14")
    ax_meter.set_xlim(-0.5, 2.5); ax_meter.set_ylim(-6, SEG_MAX + 4)
    ax_meter.set_title("Signaalsterkte — Top 3 kanalen", color=WHITE, fontsize=10)
    ax_meter.set_xticks([0, 1, 2]); ax_meter.set_xticklabels(["", "", ""])
    ax_meter.set_yticks([])
    for sp in ax_meter.spines.values(): sp.set_color(GRAY)

    SEG_W, SEG_H, SEG_GAP = 0.62, SEG_DB * 0.82, SEG_DB * 0.18
    _segs = []
    for ch in range(3):
        ch_segs = []
        for s in range(N_CL):
            r = plt.Rectangle((ch - SEG_W/2, s * SEG_DB + SEG_GAP/2),
                               SEG_W, SEG_H, facecolor=seg_off(s),
                               edgecolor="none", zorder=2)
            ax_meter.add_patch(r); ch_segs.append(r)
        _segs.append(ch_segs)

    thr_line  = ax_meter.axhline(det.threshold, color=RED, ls="--", lw=1.0, alpha=0.7)
    _mfreq    = [ax_meter.text(ch, -2.5, "—", ha="center", va="top",
                               fontsize=8, color=WHITE, fontweight="bold") for ch in range(3)]
    _mdb      = [ax_meter.text(ch, 1, "", ha="center", va="bottom",
                               fontsize=9, color=WHITE, fontweight="bold", zorder=5) for ch in range(3)]

    # Alarm
    ax_alrm.set_xlim(0, 1); ax_alrm.set_ylim(0, 1)
    alrm_bg  = plt.Rectangle((0,0), 1, 1, transform=ax_alrm.transAxes, facecolor=PANEL, zorder=0)
    ax_alrm.add_patch(alrm_bg)
    alrm_txt = ax_alrm.text(0.5, 0.62, "● PRIOSENSE",
                            ha="center", va="center", fontsize=13, fontweight="bold",
                            color=WHITE, transform=ax_alrm.transAxes)
    stat_txt = ax_alrm.text(0.5, 0.22, "—", ha="center", va="center",
                            fontsize=9, color=ORANGE, transform=ax_alrm.transAxes)
    ax_alrm.axis("off")

    # Eigen drempel voor klassiek venster (onafhankelijk van hoofdvenster)
    _cl_thr = [float(det.threshold)]

    # Sliders
    sl_thr  = _MplSlider(ax_thr,  "Drempel (dB)", 5,     50,  valinit=_cl_thr[0],        valstep=1,    color=RED)
    sl_gain = _MplSlider(ax_gain, "Gain (dB)",    0,     49,  valinit=det.gain_db,        valstep=1,    color=CYAN)
    sl_freq = _MplSlider(ax_freq, "Center (MHz)", 379.0, 386.0, valinit=det.center_freq/1e6, valstep=0.05, color=ORANGE)
    for sl in [sl_thr, sl_gain, sl_freq]:
        sl.label.set_color(WHITE); sl.valtext.set_color(WHITE); sl.ax.set_facecolor(PANEL)

    def on_thr(v):
        _cl_thr[0] = float(v)
        thr_line.set_ydata([v, v])
    def on_gain(v): det.auto_gain = False; det.set_gain(float(v), auto=False)
    def on_freq(v):
        det.set_center_freq(float(v))
        nf = det._calc_freqs(int(round(float(v) * 1e6)))
        line_pwr.set_xdata(nf); line_base.set_xdata(nf)
        ax_spec.set_xlim(nf[0], nf[-1])
        ax_spec.set_title(f"Spectrum  ({nf[0]:.1f}–{nf[-1]:.1f} MHz)", color=WHITE, fontsize=10)
    sl_thr.on_changed(on_thr); sl_gain.on_changed(on_gain); sl_freq.on_changed(on_freq)

    # Knoppen
    btn_auto  = _MplButton(ax_auto,  "Auto Gain: UIT", color=PANEL, hovercolor=GRAY)
    btn_reset = _MplButton(ax_reset, "Reset Baseline",  color=PANEL, hovercolor=GRAY)
    for btn in [btn_auto, btn_reset]:
        btn.label.set_color(WHITE); btn.label.set_fontsize(9)

    def on_auto(e):
        det.auto_gain = not det.auto_gain; det.set_gain(det.gain_db, auto=det.auto_gain)
        btn_auto.label.set_text("Auto Gain: AAN" if det.auto_gain else "Auto Gain: UIT")
        btn_auto.label.set_color(GREEN if det.auto_gain else WHITE)
    def on_reset(e): det.reset_baseline()
    btn_auto.on_clicked(on_auto); btn_reset.on_clicked(on_reset)

    ax_info.axis("off")
    ax_info.text(0.5, 0.5, f"Dongle #{DEVICE_IDX}  ·  SR: {SAMPLE_RATE/1e6:.1f} MHz  ·  FFT: {FFT_SIZE}",
                 ha="center", va="center", fontsize=8, color=GRAY, transform=ax_info.transAxes)
    ax_time.axis("off")
    time_txt = ax_time.text(0.5, 0.5, "", ha="center", va="center",
                            fontsize=8, color=GRAY, transform=ax_time.transAxes)

    # Animatie — klassiek venster werkt volledig onafhankelijk van hoofdvenster
    def update(_):
        with det._lock:
            pwr   = det.power.copy()
            base  = det.baseline.copy() if det.baseline is not None else pwr.copy()
            freqs = det.freqs.copy()
            stat  = det.status
        thr = _cl_thr[0]
        hard_thr_cl = det.hard_threshold
        line_pwr.set_ydata(pwr); line_base.set_ydata(base)
        diff_arr = pwr - base
        ch_vals = []
        for cf in TETRA_FREQS:
            if freqs[0] <= cf <= freqs[-1]:
                idx = int(np.argmin(np.abs(freqs - cf)))
                ch_vals.append((float(diff_arr[idx]), cf))
        ch_vals.sort(key=lambda x: x[0], reverse=True)
        while len(ch_vals) < 3: ch_vals.append((0.0, 0.0))
        # Alarm berekend op eigen drempel (onafhankelijk van hoofdvenster)
        best_cl = ch_vals[0][0] if ch_vals else 0.0
        best_cf = ch_vals[0][1] if ch_vals else 0.0
        cl_alrm = best_cl > thr
        cl_red  = best_cl > hard_thr_cl
        for ch in range(3):
            db_val, freq_val = ch_vals[ch]
            db_cl = max(0.0, min(db_val, SEG_MAX))
            n_lit = int(db_cl / SEG_DB)
            for s in range(N_CL):
                _segs[ch][s].set_facecolor(seg_on(s) if s < n_lit else seg_off(s))
            if freq_val > 0:
                _mfreq[ch].set_text(f"{freq_val:.3f} MHz")
                _mfreq[ch].set_color(RED if db_val > thr else WHITE)
                _mdb[ch].set_text(f"+{max(0.0,db_val):.1f}")
                _mdb[ch].set_color(RED if db_val > thr else GREEN)
                _mdb[ch].set_y(db_cl + 0.5)
            else:
                _mfreq[ch].set_text("—"); _mfreq[ch].set_color(GRAY); _mdb[ch].set_text("")
        if cl_alrm:
            alrm_txt.set_text(f"⚠  TETRA  {best_cf:.3f} MHz  +{best_cl:.1f} dB  ⚠")
            alrm_txt.set_color(RED if cl_red else ORANGE)
            alrm_bg.set_facecolor(ALARM_BG)
        else:
            alrm_txt.set_text("● PRIOSENSE")
            alrm_txt.set_color(WHITE if "Scan" in stat else ORANGE)
            alrm_bg.set_facecolor(PANEL)
        stat_txt.set_text(stat)
        time_txt.set_text(datetime.now().strftime("%H:%M:%S") +
                          f"  |  {freqs[0]:.1f}–{freqs[-1]:.1f} MHz")
        thr_line.set_ydata([thr, thr])
        return []

    ani = _mpl_anim.FuncAnimation(fig, update, interval=500, blit=False, cache_frame_data=False)
    ani._fig = fig
    _CLASSIC_ANIS.append(ani)
    plt.show(block=False)
    return ani


# ══════════════════════════════════════════════════════════════════════════════
#  PyQt6 GUI
# ══════════════════════════════════════════════════════════════════════════════
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSlider, QFrame, QSizePolicy,
    QProgressBar, QDialog, QTabWidget, QStackedWidget, QComboBox, QLineEdit,
)
from PyQt6.QtCore import Qt, QTimer, QRectF, pyqtSignal, QSettings
from PyQt6.QtGui import (
    QPainter, QColor, QFont, QPainterPath, QTransform, QIcon, QPixmap, QPen,
)
import pyqtgraph as pg

# ── Kleurenpalet (Apple Dark) ─────────────────────────────────────────────────
C = {
    "bg":     "#1c1c1e",
    "panel":  "#2c2c2e",
    "panel2": "#3a3a3c",
    "sep":    "#38383a",
    "blue":   "#0a84ff",
    "green":  "#30d158",
    "yellow": "#ffd60a",
    "red":    "#ff453a",
    "orange": "#ff9f0a",
    "white":  "#ffffff",
    "gray1":  "#ebebf5",
    "gray2":  "#8e8e93",
    "gray3":  "#48484a",
}

def _qc(k): return QColor(C[k])

# ── Rijmodi ───────────────────────────────────────────────────────────────────
MODES = [
    {"name": "Stad",     "slot_floor": 22, "threshold": 32, "hard_threshold": 45, "hang_time": 5.0, "gain_db": 30},
    {"name": "Custom",   "slot_floor": 15, "threshold": 28, "hard_threshold": 40, "hang_time": 4.0, "gain_db": 40},
    {"name": "Snelweg",  "slot_floor": 10, "threshold": 25, "hard_threshold": 42, "hang_time": 3.0, "gain_db": 40},
]
MODE_COLORS = {"Stad": C["orange"], "Custom": C["blue"], "Snelweg": C["green"]}

# Segment kleuren (logical 0=groen-laag, 9=rood-hoog)
_SEG_ON  = [QColor("#30d158")] * 4 + [QColor("#ffd60a")] * 4 + [QColor("#ff453a")] * 2
_SEG_OFF = [QColor("#0a1f10")] * 4 + [QColor("#1f1a00")] * 4 + [QColor("#2d1110")] * 2


def _sys_font(size, bold=False):
    f = QFont()
    sys_name = platform.system()
    if sys_name == "Windows":
        f.setFamily("Segoe UI")
    elif sys_name == "Darwin":
        f.setFamily("SF Pro Display")
    else:
        f.setFamily("Ubuntu")
    f.setPointSize(size)
    if bold:
        f.setWeight(QFont.Weight.Bold)
    return f


def _slider_qss(color):
    return f"""
    QSlider::groove:horizontal {{
        height: 4px;
        background: {C['sep']};
        border-radius: 2px;
        margin: 0px;
    }}
    QSlider::sub-page:horizontal {{
        background: {color};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {color};
        width: 14px;
        height: 14px;
        margin: -5px 0px;
        border-radius: 7px;
    }}
    QSlider::handle:horizontal:hover {{
        background: white;
    }}
    """


# ── Global stylesheet ─────────────────────────────────────────────────────────
QSS = f"""
QMainWindow, QWidget {{
    background-color: {C['bg']};
    color: {C['gray1']};
}}
QPushButton {{
    background-color: {C['panel']};
    color: {C['gray2']};
    border: 1px solid {C['sep']};
    border-radius: 8px;
    padding: 7px 14px;
    font-size: 12px;
}}
QPushButton:hover {{
    background-color: {C['panel2']};
    color: {C['gray1']};
    border-color: {C['gray3']};
}}
QPushButton:pressed {{
    background-color: {C['gray3']};
    color: {C['white']};
}}
QLabel {{
    color: {C['gray2']};
    background: transparent;
}}
QFrame#divider {{
    background-color: {C['sep']};
    max-height: 1px;
    border: none;
}}
"""


# ── SignalBarsWidget ──────────────────────────────────────────────────────────
class SignalBarsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._slots          = [{"freq": None, "db": 0.0} for _ in range(3)]
        self._threshold      = float(THRESHOLD_SOFT)
        self._hard_threshold = float(THRESHOLD_SOFT) + 10.0
        self._slot_floor     = float(SLOT_FLOOR)
        self._trends         = [0, 0, 0]
        self._known_freqs    = set()
        self.setMinimumSize(260, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def update_data(self, slots, threshold, slot_floor=20.0, trends=None, hard_threshold=None, known_freqs=None):
        self._slots          = [dict(s) for s in slots]
        self._threshold      = float(threshold)
        self._slot_floor     = float(slot_floor)
        self._hard_threshold = float(hard_threshold) if hard_threshold else float(threshold) + 10.0
        self._trends         = trends or [0, 0, 0]
        self._known_freqs    = known_freqs or set()
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, _qc("panel"))

        TITLE_H = 28
        p.setFont(_sys_font(9, bold=True))
        p.setPen(_qc("gray2"))
        p.drawText(0, 0, W, TITLE_H,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                   "SIGNAALSTERKTE")

        LABEL_H  = 62
        bars_top = TITLE_H + 4
        bars_h   = H - bars_top - LABEL_H
        seg_gap  = 3
        pad_x    = 10
        pad_y    = 8

        section_w   = W / 3
        seg_w       = section_w * 0.52
        seg_h_total = (bars_h - 2 * pad_y - (N_SEGS - 1) * seg_gap) / N_SEGS
        seg_h       = max(4.0, seg_h_total - 1)

        green_range  = max(1.0, self._threshold      - self._slot_floor)
        yellow_range = max(1.0, self._hard_threshold - self._threshold)

        for bi in range(3):
            slot   = self._slots[bi]
            db_val = float(slot.get("db", 0.0))
            freq   = slot.get("freq", None)
            if db_val <= self._slot_floor:
                n_lit = 0
            elif db_val < self._threshold:
                # Groene zone: segs 1-4
                n_lit = max(1, min(4, int((db_val - self._slot_floor) / green_range * 4) + 1))
            elif db_val < self._hard_threshold:
                # Gele zone: segs 5-8
                n_lit = 4 + max(1, min(4, int((db_val - self._threshold) / yellow_range * 4) + 1))
            else:
                # Rode zone: segs 9-10
                n_lit = min(N_SEGS, 8 + max(1, min(2, int((db_val - self._hard_threshold) / 5) + 1)))

            cx      = section_w * bi + section_w / 2
            seg_x   = cx - seg_w / 2
            track_x = seg_x - pad_x
            track_w = seg_w + 2 * pad_x

            track_path = QPainterPath()
            track_path.addRoundedRect(QRectF(track_x, bars_top, track_w, bars_h), 10, 10)
            p.fillPath(track_path, _qc("panel2"))

            for vi in range(N_SEGS):
                li  = N_SEGS - 1 - vi
                lit = li < n_lit
                y   = bars_top + pad_y + vi * (seg_h_total + seg_gap)

                seg_rect = QRectF(seg_x, y, seg_w, seg_h)
                seg_path = QPainterPath()
                seg_path.addRoundedRect(seg_rect, 3, 3)

                if lit:
                    glow = QColor(_SEG_ON[li])
                    glow.setAlpha(55)
                    glow_path = QPainterPath()
                    glow_path.addRoundedRect(
                        QRectF(seg_x - 3, y - 1, seg_w + 6, seg_h + 2), 5, 5)
                    p.fillPath(glow_path, glow)
                    p.fillPath(seg_path, _SEG_ON[li])
                else:
                    p.fillPath(seg_path, _SEG_OFF[li])

            lx = int(track_x - pad_x)
            lw = int(track_w + 2 * pad_x)
            ly = H - LABEL_H + 4

            if freq is not None:
                if db_val >= self._hard_threshold:
                    fc = _qc("red")
                elif db_val > self._threshold:
                    fc = _qc("yellow")
                else:
                    fc = _qc("green")
                p.setFont(_sys_font(8, bold=True))
                p.setPen(fc)
                p.drawText(lx, ly, lw, 20,
                           int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                           f"{freq:.3f} MHz")
                p.setFont(_sys_font(7))
                p.setPen(_qc("gray2"))
                p.drawText(lx, ly + 21, lw, 18,
                           int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                           f"+{db_val:.0f} dB")
                # Richting indicator
                trend = self._trends[bi]
                if trend == 1:
                    arrow = "▲ Nadert"
                    ac    = _qc("green")
                elif trend == -1:
                    arrow = "▼ Rijdt weg"
                    ac    = _qc("orange")
                else:
                    arrow = "► Stabiel"
                    ac    = _qc("gray2")
                p.setFont(_sys_font(7, bold=True))
                p.setPen(ac)
                p.drawText(lx, ly + 39, lw, 16,
                           int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                           arrow)
                # ⚑ Bekend kanaal label
                if round(freq, 3) in getattr(self, '_known_freqs', set()):
                    p.setFont(_sys_font(7, bold=True))
                    p.setPen(QColor("#ffd60a"))
                    p.drawText(lx, ly + 55, lw, 16,
                               int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                               "⚑ Bekend kanaal")
            else:
                p.setFont(_sys_font(11, bold=True))
                p.setPen(_qc("gray3"))
                p.drawText(lx, ly, lw, 24,
                           int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                           "—")

        p.end()


# ── AlarmCard ─────────────────────────────────────────────────────────────────
class AlarmCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(90)
        self.setMaximumHeight(115)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(5)

        row = QHBoxLayout()
        self.dot   = QLabel("●")
        self.dot.setFont(_sys_font(20))
        self.title = QLabel("PrioSense")
        self.title.setFont(_sys_font(15, bold=True))
        row.addWidget(self.dot)
        row.addSpacing(8)
        row.addWidget(self.title)
        row.addStretch()
        layout.addLayout(row)

        self.detail = QLabel("—")
        self.detail.setFont(_sys_font(12))
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.detail)

        self._set("idle")

    def _set(self, mode, freq=0.0, db=0.0):
        styles = {
            "idle":   (C['panel'],  "1px solid " + C['sep'],    C['gray3'], C['gray1'], "—",                              C['gray3']),
            "orange": ("#2a1b00",   "2px solid " + C['orange'], C['orange'], C['white'], f"{freq:.3f} MHz  +{db:.1f} dB", C['orange']),
            "red":    ("#2d0b0a",   "2px solid " + C['red'],    C['red'],   C['white'], f"{freq:.3f} MHz  +{db:.1f} dB", C['red']),
        }
        bg, border, dot_col, title_col, detail_txt, detail_col = styles[mode]
        self.setStyleSheet(f"""
            AlarmCard {{
                background-color: {bg};
                border: {border};
                border-radius: 12px;
            }}
        """)
        self.dot.setStyleSheet(f"color: {dot_col}; background: transparent;")
        self.title.setStyleSheet(f"color: {title_col}; background: transparent;")
        self.detail.setText(detail_txt)
        self.detail.setStyleSheet(f"color: {detail_col}; background: transparent;")

    def set_idle(self):           self._set("idle")
    def set_orange(self, f, db): self._set("orange", f, db)
    def set_red(self, f, db):    self._set("red",    f, db)


# ── LabeledSlider ─────────────────────────────────────────────────────────────
class LabeledSlider(QWidget):
    valueChanged = pyqtSignal(float)

    def __init__(self, label, lo, hi, init, step=1.0, fmt="{:.0f}",
                 color=None, parent=None):
        super().__init__(parent)
        if color is None: color = C['blue']
        self._lo   = lo
        self._hi   = hi
        self._step = step
        self._fmt  = fmt
        n_steps    = round((hi - lo) / step)

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(4)

        hrow = QHBoxLayout()
        lbl  = QLabel(label)
        lbl.setFont(_sys_font(10))
        lbl.setStyleSheet(f"color: {C['gray2']};")
        self.val_lbl = QLabel(fmt.format(init))
        self.val_lbl.setFont(_sys_font(10))
        self.val_lbl.setStyleSheet(f"color: {C['gray1']};")
        self.val_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hrow.addWidget(lbl)
        hrow.addWidget(self.val_lbl)
        vbox.addLayout(hrow)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, n_steps)
        self.slider.setValue(round((init - lo) / step))
        self.slider.setStyleSheet(_slider_qss(color))
        self.slider.valueChanged.connect(self._emit)
        vbox.addWidget(self.slider)

    def _emit(self, step_val):
        val = self._lo + step_val * self._step
        self.val_lbl.setText(self._fmt.format(val))
        self.valueChanged.emit(val)

    def value(self):
        return self._lo + self.slider.value() * self._step


# ── WaterfallWindow ───────────────────────────────────────────────────────────
class WaterfallWindow(QMainWindow):
    def __init__(self, det, parent=None):
        super().__init__(parent)
        self.det = det
        self.setWindowTitle("PrioSense — Waterfall")
        self.setMinimumSize(820, 360)
        self.setStyleSheet(f"background-color: {C['bg']};")

        cw = QWidget()
        self.setCentralWidget(cw)
        lay = QVBoxLayout(cw)
        lay.setContentsMargins(8, 8, 8, 8)

        self.pw = pg.PlotWidget()
        self.pw.setBackground(C['panel'])
        self.pw.setLabel('bottom', 'MHz')
        self.pw.setLabel('left', 'Tijd (frames)')
        self.pw.getAxis('left').setTextPen(QColor(C['gray2']))
        self.pw.getAxis('bottom').setTextPen(QColor(C['gray2']))
        lay.addWidget(self.pw)

        with det._lock:
            data  = det.wfall.copy()
            freqs = det.freqs.copy()

        self.img = pg.ImageItem()
        self.pw.addItem(self.img)

        cmap = pg.colormap.get('inferno')
        self.img.setColorMap(cmap)
        self.img.setLevels((-80, -20))
        self._apply_transform(freqs, data.shape)
        self.img.setImage(data.T)
        self.pw.setXRange(freqs[0], freqs[-1])
        self.pw.setYRange(0, WFALL_ROWS)

        try:
            bar = pg.ColorBarItem(values=(-80, -20), colorMap=cmap, label='dBm')
            bar.setImageItem(self.img, insert_in=self.pw)
        except Exception:
            pass

    def _apply_transform(self, freqs, shape):
        sx = (freqs[-1] - freqs[0]) / shape[1]
        tr = QTransform()
        tr.translate(freqs[0], 0)
        tr.scale(sx, 1.0)
        self.img.setTransform(tr)

    def refresh(self, data, freqs):
        self._apply_transform(freqs, data.shape)
        self.img.setImage(data.T)


# ── RawDataWidget ─────────────────────────────────────────────────────────────
class RawDataWidget(QWidget):
    PEAK_HOLD_S = 3.0

    def __init__(self, det, parent=None):
        super().__init__(parent)
        self.det    = det
        self._peaks = {}   # freq → (peak_db, expire_time)
        self.setMinimumSize(380, 410)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, _qc("bg"))

        # Titel
        p.setFont(_sys_font(9, bold=True))
        p.setPen(_qc("gray2"))
        p.drawText(0, 2, W, 20,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                   "RAW DATA — PrioSense")

        with self.det._lock:
            freqs      = self.det.freqs.copy()
            slot_floor = self.det.slot_floor
            raw_peaks  = dict(self.det.raw_peaks)   # peak-hold van detector loop

        LABEL_W  = 56
        DB_W     = 36
        PAD_L    = 6
        BAR_X    = PAD_L + LABEL_W + 4
        BAR_MAX  = W - BAR_X - DB_W - 8
        DB_SCALE = 50.0
        ROW_H    = 19
        BAR_H    = 9
        start_y  = 26

        now = time.time()
        for i, cf in enumerate(TETRA_FREQS):
            pk_db, pk_exp = raw_peaks.get(cf, (0.0, 0.0))
            if now > pk_exp:
                pk_db = 0.0   # peak verlopen
            diff = pk_db      # huidige real-time diff niet apart beschikbaar, gebruik peak

            y       = start_y + i * ROW_H
            active  = pk_db > slot_floor
            bar_y   = y + (ROW_H - BAR_H) // 2
            bar_len = max(0, min(BAR_MAX, int(pk_db / DB_SCALE * BAR_MAX)))

            # Rij-achtergrond
            if active:
                p.fillRect(2, y, W - 4, ROW_H - 2, QColor(36, 38, 46))

            # Frequentie label
            p.setFont(_sys_font(7, bold=active))
            p.setPen(_qc("white") if active else _qc("gray3"))
            p.drawText(PAD_L, y, LABEL_W, ROW_H,
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       f"{cf:.3f}")

            # Balk achtergrond
            p.fillRect(BAR_X, bar_y, BAR_MAX, BAR_H, QColor("#18181c"))

            # Gekleurde balk (peak-hold waarde)
            if bar_len > 0:
                if pk_db < slot_floor:  bar_col = QColor(C['gray3'])
                elif pk_db < 25:        bar_col = QColor(C['green'])
                elif pk_db < 35:        bar_col = QColor(C['yellow'])
                else:                   bar_col = QColor(C['red'])
                p.fillRect(BAR_X, bar_y, bar_len, BAR_H, bar_col)

            # Slot_floor streepje (oranje)
            sf_x = BAR_X + int(slot_floor / DB_SCALE * BAR_MAX)
            p.setPen(QColor(C['orange']))
            p.drawLine(sf_x, y + 3, sf_x, y + ROW_H - 4)

            # dB waarde (toont peak)
            if active:
                if pk_db < 25:  dc = _qc("green")
                elif pk_db < 35: dc = _qc("yellow")
                else:           dc = _qc("red")
            else:
                dc = _qc("gray3")
            p.setFont(_sys_font(7))
            p.setPen(dc)
            sign = "+" if pk_db >= 0 else ""
            p.drawText(BAR_X + BAR_MAX + 4, y, DB_W, ROW_H,
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       f"{sign}{pk_db:.0f}")

        p.end()


# ── RawDataWindow ─────────────────────────────────────────────────────────────
class RawDataWindow(QMainWindow):
    def __init__(self, det, parent=None):
        super().__init__(parent)
        self.det = det
        self.setWindowTitle("Raw Data")
        self.setFixedSize(390, 450)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool)
        self.setStyleSheet(f"background-color: {C['bg']};")

        self._raw = RawDataWidget(det)
        self.setCentralWidget(self._raw)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._raw.update)
        self._timer.start(500)

    def closeEvent(self, event):
        self._timer.stop()
        event.accept()


# ── DetectionHistoryWidget ────────────────────────────────────────────────────
class DetectionHistoryWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries = []  # [(ts, freq, db, level)]
        self.setMinimumWidth(180)

    def add(self, freq, db, level):
        ts = datetime.now().strftime("%H:%M:%S")
        self._entries.insert(0, (ts, freq, db, level))
        if len(self._entries) > 25:
            self._entries.pop()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, _qc("panel"))
        p.setFont(_sys_font(8, bold=True))
        p.setPen(_qc("gray2"))
        p.drawText(0, 0, W, 22,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                   "DETECTIE GESCHIEDENIS")
        if not self._entries:
            p.setFont(_sys_font(9))
            p.setPen(_qc("gray3"))
            p.drawText(0, 24, W, H - 24, int(Qt.AlignmentFlag.AlignCenter),
                       "Geen detecties nog")
            p.end(); return
        ROW_H = 28
        y = 24
        for ts, freq, db, level in self._entries:
            if y + ROW_H > H: break
            bg = QColor("#2d0b0a") if level == 2 else QColor("#2d1a00") if level == 1 else _qc("panel2")
            p.fillRect(2, y, W - 4, ROW_H - 1, bg)
            dc = _qc("red") if level == 2 else _qc("orange")
            p.setPen(dc); p.setBrush(dc)
            p.drawEllipse(7, y + ROW_H // 2 - 4, 8, 8)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setFont(_sys_font(7)); p.setPen(_qc("gray2"))
            p.drawText(20, y, 54, ROW_H,
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), ts)
            p.setFont(_sys_font(8, bold=True)); p.setPen(_qc("white"))
            p.drawText(76, y, 95, ROW_H,
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       f"{freq:.3f} MHz")
            p.setPen(dc)
            p.drawText(W - 54, y, 48, ROW_H,
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                       f"+{db:.0f} dB")
            y += ROW_H
        p.end()


# ── WaterfallPanelWidget ──────────────────────────────────────────────────────
class WaterfallPanelWidget(QWidget):
    def __init__(self, det, parent=None):
        super().__init__(parent)
        self.det = det
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.pw = pg.PlotWidget()
        self.pw.setBackground(C['panel'])
        self.pw.setLabel('bottom', 'MHz')
        self.pw.getAxis('left').setTextPen(QColor(C['gray2']))
        self.pw.getAxis('bottom').setTextPen(QColor(C['gray2']))
        self.pw.setMouseEnabled(x=False, y=False)
        lay.addWidget(self.pw)
        with det._lock:
            data  = det.wfall.copy()
            freqs = det.freqs.copy()
        self.img = pg.ImageItem()
        self.pw.addItem(self.img)
        self.img.setColorMap(pg.colormap.get('inferno'))
        self.img.setImage(data.T)
        self._apply_transform(freqs, data.shape)

    def _apply_transform(self, freqs, shape):
        tr = QTransform()
        tr.translate(freqs[0], 0)
        tr.scale((freqs[-1] - freqs[0]) / shape[1], 1)
        self.img.setTransform(tr)
        self.pw.setXRange(freqs[0], freqs[-1])

    def refresh(self, data, freqs):
        self.img.setImage(data.T)
        self._apply_transform(freqs, data.shape)


# ── PanelSlot ─────────────────────────────────────────────────────────────────
class PanelSlot(QWidget):
    def __init__(self, names, widgets, initial_idx=0, parent=None):
        super().__init__(parent)
        self._names = names
        self._idx   = initial_idx % max(len(widgets), 1)
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)
        # Header
        hdr = QWidget()
        hdr.setFixedHeight(26)
        hdr.setStyleSheet(f"background-color: {C['panel2']};")
        hlay = QHBoxLayout(hdr)
        hlay.setContentsMargins(8, 0, 4, 0)
        self._title_lbl = QLabel(names[self._idx])
        self._title_lbl.setFont(_sys_font(8, bold=True))
        self._title_lbl.setStyleSheet(f"color: {C['gray2']};")
        hlay.addWidget(self._title_lbl)
        hlay.addStretch()
        if len(widgets) > 1:
            btn = QPushButton("⊞  Wissel")
            btn.setFixedHeight(20)
            btn.setFont(_sys_font(7))
            btn.setStyleSheet(f"""
                QPushButton {{ background:{C['panel']}; color:{C['gray2']};
                    border:1px solid {C['sep']}; border-radius:4px; padding:0 6px; }}
                QPushButton:hover {{ color:{C['white']}; }}
            """)
            btn.clicked.connect(self.cycle)
            hlay.addWidget(btn)
        vbox.addWidget(hdr)
        # Stack
        self._stack = QStackedWidget()
        for w in widgets:
            self._stack.addWidget(w)
        self._stack.setCurrentIndex(self._idx)
        vbox.addWidget(self._stack, stretch=1)

    def cycle(self):
        self._idx = (self._idx + 1) % self._stack.count()
        self._stack.setCurrentIndex(self._idx)
        if self._idx < len(self._names):
            self._title_lbl.setText(self._names[self._idx])

    def current_name(self):
        return self._names[self._idx] if self._idx < len(self._names) else ""


# ── CompactWindow ─────────────────────────────────────────────────────────────
class CompactWindow(QMainWindow):
    def __init__(self, det, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PrioSense")
        self.setFixedSize(300, 370)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet(QSS)
        cw = QWidget()
        self.setCentralWidget(cw)
        lay = QVBoxLayout(cw)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        self.alarm_card = AlarmCard()
        self.alarm_card.setMaximumHeight(80)
        lay.addWidget(self.alarm_card)
        self.bars = SignalBarsWidget()
        lay.addWidget(self.bars, stretch=1)
        hint = QLabel("Druk  C  voor volledig scherm")
        hint.setFont(_sys_font(7))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f"color: {C['gray3']};")
        lay.addWidget(hint)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_C:
            self.hide()
            if self.parent():
                self.parent().show()
                self.parent().activateWindow()


# ── TestResultDialog ──────────────────────────────────────────────────────────
class TestResultDialog(QDialog):
    def __init__(self, results, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Test Modus Resultaat")
        self.setFixedWidth(380)
        self.setStyleSheet(f"background-color: {C['bg']}; color: {C['white']};")

        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 20, 20, 20)

        # Titel
        title = QLabel("📡  Locatie Testrapport")
        title.setFont(_sys_font(14, bold=True))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)

        # Score groot
        score = results["score"]
        score_col = C["green"] if score >= 60 else C["orange"] if score >= 30 else C["red"]
        score_lbl = QLabel(f"{score}/100")
        score_lbl.setFont(_sys_font(42, bold=True))
        score_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        score_lbl.setStyleSheet(f"color: {score_col};")
        lay.addWidget(score_lbl)

        verdict = ("Uitstekend bereik 🟢" if score >= 60 else
                   "Matig bereik 🟡"      if score >= 30 else
                   "Slecht bereik 🔴")
        v_lbl = QLabel(verdict)
        v_lbl.setFont(_sys_font(11, bold=True))
        v_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v_lbl.setStyleSheet(f"color: {score_col};")
        lay.addWidget(v_lbl)

        # Scheidingslijn
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C['sep']};")
        lay.addWidget(sep)

        # Details
        def _row(label, value, color=C["gray1"]):
            row = QWidget()
            h = QHBoxLayout(row); h.setContentsMargins(0,0,0,0)
            lbl = QLabel(label); lbl.setFont(_sys_font(9))
            lbl.setStyleSheet(f"color: {C['gray2']};")
            val = QLabel(value); val.setFont(_sys_font(9, bold=True))
            val.setStyleSheet(f"color: {color};")
            val.setAlignment(Qt.AlignmentFlag.AlignRight)
            h.addWidget(lbl); h.addStretch(); h.addWidget(val)
            lay.addWidget(row)

        _row("Hoogste piek",       f"+{results['peak_max']:.0f} dB",  C["red"])
        _row("Gemiddelde piek",    f"+{results['peak_avg']:.1f} dB",  C["orange"])
        _row("Actieve kanalen",    str(results["active_ch"]))
        _row("Detecties",          str(results["detections"]))
        _row("Ruisvloer",          f"{results['noise_floor']:.1f} dB", C["gray2"])
        _row("Meetduur",           "60 seconden")

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C['sep']};")
        lay.addWidget(sep2)

        tip = QLabel("💡 Hogere score = betere locatie voor ontvangst")
        tip.setFont(_sys_font(8))
        tip.setStyleSheet(f"color: {C['gray2']};")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tip.setWordWrap(True)
        lay.addWidget(tip)

        btn = QPushButton("Sluiten")
        btn.clicked.connect(self.accept)
        lay.addWidget(btn)


# ── HulpdienstAlert ───────────────────────────────────────────────────────────
class HulpdienstAlert(QWidget):
    """Grote overlay die verschijnt als signaal > rood alarm."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(640, 220)
        self._adb  = 0.0
        self._afrq = 0.0
        self._flash = False
        self._flash_timer = QTimer(self)
        self._flash_timer.timeout.connect(self._do_flash)

    def show_alert(self, freq, db):
        self._afrq = freq
        self._adb  = db
        self._flash = True
        self._flash_timer.start(120)
        # Centreer boven het hoofdvenster
        if self.parent():
            pg = self.parent().geometry()
            x  = pg.x() + (pg.width()  - self.width())  // 2
            y  = pg.y() + (pg.height() - self.height()) // 2 - 40
            self.move(x, y)
        self.show()
        # Op Mac NIET naar voren halen — voorkomt fullscreen overname
        if sys.platform != "darwin":
            self.raise_()
        self.update()

    def update_alert(self, freq, db):
        self._afrq = freq
        self._adb  = db
        self.update()

    def hide_alert(self):
        self._flash_timer.stop()
        self.hide()

    def _do_flash(self):
        self._flash = not self._flash
        self.update()
        if not self._flash:
            self._flash_timer.stop()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        # Achtergrond
        bg = QColor("#cc0000") if self._flash else QColor("#8b0000")
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, W, H), 18, 18)
        p.fillPath(path, bg)

        # Rand
        p.setPen(QColor("#ff4444"))
        p.drawPath(path)

        # Emoji + hoofdtekst
        p.setPen(QColor("#ffffff"))
        p.setFont(_sys_font(38, bold=True))
        p.drawText(0, 10, W, 90,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                   "🚨  HULPDIENST GEDETECTEERD  🚨")

        # Frequentie
        p.setFont(_sys_font(20, bold=True))
        p.setPen(QColor("#ffcccc"))
        p.drawText(0, 100, W, 50,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                   f"{self._afrq:.3f} MHz")

        # dB waarde groot
        p.setFont(_sys_font(28, bold=True))
        p.setPen(QColor("#ffffff"))
        p.drawText(0, 148, W, 56,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                   f"+{self._adb:.0f} dB")
        p.end()


# ── MainWindow ────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self, det: TcpDetector):
        super().__init__()
        self.det        = det
        self._wfall_win   = None
        self._raw_win     = None
        self._hulp_alert  = HulpdienstAlert(self)
        self._slot_hist   = [deque(maxlen=10) for _ in range(3)]
        self._session_peaks  = []
        self._test_running   = False
        self._test_ticks     = 0
        self._test_data      = {"peaks": [], "channels": set(), "baselines": []}

        # Instellingen laden
        self._settings = QSettings("PrioSense", "PrioSense")
        def _load(key, default, cast=float, lo=None, hi=None):
            try:
                v = cast(self._settings.value(key, default))
                if lo is not None and v < lo: return cast(default)
                if hi is not None and v > hi: return cast(default)
                return v
            except Exception:
                return cast(default)
        det.threshold      = _load("threshold",      det.threshold,      lo=5,   hi=50)
        det.gain_db        = _load("gain_db",        det.gain_db,        lo=0,   hi=49)
        det.center_freq    = _load("center_freq",    det.center_freq,    cast=int, lo=375_000_000, hi=390_000_000)
        det.slot_floor     = _load("slot_floor",     det.slot_floor,     lo=0,   hi=40)
        det.hang_time      = _load("hang_time",      det.hang_time,      lo=0.5, hi=10)
        det.hard_threshold = _load("hard_threshold", det.hard_threshold, lo=10,  hi=60)
        det.auto_gain      = self._settings.value("auto_gain", "false") == "true"
        det.muted          = self._settings.value("muted",     "false") == "true"
        saved_mode         = _load("mode_idx", 1, cast=int, lo=0, hi=len(MODES)-1)
        # Custom modus waarden laden
        MODES[1]["slot_floor"]     = _load("custom_floor", MODES[1]["slot_floor"],     lo=0,   hi=40)
        MODES[1]["threshold"]      = _load("custom_thr",   MODES[1]["threshold"],      lo=5,   hi=50)
        MODES[1]["hard_threshold"] = _load("custom_hard",  MODES[1]["hard_threshold"], lo=10,  hi=60)
        MODES[1]["hang_time"]      = _load("custom_hang",  MODES[1]["hang_time"],      lo=0.5, hi=10)

        self.setWindowTitle("PrioSense")
        self.setMinimumSize(1100, 680)
        self.setStyleSheet(QSS)

        self._compact_win = CompactWindow(det, parent=self)

        cw = QWidget()
        self.setCentralWidget(cw)
        main_vbox = QVBoxLayout(cw)
        main_vbox.setContentsMargins(14, 10, 14, 6)
        main_vbox.setSpacing(6)

        # Titel
        title = QLabel("P R I O S E N S E")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(_sys_font(15, bold=True))
        title.setStyleSheet(f"color: {C['gray1']}; letter-spacing: 2px;")
        main_vbox.addWidget(title)

        # Content rij
        content = QWidget()
        content_hbox = QHBoxLayout(content)
        content_hbox.setContentsMargins(0, 0, 0, 0)
        content_hbox.setSpacing(14)
        main_vbox.addWidget(content, stretch=1)

        # ── Panel widgets aanmaken ────────────────────────────────────────────
        pg.setConfigOptions(antialias=True)
        self.spec = pg.PlotWidget()
        self.spec.setBackground(C['panel'])
        self.spec.showGrid(x=True, y=True, alpha=0.08)
        self.spec.setYRange(-90, -10)
        self.spec.setXRange(det.freqs[0], det.freqs[-1])
        self.spec.setLabel('left', 'dBm')
        self.spec.setLabel('bottom', 'MHz')
        self.spec.getAxis('left').setTextPen(QColor(C['gray2']))
        self.spec.getAxis('bottom').setTextPen(QColor(C['gray2']))
        self.spec.getAxis('left').setPen(QColor(C['sep']))
        self.spec.getAxis('bottom').setPen(QColor(C['sep']))
        self.spec.setMouseEnabled(x=False, y=False)
        for cf in TETRA_FREQS:
            self.spec.addItem(pg.InfiniteLine(pos=cf, angle=90,
                pen=pg.mkPen(color=(10, 132, 255, 38), width=0.8)))
        self.curve_pwr = self.spec.plot(det.freqs, det.power,
            pen=pg.mkPen(color='#0a84ff', width=1.8))
        base_pen = pg.mkPen(color='#ff9f0a', width=1.0)
        base_pen.setStyle(Qt.PenStyle.DashLine)
        self.curve_base = self.spec.plot(det.freqs, det.power, pen=base_pen)
        legend = self.spec.addLegend(offset=(-10, 10))
        legend.setLabelTextColor(C['gray2'])
        legend.addItem(self.curve_pwr, 'Vermogen')
        legend.addItem(self.curve_base, 'Baseline')

        self.wfall_panel = WaterfallPanelWidget(det)
        self.raw_panel   = RawDataWidget(det)
        self.bars        = SignalBarsWidget()
        self.hist_panel  = DetectionHistoryWidget()

        # ── Linker kolom: 2 panel slots ───────────────────────────────────────
        left  = QWidget()
        l_box = QVBoxLayout(left)
        l_box.setContentsMargins(0, 0, 0, 0)
        l_box.setSpacing(6)

        self.slot_top = PanelSlot(
            names   = ["Spectrum", "Waterfall", "Raw Data"],
            widgets = [self.spec, self.wfall_panel, self.raw_panel],
            initial_idx=0)
        self.slot_bot = PanelSlot(
            names   = ["Signaalbalken"],
            widgets = [self.bars],
            initial_idx=0)

        l_box.addWidget(self.slot_top, stretch=2)
        l_box.addWidget(self.slot_bot, stretch=3)
        content_hbox.addWidget(left, stretch=3)

        # ── Rechter kolom: tabs ───────────────────────────────────────────────
        self._tabs = QTabWidget()
        self._tabs.setFixedWidth(280)
        self._tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border: none; background: {C['panel']}; border-radius: 8px; }}
            QTabBar::tab {{ background: {C['panel2']}; color: {C['gray2']}; padding: 6px 14px;
                border-radius: 6px 6px 0 0; margin-right: 2px; }}
            QTabBar::tab:selected {{ background: {C['panel']}; color: {C['white']}; }}
        """)

        # Tab Monitor
        tab_mon = QWidget()
        tm_box  = QVBoxLayout(tab_mon)
        tm_box.setContentsMargins(8, 8, 8, 8)
        tm_box.setSpacing(8)
        self.alarm_card = AlarmCard()
        tm_box.addWidget(self.alarm_card)
        tm_box.addWidget(self._divider())
        self.score_lbl = QLabel("Sessie: 0 detecties")
        self.score_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.score_lbl.setFont(_sys_font(8))
        self.score_lbl.setStyleSheet(f"color: {C['gray2']};")
        tm_box.addWidget(self.score_lbl)
        tm_box.addWidget(self._divider())
        tm_box.addWidget(self.hist_panel, stretch=1)
        tm_box.addWidget(self._divider())
        self.btn_test = QPushButton("📡  Test Modus  (60s)")
        self.btn_test.clicked.connect(self._on_test)
        tm_box.addWidget(self.btn_test)
        self.test_bar = QProgressBar()
        self.test_bar.setRange(0, 120); self.test_bar.setValue(0)
        self.test_bar.setVisible(False)
        self.test_bar.setStyleSheet(f"""
            QProgressBar {{ background:{C['panel2']}; border-radius:4px; height:12px; }}
            QProgressBar::chunk {{ background:{C['blue']}; border-radius:4px; }}
        """)
        tm_box.addWidget(self.test_bar)
        tm_box.addStretch()
        self.stat_lbl = QLabel("Opstarten…")
        self.stat_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stat_lbl.setFont(_sys_font(9))
        tm_box.addWidget(self.stat_lbl)
        self.time_lbl = QLabel("")
        self.time_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_lbl.setFont(_sys_font(18, bold=True))
        self.time_lbl.setStyleSheet(f"color: {C['gray1']};")
        tm_box.addWidget(self.time_lbl)
        self._tabs.addTab(tab_mon, "Monitor")

        # Tab Instellingen
        tab_set = QWidget()
        ts_box  = QVBoxLayout(tab_set)
        ts_box.setContentsMargins(8, 8, 8, 8)
        ts_box.setSpacing(8)
        self.sl_thr = LabeledSlider("Drempel dB", 5, 50, det.threshold,
            step=1.0, color=C['red'])
        self.sl_thr.valueChanged.connect(lambda v: setattr(self.det, 'threshold', v))
        ts_box.addWidget(self._panel(self.sl_thr))
        self.sl_gain = LabeledSlider("Gain dB", 0, 49, det.gain_db,
            step=1.0, color=C['blue'])
        self.sl_gain.valueChanged.connect(self._on_gain)
        ts_box.addWidget(self._panel(self.sl_gain))
        self.sl_freq = LabeledSlider("Center MHz", 379.0, 386.0, det.center_freq / 1e6,
            step=0.05, fmt="{:.2f}", color=C['orange'])
        self.sl_freq.valueChanged.connect(self._on_freq)
        ts_box.addWidget(self._panel(self.sl_freq))

        # Verbindingsmodus dropdown
        conn_row = QWidget()
        conn_hlay = QHBoxLayout(conn_row)
        conn_hlay.setContentsMargins(0, 0, 0, 0)
        conn_hlay.setSpacing(6)
        conn_lbl = QLabel("Modus:")
        conn_lbl.setFont(_sys_font(9))
        conn_lbl.setStyleSheet(f"color:{C['gray2']};")
        conn_hlay.addWidget(conn_lbl)
        self._conn_combo = QComboBox()
        self._conn_combo.setFixedHeight(28)
        self._conn_combo.setFont(_sys_font(9))
        self._conn_combo.setStyleSheet(f"""
            QComboBox {{
                background:{C['panel2']}; color:{C['gray1']};
                border:1px solid {C['sep']}; border-radius:5px; padding:0 8px;
            }}
            QComboBox:hover {{ border-color:{C['blue']}; }}
            QComboBox QAbstractItemView {{
                background:{C['panel2']}; color:{C['white']};
                selection-background-color:{C['panel']};
            }}
        """)
        for mode_name in CONNECTION_MODES:
            self._conn_combo.addItem(mode_name)
        saved_conn = self._settings.value("connection_mode", "PC")
        if saved_conn in CONNECTION_MODES:
            self._conn_combo.setCurrentText(saved_conn)
            det.connection_mode = saved_conn
        self._conn_combo.currentTextChanged.connect(self._on_conn_mode)
        conn_hlay.addWidget(self._conn_combo, stretch=1)
        ts_box.addWidget(conn_row)

        # IP-adres invoer (alleen zichtbaar in Android modus)
        self._ip_row = QWidget()
        ip_hlay = QHBoxLayout(self._ip_row)
        ip_hlay.setContentsMargins(0, 0, 0, 0)
        ip_hlay.setSpacing(6)
        ip_lbl = QLabel("Telefoon IP:")
        ip_lbl.setFont(_sys_font(9))
        ip_lbl.setStyleSheet(f"color:{C['gray2']};")
        ip_hlay.addWidget(ip_lbl)
        self._ip_edit = QLineEdit(self._settings.value("android_host", "192.168.0.144"))
        self._ip_edit.setFixedHeight(28)
        self._ip_edit.setFont(_sys_font(9))
        self._ip_edit.setPlaceholderText("bijv. 192.168.0.144")
        self._ip_edit.setStyleSheet(f"""
            QLineEdit {{
                background:{C['panel2']}; color:{C['blue']};
                border:1px solid {C['blue']}; border-radius:5px; padding:0 8px;
            }}
        """)
        self._ip_edit.textChanged.connect(self._on_android_ip)
        ip_hlay.addWidget(self._ip_edit, stretch=1)
        ts_box.addWidget(self._ip_row)
        self._ip_row.setVisible(det.connection_mode == "Android")
        det.android_host = self._settings.value("android_host", "192.168.0.144")
        ts_box.addWidget(self._divider())

        # Band venster dropdown
        band_presets = [
            ("Volledig (382.0)",  382.0),
            ("Laag   380–382",    381.0),
            ("Midden 381–383",    382.0),
            ("Hoog   382–384",    383.0),
            ("Top    383–385",    384.0),
        ]
        self._band_combo = QComboBox()
        self._band_combo.setFixedHeight(28)
        self._band_combo.setFont(_sys_font(8))
        self._band_combo.setStyleSheet(f"""
            QComboBox {{
                background:{C['panel2']}; color:{C['gray2']};
                border:1px solid {C['sep']}; border-radius:5px; padding:0 8px;
            }}
            QComboBox:hover {{ border-color:{C['orange']}; color:{C['white']}; }}
            QComboBox QAbstractItemView {{
                background:{C['panel2']}; color:{C['white']};
                selection-background-color:{C['panel']};
            }}
        """)
        for label, _ in band_presets:
            self._band_combo.addItem(label)
        self._band_freqs = [mhz for _, mhz in band_presets]
        self._band_combo.currentIndexChanged.connect(self._on_band_select)
        ts_box.addWidget(self._band_combo)
        ts_box.addWidget(self._divider())
        self._mode_idx = saved_mode

        # Modus rij: naam knop + ⓘ info knop
        mode_row = QWidget()
        mode_hlay = QHBoxLayout(mode_row)
        mode_hlay.setContentsMargins(0, 0, 0, 0)
        mode_hlay.setSpacing(6)
        self.btn_mode = QPushButton(MODES[self._mode_idx]["name"])
        _mc = MODE_COLORS[MODES[self._mode_idx]["name"]]
        self.btn_mode.setStyleSheet(f"color:{_mc}; border-color:{_mc};")
        self.btn_mode.setMinimumHeight(34)
        self.btn_mode.clicked.connect(self._on_mode)
        mode_hlay.addWidget(self.btn_mode, stretch=1)
        self.btn_mode_info = QPushButton("ⓘ")
        self.btn_mode_info.setFixedSize(34, 34)
        self.btn_mode_info.setFont(_sys_font(14))
        self.btn_mode_info.setStyleSheet(f"color:{C['gray2']}; border-color:{C['sep']};")
        self.btn_mode_info.clicked.connect(self._show_mode_info)
        mode_hlay.addWidget(self.btn_mode_info)
        ts_box.addWidget(mode_row)

        # Custom modus sliders
        self._custom_section = QWidget()
        cs_box = QVBoxLayout(self._custom_section)
        cs_box.setContentsMargins(0, 0, 0, 0)
        cs_box.setSpacing(4)
        custom = MODES[1]  # Custom is altijd index 1
        self.sl_custom_floor = LabeledSlider("Custom vloer dB",   0,  40, custom["slot_floor"],     step=1.0, color=C['gray2'])
        self.sl_custom_thr   = LabeledSlider("Custom oranje dB",  5,  50, custom["threshold"],      step=1.0, color=C['orange'])
        self.sl_custom_hard  = LabeledSlider("Custom rood dB",   10,  60, custom["hard_threshold"], step=1.0, color=C['red'])
        self.sl_custom_hang  = LabeledSlider("Custom hang s",    0.5, 10, custom["hang_time"],      step=0.5, fmt="{:.1f}", color=C['blue'])
        self.sl_custom_floor.valueChanged.connect(lambda v: self._update_custom("slot_floor",     v))
        self.sl_custom_thr  .valueChanged.connect(lambda v: self._update_custom("threshold",      v))
        self.sl_custom_hard .valueChanged.connect(lambda v: self._update_custom("hard_threshold", v))
        self.sl_custom_hang .valueChanged.connect(lambda v: self._update_custom("hang_time",      v))
        for sl in [self.sl_custom_floor, self.sl_custom_thr, self.sl_custom_hard, self.sl_custom_hang]:
            cs_box.addWidget(self._panel(sl))
        self._custom_section.setVisible(self._mode_idx == 1)
        ts_box.addWidget(self._custom_section)
        self.btn_auto = QPushButton("Auto Gain  UIT")
        self.btn_auto.clicked.connect(self._on_auto)
        ts_box.addWidget(self.btn_auto)
        btn_reset = QPushButton("Reset Baseline  [R]")
        btn_reset.clicked.connect(det.reset_baseline)
        ts_box.addWidget(btn_reset)
        ts_box.addWidget(self._divider())
        btn_wfall = QPushButton("Waterfall venster")
        btn_wfall.clicked.connect(self._open_waterfall)
        ts_box.addWidget(btn_wfall)
        btn_raw = QPushButton("Raw Data venster")
        btn_raw.clicked.connect(self._open_raw)
        ts_box.addWidget(btn_raw)
        btn_klassiek = QPushButton("Klassiek")
        btn_klassiek.clicked.connect(self._open_klassiek)
        ts_box.addWidget(btn_klassiek)

        self.btn_debug = QPushButton("⏺  Debug Log: UIT")
        self.btn_debug.setMinimumHeight(34)
        self.btn_debug.setFont(_sys_font(9))
        self.btn_debug.setStyleSheet(f"color:{C['gray2']}; border-color:{C['sep']};")
        self.btn_debug.clicked.connect(self._toggle_debug)
        ts_box.addWidget(self.btn_debug)

        self.btn_agr = QPushButton("⚡  AGR: AAN")
        self.btn_agr.setMinimumHeight(34)
        self.btn_agr.setFont(_sys_font(9))
        self.btn_agr.setStyleSheet(f"color:{C['green']}; border-color:{C['green']};")
        self.btn_agr.clicked.connect(self._toggle_agr)
        ts_box.addWidget(self.btn_agr)
        ts_box.addStretch()
        hint = QLabel("R=Reset  P=Politie  M=Modus  C=Compact")
        hint.setFont(_sys_font(7))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f"color: {C['gray3']};")
        ts_box.addWidget(hint)
        self._tabs.addTab(tab_set, "Instellingen")

        content_hbox.addWidget(self._tabs)

        # ── Onderste balk ─────────────────────────────────────────────────────
        bottom = QWidget()
        bottom.setFixedHeight(56)
        bot_lay = QHBoxLayout(bottom)
        bot_lay.setContentsMargins(0, 4, 0, 0)
        bot_lay.setSpacing(10)

        self.btn_police = QPushButton("🚨  POLITIE GEZIEN  [P]")
        self.btn_police.setMinimumHeight(46)
        self.btn_police.setFont(_sys_font(13, bold=True))
        self.btn_police.setStyleSheet(f"""
            QPushButton {{ background-color:#2d0b0a; color:{C['red']};
                border:2px solid {C['red']}; border-radius:10px; }}
            QPushButton:hover {{ background-color:#4a1110; color:white; }}
            QPushButton:pressed {{ background-color:{C['red']}; color:white; }}
        """)
        self.btn_police.clicked.connect(self._on_police)
        bot_lay.addWidget(self.btn_police, stretch=3)

        btn_compact = QPushButton("⊡  Compact  [C]")
        btn_compact.setMinimumHeight(46)
        btn_compact.clicked.connect(self._toggle_compact)
        bot_lay.addWidget(btn_compact, stretch=1)

        btn_bars_fs = QPushButton("▦")
        btn_bars_fs.setMinimumHeight(46)
        btn_bars_fs.setFixedWidth(46)
        btn_bars_fs.setFont(_sys_font(16))
        btn_bars_fs.setToolTip("Volledig scherm balkjes")
        btn_bars_fs.clicked.connect(self._open_bars_fullscreen)
        bot_lay.addWidget(btn_bars_fs)

        self.btn_mute = QPushButton("🔊  Geluid")
        self.btn_mute.setMinimumHeight(46)
        self.btn_mute.clicked.connect(self._toggle_mute)
        bot_lay.addWidget(self.btn_mute, stretch=1)

        main_vbox.addWidget(bottom)

        # Mute knop juiste staat bij opstarten
        if det.muted:
            self.btn_mute.setText("🔇  Gedempt")
            self.btn_mute.setStyleSheet(f"color: {C['gray2']}; border-color: {C['gray2']};")

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

    @staticmethod
    def _divider():
        d = QFrame()
        d.setObjectName("divider")
        d.setFixedHeight(1)
        return d

    @staticmethod
    def _panel(widget):
        outer = QFrame()
        outer.setObjectName("controlPanel")
        outer.setStyleSheet(f"""
            QFrame#controlPanel {{
                background-color: {C['panel']};
                border: 1px solid {C['sep']};
                border-radius: 10px;
            }}
        """)
        lay = QVBoxLayout(outer)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.addWidget(widget)
        return outer

    def _mode_label(self, m):
        return m["name"]

    def _show_mode_info(self):
        m   = MODES[self._mode_idx]
        col = MODE_COLORS[m["name"]]
        msg = (f"<b style='color:{col}'>{m['name']}</b><br><br>"
               f"Vloer:&nbsp;&nbsp;&nbsp;&nbsp;<b>{m['slot_floor']} dB</b><br>"
               f"Oranje:&nbsp;&nbsp;<b>{m['threshold']} dB</b><br>"
               f"Rood:&nbsp;&nbsp;&nbsp;&nbsp;<b>{m['hard_threshold']} dB</b><br>"
               f"Hang:&nbsp;&nbsp;&nbsp;&nbsp;<b>{m['hang_time']:.1f} s</b>")
        from PyQt6.QtWidgets import QToolTip
        QToolTip.showText(self.btn_mode_info.mapToGlobal(
            self.btn_mode_info.rect().bottomLeft()), msg, self.btn_mode_info)

    def _update_custom(self, key, value):
        MODES[1][key] = value
        if self._mode_idx == 1:
            if key == "slot_floor":     self.det.slot_floor     = value
            elif key == "threshold":    self.det.threshold      = value
            elif key == "hard_threshold": self.det.hard_threshold = value
            elif key == "hang_time":    self.det.hang_time      = value

    def _on_mode(self):
        self._mode_idx = (self._mode_idx + 1) % len(MODES)
        m = MODES[self._mode_idx]
        self.det.threshold       = m["threshold"]
        self.det.hard_threshold  = m["hard_threshold"]
        self.det.slot_floor      = m["slot_floor"]
        self.det.hang_time       = m["hang_time"]
        self.det.mode_name       = m["name"]
        self.det.auto_gain       = False
        self.det.set_gain(m["gain_db"], auto=False)
        self.sl_thr.slider.setValue(
            round((m["threshold"] - self.sl_thr._lo) / self.sl_thr._step))
        self.sl_gain.slider.setValue(
            round((m["gain_db"] - self.sl_gain._lo) / self.sl_gain._step))
        self.btn_auto.setText("Auto Gain  UIT")
        self.btn_auto.setStyleSheet("")
        col = MODE_COLORS[m["name"]]
        self.btn_mode.setText(m["name"])
        self.btn_mode.setStyleSheet(f"color:{col}; border-color:{col};")
        self.btn_mode_info.setStyleSheet(f"color:{col}; border-color:{col};")
        self._custom_section.setVisible(self._mode_idx == 1)

    def _on_police(self):
        self.det._log_police()
        self._beep_police()
        # Frequentie geheugen — sla sterkste freq van laatste 2 min op
        best = self.det.get_best_freq_last_2min()
        if best is not None:
            self.det.save_known_freq(best)

    def _beep_police(self):
        """Knipperende knop + beep als bevestiging."""
        original_style = self.btn_police.styleSheet()
        self.btn_police.setStyleSheet(f"""
            QPushButton {{
                background-color: {C['red']};
                color: white;
                border: 2px solid {C['red']};
                border-radius: 10px;
            }}
        """)
        threading.Thread(target=_play_beep, daemon=True).start()
        QTimer.singleShot(400, lambda: self.btn_police.setStyleSheet(original_style))

    def _on_gain(self, v):
        self.det.auto_gain = False
        self.det.set_gain(v, auto=False)
        self.btn_auto.setText("Auto Gain  UIT")
        self.btn_auto.setStyleSheet("")

    def _on_freq(self, v):
        self.det.set_center_freq(v)
        new_f = self.det._calc_freqs(int(round(v * 1e6)))
        self.curve_pwr.setData(new_f, self.det.power)
        self.curve_base.setData(new_f, self.det.power)
        self.spec.setXRange(new_f[0], new_f[-1])

    def _on_band_select(self, idx):
        mhz = self._band_freqs[idx]
        self.sl_freq.setValue(mhz)

    def _on_conn_mode(self, mode_name):
        self.det.connection_mode = mode_name
        self._settings.setValue("connection_mode", mode_name)
        self._ip_row.setVisible(mode_name == "Android")
        color = C['blue'] if mode_name == "Android" else C['green']
        self._conn_combo.setStyleSheet(f"""
            QComboBox {{
                background:{C['panel2']}; color:{color};
                border:1px solid {color}; border-radius:5px; padding:0 8px;
            }}
            QComboBox QAbstractItemView {{
                background:{C['panel2']}; color:{C['white']};
                selection-background-color:{C['panel']};
            }}
        """)

    def _on_android_ip(self, text):
        self.det.android_host = text.strip()
        self._settings.setValue("android_host", text.strip())

    def _on_auto(self):
        self.det.auto_gain = not self.det.auto_gain
        self.det.set_gain(self.det.gain_db, auto=self.det.auto_gain)
        if self.det.auto_gain:
            self.btn_auto.setText("Auto Gain  AAN")
            self.btn_auto.setStyleSheet(
                f"color: {C['blue']}; border-color: {C['blue']};")
        else:
            self.btn_auto.setText("Auto Gain  UIT")
            self.btn_auto.setStyleSheet("")

    def _on_test(self):
        if self._test_running:
            return
        self._test_running = True
        self._test_ticks   = 0
        self._test_data    = {"peaks": [], "channels": set(), "baselines": []}
        self.btn_test.setText("Meten…")
        self.btn_test.setEnabled(False)
        self.test_bar.setValue(0)
        self.test_bar.setVisible(True)

    def _finish_test(self):
        self._test_running = False
        self.btn_test.setText("📡  Test Modus  (60s)")
        self.btn_test.setEnabled(True)
        self.test_bar.setVisible(False)

        peaks   = self._test_data["peaks"]
        pk_max  = max(peaks) if peaks else 0.0
        pk_avg  = sum(peaks) / len(peaks) if peaks else 0.0
        n_ch    = len(self._test_data["channels"])
        n_det   = len(peaks)
        bls     = self._test_data["baselines"]
        noise   = sum(bls) / len(bls) if bls else -80.0

        # Score: 0-100
        score_pk  = min(40, pk_avg * 0.8)
        score_ch  = min(30, n_ch * 1.5)
        score_det = min(30, n_det * 0.5)
        score     = int(score_pk + score_ch + score_det)

        results = {
            "score":       score,
            "peak_max":    pk_max,
            "peak_avg":    pk_avg,
            "active_ch":   n_ch,
            "detections":  n_det,
            "noise_floor": noise,
        }
        dlg = TestResultDialog(results, parent=self)
        dlg.exec()

    def keyPressEvent(self, event):
        k = event.key()
        if k == Qt.Key.Key_R:
            self.det.reset_baseline()
        elif k == Qt.Key.Key_P:
            self._on_police()
        elif k == Qt.Key.Key_M:
            self._on_mode()
        elif k == Qt.Key.Key_C:
            self._toggle_compact()

    def _toggle_mute(self):
        self.det.muted = not self.det.muted
        if self.det.muted:
            self.btn_mute.setText("🔇  Gedempt")
            self.btn_mute.setStyleSheet(f"color: {C['gray2']}; border-color: {C['gray2']};")
        else:
            self.btn_mute.setText("🔊  Geluid")
            self.btn_mute.setStyleSheet("")

    def _toggle_compact(self):
        if self._compact_win.isVisible():
            self._compact_win.hide()
            self.show()
            self.activateWindow()
        else:
            self._compact_win.show()
            self._compact_win.activateWindow()
            self.hide()

    def _open_klassiek(self):
        open_classic_view(self.det)

    def _toggle_agr(self):
        self.det.agr_enabled = not self.det.agr_enabled
        if self.det.agr_enabled:
            self.btn_agr.setText("⚡  AGR: AAN")
            self.btn_agr.setStyleSheet(f"color:{C['green']}; border-color:{C['green']};")
        else:
            self.det.agr_active = False
            self.btn_agr.setText("⚡  AGR: UIT")
            self.btn_agr.setStyleSheet(f"color:{C['gray2']}; border-color:{C['sep']};")

    def _open_bars_fullscreen(self):
        if not hasattr(self, '_bars_fs_win') or self._bars_fs_win is None:
            self._bars_fs_win = BarFullscreenWindow(self.det)
        if self._bars_fs_win.isVisible():
            self._bars_fs_win.hide()
        else:
            self._bars_fs_win.showFullScreen()

    def _toggle_debug(self):
        self.det.debug_logging = not self.det.debug_logging
        if self.det.debug_logging:
            self.btn_debug.setText("⏺  Debug Log: AAN")
            self.btn_debug.setStyleSheet(f"color:{C['red']}; border-color:{C['red']};")
        else:
            self.btn_debug.setText("⏺  Debug Log: UIT")
            self.btn_debug.setStyleSheet(f"color:{C['gray2']}; border-color:{C['sep']};")

    def _open_raw(self):
        if self._raw_win is not None and self._raw_win.isVisible():
            self._raw_win.raise_()
            return
        self._raw_win = RawDataWindow(self.det, parent=self)
        self._raw_win.show()

    def _open_waterfall(self):
        if self._wfall_win is not None and self._wfall_win.isVisible():
            self._wfall_win.raise_()
            return
        self._wfall_win = WaterfallWindow(self.det, parent=self)
        self._wfall_win.show()

    def _tick(self):
        try:
            self._tick_inner()
        except Exception as e:
            import traceback
            print(f"[TICK FOUT] {e}")
            traceback.print_exc()

    def _tick_inner(self):
        want_wfall = ((self._wfall_win is not None and self._wfall_win.isVisible()) or
                      self.slot_top.current_name() == "Waterfall")
        with self.det._lock:
            pwr        = self.det.power.copy()
            base       = (self.det.baseline.copy()
                          if self.det.baseline is not None else pwr.copy())
            freqs      = self.det.freqs.copy()
            alarm      = self.det.alarm
            alvl       = self.det.alarm_level
            afrq       = self.det.alarm_freq
            adb        = self.det.alarm_db
            stat       = self.det.status
            slots_snap = [dict(s) for s in self.det.slots]
            wfall_data = self.det.wfall.copy() if want_wfall else None
            agr_active = self.det.agr_active
            gain_now   = self.det.gain_db
            known_freqs = self.det.known_freqs

        self.curve_pwr.setData(freqs, pwr)
        self.curve_base.setData(freqs, base)

        # Trend berekenen per slot
        trends = []
        for i, slot in enumerate(slots_snap):
            db = float(slot.get("db", 0.0))
            self._slot_hist[i].append(db)
            h = list(self._slot_hist[i])
            if len(h) >= 6 and slot.get("freq") is not None:
                recent = sum(h[-3:]) / 3
                older  = sum(h[:3])  / 3
                diff_t = recent - older
                if diff_t > 3:    trends.append(1)
                elif diff_t < -3: trends.append(-1)
                else:             trends.append(0)
            else:
                trends.append(0)

        self.bars.update_data(slots_snap, self.det.threshold, self.det.slot_floor, trends,
                              hard_threshold=self.det.hard_threshold,
                              known_freqs=known_freqs)

        # AGR badge + live gain
        if agr_active:
            self.btn_agr.setText(f"⚡  AGR: AAN  ({gain_now:.0f} dB)")
            self.btn_agr.setStyleSheet(f"color:{C['orange']}; border-color:{C['orange']};")
        elif self.det.agr_enabled:
            self.btn_agr.setText(f"⚡  AGR: AAN  ({gain_now:.0f} dB)")
            self.btn_agr.setStyleSheet(f"color:{C['green']}; border-color:{C['green']};")
        else:
            self.btn_agr.setText(f"⚡  AGR: UIT  ({gain_now:.0f} dB)")
            self.btn_agr.setStyleSheet(f"color:{C['gray2']}; border-color:{C['sep']};")

        # Fullscreen balkjes updaten
        if hasattr(self, '_bars_fs_win') and self._bars_fs_win and self._bars_fs_win.isVisible():
            self._bars_fs_win.bars.update_data(slots_snap, self.det.threshold,
                                               self.det.slot_floor, trends,
                                               hard_threshold=self.det.hard_threshold,
                                               known_freqs=known_freqs)

        if not alarm:
            self.alarm_card.set_idle()
            self._compact_win.alarm_card.set_idle()
            self._hulp_alert.hide_alert()
        elif alvl == 2:
            self.alarm_card.set_red(afrq, adb)
            self._compact_win.alarm_card.set_red(afrq, adb)
            if not self._hulp_alert.isVisible():
                self._hulp_alert.show_alert(afrq, adb)
                # Windows toast als geminimaliseerd
                if self.isMinimized() and sys.platform == "win32":
                    _show_toast("PrioSense 🚨", f"Hulpdienst: {afrq:.3f} MHz  +{adb:.0f} dB")
            else:
                self._hulp_alert.update_alert(afrq, adb)
        else:
            self.alarm_card.set_orange(afrq, adb)
            self._compact_win.alarm_card.set_orange(afrq, adb)
            self._hulp_alert.hide_alert()

        # Detectie geschiedenis bijwerken bij nieuwe alarm
        if alarm and adb > 0:
            prev = getattr(self, '_last_hist_alarm', False)
            if not prev:
                self.hist_panel.add(afrq, adb, alvl)
        self._last_hist_alarm = alarm

        # Compact venster balkjes
        if self._compact_win.isVisible():
            self._compact_win.bars.update_data(
                slots_snap, self.det.threshold, self.det.slot_floor, trends,
                hard_threshold=self.det.hard_threshold)

        # Waterfall panel updaten als zichtbaar
        top_name = self.slot_top.current_name()
        if top_name == "Waterfall" and wfall_data is not None:
            self.wfall_panel.refresh(wfall_data, freqs)

        # Raw data panel updaten
        if top_name == "Raw Data":
            self.raw_panel.update()

        # Sessie score bijwerken
        if alarm and adb > 0:
            self._session_peaks.append(adb)
        n  = len(self._session_peaks)
        avg = sum(self._session_peaks) / n if n else 0
        self.score_lbl.setText(
            f"Sessie: {n} detecties  ·  gem. +{avg:.0f} dB" if n
            else "Sessie: geen detecties nog")

        # Test modus data verzamelen
        if self._test_running:
            self._test_ticks += 1
            self.test_bar.setValue(self._test_ticks)
            with self.det._lock:
                raw_peaks = dict(self.det.raw_peaks)
                bl = self.det.baseline.copy() if self.det.baseline is not None else None
            for cf, (pk, _) in raw_peaks.items():
                if pk > self.det.slot_floor:
                    self._test_data["peaks"].append(pk)
                    self._test_data["channels"].add(cf)
            if bl is not None:
                self._test_data["baselines"].append(float(np.mean(bl)))
            if self._test_ticks >= 120:
                self._finish_test()

        self.stat_lbl.setText(stat)
        self.time_lbl.setText(datetime.now().strftime("%H:%M:%S"))

        if wfall_data is not None and self._wfall_win is not None and self._wfall_win.isVisible():
            self._wfall_win.refresh(wfall_data, freqs)

    def closeEvent(self, event):
        # Instellingen opslaan
        self._settings.setValue("threshold",   self.det.threshold)
        self._settings.setValue("gain_db",     self.det.gain_db)
        self._settings.setValue("auto_gain",   str(self.det.auto_gain).lower())
        self._settings.setValue("center_freq", self.det.center_freq)
        self._settings.setValue("slot_floor",  self.det.slot_floor)
        self._settings.setValue("hang_time",   self.det.hang_time)
        self._settings.setValue("mode_idx",       self._mode_idx)
        self._settings.setValue("hard_threshold", self.det.hard_threshold)
        self._settings.setValue("muted",          str(self.det.muted).lower())
        self._settings.setValue("custom_floor",   MODES[1]["slot_floor"])
        self._settings.setValue("custom_thr",     MODES[1]["threshold"])
        self._settings.setValue("custom_hard",    MODES[1]["hard_threshold"])
        self._settings.setValue("custom_hang",    MODES[1]["hang_time"])
        self._settings.setValue("connection_mode", self.det.connection_mode)
        self._settings.setValue("android_host",    self.det.android_host)
        self._timer.stop()
        self.det.stop()
        if self._wfall_win:
            self._wfall_win.close()
        if self._raw_win:
            self._raw_win.close()
        event.accept()


# ── Windows Toast notificatie ─────────────────────────────────────────────────
def _show_toast(title, message):
    """Toon een Windows toast notificatie via PowerShell."""
    if sys.platform != "win32":
        return
    try:
        ps = f"""
$app = 'PrioSense'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($t)
$xml.GetElementsByTagName('text')[0].InnerText = '{title}'
$xml.GetElementsByTagName('text')[1].InnerText = '{message}'
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
"""
        subprocess.Popen(
            ['powershell', '-WindowStyle', 'Hidden', '-Command', ps],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
    except Exception:
        pass


# ── BarFullscreenWindow ───────────────────────────────────────────────────────
class BarFullscreenWindow(QWidget):
    def __init__(self, det):
        super().__init__(None)
        self.det = det
        self.setWindowTitle("PrioSense — Balkjes")
        self.setStyleSheet(QSS)
        self.setWindowFlags(Qt.WindowType.Window)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        self.bars = SignalBarsWidget()
        lay.addWidget(self.bars)
        hint = QLabel("Druk ESC om te sluiten")
        hint.setFont(_sys_font(8))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f"color:{C['gray3']};")
        lay.addWidget(hint)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()


# ── App icoon ─────────────────────────────────────────────────────────────────
def _make_icon():
    """Laad of genereer PrioSense icoon."""
    # Probeer eerst het ingebundelde .ico bestand te laden
    if getattr(sys, 'frozen', False):
        ico = os.path.join(sys._MEIPASS, 'priosense.ico')
    else:
        ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'priosense.ico')
    if os.path.exists(ico):
        return QIcon(ico)

    """Genereer PrioSense icoon: donkere achtergrond + signaalcirkels."""
    sz = 256
    px = QPixmap(sz, sz)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Achtergrond — afgerond vierkant
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(0, 0, sz, sz), 54, 54)
    p.fillPath(bg, QColor("#1c1c1e"))

    # Signaalcirkels (3 bogen van klein naar groot)
    cx, cy = sz * 0.5, sz * 0.62
    for i, r in enumerate([38, 68, 98]):
        alpha = 255 - i * 55
        col = QColor("#0a84ff"); col.setAlpha(alpha)
        pen = QPen(col, 14 - i * 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), 25 * 16, 130 * 16)

    # Middelpunt stip
    p.setBrush(QColor("#0a84ff"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QRectF(cx - 10, cy - 10, 20, 20))

    # "PS" tekst
    p.setPen(QColor("#ffffff"))
    f = QFont("Arial", 52, QFont.Weight.Bold)
    p.setFont(f)
    p.drawText(QRectF(0, 18, sz, 80),
               int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
               "PS")

    p.end()

    # Opslaan als .ico naast het script/exe
    ico_path = os.path.join(os.path.dirname(
        sys.executable if getattr(sys, 'frozen', False)
        else os.path.abspath(__file__)), "priosense.ico")
    try: px.save(ico_path, "ICO")
    except Exception: pass

    return QIcon(px)


# ── Opstarten ─────────────────────────────────────────────────────────────────
def run():
    # PyInstaller splash sluiten zodra Python geladen is
    try:
        import pyi_splash
        pyi_splash.close()
    except ImportError:
        pass

    # Windows taakbalk icoon instellen
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PrioSense.C2000.1")

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(QSS)
    icon = _make_icon()
    app.setWindowIcon(icon)

    det = TcpDetector()
    try:
        det.start()
    except RuntimeError as e:
        from PyQt6.QtWidgets import QMessageBox
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Critical)
        msg.setWindowTitle("Verbindingsfout")
        msg.setText(str(e))
        msg.exec()
        sys.exit(1)

    win = MainWindow(det)
    win.setWindowIcon(icon)
    win.show()

    # Windows taakbalk icoon via ExtractIconW direct uit de exe
    if sys.platform == "win32":
        try:
            import ctypes
            WM_SETICON   = 0x0080
            ICON_SMALL2  = 2
            hwnd = int(win.winId())
            exe  = sys.executable
            hicon = ctypes.windll.shell32.ExtractIconW(0, exe, 0)
            if hicon and hicon != 1:
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL2, hicon)
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 1, hicon)
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 0, hicon)
        except Exception as e:
            print(f"[ICOON] {e}")

    sys.exit(app.exec())


if __name__ == "__main__":
    run()
