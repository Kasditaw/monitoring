@echo off
cd /d "%~dp0"
title Membuat Shortcut Desktop LabGuard PRO
color 0B

echo ========================================================
echo   MEMBUAT SHORTCUT APLIKASI DESKTOP LABGUARD PRO GURU
echo ========================================================
echo.

cscript //nologo BUAT_SHORTCUT.vbs

echo.
echo ========================================================
echo Ikon aplikasi telah siap di Desktop komputer ini:
echo 👉 "LabGuard PRO - Guru"
echo.
echo Cukup klik dua kali (double-click) ikon tersebut di
echo Desktop kapan saja untuk membuka aplikasi monitoring.
echo ========================================================
echo.
pause
