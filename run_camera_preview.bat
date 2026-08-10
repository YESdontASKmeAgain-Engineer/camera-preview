@echo off
cd /d "%~dp0"
python camera_preview.py
if errorlevel 1 pause
