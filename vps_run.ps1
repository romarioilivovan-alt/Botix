$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "ERROR: venv not found. Run vps_setup.ps1 first." -ForegroundColor Red
    exit 1
}

if (-not $env:ZFEE_CONFIG_PATH) {
    $env:ZFEE_CONFIG_PATH = "$root\config.real_lineA_contract_v2.json"
}
if (-not $env:ZFEE_RUN_DIR) {
    $env:ZFEE_RUN_DIR = "$root\runs\lineA_real_v2"
}
if (-not (Test-Path $env:ZFEE_RUN_DIR)) {
    New-Item -ItemType Directory -Path $env:ZFEE_RUN_DIR | Out-Null
}
if (-not $env:ZFEE_DB_PATH) {
    $stamp = (Get-Date).ToString("yyyyMMdd_HHmmss")
    $env:ZFEE_DB_PATH = Join-Path $env:ZFEE_RUN_DIR "real_$stamp.sqlite"
}
if (-not $env:ZFEE_UNIVERSE_CACHE) {
    $env:ZFEE_UNIVERSE_CACHE = "$root\.universe_cache.real_lineA_contract_v2.json"
}

$config = Get-Content $env:ZFEE_CONFIG_PATH -Raw | ConvertFrom-Json
$port = if ($env:ZFEE_PORT) { [int]$env:ZFEE_PORT } elseif ($config.port) { [int]$config.port } else { 8086 }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

$listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    Write-Host "Stopping process on port $port ..." -ForegroundColor Yellow
    Stop-Process -Id $listener.OwningProcess -Force
}

Write-Host "=== Starting MEXC 0-fee Flipper (live lineA default) ===" -ForegroundColor Cyan
Write-Host "Config:      $env:ZFEE_CONFIG_PATH"
Write-Host "DB:          $env:ZFEE_DB_PATH"
Write-Host "Cache:       $env:ZFEE_UNIVERSE_CACHE"
Write-Host "Dashboard:   http://<VPS_IP>:$port"
Write-Host ""
Write-Host "Before Start in UI:" -ForegroundColor White
Write-Host "  1. Open the dashboard" -ForegroundColor Gray
Write-Host "  2. Paste fresh MEXC Web UID" -ForegroundColor Gray
Write-Host "  3. Confirm Auth: OK" -ForegroundColor Gray
Write-Host "  4. Only then press Start" -ForegroundColor Gray
Write-Host ""
Write-Host "Press Ctrl+C to stop."
Write-Host ""

& $python -m uvicorn backend.app:app --host 0.0.0.0 --port $port
