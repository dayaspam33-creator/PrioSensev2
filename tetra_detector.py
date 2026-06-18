#!/usr/bin/env python3
"""
PrioSense v1
PyQt6 · Dark UI · 3 signaalbalken · Slot-tracking · Auto-reconnect
"""

from collections import deque
import numpy as np
import socket, struct, subprocess, threading, time, sys, os, wave, math, platform
from datetime import datetime

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

# ── Instellingen ──────────────────────────────────────────────────────────────
DEFAULT_CENTER   = 382_500_000
SAMPLE_RATE      = 3_200_000
FFT_SIZE         = 4096        # 0.78 kHz/bin — fijne resolutie + ~6 dB processing gain
WFALL_ROWS       = 100
# Detectie (overgenomen van tetra-monitor): per kanaal energie-integratie over
# 25 kHz + CFAR (lokale ruis uit buurkanalen) i.p.v. dB-middeling vs baseline.
CHANNEL_KHZ      = 25.0        # TETRA-kanaalraster
CFAR_HALF_CHANS  = 12          # buurkanalen voor lokale ruisschatting (mediaan)
CHAN_SMOOTH_A    = 0.20        # tijdmiddeling energie per kanaal
PEAK_TAU         = 0.3         # s — nahang van de detectiepiek (korte bursts)
DC_NULL_BINS     = 2           # ± bins rond center dempen (DC/LO-lek)
OCC_PEAK_FRAC    = 0.40        # 1 bin > 40% kanaalenergie = smalle storing (birdie)
THRESHOLD_SOFT   = 30
THRESHOLD_HARD   = 10
GAIN_DB          = 40
TCP_HOST         = "127.0.0.1"
TCP_PORT         = 1234   # kan via --port worden overschreven
DEVICE_IDX       = 0      # kan via --device worden overschreven
EXTERN_RTLTCP    = False  # via --extern: zelf geen rtl_tcp starten
TITLE_SUFFIX     = ""     # via --titel: toevoeging aan venstertitel
SETTINGS_APP     = "PrioSense"  # aparte opslag per instantie mogelijk
TILE             = None   # via --tile L/M/R: vensterhelft op het scherm

LOG_COOLDOWN     = 10.0
N_SEGS           = 10
DB_PER_BLOCK     = 4.5   # voor absolute schaal (niet meer gebruikt in bars)
DB_PER_BLOCK_REL = 2.8   # relatieve schaal: 25 dB boven slot_floor = 10 segmenten
HANG_TIME        = 4.0
N_SMOOTH         = 15
DECAY_DB_S       = 9.0
BASELINE_FREEZE  = 20.0   # vaste freeze-grens, los van drempel
SLOT_FLOOR       = 20.0   # minimum dB om in balk te tonen, los van drempel

# Wanted-level ("gezocht-niveau", GTA-stijl): heat bouwt op met sterke detecties
# en koelt af bij rust. 5 sterren = heat 50.
HEAT_PER_STAR    = 10.0    # heat per ster
HEAT_MAX         = 52.0    # plafond
HEAT_GAIN        = 1.2     # heat per dB dat een detectie boven zijn event-piek komt
HEAT_COOLDOWN    = HEAT_PER_STAR / 30.0   # afkoeling: ~1 ster per 30 s

