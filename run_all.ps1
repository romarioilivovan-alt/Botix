# Launches focused experiment: 4 simple direction-following bots with TAKER entry.
# 50 USDT paper balance, 100x leverage, max 5 slots.

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$strategies = @(
    @{ Name = "raw_momentum";     Port = 8090 },
    @{ Name = "inv_raw_momentum"; Port = 8091 },
    @{ Name = "book_lean";        Port = 8092 },
    @{ Name = "inv_book_lean";    Port = 8093 }
)

# Wipe stale databases for a clean run.
foreach ($s in $strategies) {
    Remove-Item ".\data.$($s.Name).sqlite" -ErrorAction SilentlyContinue
    Remove-Item ".\.universe_cache.$($s.Name).json" -ErrorAction SilentlyContinue
}

foreach ($s in $strategies) {
    $cmd = @"
`$Host.UI.RawUI.WindowTitle = '$($s.Name) :: $($s.Port)';
`$env:ZFEE_CONFIG_PATH = '$root\configs\$($s.Name).json';
`$env:ZFEE_DB_PATH = '$root\data.$($s.Name).sqlite';
`$env:ZFEE_UNIVERSE_CACHE = '$root\.universe_cache.$($s.Name).json';
`$env:PYTHONIOENCODING = 'utf-8';
`$env:PYTHONUNBUFFERED = '1';
& '$root\.venv\Scripts\python.exe' -m uvicorn backend.app:app --host 127.0.0.1 --port $($s.Port);
"@
    Start-Process powershell -ArgumentList @("-NoExit", "-Command", $cmd) -WindowStyle Normal
    Write-Host "[$($s.Name)] http://127.0.0.1:$($s.Port) -> data.$($s.Name).sqlite"
    Start-Sleep -Seconds 2
}

Write-Host ""
Write-Host "Experiment design - 4 bots:"
Write-Host "  raw_momentum      direction = sign(Binance dPrice over 1s)"
Write-Host "  inv_raw_momentum  control: opposite of raw_momentum"
Write-Host "  book_lean         direction = sign(MEXC top-5 imbalance)"
Write-Host "  inv_book_lean     control: opposite of book_lean"
Write-Host ""
Write-Host "All use TAKER entry (immediate market fill - fixes adverse selection)"
Write-Host "Starting balance: 50 USDT  Leverage: 100x  0-fee symbols only"
Write-Host ""
Write-Host "Compare with: .\.venv\Scripts\python.exe compare.py"
