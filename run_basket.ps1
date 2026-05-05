# Top-WR basket bot. Trades 8 highest-WR symbols from raw_momentum experiments.
# Diversified vs solo-TAO so a thin-book artifact on any single coin doesn't dominate.

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Remove-Item ".\data.real_basket_top.sqlite" -ErrorAction SilentlyContinue
Remove-Item ".\.universe_cache.real_basket_top.json" -ErrorAction SilentlyContinue

$cmd = @"
`$Host.UI.RawUI.WindowTitle = 'TOP basket :: 8096';
`$env:ZFEE_CONFIG_PATH = '$root\configs\real_basket_top.json';
`$env:ZFEE_DB_PATH = '$root\data.real_basket_top.sqlite';
`$env:ZFEE_UNIVERSE_CACHE = '$root\.universe_cache.real_basket_top.json';
`$env:PYTHONIOENCODING = 'utf-8';
`$env:PYTHONUNBUFFERED = '1';
& '$root\.venv\Scripts\python.exe' -m uvicorn backend.app:app --host 127.0.0.1 --port 8096;
"@
Start-Process powershell -ArgumentList @("-NoExit", "-Command", $cmd) -WindowStyle Normal

Write-Host ""
Write-Host "TOP basket bot launched on http://127.0.0.1:8096"
Write-Host ""
Write-Host "Symbols: TAO, ARB, ASTER, ENA, AAVE, FARTCOIN, ETC, ICP"
Write-Host "  4 concurrent slots (24% margin each, max leverage)"
Write-Host "  raw_momentum, min_bps=3.0 (less noise)"
Write-Host ""
Write-Host "Mode is PAPER. Switch to 'real' in configs/real_basket_top.json after validation."
