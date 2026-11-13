# Register the AgentMemory daily summary-refresh task.
#
# Why: PRELOAD_SUMMARY.md (the distilled memory injected into every session) only stays
# useful if someone re-runs build_summary.py. Nobody remembers to. This task runs daily
# and self-skips when nothing changed (--if-stale), so quiet days cost nothing and the
# injected text never churns (churn = provider prefix cache miss).
#
# The same script also rebuilds PRELOAD.md (the warm-up index, no LLM, cheap).
#
# Idempotent: -Force overwrites an existing task of the same name.

$taskName   = 'AgentMemory-summary-refresh'
$scriptPath = 'D:\AgentMemory\scripts\refresh-summary.bat'
$startAt    = (Get-Date '08:30')

if (-not (Test-Path $scriptPath)) {
    throw "Missing $scriptPath - create the launcher first."
}

$action  = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$scriptPath`"" -WorkingDirectory 'D:\AgentMemory'
# Daily at 08:30; StartWhenAvailable makes a missed run fire on the next boot/logon
# instead of silently skipping the day (laptop is often off at 08:30).
$trigger = New-ScheduledTaskTrigger -Daily -At $startAt
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description 'Daily refresh of AgentMemory PRELOAD_SUMMARY.md + PRELOAD.md (self-skips when no entry changed; log: D:\AgentMemory\summary.log)' | Out-Null

$t = Get-ScheduledTask -TaskName $taskName
$i = $t | Get-ScheduledTaskInfo
Write-Host ('Registered: ' + $t.TaskName + ' | State: ' + $t.State)
Write-Host ('NextRunTime: ' + $i.NextRunTime)
Write-Host ('Action: ' + $t.Actions[0].Execute + ' ' + $t.Actions[0].Arguments)
Write-Host ('Start in: ' + $t.Actions[0].WorkingDirectory)
