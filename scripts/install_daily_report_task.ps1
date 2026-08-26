[CmdletBinding()]
param(
    [string]$At = "10:05",
    [string]$TaskName = "Joytag-Daily-Report"
)

$ErrorActionPreference = "Stop"
$fetchScript = Join-Path $PSScriptRoot "fetch_daily_report.ps1"
$repoRoot = Split-Path -Parent $PSScriptRoot
$outputDir = Join-Path $repoRoot "runtime-reports\daily"

if (-not (Test-Path -LiteralPath $fetchScript -PathType Leaf)) {
    throw "The daily report fetch script was not found."
}

try {
    $time = [DateTime]::Today.Add([TimeSpan]::Parse($At))
}
catch {
    throw "-At must be a local 24-hour time such as 10:05."
}

$action = New-ScheduledTaskAction `
    -Execute "PowerShell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"{0}`" -OutputDir `"{1}`"" -f $fetchScript, $outputDir)
$trigger = New-ScheduledTaskTrigger -Daily -At $time
$principal = New-ScheduledTaskPrincipal `
    -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
    -LogonType InteractiveToken `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Description "Fetch the Joytag daily collection and library report over SSH." `
    -Force | Out-Null

Write-Output ("registered_task name={0} local_time={1} report_source=Asia/Shanghai_09:00" -f $TaskName, $At)
