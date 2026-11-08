@echo off
rem =====================================================================
rem  WzToolkit 构建脚本：双击本文件即可（唯一的入口）
rem
rem  本文件按 GBK(cp936) 保存，别用 UTF-8 另存，否则 cmd 里中文全是乱码。
rem
rem  干了三件事：
rem    1) 把 requirements.txt 的依赖装进 <项目根>\.pylibs\（不碰系统 Python）
rem    2) bin\ 里缺原生工具（NoLifeWzToNx/nxmap/nxmob/nxscan/nxchar）就现编
rem       —— 平时这些 exe 随仓库提供，一般用不到；编不动会明确报错
rem    3) 用 .pylibs 里的 PyInstaller 把 src\wztool.py + vendor\ + webui\ 打成
rem       bin\wztool.exe（onefile）
rem
rem  改了 wztool.py / webui\ / vendor\ 之后跑一次；首次需要联网装依赖（约 200MB）。
rem
rem    build.bat              -> 构建并暂停（双击用）
rem    build.bat --no-pause   -> 构建不暂停（给别的脚本调用）
rem =====================================================================
setlocal
rem 固定控制台代码页，保证下面的中文在任何终端里都能正常显示
chcp 936 >nul 2>nul
cd /d "%~dp0.."
set "ROOT=%CD%"
set "OUT=%ROOT%\bin\wztool.exe"
set "PYLIBS=%ROOT%\.pylibs"
set "SYS32=%SystemRoot%\System32"
rem 只认项目内的依赖，不去翻用户目录 / 系统 Python 的 site-packages
set "PYTHONNOUSERSITE=1"

rem 系统工具一律写全路径：PATH 里要是排着 Git/MSYS 的同名工具会被抢先调走
"%SYS32%\where.exe" python >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到 python，需要一个 3.9+ 的解释器在 PATH 里。
    echo.
    pause
    exit /b 1
)

echo === 1/3 准备 Python 依赖：.pylibs ===
echo.
python "%~dp0deps.py"
if errorlevel 1 (
    echo.
    echo [错误] 依赖准备失败，按上面的提示处理后重跑（多半是网络或 pip 源）。
    echo.
    pause
    exit /b 1
)
echo.

echo === 2/3 检查原生工具：bin ===
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
"%SYS32%\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0_compile-tools.ps1"
if errorlevel 1 (
    echo.
    echo [错误] 原生工具编译失败。多半是这台机器没装 VS 的 C++ 工具集。
    echo        能编的话就先编好；不能编就把仓库里的 bin\ 整个拷回来，再重跑本脚本。
    echo.
    pause
    exit /b 1
)
echo.

:pack
echo === 3/3 打包 wztool.exe ===
echo.
rem 正在运行的 wztool.exe 会锁住文件，先问一下要不要关掉
"%SYS32%\tasklist.exe" /FI "IMAGENAME eq wztool.exe" /NH 2>nul | "%SYS32%\findstr.exe" /I "wztool.exe" >nul
if errorlevel 1 goto do_pack
echo [提示] wztool.exe 正在运行（8000 端口的 WebUI），重新打包前要先关掉它。
choice /C YN /N /M "现在关掉? [Y/N] "
if errorlevel 2 (
    echo 已取消，未打包。
    echo.
    pause
    exit /b 1
)
"%SYS32%\taskkill.exe" /F /IM wztool.exe >nul 2>nul
"%SYS32%\timeout.exe" /t 2 /nobreak >nul

:do_pack
set "PYTHONPATH=%PYLIBS%"
python -m PyInstaller --clean --noconfirm --onefile --name wztool ^
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
