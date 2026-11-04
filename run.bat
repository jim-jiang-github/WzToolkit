@echo off
rem Start WzToolkit WebUI (CLI web shell)
rem Uses wztool.exe if present, otherwise falls back to python wztool.py
setlocal
cd /d "%~dp0"

if exist "wztool.exe" (
    "wztool.exe" web %*
) else (
    python "wztool.py" web %*
)

endlocal
