@echo off
rem ASCII only -- see the note in run.bat.
rem chcp 65001 is safe here precisely because this file has no Cyrillic:
rem it is needed so the app's Ukrainian output shows correctly in the console.
chcp 65001 >nul
title Prozorro Downloader (console)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" goto :novenv

"%PY%" -m app.main
echo.
echo Exit code: %ERRORLEVEL%
pause
exit /b 0

:novenv
echo.
echo Virtual environment .venv not found.
echo.
echo     python -m venv .venv
echo     .venv\Scripts\pip install -r requirements.txt
echo.
pause
exit /b 1
