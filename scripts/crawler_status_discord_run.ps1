$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$logDir = Join-Path $root "logs"
$log = Join-Path $logDir "crawler-status-discord.log"
New-Item -ItemType Directory -Force $logDir | Out-Null

function Write-Log([string]$Message) {
    $ts = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
    Add-Content -LiteralPath $log -Encoding UTF8 -Value "$ts $Message"
}

Set-Location $root
try {
    $output = & python "scripts/crawler_status_discord.py" 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Write-Log "FAIL exit=$LASTEXITCODE $output"
        exit $LASTEXITCODE
    }
    Write-Log "ok $output".Trim()
    exit 0
} catch {
    Write-Log "ERROR $_"
    exit 1
}
