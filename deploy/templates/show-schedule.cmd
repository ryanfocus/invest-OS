@echo off
REM Show the current scheduled tasks. Read-only, changes nothing.
REM ASCII-only on purpose: cmd reads this file in the system codepage BEFORE
REM chcp takes effect, so UTF-8 comments get mangled and can break parsing.
REM The explanations live in README.md, which handles UTF-8 fine.
REM
REM Why a .cmd wrapper instead of just shipping the .ps1:
REM   * Double-clicking a .ps1 opens Notepad, it does not run it.
REM   * A stock Windows client refuses to run .ps1 at all (Restricted).
REM   * A .ps1 that arrived inside a zip from the internet carries the
REM     Mark-of-the-Web and is blocked even under RemoteSigned.
REM   -ExecutionPolicy Bypass clears all three (verified 2026-09-02).

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_schedule.ps1" -Show

echo.
pause
