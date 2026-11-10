@echo off
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel% neq 0 (
    if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
        set "PATH=%LOCALAPPDATA%\Programs\Python\Python313;%LOCALAPPDATA%\Programs\Python\Python313\Scripts;%PATH%"
    ) else if exist "C:\laragon\bin\python\python-3.13\python.exe" (
        set "PATH=C:\laragon\bin\python-3.13;C:\laragon\bin\python-3.13\Scripts;%PATH%"
    ) else if exist "C:\laragon\bin\python\python.exe" (
        set "PATH=C:\laragon\bin\python;%PATH%"
    )
)
python app_guru.py
