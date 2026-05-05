# Start the bot on the VPS.
# Run after vps_setup.ps1 has been executed.

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = "$root\.venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "ERROR: venv not found. Run vps_setup.ps1 first." -ForegroundColor Red
    exit 1
}

# Default config: paper mode on selected coins (AAVE, PEPE, PENGU, BCH, DOGE, ICP)
# You can override by setting ZFEE_CONFIG_PATH before running this script.
if (-not $env:ZFEE_CONFIG_PATH) {
    $env:ZFEE_CONFIG_PATH = "$root\config.json"
}
if (-not $env:ZFEE_DB_PATH) {
    $env:ZFEE_DB_PATH = "$root\data\trades.sqlite"
}
if (-not $env:ZFEE_UNIVERSE_CACHE) {
    $env:ZFEE_UNIVERSE_CACHE = "$root\.universe_cache.json"
}

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

Write-Host "=== Starting MEXC 0-fee Flipper ===" -ForegroundColor Cyan
Write-Host "Config:  $env:ZFEE_CONFIG_PATH"
Write-Host "DB:      $env:ZFEE_DB_PATH"
Write-Host "Access dashboard at: http://<VPS_IP>:8000"
Write-Host ""
Write-Host "Press Ctrl+C to stop."
Write-Host ""

& $python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000
