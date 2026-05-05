# Bollinger reversion (mean-revert on MEXC own price) - 8 tokens
# Different strategy class than direction-following: classical textbook mean revert.

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$strategies = @(
    @{ Name = "bb_tao";      Port = 8120 },
    @{ Name = "bb_arb";      Port = 8121 },
    @{ Name = "bb_aave";     Port = 8122 },
    @{ Name = "bb_fartcoin"; Port = 8123 },
    @{ Name = "bb_icp";      Port = 8124 },
    @{ Name = "bb_eth";      Port = 8125 },
    @{ Name = "bb_btc";      Port = 8126 },
    @{ Name = "bb_bch";      Port = 8127 }
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
Write-Host "8 bots running bb_revert (Bollinger band mean reversion on MEXC own price)"
Write-Host "  Algorithm: short when MEXC > 60s mean + 2sigma, long when below -2sigma"
Write-Host "  Independent of Binance lag - pure single-venue microstructure"
