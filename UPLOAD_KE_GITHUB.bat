@echo off
cd /d "%~dp0"
title LabGuard PRO - Upload ke GitHub (Kasditaw/monitoring)
color 0B

echo ========================================================
echo   LABGUARD PRO - UPLOAD PROJEK KE GITHUB
echo   Akun: Kasditaw
echo   Repo: https://github.com/Kasditaw/monitoring
echo ========================================================
echo.
echo Seluruh file projek (server, client, web) sudah dikemas rapi.
echo.
echo Mengunggah (push) file ke GitHub sekarang...
echo --------------------------------------------------------
echo CATATAN:
echo Jika muncul pop-up login atau browser Windows,
echo silakan klik "Sign in with your browser" untuk izin akses.
echo --------------------------------------------------------
echo.

git push -u origin main --force

if %errorlevel% equ 0 (
    color 0A
    echo.
    echo ========================================================
    echo   [SUKSES 100%%] Seluruh projek berhasil di-upload!
    echo   Buka link: https://github.com/Kasditaw/monitoring
    echo ========================================================
) else (
    color 0C
    echo.
    echo [INFO] Jika gagal, pastikan koneksi internet stabil
    echo dan jendela otentikasi login GitHub telah disetujui.
)

echo.
pause
