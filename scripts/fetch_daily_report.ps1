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

function Invoke-RemoteReportBytes {
    param([Parameter(Mandatory = $true)][string]$FileName)

    $target = "$SshUser@$SshHost"
    # SSH output is transported as ASCII Base64 so Windows PowerShell cannot
    # reinterpret the report bytes through the local console code page.
    $remoteCommand = "docker exec $RemoteContainer base64 -w 0 $RemoteReportDir/$FileName"
    $sshArgs = @(
        "-p", "$SshPort",
        "-i", $SshKeyPath,
        "-o", "BatchMode=yes",
        $target,
        $remoteCommand
    )
    $encoded = & ssh @sshArgs 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Remote report fetch failed for $FileName (exit code $LASTEXITCODE)."
    }
    $encodedText = ($encoded -join "") -replace "\s", ""
    if ([string]::IsNullOrWhiteSpace($encodedText)) {
        throw "Remote report fetch returned no data for $FileName."
    }
    try {
        $bytes = [Convert]::FromBase64String($encodedText)
        return ,$bytes
    }
    catch {
        throw "Remote report fetch returned invalid Base64 for $FileName."
    }
}

function Convert-ReportBytesToText {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$FileName
    )

    try {
        $utf8 = [System.Text.UTF8Encoding]::new($false, $true)
        return $utf8.GetString($Bytes)
    }
    catch {
        throw "Remote report is not valid UTF-8 for $FileName."
    }
}

function Write-AtomicBytes {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Bytes
    )

    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temp = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::WriteAllBytes($temp, $Bytes)
        Move-Item -LiteralPath $temp -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $temp) {
            Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        }
    }
}

$jsonBytes = Invoke-RemoteReportBytes -FileName "latest.json"
$markdownBytes = Invoke-RemoteReportBytes -FileName "latest.md"
$jsonText = Convert-ReportBytesToText -Bytes $jsonBytes -FileName "latest.json"
$markdownText = Convert-ReportBytesToText -Bytes $markdownBytes -FileName "latest.md"

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
    Write-AtomicBytes -Path $dateJson -Bytes $jsonBytes
    Write-AtomicBytes -Path $dateMarkdown -Bytes $markdownBytes
    Write-AtomicBytes -Path $latestJson -Bytes $jsonBytes
    Write-AtomicBytes -Path $latestMarkdown -Bytes $markdownBytes

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
