# Night experiment: 10 algorithms running on basket of 8 liquid coins.
# $1000 paper balance per bot, kill switch effectively off (99% threshold),
# 12% margin per slot = each coin has its own balance allocation within the bot.
#
# Algorithms span direction-following, mean-reversion, microstructure, multi-signal
# - giving us 10 different "shots on goal" for finding edge overnight.

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$strategies = @(
    @{ Name = "night_raw_momentum_basic"; Port = 8200 },
    @{ Name = "night_raw_momentum_slow";  Port = 8201 },
    @{ Name = "night_raw_momentum_inv";   Port = 8202 },
    @{ Name = "night_bb_revert_tight";    Port = 8203 },
    @{ Name = "night_bb_revert_loose";    Port = 8204 },
    @{ Name = "night_imbalance";          Port = 8205 },
    @{ Name = "night_book_lean";          Port = 8206 },
    @{ Name = "night_ofi";                Port = 8207 },
    @{ Name = "night_confluence";         Port = 8208 },
    @{ Name = "night_meanrev";            Port = 8209 }
)

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
    Write-Host "[$($s.Name)] http://127.0.0.1:$($s.Port)"
    Start-Sleep -Seconds 2
}

Write-Host ""
Write-Host "10 algorithms running overnight on 8-coin basket"
Write-Host "  Coins: TAO, ARB, AAVE, FARTCOIN, ICP, ETH, BCH, ASTER"
Write-Host "  Balance per bot: \$1000 paper (max leverage, 12% margin per coin slot)"
Write-Host "  Kill switch: OFF (will run until you stop them)"
Write-Host ""
Write-Host "Algorithms:"
Write-Host "  8200 raw_momentum_basic    1s velocity baseline"
Write-Host "  8201 raw_momentum_slow     5s velocity (less noise)"
Write-Host "  8202 raw_momentum_inv      inverted direction (control)"
Write-Host "  8203 bb_revert_tight       MEXC own price, 1.5sigma entry"
Write-Host "  8204 bb_revert_loose       MEXC own price, 2.5sigma entry"
Write-Host "  8205 imbalance             top-5 book imbalance"
Write-Host "  8206 book_lean             simpler book ratio"
Write-Host "  8207 ofi                   Binance order flow imbalance"
Write-Host "  8208 confluence            multi-signal (3 indicators)"
Write-Host "  8209 meanrev               classic z-score MEXC vs Binance"
Write-Host ""
Write-Host "In the morning: .\.venv\Scripts\python.exe compare.py"
