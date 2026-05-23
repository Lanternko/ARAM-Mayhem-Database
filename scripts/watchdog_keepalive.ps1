$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$logDir = Join-Path $root "logs"
$keepaliveLog = Join-Path $logDir "mayhem-lcu-watchdog-keepalive.log"
$watchdogOut = Join-Path $logDir "mayhem-lcu-watchdog.out.log"
$watchdogErr = Join-Path $logDir "mayhem-lcu-watchdog.err.log"

New-Item -ItemType Directory -Force $logDir | Out-Null

function Write-KeepaliveLog {
    param([string]$Message)
    $timestamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
    Add-Content -LiteralPath $keepaliveLog -Encoding UTF8 -Value "$timestamp $Message"
}

$watchdog = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -eq "python.exe" -and
        $_.CommandLine -like "*mayhem_lcu_watchdog.py*"
    } |
    Select-Object -First 1

if ($watchdog) {
    Write-KeepaliveLog "ok watchdog_pid=$($watchdog.ProcessId)"
    exit 0
}

$argsList = @(
    "scripts/mayhem_lcu_watchdog.py",
    "--workers", "3",
    "--degraded-workers", "2",
    "--degrade-client-mb", "5200",
    "--client-restart-mb", "5800",
    "--worker-start-max-client-mb", "4200",
    "--manual-seed-pending-cap", "40",
    "--check-interval-sec", "60",
    "--client-ready-timeout-sec", "600",
    "--games-per-player", "4"
)

$process = Start-Process `
    -FilePath "python" `
    -ArgumentList $argsList `
    -WorkingDirectory $root `
    -RedirectStandardOutput $watchdogOut `
    -RedirectStandardError $watchdogErr `
    -WindowStyle Hidden `
    -PassThru

Write-KeepaliveLog "started watchdog_pid=$($process.Id)"
