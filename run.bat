@echo off
rem ASCII only. Cyrillic text here breaks cmd.exe: after chcp it loses its
rem position in the file and starts executing fragments of lines as commands.
title Prozorro Downloader
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" goto :novenv

start "" "%PY%" -m app.main
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
