@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  HBR capture widget - double click to launch.
rem  Uses pythonw.exe so NO console window appears at all,
rem  which also means nothing extra can cover the game.
rem
rem  No --match needed: the game window is auto-detected by its
rem  window class (UnityWndClass). The launcher shares the exact
rem  same process name, so class name is the only reliable signal.
rem
rem  Triggers on by default: damage (saves the instant a damage
rem  number is recognised) + settle. Turn either off with
rem  --no-damage-trigger / --no-auto if you only want one.
rem ============================================================

set "PYW=F:\python\pythonw.exe"
if not exist "%PYW%" for %%I in (pythonw.exe) do set "PYW=%%~$PATH:I"

if not exist "%PYW%" (
  echo Cannot find pythonw.exe
  echo Edit this file and set PYW to your pythonw.exe path.
  pause
  exit /b 1
)

start "" "%PYW%" -m hbr_capture.cli widget --outdir frames
exit /b 0