_SEARCH_PATHS = [
    "/usr/local/bin/rtl_tcp",
    "/opt/homebrew/bin/rtl_tcp",
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

# Volledig TETRA 25 kHz raster over uplink (380-385) én downlink (390-395 MHz).
# Detectie skipt automatisch kanalen buiten het zichtbare venster.
TETRA_FREQS = [round(380.0 + i * 0.025, 3) for i in range(int((395.0 - 380.0) / 0.025) + 1)]

# Lichte rasterlijnen voor de spectrumweergave (elke 0.25 MHz, anders te druk).
DISPLAY_GRID = [round(380.0 + i * 0.25, 2) for i in range(int((395.0 - 380.0) / 0.25) + 1)]

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
        # Blackman-window: betere zijlob-onderdrukking dan Hann → minder lekkage
        self._window       = np.blackman(FFT_SIZE).astype(np.float32)
        self._win_norm     = float(np.sum(self._window))
        self.baseline      = None
        # CFAR-detectie state (energie per kanaal)
        self._dc_bin       = FFT_SIZE // 2
        self.ch_avg        = None    # tijdgemiddelde energie per kanaal
        self.ch_peak       = None    # piek-hold energie per kanaal
        self.noise_floor   = -80.0   # weergavelijn (percentiel spectrum)
        self._last_frame_t = None
        self._chan_active  = {}      # cf → tijdstip continu actief (blacklist)
        self._chan_quiet   = {}      # cf → tijdstip stil
        self._chan_black   = set()   # geblacklistte kanalen (constante storing)
        self.clip_peak     = 0.0     # ruwe IQ-piek (1.0 = clipping)
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
        # Live sterkste kanaal (zonder piek-hold) — voor de meter/balk-weergave
        self.live_db   = 0.0
        self.live_freq = None
        # Wanted-level (gezocht-niveau): heat + sterren
        self.wanted_heat  = 0.0
        self.wanted_stars = 0
        self._evt_peak    = None
        self._alarm_cleared_at = 0.0
        self._ch_history  = {}
        self.raw_peaks    = {}
        self.debug_logging    = False
        self._last_debug_log  = 0.0
        self._dbg_peak        = {}    # piek per kanaal sinds laatste debug-schrijf
        # AGR
        self.agr_enabled      = True
        self.agr_active       = False
        self._agr_orig_gain   = None
        self._agr_clear_time  = 0.0
        # Bezettingscheck — onderdrukt smalle birdies/storing
        self.occupancy_check  = True
        # Patroonherkenning — bursts per kanaal tellen (activiteitsniveau)
        self._ch_above        = set()   # kanalen die nu boven de vloer zitten
        self._burst_times     = {}      # cf → deque met burst-tijdstempels
        self.alarm_activity   = 0       # aantal bursts laatste 10s op alarm-kanaal
        # Adaptief storingsfilter — onderdrukt kanalen die te lang onafgebroken
        # hoog staan (= storing), past zich aan tijdens het rijden
        self.adaptive_filter      = False
        self._hot_since           = {}    # freq → tijdstip dat kanaal continu hoog werd
        self._suppressed          = set() # freqs die nu als storing onderdrukt worden
        self.interference_secs    = 25.0  # na X sec onafgebroken hoog → storing
        # Waterfall-kleurschaal bovengrens (lager = zwakke signalen feller)
        self.wfall_max            = -20.0
        # Bekende kanalen (vaste politiekanalen) — krijgen ster + aparte kleur
        self.known_channels       = {382.900}
        # Breedte (kHz) van het huidige alarm-signaal
        self.alarm_width          = 0.0

    def _calc_freqs(self, center_hz):
        f = np.linspace((center_hz - SAMPLE_RATE/2) / 1e6,
                        (center_hz + SAMPLE_RATE/2) / 1e6, FFT_SIZE)
        # MHz per bin — voor snelle index-berekening i.p.v. argmin
        self._bin_mhz = (f[-1] - f[0]) / (FFT_SIZE - 1)
        # Aantal bins in een 25 kHz TETRA-kanaal (afhankelijk van FFT/sample rate)
        self._ch_bins     = max(2, int(round(0.025 / self._bin_mhz)))
        self._ch_halfbins = max(1, self._ch_bins // 2)
        # Kanaal-index map opbouwen: per 25 kHz-kanaal de bin-indices (voor
        # energie-integratie). Eén keer per afstemming berekend.
        step = CHANNEL_KHZ / 1000.0; half = step / 2.0
        start = math.ceil(f[0] / step) * step
        self._chan_idx = []
        for cf in np.arange(start, f[-1], step):
            idx = np.where((f >= cf - half) & (f < cf + half))[0]
            if idx.size:
                self._chan_idx.append((round(float(cf), 3), idx))
        # Vlakke index + segment-offsets voor snelle energie (np.add.reduceat
        # i.p.v. een Python-lus per kanaal → veel sneller, élk frame haalbaar).
        flat, segs, off = [], [], 0
        for _, idx in self._chan_idx:
            segs.append(off); flat.append(idx); off += len(idx)
        self._chan_flat = np.concatenate(flat) if flat else np.array([], dtype=int)
        self._chan_segs = np.array(segs, dtype=int) if segs else np.array([0])
        self._chan_freqs = [cf for cf, _ in self._chan_idx]
        # energie-buffers resetten (andere indeling)
        self.ch_avg = None; self.ch_peak = None
        return f

    def _drain(self, pipe):
        try:
            for line in pipe:
                t = line.decode(errors="replace").rstrip()
                if t: print(f"[rtl_tcp] {t}")
        except Exception:
            pass

    def _connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(5)
        self._sock.connect((TCP_HOST, TCP_PORT))
        self._sock.settimeout(2)
        try: self._sock.recv(12)
        except Exception: pass
        send_cmd(self._sock, 0x01, self.center_freq)
        send_cmd(self._sock, 0x02, SAMPLE_RATE)
        send_cmd(self._sock, 0x08, 0)   # digitale RTL2832-AGC uit → vast niveau
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
        # --extern: rtl_tcp draait al (bv. vergelijkingsopstelling), niet zelf starten
        if EXTERN_RTLTCP:
            print(f"Extern modus — verbinden met bestaande rtl_tcp op {TCP_HOST}:{TCP_PORT}")
            try:
                self._connect()
            except Exception as e:
                raise RuntimeError(f"Kan geen verbinding maken op {TCP_HOST}:{TCP_PORT}.\n\n"
                                   f"Draait rtl_tcp op deze poort?\n\nFout: {e}")
            self.running = True
            self._log_session("SESSION_START")
            threading.Thread(target=self._loop, daemon=True).start()
            return
        # rtl_tcp.exe opstarten
        if sys.platform != "win32":
            os.system("pkill rtl_tcp 2>/dev/null")
            time.sleep(0.5)
        if os.path.exists(RTL_TCP_PATH):
            print(f"rtl_tcp starten: {RTL_TCP_PATH}")
            _flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            self._proc = subprocess.Popen(
                [RTL_TCP_PATH, "-a", TCP_HOST, "-p", str(TCP_PORT),
                 "-d", str(DEVICE_IDX), "-f", str(self.center_freq),
                 "-s", str(SAMPLE_RATE)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=_flags)
            threading.Thread(target=self._drain, args=(self._proc.stdout,),
                             daemon=True).start()
            time.sleep(3.0)
        else:
            print(f"rtl_tcp niet gevonden — probeer {TCP_HOST}:{TCP_PORT}")
        try:
            self._connect()
        except Exception as e:
            raise RuntimeError(f"Kan geen verbinding maken op {TCP_HOST}:{TCP_PORT}.\n\n"
                               f"Controleer of de dongle is aangesloten en rtl_tcp draait.\n\nFout: {e}")
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
                chunk = self._sock.recv(65536)
                if not chunk:
                    if not self._try_reconnect(): break
                    buf = bytearray(); continue
                buf.extend(chunk)
                while len(buf) >= needed:
                    # Élk frame verwerken (vangt elke korte burst, zoals tetra-monitor)
                    raw     = buf[:needed]; del buf[:needed]
                    iq      = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5) / 127.5
                    self.clip_peak = float(np.abs(iq).max())     # 1.0 = tegen clipping
                    samples = (iq[0::2] + 1j * iq[1::2]) * self._window
                    fft     = np.fft.fftshift(np.abs(np.fft.fft(samples, FFT_SIZE)))
                    lin     = (fft / FFT_SIZE) ** 2 + 1e-20       # lineair vermogen per bin
                    # DC-spike (LO-lek op center) dempen met lokale mediaan
                    dc = self._dc_bin
                    ref = np.concatenate([lin[dc-9:dc-3], lin[dc+4:dc+10]])
                    if ref.size:
                        lin[dc-DC_NULL_BINS:dc+DC_NULL_BINS+1] = np.median(ref)
                    power   = 10.0 * np.log10(lin)                # dB (== 20·log10 amplitude)
                    self.n_frames += 1
                    with self._lock:
                        self.wfall    = np.roll(self.wfall, 1, axis=0)
                        self.wfall[0] = power
                        self.power    = power
                        now      = time.time()
                        hard_thr = self.hard_threshold
                        # Energie per kanaal: integratie over 25 kHz (gevectoriseerd)
                        ch_energy = np.add.reduceat(lin[self._chan_flat], self._chan_segs)
                        nf_now = float(np.percentile(power, 30))
                        if self.n_frames < self.WARMUP:
                            a = 0.1
                            self.noise_floor = nf_now if self.ch_avg is None else (1-a)*self.noise_floor + a*nf_now
                            self.ch_avg  = ch_energy if self.ch_avg is None else (1-a)*self.ch_avg + a*ch_energy
                            self.ch_peak = ch_energy.copy() if self.ch_peak is None else np.maximum(ch_energy, self.ch_peak)
                            self.baseline = np.full(FFT_SIZE, self.noise_floor, dtype=np.float32)
                            pct = int(100 * self.n_frames / self.WARMUP)
                            self.status      = f"Ruisvloer meten  {pct}%"
                            self.alarm       = False
                            self.alarm_level = 0
                        else:
                            self.status  = "Scannen"
                            self.noise_floor = 0.995*self.noise_floor + 0.005*nf_now
                            self.baseline = np.full(FFT_SIZE, self.noise_floor, dtype=np.float32)
                            dt_f = 0.0 if self._last_frame_t is None else min(0.5, now - self._last_frame_t)
                            self._last_frame_t = now
                            # Tijdmiddeling + piek-hold per kanaal (vangt korte bursts)
                            self.ch_avg  = (1-CHAN_SMOOTH_A)*self.ch_avg + CHAN_SMOOTH_A*ch_energy
                            self.ch_peak = np.maximum(ch_energy, self.ch_peak * math.exp(-dt_f / PEAK_TAU))
                            # CFAR: lokale ruis = mediaan van naburige kanalen
                            h = CFAR_HALF_CHANS
                            padded = np.pad(self.ch_avg, h, mode="edge")
                            cwin = np.lib.stride_tricks.sliding_window_view(padded, 2*h+1)
                            local = np.median(cwin, axis=1) + 1e-20
                            level_avg  = 10.0*np.log10(self.ch_avg / local)
                            level_peak = 10.0*np.log10(self.ch_peak / local)
                            levels = np.maximum(level_avg, level_peak)   # dB boven lokale ruis
                            raw_ch = {}
                            for ci, (cf, idx) in enumerate(self._chan_idx):
                                level = float(levels[ci])
                                # Birdie-check: zit bijna alle energie in 1 bin → smalle storing
                                if self.occupancy_check and level > self.slot_floor:
                                    seg = lin[idx]; ssum = float(seg.sum())
                                    if ssum > 0 and float(seg.max())/ssum > OCC_PEAK_FRAC:
                                        level = min(level, self.slot_floor - 2.0)
                                raw_ch[cf] = level
                            # Peak-hold voor raw data venster (elke FFT-frame bijgewerkt)
                            for cf, rdb in raw_ch.items():
                                pk_db, pk_exp = self.raw_peaks.get(cf, (rdb, now + 3.0))
                                if rdb >= pk_db or now > pk_exp:
                                    self.raw_peaks[cf] = (rdb, now + 3.0)
                                else:
                                    self.raw_peaks[cf] = (pk_db, pk_exp)

                            # Patroonherkenning — detecteer bursts (stijgende flank boven de vloer)
                            for cf, rdb in raw_ch.items():
                                if rdb > self.slot_floor:
                                    if cf not in self._ch_above:
                                        # Nieuwe burst op dit kanaal
                                        self._ch_above.add(cf)
                                        self._burst_times.setdefault(cf, deque()).append(now)
                                else:
                                    self._ch_above.discard(cf)
                            # Oude bursts (>10s) opruimen
                            for cf, dq in self._burst_times.items():
                                while dq and now - dq[0] > 10.0:
                                    dq.popleft()
                            # (Ruisvloer/baseline wordt nu per frame gezet als CFAR-niveau;
                            #  geen aparte baseline-freeze meer nodig.)
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
                            # Adaptief storingsfilter: kanaal dat te lang onafgebroken
                            # hoog staat = storing → onderdrukken. Reset zodra het wegvalt.
                            for cf, db in raw_ch.items():
                                if db > self.threshold:
                                    if cf not in self._hot_since:
                                        self._hot_since[cf] = now
                                    elif now - self._hot_since[cf] > self.interference_secs:
                                        self._suppressed.add(cf)
                                elif db < self.slot_floor:
                                    # kanaal weer stil → vrijgeven
                                    self._hot_since.pop(cf, None)
                                    self._suppressed.discard(cf)

                            # Alarm & beep op basis van RUWE waarden (geen smoothing)
                            # Bij actief filter: onderdrukte (storings)kanalen overslaan
                            if self.adaptive_filter and self._suppressed:
                                alarm_ch = {cf: db for cf, db in raw_ch.items()
                                            if cf not in self._suppressed}
                            else:
                                alarm_ch = raw_ch
                            best_raw_db   = max(alarm_ch.values(), default=0.0)
                            best_raw_freq = max(alarm_ch, key=alarm_ch.get) if alarm_ch else 0.0

                            # Live waarden voor de weergave (volgt het signaal direct,
                            # zonder piek-hold/hang — zodat de meter terugvalt)
                            self.live_db   = best_raw_db
                            self.live_freq = best_raw_freq if best_raw_db > self.slot_floor else None

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
                                # (geen piep meer op de drempel — alleen rode sirene)
                            elif now >= self._alarm_until:
                                self.alarm = False; self.alarm_level = 0
                                self._last_red_beep = 0.0

                            # Wanted-level: koel continu af; groei met de sterkte van
                            # detecties (alleen op nieuwe pieken binnen een event, zodat
                            # een constante bron niet eindeloos opbouwt).
                            self.wanted_heat = max(0.0, self.wanted_heat - HEAT_COOLDOWN * dt_f)
                            if self.alarm and self.alarm_level >= 1:
                                if self._evt_peak is None:
                                    self._evt_peak = self.threshold
                                if best_raw_db > self._evt_peak:
                                    self.wanted_heat = min(
                                        HEAT_MAX,
                                        self.wanted_heat + HEAT_GAIN * (best_raw_db - self._evt_peak))
                                    self._evt_peak = best_raw_db
                            else:
                                self._evt_peak = None
                            self.wanted_stars = min(5, int(self.wanted_heat // HEAT_PER_STAR))

                            # Continu activiteitsniveau: meeste bursts op enig kanaal in
                            # de laatste 10s (altijd zichtbaar, ook zonder alarm)
                            self.alarm_activity = max(
                                (len(dq) for dq in self._burst_times.values()), default=0)

                            # Signaalbreedte (kHz) op het alarm-kanaal — tel bins boven 6 dB
                            if self.alarm and self.freqs[0] <= self.alarm_freq <= self.freqs[-1]:
                                aidx = int(round((self.alarm_freq - self.freqs[0]) / self._bin_mhz))
                                wlo = max(0, aidx - self._ch_bins); whi = min(FFT_SIZE, aidx + self._ch_bins)
                                seg = power[wlo:whi] - self.noise_floor
                                pk = float(np.max(seg)) if seg.size else 0.0
                                wide_bins = int(np.count_nonzero(seg > pk - 6.0))
                                self.alarm_width = wide_bins * self._bin_mhz * 1000.0  # kHz
                            else:
                                self.alarm_width = 0.0

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

                            # Debug logging — piek per kanaal bijhouden (vangt korte bursts)
                            if self.debug_logging:
                                for cf, dbv in raw_ch.items():
                                    if dbv > self._dbg_peak.get(cf, -999.0):
                                        self._dbg_peak[cf] = dbv
                                # Elke 2s wegschrijven en pieken resetten
                                if now - self._last_debug_log >= 2.0:
                                    self._last_debug_log = now
                                    try:
                                        write_header = not os.path.exists(DEBUG_LOG_PATH)
                                        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                                            if write_header:
                                                f.write("timestamp,gain_db,alarm_level,baseline_avg,max_piek," +
                                                        ",".join(f"ch_{cf:.3f}" for cf in sorted(raw_ch)) + "\n")
                                            baseline_avg = float(np.mean(self.baseline)) if self.baseline is not None else 0.0
                                            pk = self._dbg_peak
                                            ch_vals = ",".join(f"{pk.get(cf, 0.0):.1f}" for cf in sorted(raw_ch))
                                            max_piek = max(pk.values(), default=0.0)
                                            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                                            f.write(f"{ts},{self.gain_db},{self.alarm_level},{baseline_avg:.1f},{max_piek:.1f},{ch_vals}\n")
                                    except Exception:
                                        pass
                                    self._dbg_peak = {}

            except socket.timeout:
                continue
            except Exception as e:
                print(f"Lus fout: {e}")
                if not self._try_reconnect(): break
                buf = bytearray()


# ══════════════════════════════════════════════════════════════════════════════
#  PyQt6 GUI
# ══════════════════════════════════════════════════════════════════════════════
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSlider, QFrame, QSizePolicy,
    QProgressBar, QDialog, QTabWidget, QStackedWidget, QComboBox, QLineEdit,
    QScrollArea,
)
from PyQt6.QtCore import (Qt, QTimer, QRectF, QPointF, pyqtSignal, QSettings, QSize,
                          QPropertyAnimation, pyqtProperty, QEasingCurve)
from PyQt6.QtGui import (
    QPainter, QColor, QFont, QPainterPath, QTransform, QIcon, QPixmap, QPen,
    QBrush, QPolygonF, QRadialGradient,
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
# Drempels nu in dB boven de LOKALE (CFAR-)ruis — schaal van tetra-monitor.
MODES = [
    {"name": "Stad",     "slot_floor": 20, "threshold": 35, "hard_threshold": 45, "hang_time": 5.0, "gain_db": 36},
    {"name": "Custom",   "slot_floor": 10, "threshold": 18, "hard_threshold": 30, "hang_time": 4.0, "gain_db": 36},
    {"name": "Snelweg",  "slot_floor":  8, "threshold": 14, "hard_threshold": 26, "hang_time": 3.0, "gain_db": 36},
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
def _set_target(w, slots, live_db, live_freq):
    """Lees het live signaal (zonder detector-piekhold, anders sterkste slot)
    en houd de piek vast gedurende hang_time. De animatie-timer loopt er
    vloeiend naartoe: snel omhoog, vasthouden, dan rustig terug."""
    if live_db is not None:
        live = float(live_db)
        w._live_freq = live_freq
    else:
        dbs = [float(s.get("db", 0.0)) for s in slots] or [0.0]
        bi  = max(range(len(dbs)), key=lambda i: dbs[i])
        live = dbs[bi]
        w._live_freq = slots[bi].get("freq") if slots else None
    w._live_db = live
    # Piek vasthouden: nieuwe piek verlengt de hang-tijd
    if live >= w._hold_db:
        w._hold_db    = live
        w._hold_until = time.monotonic() + max(0.0, w._hang_time)


def _ease_step(w):
    """Eén animatiestap richting de effectieve doelwaarde (met hang-hold).
    Retourneert True als er iets veranderde (dan moet hertekend worden)."""
    now = time.monotonic()
    if now < w._hold_until:
        eff = w._hold_db                 # nog binnen hang_time → vasthouden
    else:
        eff = w._live_db                 # hang voorbij → terugvallen
        w._hold_db = w._live_db
    w._target_db = eff
    if eff >= w._disp_db:
        nd = w._disp_db + 0.35 * (eff - w._disp_db)   # vloeiend omhoog
        if abs(eff - nd) < 0.05:
            nd = eff
    else:
        # Rustig terug: ~1 seconde per segment (vakje). Stap = dB-breedte van
        # het huidige vakje gedeeld door ~30 frames/seconde.
        dec = _seg_db_width(w) / 30.0
        nd  = max(eff, w._disp_db - dec)
    changed = abs(nd - w._disp_db) > 0.02
    w._disp_db = nd
    return changed


def _seg_db_width(w):
    """dB-breedte van één segment (vakje) op het huidige niveau."""
    if w._disp_db >= w._hard_threshold:
        return 5.0
    if w._disp_db >= w._threshold:
        return max(1.0, (w._hard_threshold - w._threshold) / 4.0)
    return max(1.0, (w._threshold - w._slot_floor) / 4.0)


def _disp_trend(w):
    """Richting: doel boven huidige weergave = nadert, eronder = rijdt weg."""
    d = w._target_db - w._disp_db
    return 1 if d > 1.0 else (-1 if d < -1.0 else 0)


class SignalBarsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._slots          = [{"freq": None, "db": 0.0} for _ in range(3)]
        self._threshold      = float(THRESHOLD_SOFT)
        self._hard_threshold = float(THRESHOLD_SOFT) + 10.0
        self._slot_floor     = float(SLOT_FLOOR)
        self._trends         = [0, 0, 0]
        self._known_freqs    = set()
        self._disp_db        = 0.0
        self._target_db      = 0.0
        self._live_db        = 0.0
        self._live_freq      = None
        self._hold_db        = 0.0
        self._hold_until     = 0.0
        self._hang_time      = float(HANG_TIME)
        self._peak_db        = 0.0
        self._peak_freq      = None
        self._peak_until     = 0.0
        self._wanted_stars   = 0
        self.setMinimumSize(260, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._anim = QTimer(self)
        self._anim.timeout.connect(self._anim_step)
        self._anim.start(33)

    # Hoe lang de dB-piekwaarde in de tekst blijft staan nadat het signaal zakt
    PEAK_TEXT_HOLD = 12.0
    # dB per tick (~33 ms) waarmee de piek-tekst terugzakt zodra de hold voorbij is
    PEAK_DECAY     = 0.2

    def _anim_step(self):
        changed = _ease_step(self)
        now = time.monotonic()
        # Frequentielabel volgt het live-signaal en verdwijnt zodra de balk leeg is.
        if self._live_freq is not None:
            self._peak_freq = self._live_freq
        elif self._disp_db <= self._slot_floor:
            self._peak_freq = None
        # Piek-markering (cyan lijn): houdt de recente top kort vast en zakt daarna
        # terug naar de balk. De dB-tekst zelf volgt de balk (_disp_db) en wordt bij
        # het tekenen gelezen, zodat getal en balken samen terugzakken.
        if self._live_freq is not None and self._disp_db > self._peak_db + 0.05:
            self._peak_db    = self._disp_db
            self._peak_until = now + self.PEAK_TEXT_HOLD
            changed = True
        elif now >= self._peak_until and self._peak_db > self._disp_db:
            self._peak_db = max(self._disp_db, self._peak_db - self.PEAK_DECAY)
            changed = True
        if changed:
            self.update()

    def update_data(self, slots, threshold, slot_floor=20.0, trends=None,
                    hard_threshold=None, known_freqs=None, live_db=None,
                    live_freq=None, hang_time=None, wanted_stars=0):
        self._slots          = [dict(s) for s in slots]
        self._threshold      = float(threshold)
        self._slot_floor     = float(slot_floor)
        self._hard_threshold = float(hard_threshold) if hard_threshold else float(threshold) + 10.0
        self._trends         = trends or [0, 0, 0]
        self._known_freqs    = known_freqs or set()
        self._wanted_stars   = int(wanted_stars)
        if hang_time is not None:
            self._hang_time = float(hang_time)
        _set_target(self, slots, live_db, live_freq)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, _qc("panel"))

        TITLE_H = 28
        p.setFont(_sys_font(9, bold=True))
        p.setPen(_qc("gray2"))
        p.drawText(12, 0, W - 60, TITLE_H,
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "SIGNAALSTERKTE")

        # Eén balk: live sterkste signaal, vloeiend gesmoothed
        db_val = self._disp_db

        LABEL_H  = 66
        bars_top = TITLE_H + 6
        bars_h   = H - bars_top - LABEL_H
        seg_gap  = 4
        pad_x    = 14
        pad_y    = 8

        seg_w       = min(240.0, W * 0.64)
        seg_h_total = (bars_h - 2 * pad_y - (N_SEGS - 1) * seg_gap) / N_SEGS
        seg_h       = max(4.0, seg_h_total - 1)

        green_range  = max(1.0, self._threshold      - self._slot_floor)
        yellow_range = max(1.0, self._hard_threshold - self._threshold)

        if db_val <= self._slot_floor:
            n_lit = 0
        elif db_val < self._threshold:
            n_lit = max(1, min(4, int((db_val - self._slot_floor) / green_range * 4) + 1))
        elif db_val < self._hard_threshold:
            n_lit = 4 + max(1, min(4, int((db_val - self._threshold) / yellow_range * 4) + 1))
        else:
            n_lit = min(N_SEGS, 8 + max(1, min(2, int((db_val - self._hard_threshold) / 5) + 1)))

        cx      = W / 2.0
        seg_x   = cx - seg_w / 2
        track_x = seg_x - pad_x
        track_w = seg_w + 2 * pad_x

        track_path = QPainterPath()
        track_path.addRoundedRect(QRectF(track_x, bars_top, track_w, bars_h), 12, 12)
        p.fillPath(track_path, _qc("panel2"))

        for vi in range(N_SEGS):
            li  = N_SEGS - 1 - vi
            lit = li < n_lit
            y   = bars_top + pad_y + vi * (seg_h_total + seg_gap)

            seg_rect = QRectF(seg_x, y, seg_w, seg_h)
            seg_path = QPainterPath()
            seg_path.addRoundedRect(seg_rect, 4, 4)

            if lit:
                glow = QColor(_SEG_ON[li])
                glow.setAlpha(55)
                glow_path = QPainterPath()
                glow_path.addRoundedRect(
                    QRectF(seg_x - 4, y - 1, seg_w + 8, seg_h + 2), 6, 6)
                p.fillPath(glow_path, glow)
                p.fillPath(seg_path, _SEG_ON[li])
            else:
                p.fillPath(seg_path, _SEG_OFF[li])

        # dB-tekst volgt de balk (_disp_db): getal en balken zakken samen terug.
        show_db   = self._disp_db
        show_freq = self._peak_freq
        ly = H - LABEL_H + 6
        if show_freq is not None and show_db > self._slot_floor:
            if show_db >= self._hard_threshold:
                fc = _qc("red")
            elif show_db > self._threshold:
                fc = _qc("yellow")
            else:
                fc = _qc("green")
            star = "★ " if show_freq in self._known_freqs else ""
            p.setFont(_sys_font(12, bold=True))
            p.setPen(fc)
            p.drawText(0, ly, W, 22,
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                       f"{star}{show_freq:.3f} MHz")
            p.setFont(_sys_font(9, bold=True))
            p.setPen(_qc("gray1"))
            p.drawText(0, ly + 22, W, 18,
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                       f"+{show_db:.0f} dB")
            trend = _disp_trend(self)
            if trend == 1:
                arrow, ac = "▲ Nadert", _qc("green")
            elif trend == -1:
                arrow, ac = "▼ Rijdt weg", _qc("orange")
            else:
                arrow, ac = "► Stabiel", _qc("gray2")
            p.setFont(_sys_font(8, bold=True))
            p.setPen(ac)
            p.drawText(0, ly + 40, W, 16,
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                       arrow)
        else:
            p.setFont(_sys_font(11, bold=True))
            p.setPen(_qc("gray3"))
            p.drawText(0, ly, W, 24,
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                       "—")

        p.end()


def _star_polygon(cx, cy, ro, ri):
    """Vijfpuntige ster als QPolygonF (punt naar boven)."""
    pts = []
    for k in range(10):
        ang = -math.pi / 2 + k * math.pi / 5
        r = ro if k % 2 == 0 else ri
        pts.append(QPointF(cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return QPolygonF(pts)


# ── SignalBarsHWidget (horizontale variant) ───────────────────────────────────
class SignalBarsHWidget(SignalBarsWidget):
    """Zijwaartse signaalbalk: dunne slats die naar rechts oplopen, met een
    piek-markering en dB/freq-uitlezing. Erft alle smoothing/peak-hold-logica;
    alleen het tekenen verschilt van de verticale balk."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(280, 110)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, _qc("panel"))

        # Compacte gekaderde box, gecentreerd in het paneel
        card_w = min(float(W - 24), 900.0)
        card_h = min(float(H - 16), 300.0)
        card_x = (W - card_w) / 2.0
        card_y = (H - card_h) / 2.0
        card_rect = QRectF(card_x, card_y, card_w, card_h)
        cpath = QPainterPath(); cpath.addRoundedRect(card_rect, 14, 14)
        p.fillPath(cpath, QColor("#161a20"))
        p.setPen(QPen(QColor("#2a2f37"), 1)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(card_rect, 14, 14)

        p.setFont(_sys_font(9, bold=True))
        p.setPen(_qc("gray2"))
        p.drawText(int(card_x + 16), int(card_y + 7), int(card_w - 60), 20,
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "SIGNAALSTERKTE")

        db     = self._disp_db
        db_max = self._hard_threshold + 8.0
        rng    = max(1.0, db_max - self._slot_floor)
        f      = max(0.0, min(1.0, (db - self._slot_floor) / rng))

        green  = QColor("#2ed158")
        orange = QColor("#ff9f0a")
        red    = QColor("#ff3b30")

        def zone_col(val):
            if val < self._threshold:      return green
            if val < self._hard_threshold: return orange
            return red

        # Responsive: smal venster (bv. compact) -> uitlezing ONDER de balk,
        # balk op volle breedte. Breed -> uitlezing naast de balk.
        narrow = card_w < 440.0
        bar_x0 = card_x + 18
        if narrow:
            bar_x1 = card_x + card_w - 18
            bar_h  = 44.0
            bar_y  = card_y + 30
        else:
            readout_w = min(210.0, card_w * 0.34)
            bar_x1 = card_x + card_w - readout_w - 16
            bar_h  = 92.0
            bar_y  = card_y + 38
        bar_w = max(60.0, bar_x1 - bar_x0)

        # Track-achtergrond
        track = QPainterPath()
        track.addRoundedRect(QRectF(bar_x0 - 6, bar_y - 6, bar_w + 12, bar_h + 12), 8, 8)
        p.fillPath(track, QColor("#10141a"))

        n     = max(8, min(22, int(bar_w / 16)))   # vast aantal: bredere slats bij groter
        pitch = bar_w / n
        sw    = max(5.0, pitch * 0.70)
        n_lit = int(round(f * n))
        for i in range(n):
            x   = bar_x0 + i * pitch + (pitch - sw) / 2.0
            val = self._slot_floor + (i + 0.5) / n * rng
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, bar_y, sw, bar_h), 2, 2)
            if i < n_lit:
                col  = zone_col(val)
                glow = QColor(col); glow.setAlpha(55)
                gpath = QPainterPath()
                gpath.addRoundedRect(QRectF(x - 2, bar_y - 1, sw + 4, bar_h + 2), 3, 3)
                p.fillPath(gpath, glow)
                p.fillPath(path, col)
            else:
                p.fillPath(path, QColor("#262c34"))

        # dB-schaal onder de balk
        lbl_y = int(bar_y + bar_h + (9 if not narrow else 5))
        p.setFont(_sys_font(7, bold=True)); p.setPen(_qc("gray3"))
        p.drawText(int(bar_x0), lbl_y, 40, 14,
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   f"{self._slot_floor:.0f}")
        p.drawText(int(bar_x1 - 40), lbl_y, 40, 14,
                   int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                   f"{db_max:.0f}")

        # Piek-markering
        if self._peak_db > self._slot_floor:
            fp = max(0.0, min(1.0, (self._peak_db - self._slot_floor) / rng))
            px = bar_x0 + fp * bar_w
            p.setPen(QPen(QColor("#9fe8ff"), 2.5))
            p.drawLine(int(px), int(bar_y - 6), int(px), int(bar_y + bar_h + 6))
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#9fe8ff"))
            p.drawPolygon(QPolygonF([QPointF(px - 5, bar_y - 6),
                                     QPointF(px + 5, bar_y - 6),
                                     QPointF(px, bar_y + 1)]))

        # dB-tekst volgt de balk (_disp_db); de cyan piek-markering hierboven
        # gebruikt nog wel _peak_db. Zo zakken getal en balken samen terug.
        show_db, show_freq = self._disp_db, self._peak_freq
        active = show_freq is not None and show_db > self._slot_floor
        trend = _disp_trend(self)
        if trend == 1:    tr_txt, ac = "▲ Nadert", _qc("green")
        elif trend == -1: tr_txt, ac = "▼ Rijdt weg", _qc("orange")
        else:             tr_txt, ac = "► Stabiel", _qc("gray2")

        if narrow:
            # Uitlezing gecentreerd onder de balk
            ry = int(bar_y + bar_h + 20)
            if active:
                p.setFont(_sys_font(20, bold=True)); p.setPen(zone_col(show_db))
                p.drawText(int(card_x), ry, int(card_w), 26,
                           int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                           f"+{show_db:.0f} dB")
                star = "★ " if show_freq in self._known_freqs else ""
                p.setFont(_sys_font(9, bold=True)); p.setPen(_qc("gray1"))
                p.drawText(int(card_x), ry + 24, int(card_w), 14,
                           int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                           f"{star}{show_freq:.3f} MHz  ·  {tr_txt}")
            else:
                p.setFont(_sys_font(15, bold=True)); p.setPen(_qc("gray3"))
                p.drawText(int(card_x), ry, int(card_w), 26,
                           int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), "—")
            div_y = ry + 44
        else:
            # Uitlezing rechts naast de balk
            rx  = bar_x1 + 14
            cyc = bar_y + bar_h / 2.0
            if active:
                p.setFont(_sys_font(int(max(20, min(32, bar_h * 0.40))), bold=True)); p.setPen(zone_col(show_db))
                p.drawText(int(rx), int(cyc - 34), int(readout_w), 42,
                           int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                           f"+{show_db:.0f} dB")
                star = "★ " if show_freq in self._known_freqs else ""
                p.setFont(_sys_font(12, bold=True)); p.setPen(_qc("gray1"))
                p.drawText(int(rx), int(cyc + 10), int(readout_w), 18,
                           int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                           f"{star}{show_freq:.3f} MHz")
                p.setFont(_sys_font(9, bold=True)); p.setPen(ac)
                p.drawText(int(rx), int(cyc + 30), int(readout_w), 14,
                           int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                           tr_txt)
            else:
                p.setFont(_sys_font(16, bold=True)); p.setPen(_qc("gray3"))
                p.drawText(int(rx), int(cyc - 12), int(readout_w), 24,
                           int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "—")
            div_y = bar_y + bar_h + 28

        # ── Heat-Level (5 sterren) ──
        stars = max(0, min(5, int(self._wanted_stars)))
        p.setPen(QPen(QColor("#23282f"), 1))
        p.drawLine(int(card_x + 16), int(div_y), int(card_x + card_w - 16), int(div_y))
        p.setFont(_sys_font(8, bold=True)); p.setPen(_qc("gray2"))
        p.drawText(int(card_x + 18), int(div_y + 16), 200, 14,
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "HEAT-LEVEL")
        lit_col = (QColor("#ffd60a") if stars <= 3
                   else QColor("#ff9f0a") if stars == 4 else QColor("#ff3b30"))
        sr   = 11.0 if narrow else 17.0
        sgap = 9.0 if narrow else 14.0
        sy = (div_y + (card_y + card_h)) / 2.0
        if narrow:
            row_w = 5 * (2 * sr + sgap) - sgap
            sx0 = card_x + (card_w - row_w) / 2.0 + sr   # gecentreerd
        else:
            sx0 = card_x + 26 + sr
        for k in range(5):
            sxc = sx0 + k * (sr * 2 + sgap)
            if k < stars:
                glow = QColor(lit_col); glow.setAlpha(60)
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(glow)
                p.drawPolygon(_star_polygon(sxc, sy, sr + 3, (sr + 3) * 0.45))
                p.setBrush(lit_col)
                p.drawPolygon(_star_polygon(sxc, sy, sr, sr * 0.45))
            else:
                p.setPen(QPen(QColor("#3a3f47"), 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPolygon(_star_polygon(sxc, sy, sr, sr * 0.45))
        if not narrow:
            if stars == 0:   st_txt, st_col = "Rustig", _qc("gray2")
            elif stars <= 2: st_txt, st_col = "Activiteit", QColor("#ffd60a")
            elif stars <= 4: st_txt, st_col = "Verhoogde activiteit", QColor("#ff9f0a")
            else:            st_txt, st_col = "Zeer druk", QColor("#ff3b30")
            tx = sx0 + 5 * (sr * 2 + sgap) + 6
            p.setFont(_sys_font(11, bold=True)); p.setPen(st_col)
            p.drawText(int(tx), int(sy - 12), int(card_x + card_w - tx - 10), 24,
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), st_txt)
        p.end()


# ── RadialMeterWidget ─────────────────────────────────────────────────────────
class RadialMeterWidget(QWidget):
    """Radiale boog-meter: toont het STERKSTE signaal als halve-cirkel gauge.
    Zelfde update_data-interface als SignalBarsWidget zodat hij inwisselbaar is."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._slots          = [{"freq": None, "db": 0.0} for _ in range(3)]
        self._threshold      = float(THRESHOLD_SOFT)
        self._hard_threshold = float(THRESHOLD_SOFT) + 10.0
        self._slot_floor     = float(SLOT_FLOOR)
        self._trends         = [0, 0, 0]
        self._known_freqs    = set()
        self._disp_db        = 0.0
        self._target_db      = 0.0
        self._live_db        = 0.0
        self._live_freq      = None
        self._hold_db        = 0.0
        self._hold_until     = 0.0
        self._hang_time      = float(HANG_TIME)
        self._wanted_stars   = 0
        self.setMinimumSize(220, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._anim = QTimer(self)
        self._anim.timeout.connect(self._anim_step)
        self._anim.start(33)

    def _anim_step(self):
        if _ease_step(self):
            self.update()

    def update_data(self, slots, threshold, slot_floor=20.0, trends=None,
                    hard_threshold=None, known_freqs=None, live_db=None,
                    live_freq=None, hang_time=None, wanted_stars=0):
        self._slots          = [dict(s) for s in slots]
        self._threshold      = float(threshold)
        self._slot_floor     = float(slot_floor)
        self._hard_threshold = float(hard_threshold) if hard_threshold else float(threshold) + 10.0
        self._trends         = trends or [0, 0, 0]
        self._known_freqs    = known_freqs or set()
        self._wanted_stars   = int(wanted_stars)
        if hang_time is not None:
            self._hang_time = float(hang_time)
        _set_target(self, slots, live_db, live_freq)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, _qc("panel"))

        # Zwarte achtergrond (dashboard-stijl)
        p.fillRect(0, 0, W, H, QColor("#000000"))

        db     = self._disp_db
        freq   = self._live_freq
        active = freq is not None and db > self._slot_floor

        # Lineaire schaal slot_floor..db_max over de 270° wijzerplaat
        db_max = self._hard_threshold + 8.0
        rng    = max(1.0, db_max - self._slot_floor)
        f      = max(0.0, min(1.0, (db - self._slot_floor) / rng))

        # Geometrie — 270° meter met opening onderaan
        cx = W / 2.0
        cy = H * 0.56                      # naaf laag → naald ruim onder de cijfers
        R  = max(46.0, min(W * 0.46, H * 0.42))
        A0, SWEEP = 225.0, 270.0          # 0 linksonder, met de klok mee

        def ang(fr):
            return A0 - SWEEP * fr

        def pt(r, deg):
            a = math.radians(deg)
            return (cx + r * math.cos(a), cy - r * math.sin(a))

        def block_col(val):
            if val < self._threshold:      return QColor("#2ed158")   # groen
            if val < self._hard_threshold: return QColor("#ff9f0a")   # oranje
            return QColor("#ff3b30")                                  # rood

        # 10 gekleurde vakjes (groen / oranje / rood) met glow
        ring_w = max(11.0, R * 0.17)
        r_ring = R - ring_w / 2.0
        ring_rect = QRectF(cx - r_ring, cy - r_ring, 2 * r_ring, 2 * r_ring)
        N, gap = 10, 2.6
        for i in range(N):
            a_lo = ang((i + 1) / N) + gap / 2.0
            a_hi = ang(i / N) - gap / 2.0
            col  = block_col(self._slot_floor + (i + 0.5) / N * rng)
            glow = QColor(col); glow.setAlpha(55)
            pen = QPen(glow, ring_w + 8); pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            p.drawArc(ring_rect, int(round(a_lo * 16)), int(round((a_hi - a_lo) * 16)))
            pen = QPen(col, ring_w); pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            p.drawArc(ring_rect, int(round(a_lo * 16)), int(round((a_hi - a_lo) * 16)))

        # Streepjes + dB-getallen binnen de ring
        r_tick = R - ring_w - 3
        n_major = 8
        for j in range(n_major + 1):
            fr  = j / n_major
            deg = ang(fr)
            x1, y1 = pt(r_tick, deg); x2, y2 = pt(r_tick - 13, deg)
            p.setPen(QPen(QColor("#e0e0e0"), 2.2))
            p.drawLine(int(x1), int(y1), int(x2), int(y2))
            lx, ly = pt(r_tick - 27, deg)
            p.setFont(_sys_font(8, bold=True))
            p.setPen(QColor("#c8c8c8"))
            p.drawText(int(lx - 16), int(ly - 9), 32, 18,
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                       f"{self._slot_floor + fr * rng:.0f}")
            if j < n_major:
                for k in range(1, 4):
                    dm = ang(fr + (k / 4.0) / n_major)
                    mx1, my1 = pt(r_tick, dm); mx2, my2 = pt(r_tick - 6, dm)
                    p.setPen(QPen(QColor("#707070"), 1.0))
                    p.drawLine(int(mx1), int(my1), int(mx2), int(my2))

        # Digitale cijferweergave (LCD-look) hoog in de wijzerplaat
        col_dig = block_col(db) if active else QColor("#5a3a12")
        big = f"{db:.0f}" if active else "--"
        num_rect = QRectF(cx - R * 0.7, cy - R * 0.80, R * 1.4, R * 0.44)
        p.setFont(_sys_font(max(7, int(R * 0.34 * 0.4)), bold=True))
        glow = QColor(col_dig); glow.setAlpha(70)
        p.setPen(glow)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            p.drawText(num_rect.translated(dx, dy),
                       int(Qt.AlignmentFlag.AlignCenter), big)
        p.setPen(col_dig)
        p.drawText(num_rect, int(Qt.AlignmentFlag.AlignCenter), big)
        # Eenheid in LCD-kadertje
        ur = QRectF(cx - R * 0.20, cy - R * 0.34, R * 0.40, R * 0.14)
        p.setPen(QPen(QColor("#3a3a3a"), 1)); p.setBrush(QColor("#0c0c0c"))
        p.drawRoundedRect(ur, 3, 3)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setFont(_sys_font(max(4, int(R * 0.10 * 0.4)), bold=True))
        p.setPen(QColor("#cfcfcf"))
        p.drawText(ur, int(Qt.AlignmentFlag.AlignCenter), "dB")

        # Naald (rood) — altijd getekend, valt vloeiend terug
        deg = ang(f)
        tip   = QPointF(*pt(R * 0.82, deg))
        left  = QPointF(*pt(6, deg + 90))
        right = QPointF(*pt(6, deg - 90))
        tail  = QPointF(*pt(R * 0.16, deg + 180))
        ncol  = QColor("#ff2a2a") if active else QColor("#8a8a8a")
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(ncol)
        p.drawPolygon(QPolygonF([tip, right, tail, left]))

        # Glanzende naaf
        hub_r = max(10.0, R * 0.13)
        hub = QRadialGradient(cx - hub_r * 0.3, cy - hub_r * 0.3, hub_r * 1.4)
        hub.setColorAt(0.0, QColor("#f2f2f2"))
        hub.setColorAt(0.55, QColor("#9a9a9a"))
        hub.setColorAt(1.0, QColor("#4a4a4a"))
        p.setBrush(QBrush(hub)); p.setPen(QPen(QColor("#2a2a2a"), 1))
        p.drawEllipse(QPointF(cx, cy), hub_r, hub_r)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#6b6b6b"))
        p.drawEllipse(QPointF(cx, cy), hub_r * 0.4, hub_r * 0.4)

        # Vast merklabel "TETRA" (cyaan) in de opening onderaan
        p.setFont(_sys_font(max(5, int(R * 0.17 * 0.4)), bold=True))
        p.setPen(QColor("#22d3ee"))
        p.drawText(0, int(cy + R * 0.42), W, int(R * 0.28),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), "TETRA")
        # Kanaal (frequentie) apart eronder
        if active:
            star = "★ " if freq in self._known_freqs else ""
            ch, ccol = f"{star}{freq:.3f} MHz", QColor("#e6e6e6")
        else:
            ch, ccol = "— geen kanaal", QColor("#666666")
        p.setFont(_sys_font(max(4, int(R * 0.11 * 0.4)), bold=True))
        p.setPen(ccol)
        p.drawText(0, int(cy + R * 0.70), W, int(R * 0.24),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), ch)
        p.end()


def _bars_icon(color):
    """Klein balken-icoontje voor de wisselknop."""
    pm = QPixmap(18, 18)
    pm.fill(Qt.GlobalColor.transparent)
    pp = QPainter(pm)
    pp.setRenderHint(QPainter.RenderHint.Antialiasing)
    pp.setPen(Qt.PenStyle.NoPen)
    pp.setBrush(QColor(color))
    pp.drawRoundedRect(QRectF(2.0,  9.0, 3.5,  7.0), 1, 1)
    pp.drawRoundedRect(QRectF(7.25, 5.0, 3.5, 11.0), 1, 1)
    pp.drawRoundedRect(QRectF(12.5, 2.0, 3.5, 14.0), 1, 1)
    pp.end()
    return QIcon(pm)


def _gauge_icon(color):
    """Klein meter-icoontje (boog + naald) voor de wisselknop."""
    pm = QPixmap(18, 18)
    pm.fill(Qt.GlobalColor.transparent)
    pp = QPainter(pm)
    pp.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pp.setPen(pen)
    pp.drawArc(QRectF(2.0, 4.0, 14.0, 14.0), 20 * 16, 140 * 16)
    pp.drawLine(9, 13, 13, 7)
    pp.end()
    return QIcon(pm)


def _hbars_icon(color):
    """Klein horizontaal-balk-icoontje voor de wisselknop."""
    pm = QPixmap(18, 18)
    pm.fill(Qt.GlobalColor.transparent)
    pp = QPainter(pm)
    pp.setRenderHint(QPainter.RenderHint.Antialiasing)
    pp.setPen(Qt.PenStyle.NoPen)
    pp.setBrush(QColor(color))
    pp.drawRoundedRect(QRectF(2.0,  3.0,  7.0, 3.5), 1, 1)
    pp.drawRoundedRect(QRectF(2.0,  7.25, 11.0, 3.5), 1, 1)
    pp.drawRoundedRect(QRectF(2.0, 11.5, 14.0, 3.5), 1, 1)
    pp.end()
    return QIcon(pm)


# ── SignalDisplay (wisselbaar: verticale balk / horizontale balk / meter) ─────
class SignalDisplay(QWidget):
    """Wisselbare signaalweergave: verticale balk, horizontale balk of radiale
    meter. Eén knop cyclet er doorheen; de keuze wordt onthouden."""
    def __init__(self, settings_key="signal_view", parent=None):
        super().__init__(parent)
        self._key   = settings_key
        self.bars   = SignalBarsWidget()    # 0 = verticale balk
        self.hbars  = SignalBarsHWidget()   # 1 = horizontale balk
        self.meter  = RadialMeterWidget()   # 2 = radiale meter
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self.bars)
        self._stack.addWidget(self.hbars)
        self._stack.addWidget(self.meter)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._stack)

        # Iconen per weergave (de knop toont die van de VOLGENDE)
        self._icons = [_bars_icon(C['gray1']), _hbars_icon(C['gray1']), _gauge_icon(C['gray1'])]
        self._btn = QPushButton(self)
        self._btn.setFixedSize(30, 24)
        self._btn.setIconSize(QSize(16, 16))
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.setToolTip("Wissel weergave: balk / horizontaal / meter")
        self._btn.setStyleSheet(
            f"QPushButton {{ background:{C['panel2']};"
            f" border:1px solid {C['sep']}; border-radius:5px; }}"
            f" QPushButton:hover {{ border:1px solid {C['gray2']}; }}")
        self._btn.clicked.connect(self.toggle)

        try:
            idx = int(QSettings("PrioSense", SETTINGS_APP).value(self._key, 0))
        except (TypeError, ValueError):
            idx = 0
        self.set_view(idx)

    def set_view(self, idx):
        idx = idx % self._stack.count()
        self._stack.setCurrentIndex(idx)
        nxt = (idx + 1) % self._stack.count()
        self._btn.setIcon(self._icons[nxt])   # icoon van de volgende weergave
        QSettings("PrioSense", SETTINGS_APP).setValue(self._key, idx)

    def toggle(self):
        self.set_view(self._stack.currentIndex() + 1)

    def resizeEvent(self, e):
        self._btn.move(self.width() - self._btn.width() - 6, 5)
        self._btn.raise_()
        super().resizeEvent(e)

    def update_data(self, *args, **kwargs):
        self.bars.update_data(*args, **kwargs)
        self.hbars.update_data(*args, **kwargs)
        self.meter.update_data(*args, **kwargs)


# ── AlarmCard ─────────────────────────────────────────────────────────────────
class AlarmCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(90)
        self.setMaximumHeight(135)

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

        self.activity = QLabel("")
        self.activity.setFont(_sys_font(9, bold=True))
        self.activity.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.activity)

        self._set("idle")

    @staticmethod
    def _activity_text(n):
        if n >= 4:   return "▪▪▪  Actief gesprek"
        if n >= 2:   return "▪▪  Activiteit"
        if n >= 1:   return "▪  Kort contact"
        return "▫  Rustig"

    def _set(self, mode, freq=0.0, db=0.0, activity=0, width=0.0, known=False):
        star = "★ " if known else ""
        w_txt = f"  ·  ~{width:.0f} kHz" if width > 0 else ""
        styles = {
            "idle":   (C['panel'],  "1px solid " + C['sep'],    C['gray3'], C['gray1'], "—",                                          C['gray3']),
            "orange": ("#2a1b00",   "2px solid " + C['orange'], C['orange'], C['white'], f"{star}{freq:.3f} MHz  +{db:.1f} dB{w_txt}", C['orange']),
            "red":    ("#2d0b0a",   "2px solid " + C['red'],    C['red'],   C['white'], f"{star}{freq:.3f} MHz  +{db:.1f} dB{w_txt}", C['red']),
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
        # Burst-teller altijd zichtbaar (+ bekend-kanaal markering indien van toepassing)
        act_txt = self._activity_text(activity)
        if mode != "idle" and known:
            act_txt += "   ·   ★ Bekend kanaal"
        self.activity.setText(act_txt)
        self.activity.setStyleSheet(f"color: {detail_col}; background: transparent;")

    def set_idle(self, activity=0):                                 self._set("idle", activity=activity)
    def set_orange(self, f, db, activity=0, width=0.0, known=False): self._set("orange", f, db, activity, width, known)
    def set_red(self, f, db, activity=0, width=0.0, known=False):    self._set("red",    f, db, activity, width, known)


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
        # Scroll-wheel negeren → scrollt het paneel i.p.v. de slider te verzetten
        self.slider.wheelEvent = lambda e: e.ignore()
        vbox.addWidget(self.slider)

    def _emit(self, step_val):
        val = self._lo + step_val * self._step
        self.val_lbl.setText(self._fmt.format(val))
        self.valueChanged.emit(val)

    def setValue(self, val):
        """Zet de slider op een waarde (triggert valueChanged)."""
        val = max(self._lo, min(self._hi, float(val)))
        self.slider.setValue(round((val - self._lo) / self._step))

    def value(self):
        return self._lo + self.slider.value() * self._step


# ── ToggleSwitch (macOS-stijl) ────────────────────────────────────────────────
class ToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, checked=False, on_color=None, parent=None):
        super().__init__(parent)
        self._checked  = bool(checked)
        self._knob     = 1.0 if checked else 0.0
        self._on_color = QColor(on_color or C['green'])
        self.setFixedSize(46, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    def isChecked(self):
        return self._checked

    def setChecked(self, val, emit=False):
        val = bool(val)
        if val == self._checked:
            return
        self._checked = val
        self._anim.stop()
        self._anim.setStartValue(self._knob)
        self._anim.setEndValue(1.0 if val else 0.0)
        self._anim.start()
        if emit:
            self.toggled.emit(val)

    def mousePressEvent(self, _e):
        self.setChecked(not self._checked, emit=True)

    def _get_knob(self): return self._knob
    def _set_knob(self, v):
        self._knob = v
        self.update()
    knob = pyqtProperty(float, _get_knob, _set_knob)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        off = QColor(C['gray3'])
        on  = self._on_color
        k   = self._knob
        track = QColor(int(off.red()   + (on.red()   - off.red())   * k),
                       int(off.green() + (on.green() - off.green()) * k),
                       int(off.blue()  + (on.blue()  - off.blue())  * k))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, W, H), H / 2, H / 2)
        d  = H - 6
        x  = 3 + (W - 6 - d) * k
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, 3, d, d))
        p.end()


