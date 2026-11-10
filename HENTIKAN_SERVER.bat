@echo off
cd /d "%~dp0"
title LabGuard PRO - Hentikan Server
color 0C

echo ========================================================
echo   LABGUARD PRO - HENTIKAN / NONAKTIFKAN SERVER
echo ========================================================
echo.
echo Pilihan tindakan:
echo [1] Hentikan Server sekarang (Stop Server & Apache)
echo [2] Hentikan Server dan HAPUS dari Auto-Startup Windows
echo [3] Hanya Hapus dari Auto-Startup (Server tetap jalan sekarang)
echo [0] Batal / Keluar
echo.
set /p "pilihan=Ketik nomor pilihan (1/2/3/0) lalu tekan ENTER: "

if "%pilihan%"=="1" goto stop_now
if "%pilihan%"=="2" goto stop_and_disable
if "%pilihan%"=="3" goto disable_only
if "%pilihan%"=="0" goto keluar
goto keluar

:stop_now
echo.
echo [1/2] Menghentikan Server LabGuard PRO (Port 8000)...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do (
    taskkill /f /pid %%a >nul 2>&1
)
taskkill /f /im pythonw.exe >nul 2>&1

echo [2/2] Menghentikan Apache HTTPD (Port 80)...
taskkill /f /im httpd.exe >nul 2>&1

echo.
echo ========================================================
echo [SUKSES] Seluruh layanan server LabGuard PRO telah DIHENTIKAN.
echo ========================================================
pause
goto keluar

:stop_and_disable
echo.
echo [1/3] Menghentikan Server LabGuard PRO (Port 8000)...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do (
    taskkill /f /pid %%a >nul 2>&1
)
taskkill /f /im pythonw.exe >nul 2>&1

echo [2/3] Menghentikan Apache HTTPD (Port 80)...
taskkill /f /im httpd.exe >nul 2>&1

echo [3/3] Menghapus Auto-Startup Windows...
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\LabGuardProServer.lnk" (
    del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\LabGuardProServer.lnk"
    echo Shortcut Startup berhasil dihapus.
) else (
    echo Shortcut Startup tidak ditemukan (sudah nonaktif).
)

echo.
echo ========================================================
echo [SUKSES] Server telah BERHENTI dan TIDAK AKAN berjalan otomatis
echo saat komputer dinyalakan lagi.
echo Untuk menjalankan kembali, gunakan: JALANKAN_WEB_SERVER.bat
echo ========================================================
pause
goto keluar

:disable_only
echo.
echo Menghapus dari Startup Windows...
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\LabGuardProServer.lnk" (
    del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\LabGuardProServer.lnk"
    echo [SUKSES] Shortcut Startup berhasil dihapus.
    echo Server tidak akan otomatis berjalan saat komputer baru dinyalakan.
) else (
    echo Shortcut Startup tidak ditemukan (sudah nonaktif).
)
pause
goto keluar

:keluar
exit /b 0

