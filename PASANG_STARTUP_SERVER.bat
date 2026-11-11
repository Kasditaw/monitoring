@echo off
cd /d "%~dp0"
title LabGuard PRO - Pasang Auto-Startup Server
color 0B

echo ========================================================
echo   PASANG AUTO-STARTUP SERVER LABGUARD PRO (PORT 8000)
echo ========================================================
echo Script ini mendaftarkan Server LabGuard PRO ke Windows Startup
echo sehingga setiap kali komputer dinyalakan / login:
echo.
echo   [+] Server FastAPI (Port 8000) otomatis berjalan di latar belakang (senyap).
echo   [+] Tidak ada jendela hitam CMD yang mengganggu di layar.
echo   [+] Dashboard web dan pemantau lab langsung siap digunakan.
echo.
echo ========================================================
echo.

set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT_PATH=%STARTUP_DIR%\LabGuardProServer.lnk"
set "VBS_HELPER=%TEMP%\make_server_startup.vbs"

echo [1/3] Membuat pintasan Startup Windows...
echo Set oWS = WScript.CreateObject("WScript.Shell") > "%VBS_HELPER%"
echo sLinkFile = "%SHORTCUT_PATH%" >> "%VBS_HELPER%"
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> "%VBS_HELPER%"
echo oLink.TargetPath = "wscript.exe" >> "%VBS_HELPER%"
echo oLink.Arguments = """%~dp0JALANKAN_SERVER_SILENT.vbs""" >> "%VBS_HELPER%"
echo oLink.WorkingDirectory = "%~dp0" >> "%VBS_HELPER%"
echo oLink.Description = "LabGuard PRO Monitoring Server (Auto-Startup Port 8000)" >> "%VBS_HELPER%"
if exist "%~dp0static\favicon.ico" (
    echo oLink.IconLocation = "%~dp0static\favicon.ico" >> "%VBS_HELPER%"
)
echo oLink.Save >> "%VBS_HELPER%"
cscript //nologo "%VBS_HELPER%"
del "%VBS_HELPER%" >nul 2>&1

echo [SUKSES] Pintasan Startup berhasil dipasang di:
echo %SHORTCUT_PATH%
echo.

echo [2/3] Memeriksa status Server...
netstat -ano | findstr :8000 | findstr LISTENING >nul
if %errorlevel% equ 0 (
    echo [INFO] Server saat ini sudah aktif di Port 8000!
) else (
    echo [INFO] Menjalankan Server LabGuard PRO di latar belakang sekarang...
    wscript.exe "%~dp0JALANKAN_SERVER_SILENT.vbs"
    timeout /t 3 /nobreak >nul
)

echo.
echo [3/3] Memverifikasi Port 8000...
netstat -ano | findstr :8000 | findstr LISTENING >nul
if %errorlevel% equ 0 (
    color 0A
    echo ========================================================
    echo   [SUKSES] SERVER TELAH AKTIF DI LATAR BELAKANG!
    echo ========================================================
    echo Server kini berjalan secara permanen dan otomatis:
    echo   - Dashboard Lokal : http://localhost:8000
    echo   - Auto-Startup    : AKTIF (otomatis hidup setiap PC menyala)
    echo.
    echo Catatan:
    echo Jika suatu saat ingin mematikan server atau mencopot dari
    echo Auto-Startup, cukup jalankan: HENTIKAN_SERVER.bat
    echo ========================================================
) else (
    color 0E
    echo [PERHATIAN] Server sedang dalam inisialisasi. Silakan periksa dalam beberapa detik.
    echo Buka http://localhost:8000 di browser Anda.
)

echo.
pause
