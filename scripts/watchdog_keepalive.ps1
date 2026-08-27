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
    # 4 producers, raised from 2. Two was a write-lock ceiling, not an LCU one:
    # both workers opened games.db directly and contended on the write lock
    # (414 "database is locked" hits across the old worker logs). Under the
    # single-writer fleet the producers never open the DB, so the ceiling is
    # now the LCU client instead.
    "--workers", "4",
    "--degraded-workers", "2",
    "--degrade-client-mb", "5200",
    "--client-restart-mb", "5800",
    # 6500, raised from 4200.  This gate only blocks STARTING workers; ones already
    # running are left alone, so the old value created a trap -- workers were
    # observed running fine with the client at 5,199MB, but once they needed a
    # restart at that size the watchdog refused forever (LeagueClient idles between
    # ~1.8GB and ~6GB, and nothing brings it back down without a client restart).
    # Sat there logging keep_worker_stopped with LCU healthy and collection at zero.
    # 6500 stays above the observed-good ceiling while still catching a client that
    # has genuinely run away.
    "--worker-start-max-client-mb", "6500",
    "--manual-seed-pending-cap", "120",
    "--check-interval-sec", "60",
    "--client-ready-timeout-sec", "600",
    "--games-per-player", "0",
    "--classic-claim-percent", "10",
    "--classic-revisit-min-hours", "10",
    "--classic-revisit-max-hours", "168",
    "--seed-riot-id-file", (Join-Path $root "data/seeds/opgg_tw.txt"),
    "--static-publish-growth-ratio", "0.10",
    "--static-publish-max-age-hours", "12",
    "--static-publish-threshold", "0",
    # Keep the current mature patch until a newer patch has enough data; then the normal
    # site build retrains empirical profiles and team-score calibration before publishing.
    # 10,000.  Champion WR and pair synergy are both shrunk toward the previous patch
    # now, so a young patch is no longer thin in the ways that used to break the page.
    # Replaying 16.14 at a 10,000-game patch with the 10-game display floor: 169/173
    # champions carry synergy at 2.12pp RMSE, versus 4.92pp for the old raw build at
    # 120,000 games.  Keep this in sync with SITE_PATCH_MIN_GAMES in aram_nn/site/db.py.
    "--static-publish-patch-prefix", "auto",
    "--static-publish-auto-patch-min-games", "10000"
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
