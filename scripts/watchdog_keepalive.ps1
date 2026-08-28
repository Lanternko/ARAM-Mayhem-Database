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
    # Back to 2 producers, measured 2026-08-28. Raising this to 4 under the
    # single-writer fleet was meant to test whether the old ceiling was the write
    # lock; it was not. Hourly games captured barely moved -- ~1,950/hr on 2
    # producers (06-13 UTC) against ~2,040/hr on 4 (14-19 UTC), inside the daily
    # swing -- so the ceiling really is the one shared LCU client every producer
    # queries. The extra two bought nothing and doubled the blast radius: the
    # fleet exits as a unit when any single producer dies (observed 2026-08-28
    # 01:37, exitcode=1, ~1.5h of collection lost), where the old per-worker
    # supervision only lost the one.
    "--workers", "2",
    "--degraded-workers", "1",
    # Lowered 2026-08-28 from 5200/5800.  Measured over 17,021 watchdog cycles
    # (30 days): counting upward crossings of each candidate threshold gives
    # ~6.7 restarts/day at 5800 and ~6.9/day at 4500 -- essentially the same.
    # Client memory does not wobble around a line; once it passes ~3.5GB it
    # almost always runs away to 6GB+, so lowering the trigger catches the same
    # excursions earlier rather than adding new ones.  Restarting at 4500
    # instead of 5800 keeps the client ~1.3GB further from the
    # worker-start-max gate below, which is what turned the 2026-08-28
    # PreEndOfGame hang into a 4.5h outage: memory blew past every gate while
    # the phase check blocked the restart.  Degrade keeps its 600MB lead so it
    # still gets a chance to shed a producer before the client is recycled.
    "--degrade-client-mb", "3900",
    "--client-restart-mb", "4500",
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
