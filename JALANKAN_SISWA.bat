@echo off
title Lab Komputer Sekolah - Komputer Siswa
color 0A
echo ========================================================
echo   MONITORING LAB KOMPUTER SEKOLAH - KOMPUTER SISWA
echo ========================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    color 0C
    echo [ERROR] Python belum terpasang di komputer ini!
    echo Silakan pasang Python atau gunakan file Client_Siswa.exe jika sudah dibuat.
    echo.
    pause
    exit /b 1
)

if exist "dist\Client_Siswa_Web.exe" (
    echo Menjalankan dist\Client_Siswa_Web.exe...
    start "" "dist\Client_Siswa_Web.exe"
    exit /b 0
)

echo [1/2] Memeriksa modul client...
python -c "import PIL, psutil, websockets" >nul 2>nul
if %errorlevel% neq 0 (
    echo Menginstal modul client yang dibutuhkan...
    pip install Pillow psutil websockets
)

echo [2/2] Membuka Agen Siswa Web...
echo.
python client_agent_web.py
if %errorlevel% neq 0 (
    pause
)
