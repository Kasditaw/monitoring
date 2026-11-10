@echo off
cd /d "%~dp0"
title Pembuat Standalone EXE - LabGuard PRO Guru
color 0B

echo ========================================================
echo   PEMBUAT FILE .EXE MANDIRI: LABGUARD PRO GURU (v2.2.0)
echo ========================================================
echo Script ini akan mengompilasi "app_guru.py" menjadi file
echo aplikasi mandiri (LabGuard_Guru.exe) untuk Windows.
echo.
pause

where pyinstaller >nul 2>nul
if %errorlevel% neq 0 (
    echo Menginstal PyInstaller...
    pip install pyinstaller
)

echo.
echo Mengompilasi aplikasi desktop guru...
pyinstaller --noconsole --name "LabGuard_Guru" --icon="app_icon.ico" --add-data "web;web" --add-data "app_icon.ico;." app_guru.py

if %errorlevel% neq 0 (
    color 0C
    echo [ERROR] Terjadi kesalahan saat mengompilasi file .exe
    pause
    exit /b 1
)

color 0A
echo.
echo ========================================================
echo [BERHASIL!] File aplikasi Desktop Guru telah dibuat di:
echo 👉 dist\LabGuard_Guru.exe
echo ========================================================
echo.
pause
