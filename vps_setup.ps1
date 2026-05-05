# VPS Setup Script for mexc0feesflipper
# Run this once on the VPS to install everything and start the bot.
# Usage: Right-click -> "Run with PowerShell" OR paste into PowerShell terminal.

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "=== MEXC 0-fee Flipper - VPS Setup ===" -ForegroundColor Cyan
Write-Host "Working directory: $root"
Write-Host ""

# ---- 1. Find Python (prefer 3.11 > 3.12 > 3.13 > any) ----
$pythonCmd = $null
foreach ($candidate in @("python3.11", "python3.12", "python3.13", "python3.14", "python3", "python")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python 3\.(1[1-9]|[2-9]\d)") {
            $pythonCmd = $candidate
            Write-Host "Found: $ver  (using '$candidate')" -ForegroundColor Green
            break
        }
    } catch {}
}

if (-not $pythonCmd) {
    Write-Host "No suitable Python found. Installing Python 3.11 via winget..." -ForegroundColor Yellow
    winget install --id Python.Python.3.11 --silent --accept-source-agreements --accept-package-agreements
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
    $pythonCmd = "python3.11"
    if (-not (Get-Command $pythonCmd -ErrorAction SilentlyContinue)) {
        $pythonCmd = "python"
    }
    Write-Host "Python installed." -ForegroundColor Green
}

# ---- 2. Create venv ----
$venvPath = "$root\.venv"
if (-not (Test-Path "$venvPath\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    & $pythonCmd -m venv $venvPath
    Write-Host "Venv created at $venvPath" -ForegroundColor Green
} else {
    Write-Host "Venv already exists, skipping creation." -ForegroundColor Gray
}

$pip = "$venvPath\Scripts\pip.exe"
$python = "$venvPath\Scripts\python.exe"

# ---- 3. Install dependencies ----
Write-Host ""
Write-Host "Installing dependencies (binary-only first to avoid C compiler)..." -ForegroundColor Yellow

# First pass: binary wheels only (fast, no compiler needed)
& $pip install --upgrade pip --quiet
& $pip install -r "$root\requirements.txt" --only-binary=:all: --quiet

if ($LASTEXITCODE -ne 0) {
    Write-Host "Binary-only install had issues. Trying with source fallback..." -ForegroundColor Yellow
    & $pip install -r "$root\requirements.txt" --quiet
}

Write-Host "Dependencies installed." -ForegroundColor Green

# ---- 4. Verify aiohttp ----
$aioVer = & $python -c "import aiohttp; print(aiohttp.__version__)" 2>&1
Write-Host "aiohttp version: $aioVer" -ForegroundColor Cyan

# ---- 5. Create data directory ----
if (-not (Test-Path "$root\data")) {
    New-Item -ItemType Directory -Path "$root\data" | Out-Null
}

Write-Host ""
Write-Host "=== Setup complete! ===" -ForegroundColor Green
Write-Host ""
Write-Host "To start the bot:" -ForegroundColor White
Write-Host "  .\vps_run.ps1" -ForegroundColor Yellow
Write-Host ""
Write-Host "Or manually:" -ForegroundColor White
Write-Host "  `$env:ZFEE_CONFIG_PATH = '.\configs\paper.json'" -ForegroundColor Gray
Write-Host "  .\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 0.0.0.0 --port 8000" -ForegroundColor Gray
