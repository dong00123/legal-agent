# Register the legal-agent crawl round as a Windows Scheduled Task.
# ASCII-only on purpose: Windows PowerShell 5.1 may mis-decode non-BOM UTF-8 scripts.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\register_crawl_task.ps1 -Time 23:30
#   powershell -ExecutionPolicy Bypass -File scripts\register_crawl_task.ps1 -Unregister
#
# Notes:
#   - The task runs as the CURRENT user (logon type: interactive), so it can reuse
#     the logged-in browser session under CRAWL_STATE_DIR. It will not run while the
#     user is logged off; register with -User/-Password yourself if you need that.
#   - "-MultipleInstances IgnoreNew" prevents two rounds from overlapping: one round
#     can block for a long time waiting on a captcha or the next-day quota.
param(
    [string]$TaskName = "LegalAgentCrawl",
    [string]$Time = "23:30",
    [string]$Bat = "",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task: $TaskName"
    return
}

if (-not $Bat) { $Bat = Join-Path $PSScriptRoot "run_crawl_once.bat" }
if (-not (Test-Path $Bat)) { throw "Not found: $Bat" }

$action = New-ScheduledTaskAction -Execute $Bat -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Force `
    -Description "Legal-agent crawler: one round of statutes + cases (backend.crawler.runner --once)" | Out-Null

Write-Host "Registered scheduled task '$TaskName' -> $Bat at $Time daily."
Write-Host "Test now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Log file:  $repo\logs\crawl.log"
