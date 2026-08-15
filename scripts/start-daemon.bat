@echo off
rem AgentMemory daemon autostart launcher (for scheduled task)
rem Skip if port 8123 is already listening (avoid duplicate instance)
cd /d D:\AgentMemory
netstat -ano | findstr ":8123" | findstr "LISTENING" > nul
if %errorlevel%==0 (
    echo [AgentMemory] daemon already running, skip
    exit /b 0
)
where python > nul 2>&1
if %errorlevel%==0 (
    start /min "" python scripts\daemon.py --once-preload
) else (
    echo [AgentMemory] python not found on PATH, fallback to Anaconda
    start /min "" F:\Anaconda3\python.exe scripts\daemon.py --once-preload
)
echo [AgentMemory] daemon started
