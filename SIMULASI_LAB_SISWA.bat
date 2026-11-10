@echo off
cd /d "%~dp0"
title LabGuard PRO - Simulasi 12 Komputer Siswa
color 0E
echo ========================================================
echo   SIMULASI 12 PC SISWA LAB KOMPUTER
echo ========================================================
echo Script ini akan membuat 12 PC siswa virtual (LAB1-PC01 s/d LAB1-PC12)
echo dengan beban CPU, RAM, aplikasi aktif, dan thumbnail acak.
echo Sangat cocok untuk menguji tampilan Web Dashboard tanpa PC fisik!
echo.
echo Pastikan JALANKAN_WEB_SERVER.bat sudah aktif terlebih dahulu.
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

python simulate_lab_clients.py 12 ws://127.0.0.1:8000/ws/agent
if %errorlevel% neq 0 (
    pause
)
