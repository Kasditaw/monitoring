ap@echo off
cd /d "%~dp0"
title LabGuard PRO - Upload ke GitHub (Kasditaw/monitoring)
color 0B

echo ========================================================
echo   LABGUARD PRO - UPLOAD OTOMATIS KE GITHUB
echo   Target Repo: https://github.com/Kasditaw/monitoring.git
echo ========================================================
echo.

rem Periksa apakah git terpasang
where git >nul 2>&1
if %errorlevel% neq 0 (
    color 0C
    echo [ERROR] Git belum terpasang di komputer ini!
    echo Unduh dan pasang Git dari: https://git-scm.com/downloads
    pause
    exit /b 1
)

rem Cek atau set identitas Git lokal jika belum ada
for /f "tokens=*" %%i in ('git config --global user.name 2^>nul') do set "GIT_USER=%%i"
if "%GIT_USER%"=="" (
    echo Mengatur identitas pengguna Git...
    git config --global user.name "Kasditaw"
    git config --global user.email "kasditaw@users.noreply.github.com"
)

rem Inisialisasi Git
if not exist ".git" (
    echo [1/4] Menginisialisasi repositori Git lokal...
    git init
) else (
    echo [1/4] Repositori Git lokal sudah aktif.
)

echo [2/4] Menyiapkan seluruh file projek (server, client, web dashboard)...
git add .

echo [3/4] Membuat paket commit lengkap...
git commit -m "Upload seluruh projek LabGuard PRO v2.9.9 Web Monitoring Lab"

rem Konfigurasi branch & remote
git branch -M main
git remote remove origin >nul 2>&1
git remote add origin https://github.com/Kasditaw/monitoring.git

echo.
echo [4/4] Mengunggah (push) seluruh file ke GitHub...
echo (Jika muncul jendela pop-up di browser Windows, klik 'Sign in with your browser' atau 'Authorize')
echo.

git push -u origin main --force

if %errorlevel% equ 0 (
    color 0A
    echo.
    echo ========================================================
    echo   [BERHASIL!] Seluruh file projek sudah ter-upload ke:
    echo   https://github.com/Kasditaw/monitoring
    echo ========================================================
    echo Silakan refresh halaman browser GitHub Anda untuk melihat hasilnya.
) else (
    color 0C
    echo.
    echo [PERHATIAN] Upload belum berhasil.
    echo Pastikan komputer terhubung ke internet dan Anda sudah login ke akun GitHub Anda.
)

echo.
pause
