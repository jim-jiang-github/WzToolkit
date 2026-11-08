@echo off
rem =====================================================================
rem  WzToolkit 打包脚本：把 Python 版打成单文件 bin\wztool.exe（双击即可）
rem
rem  本文件按 GBK(cp936) 保存，别用 UTF-8 另存，否则 cmd 里中文全是乱码。
rem  PyInstaller onefile：src\wztool.py + vendor\（render_map / Mob.txt 等）
rem  + static\（WebUI 页面）-> bin\wztool.exe，和 nxmap.exe 等原生工具放一起。
rem  改过 wztool.py / static\ / vendor\ 之后跑一次。
rem
rem    build-exe.bat              -> 打包并暂停（双击用）
rem    build-exe.bat --no-pause   -> 打包不暂停（给别的脚本调用）
rem =====================================================================
setlocal
rem 固定控制台代码页，保证下面的中文在任何终端里都能正常显示
chcp 936 >nul 2>nul
cd /d "%~dp0.."
set "ROOT=%CD%"
set "OUT=%ROOT%\bin\wztool.exe"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到 python，需要 3.9+ 并已安装 pyinstaller。
    echo.
    pause
    exit /b 1
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [错误] 没装 PyInstaller，请先执行：
    echo     python -m pip install pyinstaller
    echo.
    pause
    exit /b 1
)

rem 正在运行的 wztool.exe 会锁住文件，先问一下要不要关掉
tasklist /FI "IMAGENAME eq wztool.exe" 2>nul | find /I "wztool.exe" >nul
if errorlevel 1 goto pack
echo [提示] wztool.exe 正在运行（8000 端口的 WebUI），重新打包前要先关掉它。
choice /C YN /N /M "现在关掉? [Y/N] "
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
    --workpath "%ROOT%\.build\wztool" ^
    --specpath "%ROOT%\.build\wztool" ^
    "%ROOT%\src\wztool.py"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [完成] 打包成功：%OUT%
    for %%F in ("%OUT%") do echo         大小 %%~zF 字节
    rem 项目根下还留着一份旧的，容易和新版本混淆；双击运行的是 bin\ 里那份
    if exist "%ROOT%\wztool.exe" (
        del /q "%ROOT%\wztool.exe" 2>nul
        if exist "%ROOT%\wztool.exe" (
            echo [提示] 项目根的旧 wztool.exe 删不掉（可能正在运行），请手动删除。
        ) else (
            echo [清理] 已删除项目根的旧 wztool.exe（现在统一放 bin\ 里）。
        )
    )
    echo 双击 bin\wztool.exe 即可打开 WebUI（等效 wztool web）。
) else (
    echo [失败] PyInstaller 退出码 %RC%
)
echo.
if /I "%~1"=="--no-pause" exit /b %RC%
pause
exit /b %RC%
