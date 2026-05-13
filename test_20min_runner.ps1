$ErrorActionPreference = "Continue"

$CodeDir = "C:\fluflip_work\code"
$Python = "C:\Program Files\Python311\python.exe"
$DurationSec = [int]($env:TEST_DURATION_SEC)
if ($DurationSec -le 0) { $DurationSec = 1200 }

Set-Location $CodeDir

$startEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$startUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss UTC")
Set-Content "$CodeDir\monitor_20_start.txt" "$startEpoch`n$startUtc" -Encoding ASCII
Add-Content "$CodeDir\run_fast_stderr.log" "`n===== 20MIN_RUNNER_START $startUtc epoch=$startEpoch duration=$DurationSec ====="
Add-Content "$CodeDir\safety_guard.log" "`n===== SAFETY_GUARD_START $startUtc epoch=$startEpoch ====="

function Stop-MatchingProcesses {
    param([string[]]$Patterns)
    $procs = Get-CimInstance Win32_Process | Where-Object {
        $cmd = $_.CommandLine
        if (-not $cmd) { return $false }
        foreach ($pat in $Patterns) {
            if ($cmd -like $pat) { return $true }
        }
        return $false
    }
    foreach ($p in $procs) {
        try { Invoke-CimMethod -InputObject $p -MethodName Terminate | Out-Null } catch {}
    }
}

function Get-BotProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -like "*run_fast.py*" -and $_.CommandLine -notlike "*test_20min_runner*"
    }
}

function Flatten-OpenPositions {
    $flattenCode = @'
import os, asyncio, time
os.environ["ZFEE_CONFIG_PATH"] = r"C:\fluflip_work\code\config.json"
from backend.config import load_config
from backend.models import UserAccount
from backend.mexc_trader import MexcTrader

async def main():
    cfg = load_config()
    acc = UserAccount(
        uid=cfg.mexc_web.web_uid.strip(),
        device_id=cfg.mexc_web.device_id.strip(),
        mhash=cfg.mexc_web.mhash.strip(),
        proxy=cfg.mexc_web.proxy,
        order_submit_path=cfg.mexc_web.order_submit_path,
    )
    trader = MexcTrader(acc, proxy=cfg.mexc_web.proxy)
    try:
        positions = await trader.get_positions_raw()
        print(f"[runner_flatten] open_before={len(positions)}", flush=True)
        for pos in positions:
            symbol = pos.get("symbol")
            ptype = int(pos.get("positionType") or 0)
            side = "LONG" if ptype == 1 else "SHORT"
            vol = float(pos.get("holdVol") or pos.get("availableVol") or 0)
            lev = int(float(pos.get("leverage") or 100))
            pid = int(pos.get("positionId") or 0)
            if vol <= 0:
                continue
            t0 = time.perf_counter()
            res = await trader.close_reduce_only(
                symbol,
                side,
                vol,
                lev,
                margin_mode=int(pos.get("openType") or 1),
                position_id=pid or None,
            )
            elapsed = (time.perf_counter() - t0) * 1000.0
            print(
                f"[runner_flatten] close {symbol} {side} vol={vol} "
                f"pid={pid} success={res.get('success')} latency_ms={elapsed:.1f} "
                f"message={res.get('message')}",
                flush=True,
            )
        await asyncio.sleep(1.5)
        after = await trader.get_positions_raw()
        print(f"[runner_flatten] open_after={len(after)}", flush=True)
    finally:
        await trader.close()

asyncio.run(main())
'@
    $flattenCode | & $Python - 1>> "$CodeDir\test_runner_flatten.log" 2>> "$CodeDir\test_runner_flatten_err.log"
}

Stop-MatchingProcesses @("*run_fast.py*", "*position_safety_guard.py*", "*watchdog_run_fast.ps1*")
Start-Sleep -Seconds 2

$guard = Start-Process -FilePath $Python `
    -ArgumentList "position_safety_guard.py" `
    -WorkingDirectory $CodeDir `
    -RedirectStandardOutput "$CodeDir\safety_guard.log" `
    -RedirectStandardError "$CodeDir\safety_guard_err.log" `
    -WindowStyle Hidden `
    -PassThru

$bot = Start-Process -FilePath $Python `
    -ArgumentList "run_fast.py" `
    -WorkingDirectory $CodeDir `
    -RedirectStandardOutput "$CodeDir\run_fast_stdout.log" `
    -RedirectStandardError "$CodeDir\run_fast_stderr.log" `
    -WindowStyle Hidden `
    -PassThru

$deadline = (Get-Date).AddSeconds($DurationSec)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    $bots = @(Get-BotProcesses)
    if ($bots.Count -eq 0) {
        $utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss UTC")
        Add-Content "$CodeDir\run_fast_stderr.log" "===== BOT_RESTART_BY_RUNNER $utc ====="
        $bot = Start-Process -FilePath $Python `
            -ArgumentList "run_fast.py" `
            -WorkingDirectory $CodeDir `
            -RedirectStandardOutput "$CodeDir\run_fast_stdout.log" `
            -RedirectStandardError "$CodeDir\run_fast_stderr.log" `
            -WindowStyle Hidden `
            -PassThru
    } elseif ($bots.Count -gt 1) {
        $keep = $bots | Sort-Object CreationDate | Select-Object -First 1
        foreach ($p in ($bots | Where-Object { $_.ProcessId -ne $keep.ProcessId })) {
            try { Invoke-CimMethod -InputObject $p -MethodName Terminate | Out-Null } catch {}
        }
    }
}

$endUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss UTC")
Add-Content "$CodeDir\run_fast_stderr.log" "===== 20MIN_RUNNER_END $endUtc ====="
Flatten-OpenPositions
Stop-MatchingProcesses @("*run_fast.py*", "*position_safety_guard.py*")
