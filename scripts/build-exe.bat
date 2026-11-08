@echo off
rem =====================================================================
rem  ��� wztool.exe ���� ˫�����ļ�����
rem
rem  PyInstaller onefile��wztool.py + vendor\��render_map / Mob.txt��
rem  + static\��WebUI ҳ�棩-> bin\wztool.exe���� nxmap.exe ��ԭ�����߷�һ��
rem  ���� wztool.py / static\ / vendor\ ֮����һ�Ρ�
rem
rem    build-exe.bat              -> ���������ͣ���ڣ�
rem    build-exe.bat --no-pause   -> �����ͣ���ڣ�����Ľű����ã�
rem =====================================================================
setlocal
cd /d "%~dp0.."
set "ROOT=%CD%"
set "OUT=%ROOT%\bin\wztool.exe"

where python >nul 2>nul
if errorlevel 1 (
    echo [����] �Ҳ��� python����Ҫ 3.9+ ��װ�� pyinstaller����
    echo.
    pause
    exit /b 1
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [����] ûװ PyInstaller����ִ�У�
    echo     python -m pip install pyinstaller
    echo.
    pause
    exit /b 1
)

rem �����е� wztool.exe �����ļ������ǻ�ʧ�� ���� ����һ��Ҫ��Ҫ�ص�
tasklist /FI "IMAGENAME eq wztool.exe" 2>nul | find /I "wztool.exe" >nul
if errorlevel 1 goto pack
echo [��ʾ] wztool.exe �������У�8000 �˿ڵ� WebUI�������ش��ǰ��Ҫ�ȹر�����
choice /C YN /N /M "���ڹر�? [Y/N] "
if errorlevel 2 (
    echo ��ȡ����δ�����
    echo.
    pause
    exit /b 1
)
taskkill /F /IM wztool.exe >nul 2>nul
timeout /t 2 /nobreak >nul

:pack
if not exist "%ROOT%\bin" mkdir "%ROOT%\bin"
echo === ��� wztool.exe ===
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
    echo [���] ����ɹ���%OUT%
    for %%F in ("%OUT%") do echo         ��С %%~zF �ֽ�
    rem ��Ŀ����ǰ�Ź�һ�ݣ����Ż�����˫�����ɰ� ���� ��ʾ�����
    if exist "%ROOT%\wztool.exe" (
        del /q "%ROOT%\wztool.exe" 2>nul
        if exist "%ROOT%\wztool.exe" (
            echo [��ʾ] ��Ŀ�����оɵ� wztool.exe��ɾ���������������У������ֶ�ɾ����
        ) else (
            echo [����] ��ɾ����Ŀ���ľ� wztool.exe������ͳһ�� bin\ ���
        )
    )
    echo ˫�� bin\wztool.exe �������� WebUI����Ч wztool web����
) else (
    echo [ʧ��] PyInstaller �˳��� %RC%
)
echo.
if /I "%~1"=="--no-pause" exit /b %RC%
pause
exit /b %RC%
