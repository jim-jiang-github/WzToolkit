@echo off
rem =====================================================================
rem  WzToolkit 原生工具编译 —— 双击本文件即可（4 个 C++ 工具 -> bin\）
rem
rem  .ps1 在资源管理器里双击只会打开编辑器（默认执行策略禁止运行），
rem  所以包一层 bat：绕过执行策略、切到项目根目录、透传参数，
rem  结束时停住窗口方便看结果。
rem
rem    build.bat              -> 全量编译（已是最新的会跳过）
rem    build.bat -Clean       -> 先清空 bin\ 和 obj\
rem    build.bat -Only nxmap  -> 只编一个：wz2nx / nxmap / nxmob / nxscan
rem =====================================================================
setlocal
cd /d "%~dp0.."
set "ROOT=%CD%"

where powershell >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到 powershell.exe。
    echo.
    pause
    exit /b 1
)

if exist "%~dp0build.ps1" goto run
echo [错误] scripts 目录下没有 build.ps1。
echo.
pause
exit /b 1

:run
echo === 开始编译原生工具 ===
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [完成] 编译成功，产物在 "%ROOT%\bin"
) else (
    echo [失败] build.ps1 退出码 %RC%
)
echo.
pause
exit /b %RC%
