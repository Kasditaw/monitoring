@echo off
cd /d "%~dp0"
title Pembuat File Standalone EXE (Untuk Lab Sekolah)
color 0D
echo ========================================================
echo   PEMBUAT FILE .EXE MANDIRI (GURU DAN SISWA)
echo ========================================================
echo Komputer di Lab TIDAK PERLU install Python jika
echo menggunakan file .exe yang dihasilkan script ini!
echo.
echo Langkah ini membutuhkan koneksi internet untuk mengunduh PyInstaller.
echo.
pause

echo [1/3] Memeriksa dan Menginstal PyInstaller...
pip install pyinstaller

echo.
echo [2/3] Membangun Dashboard_Guru.exe...
pyinstaller --noconsole --onefile --name "Dashboard_Guru" main.py

echo.
echo [3/3] Membangun Client_Siswa.exe...
pyinstaller --noconsole --onefile --name "Client_Siswa" client_agent.py

color 0A
echo.
echo ========================================================
echo [SELESAI!] Berkas .exe mandiri telah tersimpan di:
echo - Folder "dist\Dashboard_Guru.exe" (Untuk Komputer Guru)
echo - Folder "dist\Client_Siswa.exe"   (Untuk Komputer Siswa Lab)
echo.
echo Anda cukup menyalin file "Client_Siswa.exe" ke seluruh
echo komputer lab siswa (bisa lewat Flashdisk atau Network Share).
echo ========================================================
echo.
pause
