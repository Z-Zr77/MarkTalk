@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem ============================================================
rem  MarkTalk - cloud API key manager  (ASCII ONLY on purpose:
rem  Chinese text inside a .bat breaks cmd's parser)
rem
rem  The key is encrypted with Windows DPAPI and bound to the
rem  CURRENT Windows user on THIS PC. Nobody else can read it,
rem  and it will not work if the file is copied elsewhere.
rem  It is stored in api_key.dat (see .gitignore - never committed).
rem ============================================================

rem ---- locate any working Python (no 3rd-party deps needed) ----
set "PY="
where python >nul 2>&1
if %errorlevel%==0 set "PY=python"
if not defined PY (
    for %%V in (313 312 311 310) do (
        if not defined PY if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" set "PY=%LocalAppData%\Programs\Python\Python%%V\python.exe"
    )
)
if not defined PY (
    for %%V in (313 312 311 310) do (
        if not defined PY if exist "C:\Python%%V\python.exe" set "PY=C:\Python%%V\python.exe"
    )
)
if not defined PY (
    echo [ERROR] No Python found.
    echo         Install from https://www.python.org/downloads/
    echo         and check "Add python.exe to PATH".
    pause
    exit /b 1
)

:menu
cls
echo ============================================================
echo   MarkTalk - cloud API key manager
echo ============================================================
echo.
echo   Current status:
"%PY%" "%~dp0engine_adapter.py" --check-key "%~dp0config.json"
echo.
echo   [1]  Set / replace key   (encrypted, input hidden)
echo   [2]  Delete saved key
echo   [3]  Show where the key file is
echo   [0]  Exit
echo.
set "CH="
set /p CH=  Choose [0-3]: 
if "%CH%"=="1" goto setkey
if "%CH%"=="2" goto delkey
if "%CH%"=="3" goto showpath
if "%CH%"=="0" exit /b 0
goto menu

:setkey
echo.
"%PY%" "%~dp0engine_adapter.py"
echo.
pause
goto menu

:delkey
echo.
"%PY%" "%~dp0engine_adapter.py" --clear-key "%~dp0config.json"
echo.
pause
goto menu

:showpath
echo.
echo   Key file: 
"%PY%" "%~dp0engine_adapter.py" --get-key-config "%~dp0config.json"
echo.
echo   This file is encrypted for the current Windows user only.
echo   Keep it private: do NOT send it to anyone.
echo.
pause
goto menu
