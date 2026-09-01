@echo off
REM Runs one stage of the OS strategy. Usage: run_stage.cmd entry | exit
REM
REM This is the PACKAGED variant -- it calls osmain.exe sitting next to it.
REM The development variant lives in tools\run_stage.cmd and calls python.
REM
REM ASCII-only on purpose: cmd reads a .bat/.cmd file in the system codepage
REM BEFORE chcp takes effect, so UTF-8 comments here get mangled and can even
REM break parsing.
REM
REM Two reasons this wrapper exists. The second one is easy to miss:
REM
REM   1. The log filename must change daily, but a scheduled task's argument
REM      is a fixed string decided at setup time. The date has to be computed
REM      here, at run time. %date% is not usable -- its format follows the
REM      system locale -- so PowerShell provides a deterministic one.
REM
REM   2. THE DAILY LOG FILES COME FROM THE ">>" REDIRECT BELOW AND NOWHERE
REM      ELSE. The program installs no file handler. Point the scheduler
REM      straight at osmain.exe and every log silently stops being written --
REM      while the only failure detector this system has is "no Discord
REM      message in the morning", which does NOT fire for "ran but went
REM      wrong". Do not remove this wrapper.

chcp 65001 >nul

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set TODAY=%%i

set ROOT=%~dp0
if not exist "%ROOT%logs" mkdir "%ROOT%logs"
"%ROOT%osmain.exe" %1 >> "%ROOT%logs\%1-%TODAY%.log" 2>&1
