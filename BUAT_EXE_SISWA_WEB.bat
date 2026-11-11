@echo off
cd /d "%~dp0"
title Pembuat File Standalone EXE - Client Siswa Web
color 0D
echo ========================================================
echo   PEMBUAT FILE .EXE MANDIRI: CLIENT SISWA WEB (v2.4.0)
echo   IT Support SD Kristen Petra 1
echo ========================================================
echo Script ini akan mengompilasi "client_agent_web.py" (v2.4.0)
echo dengan fitur baru: Adaptive Screen Frame Rate (Smart Difference Hash)
echo menjadi SATU FILE MANDIRI (.exe) sehingga PC Siswa TIDAK
echo PERLU menginstal Python sama sekali!
echo.
echo Versi Rilis : v2.4.0
echo Tanggal     : 11 November 2026
echo File Output : dist\Client_Siswa_Web.exe ^& dist\Client_Siswa_Web_v2.4.0.exe
echo.
pause

where python >nul 2>nul
if %errorlevel% neq 0 (
    if exist "C:\laragon\bin\python\python-3.13\python.exe" (
        set "PATH=C:\laragon\bin\python\python-3.13;C:\laragon\bin\python\python-3.13\Scripts;%PATH%"
    ) else if exist "C:\laragon\bin\python\python.exe" (
        set "PATH=C:\laragon\bin\python;%PATH%"
    ) else (
        color 0C
        echo [ERROR] Python belum terpasang di komputer ini!
        pause
        exit /b 1
    )
)

echo.
echo [1/2] Memeriksa PyInstaller...
python -c "import PyInstaller" >nul 2>nul
if %errorlevel% neq 0 (
    echo Menginstal PyInstaller...
    pip install pyinstaller
)

echo.
echo [2/2] Mengompilasi client_agent_web.py menjadi Client_Siswa_Web.exe...
pyinstaller --noconsole --onefile --icon="app_icon.ico" --name "Client_Siswa_Web" client_agent_web.py

if %errorlevel% neq 0 (
    color 0C
    echo [ERROR] Terjadi kesalahan saat membuat file .exe
    pause
    exit /b 1
)

color 0A
echo.
echo ========================================================
echo [BERHASIL!] File aplikasi siswa telah dibuat di:
echo 👉 dist\Client_Siswa_Web.exe

rem Buat salinan dengan nama versi resmi (misal: Client_Siswa_Web_v2.4.0.exe)
copy /y "dist\Client_Siswa_Web.exe" "dist\Client_Siswa_Web_v2.4.0.exe" >nul
echo 👉 dist\Client_Siswa_Web_v2.4.0.exe
echo.
echo FITUR FILE .EXE INI:
echo 1. Standalone: PC Siswa TIDAK PERLU install Python sama sekali!
echo 2. Langsung Download: Bisa diunduh langsung dari browser PC Siswa:
echo    http://[IP-GURU]:8000/download/Client_Siswa_Web_v2.4.0.exe
echo    http://[IP-GURU]:8000/download/agent
echo 3. Pemasang 1-Klik: Double-click lalu klik "Pasang & Jalankan di Latar Belakang"
echo 4. Auto-Startup: Otomatis mendaftar ke Windows Startup saat boot.
echo 5. Background Stealth: Berjalan di latar belakang tanpa jendela hitam CMD.
echo ========================================================
echo.
pause
