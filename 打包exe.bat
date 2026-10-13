@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Build MarkTalk GUI EXE
echo ============================================
echo.
echo   This file is intentionally ASCII-only.
echo   Chinese text inside a .bat breaks cmd's parser
echo   (the ^ line-continuations get mangled).
echo   The output name MarkTalk_GUI.exe is ASCII, so
echo   no unicode escaping is needed any more.
echo.

rem ---- locate Python: must have ALL build deps ----------------------
rem  Delayed expansion is REQUIRED here: inside a parenthesised block
rem  %errorlevel% is expanded when the block is PARSED, not when it runs,
rem  so the import check below would always see a stale value.
rem -------------------------------------------------------------------
setlocal enabledelayedexpansion
set "PY="
where python >nul 2>&1
if !errorlevel!==0 (
    python -c "import PyInstaller, pystray, PIL, tkinterdnd2" >nul 2>&1
    if !errorlevel!==0 set "PY=python"
)
if not defined PY (
    for %%V in (313 312 311 310) do (
        if not defined PY if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" (
            "%LocalAppData%\Programs\Python\Python%%V\python.exe" -c "import PyInstaller, pystray, PIL, tkinterdnd2" >nul 2>&1
            if !errorlevel!==0 set "PY=%LocalAppData%\Programs\Python\Python%%V\python.exe"
        )
        if not defined PY if exist "C:\Python%%V\python.exe" (
            "C:\Python%%V\python.exe" -c "import PyInstaller, pystray, PIL, tkinterdnd2" >nul 2>&1
            if !errorlevel!==0 set "PY=C:\Python%%V\python.exe"
        )
    )
)
if not defined PY (
    echo [ERROR] No Python with the build dependencies was found.
    echo         Required packages: pyinstaller pystray pillow tkinterdnd2
    echo.
    echo         1^) Install Python from https://www.python.org/downloads/
    echo            and check "Add python.exe to PATH"
    echo         2^) Then run:  pip install pyinstaller pystray pillow tkinterdnd2
    pause
    exit /b 1
)
echo   Using Python: %PY%

echo [1/3] Checking dependencies...
"%PY%" -c "import PyInstaller, pystray, PIL, tkinterdnd2" 2>nul
if errorlevel 1 (
    echo       Installing missing deps...
    "%PY%" -m pip install --disable-pip-version-check pyinstaller pystray pillow tkinterdnd2
)

echo [2/3] Generating icon...
if not exist "app.ico" (
    "%PY%" -c "from PIL import Image,ImageDraw;img=Image.new('RGBA',(256,256),(0,0,0,0));d=ImageDraw.Draw(img);d.rounded_rectangle([13,13,243,243],radius=59,fill=(59,130,246,255));d.rounded_rectangle([54,64,202,161],radius=26,fill=(255,255,255,255));d.polygon([(82,156),(82,187),(123,156)],fill=(255,255,255,255));img.save('app.ico',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
)

echo [3/3] Running PyInstaller (1-3 minutes)...
rem NOTE: --icon/--add-data must be ABSOLUTE paths: PyInstaller resolves
rem       relative data paths against the .spec dir (%TEMP%\lcg_build),
rem       not the current directory.
"%PY%" -m PyInstaller --noconfirm --onefile --windowed --clean --name "MarkTalk_GUI" --icon "%~dp0app.ico" --add-data "%~dp0app.ico;." --add-data "%~dp0mobile.html;." --collect-all tkinterdnd2 --distpath "%~dp0dist" --workpath "%TEMP%\lcg_build\work" --specpath "%TEMP%\lcg_build" --exclude-module numpy --exclude-module pandas --exclude-module matplotlib --exclude-module scipy --exclude-module PyQt5 --exclude-module PySide2 --exclude-module IPython --exclude-module pytest "%~dp0longchat_gui.py"

if errorlevel 1 (
    echo.
    echo [FAILED] See the log above.
    pause
    exit /b 1
)

echo.
echo Copying exe next to memories\ ...
rem ASCII output name -> no unicode escape needed
"%PY%" -c "import shutil; shutil.copy(r'dist\MarkTalk_GUI.exe', 'MarkTalk_GUI.exe')"
if errorlevel 1 (
    echo [WARN] copy failed, please copy dist\MarkTalk_GUI.exe here manually
) else (
    echo       updated exe in this folder
)

echo.
echo ============================================
echo   DONE
echo   exe name: MarkTalk_GUI.exe  (in this folder)
echo.
echo   The exe contains only program code - NO chat
echo   history. It reads/writes memories\ next to
echo   itself, so keep exe and memories\ together.
echo.
echo   Custom data dir:  --data-dir "D:\my chats"
echo ============================================
pause
