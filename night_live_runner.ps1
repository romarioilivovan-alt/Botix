$ErrorActionPreference = "Continue"
$CodeDir = "C:\fluflip_work\code"
$Python = "C:\Program Files\Python311\python.exe"
$RunnerLog = "$CodeDir\overnight_runner.log"
Set-Location $CodeDir
function Log($msg) { Add-Content -Path $RunnerLog -Value "$(Get-Date -Format o) $msg" }
function Stop-MatchingProcesses { param([string[]]$Patterns) $procs = Get-CimInstance Win32_Process | Where-Object { $cmd=$_.CommandLine; if(-not $cmd){return $false}; foreach($pat in $Patterns){ if($cmd -like $pat){return $true} }; return $false }; foreach($p in $procs){ try{ Invoke-CimMethod -InputObject $p -MethodName Terminate | Out-Null }catch{} } }
function Get-ProcLike($pattern) { @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like $pattern }) }
Log "OVERNIGHT_START BCH_4000_OTHERS_150_CRYPTO_STOCKS leverage=100"
Stop-MatchingProcesses @("*run_fast.py*", "*position_safety_guard.py*", "*watchdog_run_fast.ps1*", "*test_10min_runner.ps1*", "*test_20min_runner.ps1*")
Start-Sleep -Seconds 2
while ($true) {
  try {
    $guard = Get-ProcLike "*position_safety_guard.py*"
    if ($guard.Count -eq 0) {
      Log "starting safety_guard"
      $env:SAFETY_MAX_HOLD_SEC = "20"
      $env:SAFETY_POLL_SEC = "1"
      Start-Process -FilePath $Python -ArgumentList "position_safety_guard.py" -WorkingDirectory $CodeDir -RedirectStandardOutput "$CodeDir\overnight_safety_guard.log" -RedirectStandardError "$CodeDir\overnight_safety_guard_err.log" -WindowStyle Hidden | Out-Null
    }
    $bots = Get-ProcLike "*run_fast.py*"
    if ($bots.Count -eq 0) {
      Log "starting run_fast.py"
      Start-Process -FilePath $Python -ArgumentList "run_fast.py" -WorkingDirectory $CodeDir -RedirectStandardOutput "$CodeDir\overnight_run_fast_stdout.log" -RedirectStandardError "$CodeDir\overnight_run_fast_stderr.log" -WindowStyle Hidden | Out-Null
    } elseif ($bots.Count -gt 1) {
      $keep = $bots | Sort-Object CreationDate | Select-Object -First 1
      foreach ($p in ($bots | Where-Object { $_.ProcessId -ne $keep.ProcessId })) { Log "terminating duplicate run_fast pid=$($p.ProcessId)"; try { Invoke-CimMethod -InputObject $p -MethodName Terminate | Out-Null } catch {} }
    }
  } catch { Log "runner error: $($_.Exception.Message)" }
  Start-Sleep -Seconds 30
}
