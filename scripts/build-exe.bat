@echo off
rem =====================================================================
rem  打包 wztool.exe —— 双击本文件即可
rem
rem  PyInstaller onefile：wztool.py + vendor\（render_map / Mob.txt）
rem  + static\（WebUI 页面）-> bin\wztool.exe（和 nxmap.exe 等原生工具放一起）
rem  改了 wztool.py / static\ / vendor\ 之后跑一次。
rem
rem    build-exe.bat              -> 打包（结束停窗口）
rem    build-exe.bat --no-pause   -> 打包后不停窗口（给别的脚本调用）
rem =====================================================================
setlocal
cd /d "%~dp0.."
set "ROOT=%CD%"
set "OUT=%ROOT%\bin\wztool.exe"

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
if not exist "%ROOT%\bin" mkdir "%ROOT%\bin"
echo === 打包 wztool.exe ===
echo.
python -m PyInstaller --clean --noconfirm --onefile --name wztool ^
    --add-data "%ROOT%\vendor;vendor" ^
    --add-data "%ROOT%\static;static" ^
    --distpath "%ROOT%\bin" ^
    --workpath "%ROOT%\build_wztool" ^
    --specpath "%ROOT%\build_wztool" ^
    "%ROOT%\src\wztool.py"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [完成] 打包成功：%OUT%
    for %%F in ("%OUT%") do echo         大小 %%~zF 字节
    rem 项目根以前放过一份，留着会让人双击到旧版 —— 提示并清掉
    if exist "%ROOT%\wztool.exe" (
        del /q "%ROOT%\wztool.exe" 2>nul
        if exist "%ROOT%\wztool.exe" (
            echo [提示] 项目根还有旧的 wztool.exe，删不掉（可能在运行），请手动删掉。
        ) else (
            echo [清理] 已删除项目根的旧 wztool.exe（现在统一在 bin\ 里）。
        )
    )
    echo 双击 bin\wztool.exe 即可启动 WebUI（等效 wztool web）。
) else (
    echo [失败] PyInstaller 退出码 %RC%
)
echo.
if /I "%~1"=="--no-pause" exit /b %RC%
pause
exit /b %RC%
