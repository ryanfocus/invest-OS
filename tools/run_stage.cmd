@echo off
REM Runs one stage of the OS strategy. Usage: run_stage.cmd entry | exit
REM
REM ASCII-only on purpose: cmd reads a .bat/.cmd file in the system codepage
REM BEFORE chcp takes effect, so UTF-8 comments here get mangled and can even
REM break parsing. The reasoning lives in tools/setup_schedule.ps1 and
REM docs/DEPLOY.md instead -- both handle UTF-8 fine.
REM
REM Why a wrapper at all: the log filename must change daily, but a scheduled
REM task's argument is a fixed string decided at setup time. The date has to be
REM computed here, at run time. %date% is not usable -- its format follows the
REM system locale -- so PowerShell provides a deterministic one.

chcp 65001 >nul

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set TODAY=%%i

set ROOT=%~dp0..
"%ROOT%\.venv\Scripts\python.exe" -X utf8 "%ROOT%\main.py" %1 >> "%ROOT%\logs\%1-%TODAY%.log" 2>&1
