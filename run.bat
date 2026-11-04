@echo off
rem WzToolkit WebUI launcher - start server on 127.0.0.1:8791 and open browser
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH.
    pause
    exit /b 1
)

if not exist "bin\nxmap.exe" (
    echo [WARN] Native tools not built yet. Run: powershell -File build.ps1
    echo        The webui can still start, but convert/export will fail.
)

start "" "http://127.0.0.1:8791/"
python run.py %*

endlocal
