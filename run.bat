@echo off
chcp 65001 >nul
title Prozorro Downloader
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Не знайдено віртуальне середовище .venv
    echo Створіть його командою:  python -m venv .venv
    echo Потім встановіть залежності:  .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

start "" "%PY%" -m app.main
