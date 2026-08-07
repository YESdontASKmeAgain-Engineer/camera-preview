@echo off
cd /d "%~dp0"
python camera_preview.py --all
if errorlevel 1 pause
