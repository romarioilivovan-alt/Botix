$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "=== MEXC 0-fee Flipper - VPS Setup ===" -ForegroundColor Cyan
Write-Host "Working directory: $root"
Write-Host ""

$pythonCmd = $null
foreach ($candidate in @("python3.11", "python3.12", "python3.13", "python3", "python")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python 3\.(1[1-9]|[2-9]\d)") {
            $pythonCmd = $candidate
            Write-Host "Found: $ver (using '$candidate')" -ForegroundColor Green
            break
        }
    } catch {}
}

if (-not $pythonCmd) {
    Write-Host "No suitable Python found. Installing Python 3.11 via winget..." -ForegroundColor Yellow
    winget install --id Python.Python.3.11 --silent --accept-source-agreements --accept-package-agreements
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
    $pythonCmd = "python"
}

$venvPath = "$root\.venv"
if (-not (Test-Path "$venvPath\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    & $pythonCmd -m venv $venvPath
}

$pip = "$venvPath\Scripts\pip.exe"
$python = "$venvPath\Scripts\python.exe"

Write-Host ""
Write-Host "Installing live dependencies..." -ForegroundColor Yellow
& $pip install --upgrade pip
& $pip install -r "$root\requirements.txt"

if (-not (Test-Path "$root\runs")) {
    New-Item -ItemType Directory -Path "$root\runs" | Out-Null
}

Write-Host ""
Write-Host "Installed Python packages:" -ForegroundColor White
Get-Content "$root\requirements.txt" | ForEach-Object { Write-Host "  $_" -ForegroundColor Gray }

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Green
Write-Host "Default live line: config.real_lineA_contract_v2.json" -ForegroundColor White
Write-Host ""
Write-Host "Next:" -ForegroundColor White
Write-Host "  1. Run .\\vps_run.ps1" -ForegroundColor Yellow
Write-Host "  2. Open the dashboard on port 8086" -ForegroundColor Yellow
Write-Host "  3. Paste fresh MEXC Web UID" -ForegroundColor Yellow
Write-Host "  4. Wait for Auth: OK" -ForegroundColor Yellow
Write-Host "  5. Only then press Start" -ForegroundColor Yellow
