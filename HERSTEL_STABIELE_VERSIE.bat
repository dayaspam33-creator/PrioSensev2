@echo off
REM Schakelt terug naar de stabiele versie (tag stabiel-v1)
REM en bouwt de exe opnieuw

echo Terugschakelen naar stabiele versie...
cd /d "C:\Users\dayas\TetraPC"
git checkout stabiel-v1 -- tetra_detector.py

echo Stoppen oude processen...
taskkill /F /IM PrioSense.exe >nul 2>&1
timeout /t 1 /nobreak >nul

echo Opnieuw bouwen...
python -m PyInstaller --onefile --windowed --name PrioSense --icon priosense.ico --add-data "priosense.ico;." --splash priosense_splash.png --clean tetra_detector.py

copy /Y _siren.wav dist\_siren.wav >nul

echo Klaar! Stabiele versie hersteld in dist\PrioSense.exe
pause
