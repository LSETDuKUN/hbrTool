@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  Console version - keeps the window so you can watch the log
rem  and troubleshoot. For normal use, double click the widget
rem  launcher instead (that one shows no console window at all).
rem
rem  No --match needed: the game window is auto-detected by its
rem  window class (UnityWndClass).
rem ============================================================

set "PY=F:\python\python.exe"
if not exist "%PY%" for %%I in (python.exe) do set "PY=%%~$PATH:I"

if not exist "%PY%" (
  echo Cannot find python.exe
  pause
  exit /b 1
)

"%PY%" -m hbr_capture.cli watch --outdir frames --damage-trigger --ask-keep
pause
