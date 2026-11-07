@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo BA data export - make sure the game is logged in and at the lobby.
python -I -B "ba_data_export\ba_export.py"
echo.
pause
