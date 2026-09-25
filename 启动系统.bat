@echo off
rem ============================================================
rem  This file is intentionally PURE ASCII.
rem  cmd.exe parses .bat files using the OEM code page (cp936 on
rem  Chinese Windows). Non-ASCII bytes here get mis-decoded and
rem  can even swallow line endings, so all Chinese messages are
rem  printed by backend\scripts\launch.py instead.
rem ============================================================
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :novenv

".venv\Scripts\python.exe" "backend\scripts\launch.py"

echo.
echo   [stopped] Press any key to close this window.
pause >nul
exit /b 0

:novenv
echo.
echo   [ERROR] Python environment not found:
echo           %~dp0.venv\Scripts\python.exe
echo.
echo   Please follow README section 4 to install dependencies first.
echo.
pause >nul
exit /b 1
