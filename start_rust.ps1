$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:ZFEE_ROOT = $Root

Set-Location (Join-Path $Root "rust-bot")

$CargoArgs = @("run", "--release", "--")
$CargoArgs += $args
cargo @CargoArgs
