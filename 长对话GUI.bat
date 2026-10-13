@echo off
rem Run the GUI from SOURCE (no exe build needed).
rem Useful when you changed longchat_gui.py and want to try it immediately.
rem NOTE: keep this file ASCII-only - Chinese in a .bat breaks cmd's parser.
cd /d "%~dp0"
rem ---- locate Python that has the GUI dependencies ------------------
rem  Delayed expansion is REQUIRED: %errorlevel% inside a parenthesised
rem  block is expanded at PARSE time, so the import test would see a
rem  stale value.
rem
rem  The dependency probe deliberately uses python.exe (console), not
rem  pythonw.exe: a failing import under pythonw.exe can pop a blocking
rem  "Fatal error" dialog instead of just setting errorlevel.
rem -------------------------------------------------------------------
setlocal enabledelayedexpansion
set "PYW="
where python >nul 2>&1
if !errorlevel!==0 (
    python -c "import pystray, PIL, tkinterdnd2" >nul 2>&1
    if !errorlevel!==0 set "PYW=pythonw"
)
if not defined PYW (
    for %%V in (313 312 311 310) do (
        if not defined PYW if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" (
            "%LocalAppData%\Programs\Python\Python%%V\python.exe" -c "import pystray, PIL, tkinterdnd2" >nul 2>&1
            if !errorlevel!==0 set "PYW=%LocalAppData%\Programs\Python\Python%%V\pythonw.exe"
        )
        if not defined PYW if exist "C:\Python%%V\python.exe" (
            "C:\Python%%V\python.exe" -c "import pystray, PIL, tkinterdnd2" >nul 2>&1
            if !errorlevel!==0 set "PYW=C:\Python%%V\pythonw.exe"
        )
    )
)
if not defined PYW (
    rem last resort: a pythonw on PATH with unverified deps beats no start
    where pythonw >nul 2>&1
    if !errorlevel!==0 set "PYW=pythonw"
)
if not defined PYW (
    echo [ERROR] No Python with the GUI dependencies was found.
    echo         Required packages: pystray pillow tkinterdnd2
    echo.
    echo         1^) Install Python from https://www.python.org/downloads/
    echo            and check "Add python.exe to PATH"
    echo         2^) Then run:  pip install pystray pillow tkinterdnd2
    pause
    exit /b 1
)
if not exist "%~dp0longchat_gui.py" (
    echo [ERROR] longchat_gui.py not found next to this .bat
    pause
    exit /b 1
)
echo   Using Python: %PYW%
start "" "%PYW%" "%~dp0longchat_gui.py"
exit
