@echo off
cd /d "%~dp0"
powershell -NoProfile -Command "Start-Process -FilePath 'F:\python\pythonw.exe' -ArgumentList '-B','-m','hbr_capture.memory_widget' -WorkingDirectory (Get-Location).Path -Verb RunAs -WindowStyle Hidden"
