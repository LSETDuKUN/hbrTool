@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "HBR 资料室" "F:\python\pythonw.exe" -m hbr_data.window
