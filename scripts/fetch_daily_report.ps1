[CmdletBinding()]
param(
    [string]$OutputDir,
    [string]$SshUser = "wyulng",
    [string]$SshHost = "43.128.130.240",
    [int]$SshPort = 22222,
    [string]$SshKeyPath,
    [string]$RemoteContainer = "joytag-backend",
    [string]$RemoteReportDir = "/app/reports"
)

$ErrorActionPreference = "Stop"

if (-not $OutputDir) {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $OutputDir = Join-Path $repoRoot "runtime-reports\daily"
}
if (-not $SshKeyPath) {
    $SshKeyPath = Join-Path $env:USERPROFILE ".ssh\id_ed25519"
}

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "OpenSSH client (ssh.exe) is not installed."
}
if (-not (Test-Path -LiteralPath $SshKeyPath -PathType Leaf)) {
    throw "SSH key was not found at the configured path."
}

function Invoke-RemoteReportFile {
    param([Parameter(Mandatory = $true)][string]$FileName)

    $target = "$SshUser@$SshHost"
    $remoteCommand = "docker exec $RemoteContainer cat $RemoteReportDir/$FileName"
    $sshArgs = @(
        "-p", "$SshPort",
        "-i", $SshKeyPath,
        "-o", "BatchMode=yes",
        $target,
        $remoteCommand
    )
    $content = & ssh @sshArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Remote report fetch failed for $FileName (exit code $LASTEXITCODE)."
    }
    return ($content -join [Environment]::NewLine)
}

function Write-AtomicText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temp = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $utf8 = [System.Text.UTF8Encoding]::new($false)
        [System.IO.File]::WriteAllText($temp, $Content, $utf8)
        Move-Item -LiteralPath $temp -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $temp) {
            Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        }
    }
}

$jsonText = Invoke-RemoteReportFile -FileName "latest.json"
$markdownText = Invoke-RemoteReportFile -FileName "latest.md"

try {
    $report = $jsonText | ConvertFrom-Json
}
catch {
    throw "The remote latest.json is not valid JSON."
}

if ($report.schema_version -ne "1.0") {
    throw "Unsupported daily report schema version."
}
if (@("success", "degraded") -notcontains [string]$report.status) {
    throw "The daily report has an invalid status."
}

$shanghaiNow = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
    [DateTimeOffset]::UtcNow,
    "China Standard Time"
)
$expectedDate = $shanghaiNow.ToString("yyyy-MM-dd")
if ([string]$report.report_date -ne $expectedDate) {
    throw "The remote report is not for the current Shanghai date."
}

try {
    $generatedAt = [DateTimeOffset]::Parse([string]$report.generated_at)
    $age = [DateTimeOffset]::UtcNow - $generatedAt.ToUniversalTime()
    if ($age.TotalHours -lt -1 -or $age.TotalHours -gt 26) {
        throw "The remote report is stale."
    }
}
catch {
    throw "The daily report generated_at value is invalid or stale."
}

if ([string]::IsNullOrWhiteSpace($markdownText)) {
    throw "The remote latest.md is empty."
}

$staging = Join-Path $OutputDir ".staging-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $staging -Force | Out-Null
try {
    $dateJson = Join-Path $staging "daily-$expectedDate.json"
    $dateMarkdown = Join-Path $staging "daily-$expectedDate.md"
    $latestJson = Join-Path $staging "latest.json"
    $latestMarkdown = Join-Path $staging "latest.md"
    Write-AtomicText -Path $dateJson -Content $jsonText
    Write-AtomicText -Path $dateMarkdown -Content $markdownText
    Write-AtomicText -Path $latestJson -Content $jsonText
    Write-AtomicText -Path $latestMarkdown -Content $markdownText

    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
    foreach ($file in @($dateJson, $dateMarkdown, $latestJson, $latestMarkdown)) {
        Move-Item -LiteralPath $file -Destination (Join-Path $OutputDir (Split-Path -Leaf $file)) -Force
    }
}
finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$cutoff = (Get-Date).AddDays(-90)
Get-ChildItem -LiteralPath $OutputDir -File -Filter "daily-*" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    ForEach-Object {
        Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
    }

Write-Output ("daily_report_fetched report_date={0} status={1} output_dir={2}" -f $expectedDate, $report.status, $OutputDir)
