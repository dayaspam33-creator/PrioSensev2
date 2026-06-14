@echo off
REM Twee PrioSense-vensters naast elkaar, elk op een eigen dongle.
REM Plug-volgorde: device 0 = SMArt+, device 1 = SMArt basic

set RTL=C:\Users\dayas\Desktop\sdrsharp-x64\rtl_tcp.exe
set EXE=C:\Users\dayas\TetraPC\dist\PrioSense.exe

echo Oude processen stoppen...
taskkill /F /IM rtl_tcp.exe >nul 2>&1
taskkill /F /IM PrioSense.exe >nul 2>&1
timeout /t 1 /nobreak >nul

echo Starten rtl_tcp device 0 (SMArt+) poort 1234...
start "" /MIN "%RTL%" -a 127.0.0.1 -p 1234 -d 0 -f 382500000 -s 3200000 -g 30

echo Starten rtl_tcp device 1 (SMArt basic) poort 1235...
start "" /MIN "%RTL%" -a 127.0.0.1 -p 1235 -d 1 -f 382500000 -s 3200000 -g 30

echo Wachten op rtl_tcp...
timeout /t 5 /nobreak >nul

echo Starten PrioSense vensters (links/rechts)...
start "" "%EXE%" --extern --port 1234 --titel "SMArt+" --tile L
timeout /t 2 /nobreak >nul
start "" "%EXE%" --extern --port 1235 --titel "SMArt basic" --tile R

echo.
echo Twee PrioSense-vensters gestart (SMArt+ en SMArt basic).
echo Sluit beide vensters als je klaar bent, sluit daarna dit venster.
echo Dit venster stopt de rtl_tcp's bij sluiten.
pause
taskkill /F /IM rtl_tcp.exe >nul 2>&1
