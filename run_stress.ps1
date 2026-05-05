# STRESS TEST: 3-5s latency + fees. Compare confluence vs raw_momentum.
# Goal: find which algorithm has REAL edge that survives bad conditions.

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$strategies = @(
    @{ Name = "stress_confluence";   Port = 8100 },
    @{ Name = "stress_raw_momentum"; Port = 8101 }
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
Write-Host "STRESS TEST RUNNING:"
Write-Host "  stress_confluence    multi-signal confirmation (3 indicators must agree)"
Write-Host "  stress_raw_momentum  baseline for comparison"
Write-Host ""
Write-Host "Both with: 4-second entry+exit latency, 1bp taker fees, 50x leverage cap"
Write-Host "  Goal: which algo has edge that survives realistic worst-case conditions"
