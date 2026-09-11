$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$logDir = Join-Path $root "logs"
$log = Join-Path $logDir "prune-stale-db-snapshots.log"
New-Item -ItemType Directory -Force $logDir | Out-Null

function Write-Log([string]$Message) {
    $ts = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
    Add-Content -LiteralPath $log -Encoding UTF8 -Value "$ts $Message"
}

Set-Location $root
try {
    $output = & python "scripts/prune_stale_db_snapshots.py" --apply 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Write-Log "FAIL exit=$LASTEXITCODE $output"
        exit $LASTEXITCODE
    }
    $trimmed = $output.Trim()
    # Nothing to prune prints nothing -- same contract as the stall alert wrapper.
    # A daily task that logs "found 0" forever trains you to stop reading the log.
    if ($trimmed) {
        Write-Log "ok $trimmed"
    }
    exit 0
} catch {
    Write-Log "ERROR $_"
    exit 1
}
