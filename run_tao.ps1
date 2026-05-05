# Single-symbol TAO bot. Uses 99% of balance per trade, max leverage, 1 concurrent slot.
# Default mode is paper - edit configs/real_tao_solo.json to switch to "real".

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Wipe stale data for clean run.
Remove-Item ".\data.real_tao_solo.sqlite" -ErrorAction SilentlyContinue
Remove-Item ".\.universe_cache.real_tao_solo.json" -ErrorAction SilentlyContinue

$cmd = @"
`$Host.UI.RawUI.WindowTitle = 'TAO solo :: 8095';
`$env:ZFEE_CONFIG_PATH = '$root\configs\real_tao_solo.json';
`$env:ZFEE_DB_PATH = '$root\data.real_tao_solo.sqlite';
`$env:ZFEE_UNIVERSE_CACHE = '$root\.universe_cache.real_tao_solo.json';
`$env:PYTHONIOENCODING = 'utf-8';
`$env:PYTHONUNBUFFERED = '1';
& '$root\.venv\Scripts\python.exe' -m uvicorn backend.app:app --host 127.0.0.1 --port 8095;
"@
Start-Process powershell -ArgumentList @("-NoExit", "-Command", $cmd) -WindowStyle Normal

Write-Host ""
Write-Host "TAO solo bot launched on http://127.0.0.1:8095"
Write-Host ""
Write-Host "Config:"
Write-Host "  Single symbol: TAO_USDT"
Write-Host "  1 concurrent position (no parallel trades)"
Write-Host "  99% margin per trade"
Write-Host "  Max leverage available on TAO (~75x)"
Write-Host "  raw_momentum algo, min_bps=4.0 (was 2.0 - frequency reduced)"
Write-Host ""
Write-Host "Mode is currently PAPER (\$100 sim balance)."
Write-Host "To switch to REAL:"
Write-Host "  1. Set mexc_web.web_uid in configs/real_tao_solo.json"
Write-Host "  2. Change mode from 'paper' to 'real'"
Write-Host "  3. Restart the bot"
