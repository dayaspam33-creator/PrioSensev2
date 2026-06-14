@echo off
REM Start 3 rtl_tcp instanties + de vergelijkingslogger voor 3 dongles
REM Plug-volgorde: device 0=SMArt+, 1=SMArt basic, 2=v5

set RTL=C:\Users\dayas\Desktop\sdrsharp-x64\rtl_tcp.exe

echo Oude rtl_tcp processen stoppen...
taskkill /F /IM rtl_tcp.exe >nul 2>&1
timeout /t 1 /nobreak >nul

echo Starten rtl_tcp device 0 (SMArt+) poort 1234...
start "" /MIN "%RTL%" -a 127.0.0.1 -p 1234 -d 0 -f 392500000 -s 3200000 -g 30

echo Starten rtl_tcp device 1 (SMArt basic) poort 1235...
start "" /MIN "%RTL%" -a 127.0.0.1 -p 1235 -d 1 -f 392500000 -s 3200000 -g 30

echo Starten rtl_tcp device 2 (v5) poort 1236...
start "" /MIN "%RTL%" -a 127.0.0.1 -p 1236 -d 2 -f 392500000 -s 3200000 -g 30

echo Wachten op rtl_tcp...
timeout /t 5 /nobreak >nul

echo Starten vergelijking - laat dit venster open staan...
echo (Sluit dit venster of druk Ctrl-C om te stoppen)
echo.
cd /d C:\Users\dayas\TetraPC
python compare3_log.py

echo.
echo Test gestopt. rtl_tcp processen opruimen...
taskkill /F /IM rtl_tcp.exe >nul 2>&1
pause
