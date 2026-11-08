@echo off
rem =====================================================================
rem  打包 wztool.exe —— 双击本文件即可
rem
rem  PyInstaller onefile：wztool.py + vendor\（render_map / Mob.txt）
rem  + static\（WebUI 页面）-> 项目根目录的 wztool.exe
rem  改了 wztool.py / static\ / vendor\ 之后跑一次。
rem
rem    build-exe.bat              -> 打包（结束停窗口）
rem    build-exe.bat --no-pause   -> 打包后不停窗口（给别的脚本调用）
rem =====================================================================
setlocal
cd /d "%~dp0.."
set "ROOT=%CD%"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到 python（需要 3.9+ 且装好 pyinstaller）。
    echo.
    pause
    exit /b 1
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [错误] 没装 PyInstaller，先执行：
    echo     python -m pip install pyinstaller
    echo.
    pause
    exit /b 1
)

rem 运行中的 wztool.exe 锁着文件，覆盖会失败 —— 先问一句要不要关掉
tasklist /FI "IMAGENAME eq wztool.exe" 2>nul | find /I "wztool.exe" >nul
if errorlevel 1 goto pack
echo [提示] wztool.exe 正在运行（8000 端口的 WebUI？），重打包前需要先关闭它。
choice /C YN /N /M "现在关闭? [Y/N] "
if errorlevel 2 (
    echo 已取消，未打包。
    echo.
    pause
    exit /b 1
)
taskkill /F /IM wztool.exe >nul 2>nul
timeout /t 2 /nobreak >nul

:pack
echo === 打包 wztool.exe ===
echo.
python -m PyInstaller --clean --noconfirm --onefile --name wztool ^
    --add-data "%ROOT%\vendor;vendor" ^
    --add-data "%ROOT%\static;static" ^
    --distpath "%ROOT%" ^
    --workpath "%ROOT%\build_wztool" ^
    --specpath "%ROOT%\build_wztool" ^
    "%ROOT%\wztool.py"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [完成] 打包成功：%ROOT%\wztool.exe
    for %%F in ("%ROOT%\wztool.exe") do echo         大小 %%~zF 字节
    echo 双击 wztool.exe 即可启动 WebUI（等效 wztool web）。
) else (
    echo [失败] PyInstaller 退出码 %RC%
)
echo.
if /I "%~1"=="--no-pause" exit /b %RC%
pause
exit /b %RC%
