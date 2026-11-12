@echo off
rem Double-click to open the surface digital twin in its own window. Close the window to quit.
title LPBF Surface Digital Twin
cd /d "%~dp0"
where py >nul 2>nul && (py twin_app.py %*) || (python twin_app.py %*)
if errorlevel 1 pause
