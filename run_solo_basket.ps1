# Per-symbol experiment: 8 top performers each in their own bot.
# Same algorithm (raw_momentum with multi-timeframe filter), same balance ($100), same leverage.
# Direct comparison of per-token edge.

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$strategies = @(
    @{ Name = "solo_tao";      Port = 8110 },
    @{ Name = "solo_arb";      Port = 8111 },
    @{ Name = "solo_aster";    Port = 8112 },
    @{ Name = "solo_ena";      Port = 8113 },
    @{ Name = "solo_aave";     Port = 8114 },
    @{ Name = "solo_fartcoin"; Port = 8115 },
    @{ Name = "solo_etc";      Port = 8116 },
    @{ Name = "solo_icp";      Port = 8117 }
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
Write-Host "8 per-symbol bots running raw_momentum with multi-timeframe filter"
Write-Host "  TAO=8110  ARB=8111  ASTER=8112  ENA=8113"
Write-Host "  AAVE=8114 FARTCOIN=8115 ETC=8116 ICP=8117"
Write-Host ""
Write-Host "Each: 1 position, 99% margin, max leverage. \$100 paper start."
Write-Host ""
Write-Host "Compare with: .\.venv\Scripts\python.exe compare.py"
