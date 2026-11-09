@echo off
rem ============================================================
rem  dev.cmd - run WebUI straight from SOURCE (no packaging).
rem  For local testing: edit src/wztool.py or webui/cli.html,
rem  relaunch this, and you get the new code in seconds.
rem
rem  - Frontend (webui/cli.html) reloads automatically (15s poll)
rem  - Backend  (src/wztool.py)  needs relaunching this script
rem  - Extra args pass through: e.g.  dev.cmd --port 8010
rem ============================================================
setlocal
cd /d "%~dp0"
if not exist ".pyruntime\python.exe" (
  echo [dev] .pyruntime not found. Run build.cmd once first -
  echo [dev] it downloads the portable Python and installs .pylibs.
  pause
  exit /b 1
)
set "PYTHONPATH=%~dp0.pylibs"
set "PYTHONNOUSERSITE=1"
".pyruntime\python.exe" "src\wztool.py" web %*
