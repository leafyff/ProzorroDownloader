@echo off
chcp 65001 >nul
title Prozorro Downloader (консоль)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
"%~dp0.venv\Scripts\python.exe" -m app.main
pause
