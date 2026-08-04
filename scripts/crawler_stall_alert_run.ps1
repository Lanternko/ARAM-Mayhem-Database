$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$logDir = Join-Path $root "logs"
$log = Join-Path $logDir "crawler-stall-alert.log"
New-Item -ItemType Directory -Force $logDir | Out-Null

function Write-Log([string]$Message) {
    $ts = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
    Add-Content -LiteralPath $log -Encoding UTF8 -Value "$ts $Message"
}

Set-Location $root
try {
    $output = & python "scripts/crawler_status_discord.py" --stall-alert 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Write-Log "FAIL exit=$LASTEXITCODE $output"
        exit $LASTEXITCODE
    }
    $trimmed = $output.Trim()
    # Silent runs (healthy, nothing to report) print nothing -- do not pad the
    # log with a line every 5 minutes; only record ticks that actually did
    # something (an alert fired, a recovery posted, or an error).
    if ($trimmed) {
        Write-Log "ok $trimmed"
    }
    exit 0
} catch {
    Write-Log "ERROR $_"
    exit 1
}
