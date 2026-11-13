@echo off
rem AgentMemory daily refresh: rebuild PRELOAD_SUMMARY.md (distilled) + PRELOAD.md (index).
rem Created 2026-11-14. Non-interactive: never prompts.
rem
rem Why a task: the summary only stays useful if someone re-runs build_summary.py.
rem Nobody remembers. This runs daily and self-skips when nothing changed
rem (--if-stale), so it costs nothing on quiet days and never churns the
rem injected text (churn = provider prefix cache miss).
rem
rem Log: D:\AgentMemory\summary.log (auto-reset when over 512 KB)
rem Timestamp via PowerShell: %date% emits a GBK CJK weekday and breaks the UTF-8 log.
rem NOTE: keep this file pure ASCII - cmd.exe reads .bat as the OEM code page.
cd /d D:\AgentMemory
set PYTHONUTF8=1
set LOG=D:\AgentMemory\summary.log
if exist "%LOG%" for %%A in ("%LOG%") do if %%~zA GTR 524288 del "%LOG%"
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'"') do set TS=%%i
echo. >> "%LOG%"
echo ===== SUMMARY START %TS% ===== >> "%LOG%"
if exist "F:\Anaconda3\python.exe" (
    set PY="F:\Anaconda3\python.exe"
) else (
    set PY=python
)

rem 1) distilled summary (LLM cold path; keeps the old file if the LLM is unavailable)
"%PY%" scripts\build_summary.py --if-stale >> "%LOG%" 2>&1
set RC1=%errorlevel%

rem 2) warm-up index (no LLM, cheap) - keeps the injected index in sync
"%PY%" scripts\build_preload.py >> "%LOG%" 2>&1
set RC2=%errorlevel%

set RC=0
if not "%RC1%"=="0" set RC=%RC1%
if not "%RC2%"=="0" set RC=%RC2%
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'"') do set TS=%%i
echo ===== SUMMARY END %TS% exit=%RC% ===== >> "%LOG%"
exit /b %RC%
