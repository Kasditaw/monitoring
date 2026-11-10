@echo off
cd /d "%~dp0"
title LabGuard PRO - IT Support SD Kristen Petra 1
color 0B
echo ========================================================
echo   LABGUARD PRO - IT SUPPORT SD KRISTEN PETRA 1
echo   Sistem Monitoring & Kontrol Laboratorium Komputer
echo ========================================================
echo.

rem [Cek Status Server Port 8000]
netstat -ano | findstr :8000 | findstr LISTENING >nul
if %errorlevel% equ 0 (
    echo [INFO] Server LabGuard PRO sudah AKTIF dan berjalan!
    echo Membuka aplikasi desktop guru...
    start "" python app_guru.py
    exit /b 0
)

where python >nul 2>nul
if %errorlevel% neq 0 (
    if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
        set "PATH=%LOCALAPPDATA%\Programs\Python\Python313;%LOCALAPPDATA%\Programs\Python\Python313\Scripts;%PATH%"
    ) else if exist "C:\laragon\bin\python\python-3.13\python.exe" (
        set "PATH=C:\laragon\bin\python\python-3.13;C:\laragon\bin\python\python-3.13\Scripts;%PATH%"
    ) else if exist "C:\laragon\bin\python\python.exe" (
        set "PATH=C:\laragon\bin\python;%PATH%"
    ) else (
        color 0C
        echo [ERROR] Python belum terpasang di komputer ini!
        echo Unduh Python dari https://www.python.org/downloads/ atau aktifkan modul Python di Laragon.
        pause
        exit /b 1
    )
)

echo [1/3] Memeriksa dependensi Web Server dan WebSocket...
python -c "import fastapi, uvicorn, websockets, psutil, PIL, multipart" >nul 2>nul
if %errorlevel% neq 0 (
    echo Mengunduh dan memasang modul (FastAPI, Uvicorn, WebSockets, psutil)...
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        color 0C
        echo [ERROR] Gagal memasang dependensi.
        pause
        exit /b 1
    )
)

echo [2/3] Mendeteksi IP Jaringan LAN...
set "LAN_IP="
for /f "tokens=*" %%i in ('python -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); print(s.getsockname()[0]); s.close()" 2^>nul') do set "LAN_IP=%%i"
if "%LAN_IP%"=="" set "LAN_IP=10.161.17.165"

echo [3/3] Menjalankan Server LabGuard PRO...
echo ========================================================
echo Server aktif! Biarkan jendela ini tetap TERBUKA.
echo Dashboard dapat diakses di:
echo - Komputer Guru Ini : http://localhost:8000
echo - Dari HP / Laptop  : http://%LAN_IP%:8000
echo ========================================================
echo.

rem Membuka aplikasi desktop guru setelah server aktif
start "" cmd /c "timeout /t 2 /nobreak >nul & start "" python app_guru.py"

python server_web.py
if %errorlevel% neq 0 (
    pause
)