# ── Instellingen-kaart & -rij (iOS-stijl) ─────────────────────────────────────
class SettingsRow(QWidget):
    """Rij met titel (+ optioneel subtitel) links en een control rechts."""
    def __init__(self, title, control=None, subtitle=None, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 9, 12, 9)
        h.setSpacing(10)
        vt = QVBoxLayout(); vt.setSpacing(1); vt.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(title); lbl.setFont(_sys_font(11))
        lbl.setStyleSheet(f"color:{C['gray1']}; background:transparent;")
        vt.addWidget(lbl)
        if subtitle:
            sub = QLabel(subtitle); sub.setFont(_sys_font(8))
            sub.setStyleSheet(f"color:{C['gray2']}; background:transparent;")
            sub.setWordWrap(True)
            vt.addWidget(sub)
        h.addLayout(vt, stretch=1)
        if control is not None:
            h.addWidget(control, alignment=Qt.AlignmentFlag.AlignVCenter)


class SettingsGroup(QWidget):
    """Sectie met grijs kopje + afgeronde kaart die rijen bevat."""
    def __init__(self, title, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(5)
        hdr = QLabel(title.upper())
        hdr.setFont(_sys_font(8, bold=True))
        hdr.setStyleSheet(f"color:{C['gray2']}; letter-spacing:1px;"
                          " background:transparent; padding-left:8px;")
        v.addWidget(hdr)
        self._card = QFrame()
        self._card.setObjectName("settingsCard")
        self._card.setStyleSheet(
            f"QFrame#settingsCard {{ background:{C['panel']};"
            f" border:1px solid {C['sep']}; border-radius:12px; }}")
        self._cv = QVBoxLayout(self._card)
        self._cv.setContentsMargins(0, 2, 0, 2)
        self._cv.setSpacing(0)
        v.addWidget(self._card)

    def add(self, widget, divider=True):
        if self._cv.count() > 0 and divider:
            line = QFrame()
            line.setFixedHeight(1)
            line.setStyleSheet(f"background:{C['sep']}; border:none;"
                               " margin-left:12px; margin-right:12px;")
            self._cv.addWidget(line)
        self._cv.addWidget(widget)


# ── WaterfallWindow ───────────────────────────────────────────────────────────
class WaterfallWindow(QMainWindow):
    LONG_ROWS = 300    # aantal rijen geschiedenis
    DECIM     = 5      # refreshes (à 100ms) per rij → 0.5s/rij → 300*0.5 = 2,5 min

    def __init__(self, det, parent=None):
        super().__init__(parent)
        self.det = det
        self.setWindowTitle("PrioSense — Waterfall (lange geschiedenis)")
        self.setMinimumSize(820, 360)
        self.setStyleSheet(f"background-color: {C['bg']};")

        cw = QWidget()
        self.setCentralWidget(cw)
        lay = QVBoxLayout(cw)
        lay.setContentsMargins(8, 8, 8, 8)

        self.pw = pg.PlotWidget()
        self.pw.setBackground(C['panel'])
        self.pw.setLabel('bottom', 'MHz')
        self.pw.setLabel('left', 'Tijd (minuten geleden)')
        self.pw.getAxis('left').setTextPen(QColor(C['gray2']))
        self.pw.getAxis('bottom').setTextPen(QColor(C['gray2']))
        lay.addWidget(self.pw)

        with det._lock:
            freqs = det.freqs.copy()

        # Eigen lange buffer met peak-hold
        self._long  = np.full((self.LONG_ROWS, FFT_SIZE), -80.0)
        self._acc   = None
        self._cnt   = 0
        self._freqs = freqs

        self.img = pg.ImageItem()
        self.pw.addItem(self.img)
        cmap = pg.colormap.get('inferno')
        self.img.setColorMap(cmap)
        self.img.setLevels((-80, det.wfall_max))
        self._apply_transform(freqs, self._long.shape)
        self.img.setImage(self._long.T, autoLevels=False)
        self.pw.setXRange(freqs[0], freqs[-1])
        # Y-as in minuten (0 = nu, bovenkant = LONG_ROWS*DECIM*0.1s geleden)
        secs = self.LONG_ROWS * self.DECIM * 0.1
        self.pw.getAxis('left').setScale(secs / 60.0 / self.LONG_ROWS)
        self.pw.setYRange(0, self.LONG_ROWS)

    def _apply_transform(self, freqs, shape):
        sx = (freqs[-1] - freqs[0]) / shape[1]
        tr = QTransform()
        tr.translate(freqs[0], 0)
        tr.scale(sx, 1.0)
        self.img.setTransform(tr)

    def refresh(self, data, freqs):
        # Bij band-wissel: buffer resetten (andere frequentie-indeling)
        if freqs[0] != self._freqs[0] or freqs[-1] != self._freqs[-1]:
            self._long[:] = -80.0
            self._freqs = freqs
            self._apply_transform(freqs, self._long.shape)
            self.pw.setXRange(freqs[0], freqs[-1])
        # Peak-hold over recente frames van de korte buffer
        cur = np.max(data[:10], axis=0)
        self._acc = cur if self._acc is None else np.maximum(self._acc, cur)
        self._cnt += 1
        if self._cnt >= self.DECIM:
            self._long = np.roll(self._long, 1, axis=0)
            self._long[0] = self._acc
            self._acc = None
            self._cnt = 0
            self.img.setLevels((-80, self.det.wfall_max))
            self.img.setImage(self._long.T, autoLevels=False)


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
        self.img.setLevels((-80, -20))
        self.img.setImage(data.T, autoLevels=False)
        self._apply_transform(freqs, data.shape)

        # Markers die actieve kanalen omvatten — 2 lijnen per slot (zijkanten),
        # max 3 slots. Per slot: (linkerlijn met label, rechterlijn).
        self._markers = []
        for s in range(3):
            _dash = pg.mkPen(color='#b0b0b0', width=2, style=Qt.PenStyle.DashLine)
            left = pg.InfiniteLine(angle=90, movable=False, pen=_dash,
                                   label="", labelOpts={'position': 0.04,
                                                        'color': '#b0b0b0',
                                                        'fill': (0, 0, 0, 120)})
            right = pg.InfiniteLine(angle=90, movable=False,
                                    pen=pg.mkPen(color='#b0b0b0', width=2,
                                                 style=Qt.PenStyle.DashLine))
            left.setVisible(False); right.setVisible(False)
            self.pw.addItem(left); self.pw.addItem(right)
            self._markers.append((left, right))

    def _apply_transform(self, freqs, shape):
        tr = QTransform()
        tr.translate(freqs[0], 0)
        tr.scale((freqs[-1] - freqs[0]) / shape[1], 1)
        self.img.setTransform(tr)
        self.pw.setXRange(freqs[0], freqs[-1])

    def refresh(self, data, freqs):
        self.img.setLevels((-80, self.det.wfall_max))
        self.img.setImage(data.T, autoLevels=False)
        self._apply_transform(freqs, data.shape)
        self._update_markers(freqs)

    def _update_markers(self, freqs):
        # Lees actieve slots en zet 2 lijnen om elk signaal (kanaalranden ±12.5 kHz)
        HALF = 0.0125   # MHz — halve TETRA-kanaalbreedte (25 kHz)
        with self.det._lock:
            slots = [(s.get("freq"), s.get("db", 0.0)) for s in self.det.slots]
            thr   = self.det.threshold
            hard  = self.det.hard_threshold
            known = set(self.det.known_channels)
        for i, (left, right) in enumerate(self._markers):
            freq, db = slots[i] if i < len(slots) else (None, 0.0)
            if freq is None or not (freqs[0] <= freq <= freqs[-1]):
                left.setVisible(False); right.setVisible(False)
                continue
            is_known = round(freq, 3) in known
            if   is_known:   col = '#5ac8fa'   # bekend kanaal → lichtblauw
            elif db >= hard: col = '#ff453a'   # rood
            elif db >  thr:  col = '#ffd60a'   # geel
            else:            col = '#b0b0b0'   # lichtgrijs
            pen = pg.mkPen(color=col, width=2, style=Qt.PenStyle.DashLine)
            left.setPen(pen);  right.setPen(pen)
            left.label.setFormat((("★ " if is_known else "") + f"{freq:.3f}"))
            left.label.setColor(col)
            left.setPos(freq - HALF)
            right.setPos(freq + HALF)
            left.setVisible(True); right.setVisible(True)


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
        self.setMinimumSize(260, 300)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet(QSS)
        self._cset = QSettings("PrioSense", SETTINGS_APP)
        cw = QWidget()
        self.setCentralWidget(cw)
        lay = QVBoxLayout(cw)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        self.alarm_card = AlarmCard()
        self.alarm_card.setMaximumHeight(80)
        lay.addWidget(self.alarm_card)
        self.bars = SignalDisplay("signal_view_compact")
        lay.addWidget(self.bars, stretch=1)
        hint = QLabel("Druk  C  voor volledig scherm")
        hint.setFont(_sys_font(7))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f"color: {C['gray3']};")
        lay.addWidget(hint)

        # Verstelbaar venster: opgeslagen grootte herstellen, anders standaard
        _g = self._cset.value("compact_geometry")
        if _g is not None:
            self.restoreGeometry(_g)
        else:
            self.resize(320, 400)

    def _save_geo(self):
        self._cset.setValue("compact_geometry", self.saveGeometry())

    def hideEvent(self, event):
        self._save_geo()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._save_geo()
        super().closeEvent(event)

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

        # Instellingen laden (aparte opslag per instantie bij vergelijking)
        self._settings = QSettings("PrioSense", SETTINGS_APP)
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
        det.center_freq    = _load("center_freq",    det.center_freq,    cast=int, lo=375_000_000, hi=397_000_000)
        det.slot_floor     = _load("slot_floor",     det.slot_floor,     lo=0,   hi=40)
        det.hang_time      = _load("hang_time",      det.hang_time,      lo=0.5, hi=10)
        det.hard_threshold = _load("hard_threshold", det.hard_threshold, lo=10,  hi=60)
        det.wfall_max      = _load("wfall_max",      det.wfall_max,      lo=-50, hi=-10)
        det.auto_gain      = self._settings.value("auto_gain", "false") == "true"
        det.muted          = self._settings.value("muted",     "false") == "true"
        det.adaptive_filter = self._settings.value("adaptive_filter", "false") == "true"
        det.occupancy_check = self._settings.value("occupancy_check", "true")  == "true"
        det.agr_enabled     = self._settings.value("agr_enabled",     "true")  == "true"
        det.debug_logging   = self._settings.value("debug_logging",   "false") == "true"
        saved_mode         = _load("mode_idx", 1, cast=int, lo=0, hi=len(MODES)-1)
        # Custom modus waarden laden
        MODES[1]["slot_floor"]     = _load("custom_floor", MODES[1]["slot_floor"],     lo=0,   hi=40)
        MODES[1]["threshold"]      = _load("custom_thr",   MODES[1]["threshold"],      lo=5,   hi=50)
        MODES[1]["hard_threshold"] = _load("custom_hard",  MODES[1]["hard_threshold"], lo=10,  hi=60)
        MODES[1]["hang_time"]      = _load("custom_hang",  MODES[1]["hang_time"],      lo=0.5, hi=10)

        # Drempels ALTIJD uit de herstelde modus halen — anders kan een oude
        # opgeslagen drempel niet overeenkomen met de getoonde modus (Stad/Snelweg
        # zijn vast; Custom-waarden staan al in MODES[1]).
        _m = MODES[saved_mode]
        det.threshold      = _m["threshold"]
        det.hard_threshold = _m["hard_threshold"]
        det.slot_floor     = _m["slot_floor"]
        det.hang_time      = _m["hang_time"]
        det.mode_name      = _m["name"]

        self.setWindowTitle("PrioSense" + TITLE_SUFFIX)
        # In vergelijkingsmodus kleiner minimum zodat 2 vensters naast elkaar passen
        if TITLE_SUFFIX:
            self.setMinimumSize(680, 620)
        else:
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

        # Statusbalk — verbinding, gain, AGR, modus in één oogopslag
        self.status_bar = QLabel("●  Opstarten…")
        self.status_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_bar.setFont(_sys_font(8, bold=True))
        self.status_bar.setFixedHeight(22)
        self.status_bar.setStyleSheet(
            f"color:{C['gray2']}; background:{C['panel2']}; border-radius:5px;")
        main_vbox.addWidget(self.status_bar)

        # Content rij
        content = QWidget()
        content_hbox = QHBoxLayout(content)
        content_hbox.setContentsMargins(0, 0, 0, 0)
        content_hbox.setSpacing(14)
        main_vbox.addWidget(content, stretch=1)

        # ── Panel widgets aanmaken ────────────────────────────────────────────
        pg.setConfigOptions(antialias=True)
        # Twee onafhankelijke sets, zodat boven- en onderpaneel allebei vrij
        # kunnen wisselen tussen Balken / Spectrum / Waterfall.
        self.spec,  self.curve_pwr,  self.curve_base  = self._make_spectrum(det)
        self.spec2, self.curve_pwr2, self.curve_base2 = self._make_spectrum(det)
        self.wfall_panel  = WaterfallPanelWidget(det)
        self.wfall_panel2 = WaterfallPanelWidget(det)
        self.bars   = SignalDisplay("signal_view_top")
        self.bars2  = SignalDisplay("signal_view_bot")
        self.hist_panel  = DetectionHistoryWidget()

        # ── Linker kolom: 2 panel slots ───────────────────────────────────────
        left  = QWidget()
        l_box = QVBoxLayout(left)
        l_box.setContentsMargins(0, 0, 0, 0)
        l_box.setSpacing(6)

        _top_idx = _load("slot_top_idx", 0, cast=int, lo=0, hi=2)
        _bot_idx = _load("slot_bot_idx", 0, cast=int, lo=0, hi=2)
        self.slot_top = PanelSlot(
            names   = ["Spectrum", "Waterfall", "Signaalbalken"],
            widgets = [self.spec, self.wfall_panel, self.bars],
            initial_idx=_top_idx)
        self.slot_bot = PanelSlot(
            names   = ["Signaalbalken", "Spectrum", "Waterfall"],
            widgets = [self.bars2, self.spec2, self.wfall_panel2],
            initial_idx=_bot_idx)

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
        self.last_det_lbl = QLabel("Laatste detectie: —")
        self.last_det_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.last_det_lbl.setFont(_sys_font(8))
        self.last_det_lbl.setStyleSheet(f"color: {C['gray3']};")
        tm_box.addWidget(self.last_det_lbl)
        self._last_detection_time = None
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

        # ── Tab Instellingen — gegroepeerde kaarten (Apple-stijl) ─────────────
        tab_set = QWidget()
        ts_box  = QVBoxLayout(tab_set)
        ts_box.setContentsMargins(10, 12, 10, 12)
        ts_box.setSpacing(16)

        _combo_qss = (f"QComboBox {{ background:{C['panel2']}; color:{C['gray1']};"
                      f" border:1px solid {C['sep']}; border-radius:6px; padding:2px 8px; }}"
                      f" QComboBox:hover {{ border-color:{C['blue']}; }}"
                      f" QComboBox QAbstractItemView {{ background:{C['panel2']};"
                      f" color:{C['white']}; selection-background-color:{C['panel']}; }}")

        def _slider_row(sl):
            w = QWidget()
            wl = QVBoxLayout(w); wl.setContentsMargins(12, 8, 12, 8); wl.setSpacing(0)
            wl.addWidget(sl)
            return w

        self._mode_idx = saved_mode

        # ═══ GEVOELIGHEID ═══════════════════════════════════════════════════
        grp_sens = SettingsGroup("Gevoeligheid")
        self.btn_mode = QPushButton(MODES[self._mode_idx]["name"])
        _mc = MODE_COLORS[MODES[self._mode_idx]["name"]]
        self.btn_mode.setStyleSheet(f"color:{_mc}; border-color:{_mc};")
        self.btn_mode.setMinimumHeight(30)
        self.btn_mode.setMinimumWidth(92)
        self.btn_mode.clicked.connect(self._on_mode)
        self.btn_mode_info = QPushButton("ⓘ")
        self.btn_mode_info.setFixedSize(30, 30)
        self.btn_mode_info.setFont(_sys_font(13))
        self.btn_mode_info.setStyleSheet(f"color:{_mc}; border-color:{_mc};")
        self.btn_mode_info.clicked.connect(self._show_mode_info)
        mode_ctrl = QWidget()
        mc_l = QHBoxLayout(mode_ctrl); mc_l.setContentsMargins(0, 0, 0, 0); mc_l.setSpacing(6)
        mc_l.addWidget(self.btn_mode); mc_l.addWidget(self.btn_mode_info)
        grp_sens.add(SettingsRow("Rijmodus", mode_ctrl, "Stad · Custom · Snelweg"))

        custom = MODES[1]  # Custom is altijd index 1
        self.sl_custom_floor = LabeledSlider("Custom vloer dB",   0,  40, custom["slot_floor"],     step=1.0, color=C['gray2'])
        self.sl_custom_thr   = LabeledSlider("Custom oranje dB",  5,  50, custom["threshold"],      step=1.0, color=C['orange'])
        self.sl_custom_hard  = LabeledSlider("Custom rood dB",   10,  60, custom["hard_threshold"], step=1.0, color=C['red'])
        self.sl_custom_hang  = LabeledSlider("Custom hang s",    0.5, 10, custom["hang_time"],      step=0.5, fmt="{:.1f}", color=C['blue'])
        self.sl_custom_floor.valueChanged.connect(lambda v: self._update_custom("slot_floor",     v))
        self.sl_custom_thr  .valueChanged.connect(lambda v: self._update_custom("threshold",      v))
        self.sl_custom_hard .valueChanged.connect(lambda v: self._update_custom("hard_threshold", v))
        self.sl_custom_hang .valueChanged.connect(lambda v: self._update_custom("hang_time",      v))
        self._custom_section = QWidget()
        cs_box = QVBoxLayout(self._custom_section); cs_box.setContentsMargins(0, 0, 0, 0); cs_box.setSpacing(0)
        for sl in [self.sl_custom_floor, self.sl_custom_thr, self.sl_custom_hard, self.sl_custom_hang]:
            line = QFrame(); line.setFixedHeight(1)
            line.setStyleSheet(f"background:{C['sep']}; border:none; margin-left:12px; margin-right:12px;")
            cs_box.addWidget(line); cs_box.addWidget(_slider_row(sl))
        self._custom_section.setVisible(self._mode_idx == 1)
        grp_sens.add(self._custom_section, divider=False)
        ts_box.addWidget(grp_sens)

        # ═══ ONTVANGER ══════════════════════════════════════════════════════
        grp_recv = SettingsGroup("Ontvanger")
        self.sl_gain = LabeledSlider("Gain dB", 0, 49, det.gain_db,
            step=1.0, color=C['blue'])
        self.sl_gain.valueChanged.connect(self._on_gain)
        grp_recv.add(_slider_row(self.sl_gain))

        self.sw_auto = ToggleSwitch(det.auto_gain, on_color=C['blue'])
        self.sw_auto.toggled.connect(self._on_auto)
        grp_recv.add(SettingsRow("Auto Gain", self.sw_auto, "Dongle regelt versterking zelf"))

        self.sl_freq = LabeledSlider("Center MHz", 379.0, 396.0, det.center_freq / 1e6,
            step=0.05, fmt="{:.2f}", color=C['orange'])
        self.sl_freq.valueChanged.connect(self._on_freq)
        grp_recv.add(_slider_row(self.sl_freq))

        band_presets = [
            ("UL midden 382.5",  382.5),
            ("UL laag 381",      381.0),
            ("UL hoog 384",      384.0),
            ("DL mast 392.5",    392.5),
            ("DL laag 391",      391.0),
            ("DL hoog 394",      394.0),
        ]
        self._band_combo = QComboBox()
        self._band_combo.setFixedHeight(28)
        self._band_combo.setFixedWidth(130)
        self._band_combo.setFont(_sys_font(8))
        self._band_combo.setStyleSheet(_combo_qss)
        for label, _ in band_presets:
            self._band_combo.addItem(label)
        self._band_freqs = [mhz for _, mhz in band_presets]
        self._band_combo.currentIndexChanged.connect(self._on_band_select)
        grp_recv.add(SettingsRow("Bandvenster", self._band_combo, "UL=voertuigen, DL=masten"))

        # Waterfall-helderheid (bovengrens kleurschaal; lager = zwak feller)
        self.sl_wfall = LabeledSlider("Waterfall helderheid", -50, -10, det.wfall_max,
            step=1.0, fmt="{:.0f}", color=C['blue'])
        self.sl_wfall.valueChanged.connect(lambda v: setattr(self.det, 'wfall_max', float(v)))
        grp_recv.add(_slider_row(self.sl_wfall))

        # Bekende kanalen (vaste politiekanalen) — komma-gescheiden MHz
        self._known_edit = QLineEdit(self._settings.value(
            "known_channels", "382.900"))
        self._known_edit.setFixedHeight(28)
        self._known_edit.setFixedWidth(130)
        self._known_edit.setFont(_sys_font(9))
        self._known_edit.setPlaceholderText("382.900")
        self._known_edit.setStyleSheet(
            f"QLineEdit {{ background:{C['panel2']}; color:#5ac8fa;"
            f" border:1px solid #5ac8fa; border-radius:6px; padding:2px 8px; }}")
        self._known_edit.textChanged.connect(self._on_known_channels)
        self._on_known_channels(self._known_edit.text())
        grp_recv.add(SettingsRow("Bekende kanalen", self._known_edit, "★ gemarkeerd"))
        ts_box.addWidget(grp_recv)

        # ═══ FILTERS ════════════════════════════════════════════════════════
        grp_filt = SettingsGroup("Filters")
        self.sw_adaptive = ToggleSwitch(det.adaptive_filter, on_color=C['green'])
        self.sw_adaptive.toggled.connect(self._on_adaptive)
        grp_filt.add(SettingsRow("Storingsfilter", self.sw_adaptive,
                                 "Onderdrukt langdurige storing tijdens rijden"))
        self.sw_occ = ToggleSwitch(det.occupancy_check, on_color=C['green'])
        self.sw_occ.toggled.connect(self._toggle_occupancy)
        grp_filt.add(SettingsRow("Birdie-filter", self.sw_occ,
                                 "Filtert vaste dongle-spoken weg"))
        self.sw_agr = ToggleSwitch(det.agr_enabled, on_color=C['green'])
        self.sw_agr.toggled.connect(self._toggle_agr)
        grp_filt.add(SettingsRow("AGR", self.sw_agr,
                                 "Automatische gain-reductie bij hard alarm"))
        ts_box.addWidget(grp_filt)

        # ═══ SYSTEEM ════════════════════════════════════════════════════════
        grp_sys = SettingsGroup("Systeem")
        btn_reset = QPushButton("Reset")
        btn_reset.setMinimumHeight(28); btn_reset.setMinimumWidth(80)
        btn_reset.clicked.connect(det.reset_baseline)
        grp_sys.add(SettingsRow("Baseline", btn_reset, "Herijk de ruisvloer  [R]"))
        btn_wfall = QPushButton("Openen")
        btn_wfall.setMinimumHeight(28); btn_wfall.setMinimumWidth(80)
        btn_wfall.clicked.connect(self._open_waterfall)
        grp_sys.add(SettingsRow("Waterfall venster", btn_wfall))
        self.sw_debug = ToggleSwitch(det.debug_logging, on_color=C['red'])
        self.sw_debug.toggled.connect(self._toggle_debug)
        grp_sys.add(SettingsRow("Debug Log", self.sw_debug,
                                "Schrijf spectrum-data naar CSV"))
        ts_box.addWidget(grp_sys)

        ts_box.addStretch()
        hint = QLabel("R = Reset    ·    M = Modus    ·    C = Compact")
        hint.setFont(_sys_font(7))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f"color: {C['gray3']};")
        ts_box.addWidget(hint)

        # Instellingen in scroll-gebied → niet samengedrukt bij klein venster
        set_scroll = QScrollArea()
        set_scroll.setWidgetResizable(True)
        set_scroll.setFrameShape(QFrame.Shape.NoFrame)
        set_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        set_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        set_scroll.setWidget(tab_set)
        self._tabs.addTab(set_scroll, "Instellingen")

        # Smalle altijd-zichtbare knop links van de tabs (in-/uitklappen)
        self.btn_sidebar = QPushButton("☰")
        self.btn_sidebar.setFixedWidth(26)
        self.btn_sidebar.setFont(_sys_font(13, bold=True))
        self.btn_sidebar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.btn_sidebar.setToolTip("Menu in-/uitklappen")
        self.btn_sidebar.setStyleSheet(
            f"QPushButton {{ background:{C['panel2']}; color:{C['gray2']};"
            f" border:1px solid {C['sep']}; border-radius:5px; }}"
            f" QPushButton:hover {{ color:{C['white']}; }}")
        self.btn_sidebar.clicked.connect(self._toggle_sidebar)
        content_hbox.addWidget(self.btn_sidebar)
        content_hbox.addWidget(self._tabs)

        # ── Onderste balk ─────────────────────────────────────────────────────
        bottom = QWidget()
        bottom.setFixedHeight(56)
        bot_lay = QHBoxLayout(bottom)
        bot_lay.setContentsMargins(0, 4, 0, 0)
        bot_lay.setSpacing(10)

        btn_compact = QPushButton("⊡  Compact  [C]")
        btn_compact.setMinimumHeight(46)
        btn_compact.setFont(_sys_font(12, bold=True))
        btn_compact.clicked.connect(self._toggle_compact)
        bot_lay.addWidget(btn_compact, stretch=2)

        btn_bars_fs = QPushButton("▦  Volledig scherm")
        btn_bars_fs.setMinimumHeight(46)
        btn_bars_fs.setFont(_sys_font(12, bold=True))
        btn_bars_fs.setToolTip("Volledig scherm balkjes")
        btn_bars_fs.clicked.connect(self._open_bars_fullscreen)
        bot_lay.addWidget(btn_bars_fs, stretch=2)

        self.btn_mute = QPushButton("🔊  Geluid")
        self.btn_mute.setMinimumHeight(46)
        self.btn_mute.setFont(_sys_font(12, bold=True))
        self.btn_mute.clicked.connect(self._toggle_mute)
        bot_lay.addWidget(self.btn_mute, stretch=2)

        main_vbox.addWidget(bottom)

        # Mute knop juiste staat bij opstarten
        if det.muted:
            self.btn_mute.setText("🔇  Gedempt")
            self.btn_mute.setStyleSheet(f"color: {C['gray2']}; border-color: {C['gray2']};")

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

        # Snelle timer voor waterfall (vangt korte bursts beter)
        self._wfall_timer = QTimer(self)
        self._wfall_timer.timeout.connect(self._tick_waterfall)
        self._wfall_timer.start(100)

        # Opgeslagen UI-staat herstellen: actief tabblad en venstergrootte/positie
        try:
            self._tabs.setCurrentIndex(int(self._settings.value("tab_idx", 0)))
        except Exception:
            pass
        if self._settings.value("sidebar_vis", "true") == "false":
            self._tabs.setVisible(False)
            self.btn_sidebar.setText("≡")
        _geo = self._settings.value("geometry")
        if _geo is not None:
            try: self.restoreGeometry(_geo)
            except Exception: pass

    def _make_spectrum(self, det):
        """Bouwt een spectrum-plot en geeft (widget, curve_pwr, curve_base) terug."""
        spec = pg.PlotWidget()
        spec.setBackground(C['panel'])
        spec.showGrid(x=True, y=True, alpha=0.08)
        spec.setYRange(-90, -10)
        spec.setXRange(det.freqs[0], det.freqs[-1])
        spec.setLabel('left', 'dBm')
        spec.setLabel('bottom', 'MHz')
        spec.getAxis('left').setTextPen(QColor(C['gray2']))
        spec.getAxis('bottom').setTextPen(QColor(C['gray2']))
        spec.getAxis('left').setPen(QColor(C['sep']))
        spec.getAxis('bottom').setPen(QColor(C['sep']))
        spec.setMouseEnabled(x=False, y=False)
        for cf in DISPLAY_GRID:
            spec.addItem(pg.InfiniteLine(pos=cf, angle=90,
                pen=pg.mkPen(color=(10, 132, 255, 38), width=0.8)))
        curve_pwr = spec.plot(det.freqs, det.power,
            pen=pg.mkPen(color='#0a84ff', width=1.8))
        base_pen = pg.mkPen(color='#ff9f0a', width=1.0)
        base_pen.setStyle(Qt.PenStyle.DashLine)
        curve_base = spec.plot(det.freqs, det.power, pen=base_pen)
        legend = spec.addLegend(offset=(-10, 10))
        legend.setLabelTextColor(C['gray2'])
        legend.addItem(curve_pwr, 'Vermogen')
        legend.addItem(curve_base, 'Baseline')
        return spec, curve_pwr, curve_base

    def _tick_waterfall(self):
        """Snelle timer (100ms) — spectrum en waterfall, vangt korte bursts."""
        try:
            top_name = self.slot_top.current_name()
            bot_name = self.slot_bot.current_name()
            ext_wf   = (self._wfall_win is not None and self._wfall_win.isVisible())
            want_wfall = ext_wf or top_name == "Waterfall" or bot_name == "Waterfall"
            want_spec  = (top_name == "Spectrum" or bot_name == "Spectrum")
            if not (want_wfall or want_spec):
                return
            with self.det._lock:
                freqs      = self.det.freqs.copy()
                pwr        = self.det.power.copy() if want_spec else None
                base       = ((self.det.baseline.copy()
                               if self.det.baseline is not None else pwr.copy())
                              if want_spec else None)
                wfall_data = self.det.wfall.copy() if want_wfall else None
            # Spectrum (boven en/of onder)
            if top_name == "Spectrum":
                self.curve_pwr.setData(freqs, pwr);  self.curve_base.setData(freqs, base)
            if bot_name == "Spectrum":
                self.curve_pwr2.setData(freqs, pwr); self.curve_base2.setData(freqs, base)
            # Waterfall (boven en/of onder)
            if top_name == "Waterfall" and wfall_data is not None:
                self.wfall_panel.refresh(wfall_data, freqs)
            if bot_name == "Waterfall" and wfall_data is not None:
                self.wfall_panel2.refresh(wfall_data, freqs)
            if ext_wf and wfall_data is not None:
                self._wfall_win.refresh(wfall_data, freqs)
        except Exception:
            pass

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
        self.sl_gain.slider.setValue(
            round((m["gain_db"] - self.sl_gain._lo) / self.sl_gain._step))
        self.sw_auto.setChecked(False)
        col = MODE_COLORS[m["name"]]
        self.btn_mode.setText(m["name"])
        self.btn_mode.setStyleSheet(f"color:{col}; border-color:{col};")
        self.btn_mode_info.setStyleSheet(f"color:{col}; border-color:{col};")
        self._custom_section.setVisible(self._mode_idx == 1)

    def _on_gain(self, v):
        self.det.auto_gain = False
        self.det.set_gain(v, auto=False)
        self.sw_auto.setChecked(False)

    def _on_freq(self, v):
        self.det.set_center_freq(v)
        new_f = self.det.freqs
        # Beide spectrum-panelen meeschuiven
        self.curve_pwr.setData(new_f, self.det.power)
        self.curve_base.setData(new_f, self.det.power)
        self.spec.setXRange(new_f[0], new_f[-1])
        self.curve_pwr2.setData(new_f, self.det.power)
        self.curve_base2.setData(new_f, self.det.power)
        self.spec2.setXRange(new_f[0], new_f[-1])

    def _on_band_select(self, idx):
        mhz = self._band_freqs[idx]
        self.sl_freq.setValue(mhz)

    def _on_known_channels(self, text):
        freqs = set()
        for part in text.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                freqs.add(round(float(part), 3))
            except ValueError:
                pass
        self.det.known_channels = freqs
        self._settings.setValue("known_channels", text)

    def _on_auto(self, checked):
        self.det.auto_gain = bool(checked)
        self.det.set_gain(self.det.gain_db, auto=self.det.auto_gain)

    def _on_adaptive(self, checked):
        self.det.adaptive_filter = bool(checked)
        if not self.det.adaptive_filter:
            # alles vrijgeven bij uitschakelen
            self.det._suppressed.clear()
            self.det._hot_since.clear()

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

    def _toggle_sidebar(self):
        vis = not self._tabs.isVisible()
        self._tabs.setVisible(vis)
        self.btn_sidebar.setText("☰" if vis else "≡")

    def _toggle_occupancy(self, checked):
        self.det.occupancy_check = bool(checked)

    def _toggle_agr(self, checked):
        self.det.agr_enabled = bool(checked)
        if not self.det.agr_enabled:
            self.det.agr_active = False

    def _open_bars_fullscreen(self):
        if not hasattr(self, '_bars_fs_win') or self._bars_fs_win is None:
            self._bars_fs_win = BarFullscreenWindow(self.det)
        if self._bars_fs_win.isVisible():
            self._bars_fs_win.hide()
        else:
            self._bars_fs_win.showFullScreen()

    def _toggle_debug(self, checked):
        self.det.debug_logging = bool(checked)

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
        with self.det._lock:
            alarm      = self.det.alarm
            alvl       = self.det.alarm_level
            afrq       = self.det.alarm_freq
            adb        = self.det.alarm_db
            stat       = self.det.status
            slots_snap = [dict(s) for s in self.det.slots]
            agr_active = self.det.agr_active
            gain_now   = self.det.gain_db
            activity   = self.det.alarm_activity
            width      = self.det.alarm_width
            known      = round(afrq, 3) in self.det.known_channels

        # (Spectrum en waterfall worden in de snelle 100ms timer bijgewerkt)

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
                              live_db=self.det.live_db, live_freq=self.det.live_freq,
                              hang_time=self.det.hang_time, wanted_stars=self.det.wanted_stars)
        self.bars2.update_data(slots_snap, self.det.threshold, self.det.slot_floor, trends,
                               hard_threshold=self.det.hard_threshold,
                               live_db=self.det.live_db, live_freq=self.det.live_freq,
                               hang_time=self.det.hang_time, wanted_stars=self.det.wanted_stars)

        # AGR actief → switch oranje kleuren als gain is verlaagd
        if hasattr(self, 'sw_agr'):
            self.sw_agr._on_color = QColor(C['orange'] if agr_active else C['green'])
            self.sw_agr.update()

        # Fullscreen balkjes updaten
        if hasattr(self, '_bars_fs_win') and self._bars_fs_win and self._bars_fs_win.isVisible():
            self._bars_fs_win.bars.update_data(slots_snap, self.det.threshold,
                                               self.det.slot_floor, trends,
                                               hard_threshold=self.det.hard_threshold,
                                               live_db=self.det.live_db,
                                               live_freq=self.det.live_freq,
                                               hang_time=self.det.hang_time,
                                               wanted_stars=self.det.wanted_stars)

        if not alarm:
            self.alarm_card.set_idle(activity)
            self._compact_win.alarm_card.set_idle(activity)
            self._hulp_alert.hide_alert()
        elif alvl == 2:
            self.alarm_card.set_red(afrq, adb, activity, width, known)
            self._compact_win.alarm_card.set_red(afrq, adb, activity, width, known)
            if not self._hulp_alert.isVisible():
                self._hulp_alert.show_alert(afrq, adb)
                # Windows toast als geminimaliseerd
                if self.isMinimized() and sys.platform == "win32":
                    _show_toast("PrioSense 🚨", f"Hulpdienst: {afrq:.3f} MHz  +{adb:.0f} dB")
            else:
                self._hulp_alert.update_alert(afrq, adb)
        else:
            self.alarm_card.set_orange(afrq, adb, activity, width, known)
            self._compact_win.alarm_card.set_orange(afrq, adb, activity, width, known)
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
                hard_threshold=self.det.hard_threshold,
                live_db=self.det.live_db, live_freq=self.det.live_freq,
                hang_time=self.det.hang_time, wanted_stars=self.det.wanted_stars)

        # (Waterfall wordt in de snelle 100ms timer bijgewerkt)

        # Sessie score bijwerken
        if alarm and adb > 0:
            self._session_peaks.append(adb)
        n  = len(self._session_peaks)
        avg = sum(self._session_peaks) / n if n else 0
        self.score_lbl.setText(
            f"Sessie: {n} detecties  ·  gem. +{avg:.0f} dB" if n
            else "Sessie: geen detecties nog")

        # Statusbalk bijwerken
        verbonden = (stat == "Scannen")
        conn_dot  = "🟢" if verbonden else "🟡"
        agr_txt   = "AGR●" if agr_active else ("AGR" if self.det.agr_enabled else "—")
        self.status_bar.setText(
            f"{conn_dot} {stat}   ·   {gain_now:.0f} dB   ·   {agr_txt}   ·   {self.det.mode_name}")
        self.status_bar.setStyleSheet(
            f"color:{C['gray1']}; background:{C['panel2']}; border-radius:5px;")

        # Laatste detectie tijd
        if alarm and adb > 0:
            self._last_detection_time = time.time()
        if self._last_detection_time is not None:
            elapsed = time.time() - self._last_detection_time
            if elapsed < 60:
                txt = f"{int(elapsed)}s geleden"
            elif elapsed < 3600:
                txt = f"{int(elapsed/60)}m geleden"
            else:
                txt = f"{int(elapsed/3600)}u geleden"
            self.last_det_lbl.setText(f"Laatste detectie: {txt}")

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
        self._settings.setValue("adaptive_filter", str(self.det.adaptive_filter).lower())
        self._settings.setValue("custom_floor",   MODES[1]["slot_floor"])
        self._settings.setValue("custom_thr",     MODES[1]["threshold"])
        self._settings.setValue("custom_hard",    MODES[1]["hard_threshold"])
        self._settings.setValue("custom_hang",    MODES[1]["hang_time"])
        # UI-staat onthouden
        self._settings.setValue("occupancy_check", str(self.det.occupancy_check).lower())
        self._settings.setValue("agr_enabled",     str(self.det.agr_enabled).lower())
        self._settings.setValue("debug_logging",   str(self.det.debug_logging).lower())
        self._settings.setValue("wfall_max",       self.det.wfall_max)
        self._settings.setValue("slot_top_idx",    self.slot_top._idx)
        self._settings.setValue("slot_bot_idx",    self.slot_bot._idx)
        self._settings.setValue("tab_idx",         self._tabs.currentIndex())
        self._settings.setValue("sidebar_vis",      str(self._tabs.isVisible()).lower())
        self._settings.setValue("geometry",        self.saveGeometry())
        self._timer.stop()
        self.det.stop()
        if self._wfall_win:
            self._wfall_win.close()
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
        self.bars = SignalDisplay("signal_view_fs")
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
    # Opstartargumenten verwerken (voor vergelijkingsopstelling met 2 instanties)
    global TCP_PORT, DEVICE_IDX, EXTERN_RTLTCP, TITLE_SUFFIX, SETTINGS_APP, TILE
    global LOG_PATH, DEBUG_LOG_PATH
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--port" and i+1 < len(argv):
            try: TCP_PORT = int(argv[i+1])
            except ValueError: pass
        elif a == "--device" and i+1 < len(argv):
            try: DEVICE_IDX = int(argv[i+1])
            except ValueError: pass
        elif a == "--extern":
            EXTERN_RTLTCP = True
        elif a == "--tile" and i+1 < len(argv):
            TILE = argv[i+1].upper()
        elif a == "--titel" and i+1 < len(argv):
            label = argv[i+1]
            TITLE_SUFFIX = " — " + label
            SETTINGS_APP = "PrioSense_" + label.replace(" ", "")
            # Aparte logbestanden per instantie zodat ze elkaar niet overschrijven
            safe = "".join(c for c in label if c.isalnum() or c in "+-_")
            LOG_PATH       = os.path.join(_LOG_DIR, f"detections_{safe}.csv")
            DEBUG_LOG_PATH = os.path.join(_LOG_DIR, f"debug_log_{safe}.csv")

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

    # Vensterhelft op het scherm zetten (vergelijkingsmodus)
    if TILE:
        scr = app.primaryScreen().availableGeometry()
        if TILE in ("L", "R"):
            w = scr.width() // 2
            x = scr.x() + (0 if TILE == "L" else w)
            win.setGeometry(x, scr.y(), w, scr.height())
        elif TILE in ("1", "2", "3"):  # drie kolommen
            w = scr.width() // 3
            x = scr.x() + (int(TILE) - 1) * w
            win.setGeometry(x, scr.y(), w, scr.height())

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
