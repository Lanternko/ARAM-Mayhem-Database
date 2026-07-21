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
        ($_.Name -eq "python.exe" -or $_.Name -eq "pythonw.exe") -and
        $_.CommandLine -like "*mayhem_lcu_watchdog.py*"
    } |
    Select-Object -First 1

if ($watchdog) {
    Write-KeepaliveLog "ok watchdog_pid=$($watchdog.ProcessId)"
    exit 0
}

$argsList = @(
    "scripts/mayhem_lcu_watchdog.py",
    "--workers", "2",
    "--degraded-workers", "1",
    "--degrade-client-mb", "5200",
    "--client-restart-mb", "5800",
    "--worker-start-max-client-mb", "4200",
    "--manual-seed-pending-cap", "120",
    "--check-interval-sec", "60",
    "--client-ready-timeout-sec", "600",
    "--games-per-player", "4",
    "--seed-riot-id-file", (Join-Path $root "data/seeds/opgg_tw.txt"),
    "--static-publish-growth-ratio", "0.10",
    "--static-publish-threshold", "0",
    # Keep the current mature patch until a newer patch has enough data; then the normal
    # site build retrains empirical profiles and team-score calibration before publishing.
    "--static-publish-patch-prefix", "auto",
    "--static-publish-auto-patch-min-games", "50000"
)

$pythonw = Join-Path (Split-Path (Get-Command python).Source -Parent) "pythonw.exe"
$pythonExe = if (Test-Path $pythonw) { $pythonw } else { "python" }

$process = Start-Process `
    -FilePath $pythonExe `
    -ArgumentList $argsList `
    -WorkingDirectory $root `
    -RedirectStandardOutput $watchdogOut `
    -RedirectStandardError $watchdogErr `
    -WindowStyle Hidden `
    -PassThru

Write-KeepaliveLog "started watchdog_pid=$($process.Id)"
