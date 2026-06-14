@echo off
REM Drie PrioSense-vensters, elk op een eigen dongle.
REM Plug-volgorde: device 0 = SMArt+, device 1 = SMArt basic, device 2 = v5

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

echo Starten rtl_tcp device 2 (v5) poort 1236...
start "" /MIN "%RTL%" -a 127.0.0.1 -p 1236 -d 2 -f 382500000 -s 3200000 -g 30

echo Wachten op rtl_tcp...
timeout /t 6 /nobreak >nul

echo Starten PrioSense vensters (3 kolommen)...
start "" "%EXE%" --extern --port 1234 --titel "SMArt+" --tile 1
timeout /t 2 /nobreak >nul
start "" "%EXE%" --extern --port 1235 --titel "SMArt basic" --tile 2
timeout /t 2 /nobreak >nul
start "" "%EXE%" --extern --port 1236 --titel "v5" --tile 3

echo.
echo Drie PrioSense-vensters gestart (SMArt+, SMArt basic, v5).
echo Zet in alle drie de Debug Log AAN voor vergelijking.
echo Sluit dit venster om de rtl_tcp's te stoppen.
pause
taskkill /F /IM rtl_tcp.exe >nul 2>&1
