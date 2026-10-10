@echo off
cd /d "%~dp0"
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $p='F:\python\pythonw.exe'; if (-not (Test-Path -LiteralPath $p)) {$p=(Get-Command pythonw.exe -ErrorAction Stop).Source}; Start-Process -FilePath $p -ArgumentList '-B','-m','hbr_live_app' -WorkingDirectory (Get-Location).Path -Verb RunAs -WindowStyle Hidden"
if errorlevel 1 pause
