@echo off
title Pasang Auto-Start Komputer Siswa Lab
color 0E
echo ========================================================
echo   PASANG AUTO-START PC SISWA (WINDOWS STARTUP)
echo ========================================================
echo Script ini akan mendaftarkan Agen Siswa ke Windows Startup
echo sehingga setiap kali PC siswa dinyalakan / login,
echo pemantau lab akan otomatis berjalan di latar belakang!
echo.
pause

set SCRIPT_DIR=%~dp0
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set VBS_PATH=%TEMP%\create_shortcut.vbs

echo Set oWS = WScript.CreateObject("WScript.Shell") > "%VBS_PATH%"
echo sLinkFile = "%STARTUP_DIR%\Monitoring_Lab_Siswa.lnk" >> "%VBS_PATH%"
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> "%VBS_PATH%"

if not exist "%LOCALAPPDATA%\LabGuardClient" mkdir "%LOCALAPPDATA%\LabGuardClient"
echo {^"server_url^": ^"ws://10.161.17.165:8000/ws/agent^", ^"lab_room^": ^"Lab RPL 1^", ^"auto_startup^": true} > "%LOCALAPPDATA%\LabGuardClient\agent_config.json"

if exist "%SCRIPT_DIR%dist\Client_Siswa_Web.exe" (
    echo oLink.TargetPath = "%SCRIPT_DIR%dist\Client_Siswa_Web.exe" >> "%VBS_PATH%"
    echo oLink.Arguments = "--silent ws://10.161.17.165:8000/ws/agent" >> "%VBS_PATH%"
    echo oLink.WorkingDirectory = "%SCRIPT_DIR%dist" >> "%VBS_PATH%"
) else if exist "%SCRIPT_DIR%dist\Client_Siswa.exe" (
    echo oLink.TargetPath = "%SCRIPT_DIR%dist\Client_Siswa.exe" >> "%VBS_PATH%"
    echo oLink.Arguments = "--auto" >> "%VBS_PATH%"
    echo oLink.WorkingDirectory = "%SCRIPT_DIR%dist" >> "%VBS_PATH%"
) else (
    echo oLink.TargetPath = "pythonw.exe" >> "%VBS_PATH%"
    echo oLink.Arguments = """%SCRIPT_DIR%client_agent_web.py"" --silent ws://10.161.17.165:8000/ws/agent" >> "%VBS_PATH%"
    echo oLink.WorkingDirectory = "%SCRIPT_DIR%" >> "%VBS_PATH%"
)

echo oLink.Description = "Classroom Monitor Client Agent Siswa" >> "%VBS_PATH%"
echo oLink.Save >> "%VBS_PATH%"
cscript //nologo "%VBS_PATH%"
del "%VBS_PATH%"

taskkill /f /im Client_Siswa_Web.exe >nul 2>&1
if exist "%SCRIPT_DIR%dist\Client_Siswa_Web.exe" (
    start "" "%SCRIPT_DIR%dist\Client_Siswa_Web.exe" --silent ws://10.161.17.165:8000/ws/agent
)

color 0A
echo.
echo ========================================================
echo [BERHASIL] Shortcut Startup telah dibuat di:
echo %STARTUP_DIR%\Monitoring_Lab_Siswa.lnk
echo.
echo Sekarang komputer siswa ini akan otomatis mencari dan
echo terhubung ke Komputer Guru setiap kali dinyalakan!
echo ========================================================
echo.
pause
