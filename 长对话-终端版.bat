@echo off
chcp 65001 >nul
title Long-Chat Manager (Terminal)
rem ---------------------------------------------------------------
rem  TERMINAL (command-line) version of the long-chat manager.
rem  This launches longchat.py, NOT the GUI.
rem  It talks to llama.cpp on port 8080 only (no dual-engine support).
rem  Start an engine first - use the "start engine" .bat in this folder.
rem
rem  NOTE: keep this file ASCII-only. Chinese text in a .bat breaks
rem        cmd's parser (line continuations get mangled).
rem ---------------------------------------------------------------
cd /d "%~dp0"
echo.
echo  ==========================================================
echo    Long-Chat Manager - TERMINAL version
echo  ==========================================================
echo    Requires a running engine on port 8080.
echo    Start it first with the "start engine - llama" .bat here.
echo.
rem ---- locate Python -------------------------------------------------
rem  longchat.py only needs the STANDARD LIBRARY, so this script works
rem  with any Python 3.8+. We still PREFER a Python that also carries
rem  the GUI deps (pystray / pillow / tkinterdnd2), so that this script
rem  and the GUI launcher pick the same interpreter - but unlike the GUI
rem  we never hard-fail just because those packages are missing.
rem  Delayed expansion is required - see the build script for the reason.
rem -------------------------------------------------------------------
setlocal enabledelayedexpansion
set "PY="
set "PYWARN="

rem tier 1 - python on PATH, with the GUI deps present
where python >nul 2>&1
if !errorlevel!==0 (
    python -c "import pystray, PIL, tkinterdnd2" >nul 2>&1
    if !errorlevel!==0 set "PY=python"
)

rem tier 2 - common install locations, with the GUI deps present
if not defined PY (
    for %%V in (313 312 311 310) do (
        if not defined PY if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" (
            "%LocalAppData%\Programs\Python\Python%%V\python.exe" -c "import pystray, PIL, tkinterdnd2" >nul 2>&1
            if !errorlevel!==0 set "PY=%LocalAppData%\Programs\Python\Python%%V\python.exe"
        )
        if not defined PY if exist "C:\Python%%V\python.exe" (
            "C:\Python%%V\python.exe" -c "import pystray, PIL, tkinterdnd2" >nul 2>&1
            if !errorlevel!==0 set "PY=C:\Python%%V\python.exe"
        )
    )
)

rem tier 3 - no dep-complete Python anywhere; the terminal only needs the
rem          standard library, so accept any Python rather than refusing
if not defined PY (
    where python >nul 2>&1
    if !errorlevel!==0 (
        set "PY=python"
        set "PYWARN=1"
    )
)
if not defined PY (
    for %%V in (313 312 311 310) do (
        if not defined PY if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" set "PY=%LocalAppData%\Programs\Python\Python%%V\python.exe"
        if not defined PY if exist "C:\Python%%V\python.exe" set "PY=C:\Python%%V\python.exe"
    )
    if defined PY set "PYWARN=1"
)

if not defined PY (
    echo  [ERROR] Python not found. Install from https://www.python.org/downloads/
    echo          and check "Add python.exe to PATH".
    pause
    exit /b 1
)
if defined PYWARN (
    echo  [NOTE] This Python lacks pystray / pillow / tkinterdnd2.
    echo         The terminal version works fine ^(standard library only^).
    echo         The GUI version may not - see the README.
    echo.
)
if not exist "%~dp0longchat.py" (
    echo  [ERROR] longchat.py not found next to this .bat
    pause
    exit /b 1
)
echo   Using Python: %PY%
"%PY%" "%~dp0longchat.py"
echo.
pause
