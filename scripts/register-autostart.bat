@echo off
chcp 65001 > nul
echo 注册 AgentMemory-daemon 开机自启任务...
schtasks /Create /TN "AgentMemory-daemon" /TR "D:\AgentMemory\scripts\start-daemon.bat" /SC ONLOGON /RL LIMITED /F
echo.
echo 查询任务状态：
schtasks /Query /TN "AgentMemory-daemon" /FO LIST
pause
