@echo off
cd /d "%~dp0"
title LabGuard PRO - Client Agent Siswa
color 0A
echo ========================================================
echo   LABGUARD PRO - CLIENT AGENT KOMPUTER SISWA
echo ========================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    if exist "C:\laragon\bin\python\python-3.13\python.exe" (
        set "PATH=C:\laragon\bin\python\python-3.13;C:\laragon\bin\python\python-3.13\Scripts;%PATH%"
    ) else if exist "C:\laragon\bin\python\python.exe" (
        set "PATH=C:\laragon\bin\python;%PATH%"
    ) else (
        color 0C
        echo [ERROR] Python belum terpasang di PC Siswa ini!
        pause
        exit /b 1
    )
)

set /p SERVER_IP="Masukkan IP Server Guru [default: 127.0.0.1]: "
if "%SERVER_IP%"=="" set SERVER_IP=127.0.0.1

set /p LAB_ROOM="Masukkan Nama Ruang Lab [default: Lab RPL 1]: "
if "%LAB_ROOM%"=="" set LAB_ROOM=Lab RPL 1

echo.
echo Menghubungkan ke ws://%SERVER_IP%:8000/ws/agent (%LAB_ROOM%)...
python client_agent_web.py ws://%SERVER_IP%:8000/ws/agent "%LAB_ROOM%"
if %errorlevel% neq 0 (
    pause
)
