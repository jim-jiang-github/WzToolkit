@echo off
rem 双击本文件即可构建（等价于 scripts\build.bat）。
rem 真正的构建逻辑都在 scripts\build.bat，这里只是给个放在根目录的入口，
rem 免得 clone 下来还要先翻 scripts 目录。
rem 参数会原样传过去，例如：build.cmd --no-pause
chcp 936 >nul 2>nul
call "%~dp0scripts\build.bat" %*
exit /b %ERRORLEVEL%
