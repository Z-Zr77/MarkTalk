@echo off
chcp 65001 >nul
title Stop engines
echo ============================================
echo   Stopping engines (8080 llama.cpp / 8000 FreeToken)
echo ============================================
echo.
where pwsh >nul 2>&1
if %errorlevel%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0engine_tool.ps1" -Action stop
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0engine_tool.ps1" -Action stop
)
echo.
timeout /t 3 >nul