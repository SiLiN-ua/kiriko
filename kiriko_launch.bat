@echo off
cd /d "%~dp0"
start "" pythonw app.py
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:5099/splash"
