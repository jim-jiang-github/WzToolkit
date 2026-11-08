@echo off
rem =====================================================================
rem  WzToolkit 构建脚本（双击本文件即可，唯一的构建入口）
rem
rem  本文件是 GBK(cp936) 编码，不是 UTF-8；改动请用 GBK 保存，否则 cmd 显示乱码。
rem
rem  流程：全部在项目目录内完成，不使用、也不污染本机的 Python。
rem    1/4  下载便携版 Python 到 .pyruntime\（已就绪则跳过），自带 pip
rem    2/4  按 requirements.txt 把依赖装进 .pylibs\（已就绪则跳过）
rem    3/4  bin\ 缺原生工具（NoLifeWzToNx/nxmap/nxmob/nxscan/nxchar）就现场编译
rem    4/4  用 .pyruntime 的解释器 + .pylibs 打包 bin\wztool.exe（onefile）
rem
rem  首次构建需要联网（解释器约 15MB + 依赖约 200MB），之后可离线重复构建。
rem  想从零重来：删掉 .pyruntime\ 和 .pylibs\ 两个目录，本脚本会自动重新拉。
rem
rem    build.bat                  -> 构建完暂停（双击用）
rem    build.bat --no-pause       -> 构建完不暂停，且不再交互询问（脚本/CI 用）
rem    build.bat --force-python   -> 强制重新下载便携 Python
rem =====================================================================
setlocal
rem 固定控制台代码页，保证下面的中文在任何终端里都能正常显示
chcp 936 >nul 2>nul
cd /d "%~dp0.."
set "ROOT=%CD%"
set "OUT=%ROOT%\bin\wztool.exe"
set "PYLIBS=%ROOT%\.pylibs"
set "PY=%ROOT%\.pyruntime\python.exe"
set "SYS32=%SystemRoot%\System32"
set "PWSH=%SYS32%\WindowsPowerShell\v1.0\powershell.exe"
rem 只认项目内的依赖，不去翻用户目录 / 系统 Python 的 site-packages
set "PYTHONNOUSERSITE=1"
set "PYTHONPYCACHEPREFIX=%ROOT%\.build\pycache"

echo === 1/4 准备解释器：.pyruntime（便携版 Python，不动本机） ===
echo.
if /I "%~1"=="--force-python" (
    "%PWSH%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0_fetch-python.ps1" -Force
) else (
    "%PWSH%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0_fetch-python.ps1"
)
if errorlevel 1 goto fail_python
if not exist "%PY%" goto fail_python
echo.

echo === 2/4 准备 Python 依赖：.pylibs ===
echo.
"%PY%" "%~dp0deps.py"
if errorlevel 1 (
    echo.
    echo [错误] 依赖准备失败，按上面的提示处理后重跑（多半是网络或 pip 源）。
    echo.
    if /I not "%~1"=="--no-pause" pause
    exit /b 1
)
echo.

echo === 3/4 检查原生工具：bin ===
set "NEEDTOOLS="
if not exist "%ROOT%\bin" mkdir "%ROOT%\bin"
for %%T in (NoLifeWzToNx nxmap nxmob nxscan nxchar) do if not exist "%ROOT%\bin\%%T.exe" (
    echo    缺 bin\%%T.exe
    set "NEEDTOOLS=1"
)
if not defined NEEDTOOLS (
    echo    五个原生工具都在，跳过编译。
    echo.
    goto pack
)
echo.
echo [构建] 开始编译缺失的原生工具（需要 VS 的 C++ 工具集）...
echo.
"%PWSH%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0_compile-tools.ps1"
if errorlevel 1 (
    echo.
    echo [错误] 原生工具编译失败。多半是这台机器没装 VS 的 C++ 工具集。
    echo        能编的话就先编好；不能编就把仓库里的 bin\ 整个拷回来，再重跑本脚本。
    echo.
    if /I not "%~1"=="--no-pause" pause
    exit /b 1
)
echo.

:pack
echo === 4/4 打包 wztool.exe ===
echo.
rem 正在运行的 wztool.exe 会锁住文件，先处理掉
"%SYS32%\tasklist.exe" /FI "IMAGENAME eq wztool.exe" /NH 2>nul | "%SYS32%\findstr.exe" /I "wztool.exe" >nul
if errorlevel 1 goto do_pack
if /I "%~1"=="--no-pause" (
    echo [提示] wztool.exe 正在运行，按 --no-pause 约定直接关闭它。
    goto kill_it
)
echo [提示] wztool.exe 正在运行（8000 端口的 WebUI），重新打包前要先关掉它。
choice /C YN /N /M "现在关掉? [Y/N] "
if errorlevel 2 (
    echo 已取消，未打包。
    echo.
    pause
    exit /b 1
)
:kill_it
"%SYS32%\taskkill.exe" /F /IM wztool.exe >nul 2>nul
"%SYS32%\timeout.exe" /t 2 /nobreak >nul

:do_pack
set "PYTHONPATH=%PYLIBS%"
"%PY%" -m PyInstaller --clean --noconfirm --onefile --name wztool ^
    --add-data "%ROOT%\vendor;vendor" ^
    --add-data "%ROOT%\webui;webui" ^
    --distpath "%ROOT%\bin" ^
    --workpath "%ROOT%\.build\wztool" ^
    --specpath "%ROOT%\.build\wztool" ^
    "%ROOT%\src\wztool.py"
set "RC=%ERRORLEVEL%"
set "PYTHONPATH="

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

:fail_python
echo.
echo [错误] 便携 Python 准备失败，见上面的提示（多半是网络）。
echo        手动做法：下载 nuget 的 python 包，把其中 tools\ 的内容放进 .pyruntime\。
echo.
if /I "%~1"=="--no-pause" exit /b 1
pause
exit /b 1
