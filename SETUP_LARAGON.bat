@echo off
cd /d "%~dp0"
title LabGuard PRO - Integrasi Laragon
color 0B
echo ========================================================
echo   LABGUARD PRO v2.0 - INTEGRASI DAN SETUP LARAGON
echo ========================================================
echo.

set "LARAGON_DIR=C:\laragon"
if not exist "%LARAGON_DIR%" (
    color 0C
    echo [PERINGATAN] Folder %LARAGON_DIR% tidak ditemukan di drive C:.
    echo Jika Laragon terpasang di lokasi lain, buka file ini dan ubah variabel LARAGON_DIR.
    echo.
    pause
    exit /b 1
)

echo [1/3] Menghubungkan folder proyek ke Laragon www...
if not exist "%LARAGON_DIR%\www\Monitoring" (
    cmd /c mklink /J "%LARAGON_DIR%\www\Monitoring" "%~dp0"
    echo       -> Junction berhasil dibuat: %LARAGON_DIR%\www\Monitoring
) else (
    echo       -> Folder %LARAGON_DIR%\www\Monitoring sudah terhubung.
)

echo.
echo [2/3] Memeriksa Virtual Host Apache Laragon...
if exist "%LARAGON_DIR%\etc\apache2\sites-enabled" (
    if not exist "%LARAGON_DIR%\etc\apache2\sites-enabled\auto.Monitoring.test.conf" (
        (
            echo ^<VirtualHost *:80^>
            echo     DocumentRoot "C:/laragon/www/Monitoring"
            echo     ServerName monitoring.test
            echo     ServerAlias *.monitoring.test
            echo     ^<Directory "C:/laragon/www/Monitoring"^>
            echo         AllowOverride All
            echo         Require all granted
            echo     ^</Directory^>
            echo ^</VirtualHost^>
        ) > "%LARAGON_DIR%\etc\apache2\sites-enabled\auto.Monitoring.test.conf"
        echo       -> Virtual Host auto.Monitoring.test.conf berhasil dipasang.
    ) else (
        echo       -> Virtual Host auto.Monitoring.test.conf sudah ada.
    )
)

echo.
echo [3/3] Memeriksa index.php Launcher...
if exist "%~dp0index.php" (
    echo       -> index.php launcher bridge sudah siap.
)

color 0A
echo.
echo ========================================================
echo   INTEGRASI LARAGON BERHASIL!
echo ========================================================
echo.
echo Cara Menggunakan di Laragon:
echo 1. Buka Laragon -> Klik "Start All" (Apache).
echo 2. Buka browser: http://monitoring.test atau http://localhost:8000
echo 3. Jalankan server Python monitoring:
echo    - Klik "Jalankan Server Sekarang" di halaman http://monitoring.test, ATAU
echo    - Klik dua kali JALANKAN_WEB_SERVER.bat
echo ========================================================
echo.
pause
