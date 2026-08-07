@echo off
cd /d "%~dp0"
python camera_preview.py --camera 0
if errorlevel 1 pause
