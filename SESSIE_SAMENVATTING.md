# PrioSense — Uitgebreide Samenvatting

## Project Overzicht
RTL-SDR detector app voor C2000 TETRA netwerk (380-385 MHz) — het digitale communicatienetwerk van Nederlandse hulpdiensten (politie, ambulance, brandweer). De app detecteert wanneer hulpvoertuigen in de buurt zijn op basis van signaalsterkte.

**Hardware:** RTL2832U + R820T2 dongle  
**Protocol:** TETRA (Terrestrial Trunked Radio)  
**Verbinding:** rtl_tcp TCP server op 127.0.0.1:1234

---

## Bestanden
| Bestand | Locatie | Beschrijving |
|---|---|---|
| `tetra_detector.py` | `C:\Users\dayas\TetraPC\` | Hoofdbestand |
| `PrioSense.exe` | `C:\Users\dayas\TetraPC\dist\` | Gebouwde app |
| `detections.csv` | `C:\Users\dayas\TetraPC\dist\` | Log bestand |
| `priosense.ico` | `C:\Users\dayas\TetraPC\` | App icoon |
| `install_mac.sh` | `C:\Users\dayas\TetraPC\` | Mac installatie script (oud, nog "TETRA" naam) |
| `_beep.wav` | naast exe/script | Alarm beepgeluid |
| `_siren.wav` | naast exe/script | Sirenegeluid bij rood alarm |

---

## Architectuur

### TcpDetector klasse
De kern van de app. Draait in een aparte thread.
- Verbindt met `rtl_tcp` via TCP socket
- Leest IQ samples, voert FFT uit
- Berekent signaalsterkte per kanaal (20 TETRA kanalen in 380-385 MHz)
- Beheert baseline tracking, slots, alarm logica

**Belangrijke constanten:**
```python
DEFAULT_CENTER  = 382_500_000  # Hz
SAMPLE_RATE     = 3_200_000    # Hz  
N_CHANNELS      = 20           # aantal kanalen
WARMUP          = 150          # frames voor baseline opbouw
N_SMOOTH        = 15           # smooth frames (nauwelijks gebruikt)
BASELINE_FREEZE = 20           # dB - baseline update stopt boven dit
DB_PER_BLOCK_REL = 2.8         # (legacy, niet meer actief gebruikt)
```

**3 slots** voor gelijktijdige detecties (3 voertuigen tegelijk volgen)

**Baseline tracking:** lopend gemiddelde van achtergrondniveau. Bevriest als signaal > 20 dB boven baseline uitkomt.

**Signaalverwerking:**
- `raw_ch` — ruwe waarden → alarm, beep, balkjes
- `ch_db` — smoothed waarden → alleen baseline update beslissing

### MainWindow klasse (PyQt6)
**Linker paneel:**
- Boven slot: wisselbaar tussen Spectrum / Waterfall / Raw Data
- Onder slot: Signaalbalken (vast, geen wissel knop)

**Rechter paneel (tabs):**
- Monitor tab: Alarm card, sessie score, detectie geschiedenis, test modus, status, tijd
- Instellingen tab: Drempel slider, Gain slider, Center freq slider, Modus knop + i, Custom sliders, Auto Gain, Reset Baseline, Waterfall/Raw Data/Klassiek knoppen

**Onderste balk:** Politie knop, Compact knop, Geluid mute knop

---

## Modi Instellingen

```python
MODES = [
    {"name": "Stad",    "slot_floor": 22, "threshold": 32, "hard_threshold": 45, "hang_time": 5.0, "gain_db": 30},
    {"name": "Custom",  "slot_floor": 15, "threshold": 28, "hard_threshold": 40, "hang_time": 4.0, "gain_db": 40},
    {"name": "Snelweg", "slot_floor": 10, "threshold": 25, "hard_threshold": 42, "hang_time": 3.0, "gain_db": 40},
]
MODE_COLORS = {"Stad": oranje, "Custom": blauw, "Snelweg": groen}
```

**Balkjes schaling (dynamisch per modus):**
- Seg 1-4 groen: slot_floor -> threshold
- Seg 5-8 geel: threshold -> hard_threshold
- Seg 9-10 rood: boven hard_threshold

**Custom modus** is volledig instelbaar via sliders in Instellingen tab (vloer, oranje, rood, hang, gain). Sliders alleen zichtbaar als Custom actief is.

---

## Alarm Systeem

| Niveau | Trigger | Visueel | Geluid |
|---|---|---|---|
| 0 — Niets | onder threshold | grijs | stil |
| 1 — Oranje | > threshold | oranje alarm card | 1x beep bij eerste activering |
| 2 — Rood | > hard_threshold | rode alarm card + hulpdienst overlay | sirene (eenmalig per activering) |

**Sirene:** 2x sweep van 800Hz -> 1400Hz -> 800Hz, speelt in aparte thread  
**Beep:** 900Hz, 0.3s, volume 0.05  
**Mute knop:** onderdrukt alle geluiden, visueel alarm blijft werken  
**Beep logica:** alleen in hoofdvenster; klassiek venster heeft eigen beep logica

---

## Detectie Logging (CSV)
- Pad: naast de exe (niet in tijdelijke PyInstaller map)
- Cooldown per (freq, slot_num) tupel om duplicaten te voorkomen
- Format: timestamp, frequentie, dB, slot nummer

---

## QSettings Opslag
Organisatie: "PrioSense", App: "PrioSense"  
Opgeslagen waarden: threshold, gain_db, auto_gain, center_freq, slot_floor, hang_time, hard_threshold, muted, mode_idx, custom_floor, custom_thr, custom_hard, custom_hang

**Bij incompatibele settings (app start niet goed):**
```
reg delete "HKEY_CURRENT_USER\Software\PrioSense" /f
```

**Robuuste loading:** elke waarde heeft min/max grenzen, bij ongeldige waarde valt hij terug op standaard.

---

## Geluid Generatie
Beide wav-bestanden worden automatisch aangemaakt bij eerste start:
```
_BEEP_WAV  -> 900Hz sinus, 0.3s
_SIREN_WAV -> sweep 800->1400->800Hz, 2 cycli x 0.4s
```
Windows: winsound.PlaySound(), Mac: afplay, Linux: aplay/paplay

---

## Icoon & Branding
- **Naam:** PrioSense (hernoemd van TETRA C2000 Detector)
- **Icoon generatie:** Pillow, 4x supersample + LANCZOS resize, 7 groottes (16/24/32/48/64/128/256px)
- **Design:** Donkere achtergrond, navy blauwe P-boog, groene signaalcirkels
- **PyInstaller:** --icon priosense.ico --add-data "priosense.ico;."
- **Taakbalk fix:** SetCurrentProcessExplicitAppUserModelID + ExtractIconW + WM_SETICON met ICON_SMALL2
- **Startup:** win.setWindowIcon(icon) expliciet op venster gezet

**Build commando:**
```
cd C:\Users\dayas\TetraPC
python -m PyInstaller --onefile --windowed --name PrioSense --icon priosense.ico --add-data "priosense.ico;." --clean tetra_detector.py
```

---

## rtl_tcp Integratie
```
RTL_TCP_PATH = C:\Users\dayas\Desktop\sdrsharp-x64\rtl_tcp.exe
TCP_HOST     = 127.0.0.1
TCP_PORT     = 1234
DEVICE_IDX   = 1
```
- Start automatisch bij opstarten app
- CREATE_NO_WINDOW flag -> geen zwart terminalvenster
- 3 seconden wachten na starten
- Auto-herverbinden bij verbinding weggevallen

---

## Problemen & Oplossingen

| Probleem | Oorzaak | Oplossing |
|---|---|---|
| App start niet / "Opstarten" status | Incompatibele QSettings van vorige versie | reg delete "HKEY_CURRENT_USER\Software\PrioSense" /f |
| Dongle oververhitting | Thermische ruis | Afkoelen, evt. ventilatie |
| Build mislukt PermissionError | Exe nog open | Sluiten of Stop-Process -Name "PrioSense" -Force |
| Baseline instabiel | Dongle te heet | Afkoelen |
| Icoon niet in taakbalk | Windows cache / AUMID | ExtractIconW + ICON_SMALL2 via Win32 API |

---

## Nog NIET Gebouwd
De **gain per modus** wijziging is in de code maar de exe is nog niet opnieuw gebouwd. Dit was de laatste wijziging:
- MODES heeft nu "gain_db" veld per modus
- _on_mode() past gain + gain slider aan bij wisselen
- Bouw nog uitvoeren:
```
cd C:\Users\dayas\TetraPC
python -m PyInstaller --onefile --windowed --name PrioSense --icon priosense.ico --add-data "priosense.ico;." --clean tetra_detector.py
```

---

## Openstaande Punten / Discussies
- **Snelweg hang time 3s** — gebruiker twijfelde of dit juist is, nog niet aangepast
- **Anti-mast filter** — bewust NIET geimplementeerd (risico dat echte politie-signalen ook gefilterd worden)
- **Smoothing (N_SMOOTH)** — technisch aanwezig maar heeft nauwelijks effect meer
- **Mac install script** — nog oude "TETRA" naam, niet bijgewerkt naar PrioSense
- **Raspberry Pi versie** — besproken maar nog niet gebouwd

---

## Ideen Besproken (nog niet geimplementeerd)
- Uurstatistieken / dag-rapport
- Kanaalactiviteit ranglijst
- Snelheidsschatting op basis van signaalverloop
- Patroonherkenning (zelfde kanaal 3x binnen 5 min)
- Pushmelding op telefoon (ntfy.sh / Pushover)
- Windows notificatie bij geminimaliseerde app
- Dongle temperatuurwaarschuwing
- Auto-herstart dongle bij verbindingsverlies
- Meerdere dongles tegelijk
- Taal instelling (NL/EN)
- Kleurthema keuze
- System tray modus
- Altijd bovenop knop
- Kaartexport als KML
- Spraakmelding via Windows TTS
- Eigen alarmsound uploaden
