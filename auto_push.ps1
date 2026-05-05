# Auto-push paper-mode SQLite snapshots to GitHub.
# Run in a separate PowerShell window — it will loop until you Ctrl+C it.
# Pushes data.sqlite + analysis.md every 30 minutes.

$ErrorActionPreference = "SilentlyContinue"
Set-Location $PSScriptRoot

while ($true) {
    try {
        # Snapshot current data and pre-compute analysis (idempotent).
        & ".\.venv\Scripts\python.exe" analyze.py | Out-File -Encoding utf8 analysis.md

        git add data.sqlite analysis.md
        $stamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
        git commit -m "snapshot $stamp" --allow-empty | Out-Null
        git push origin main 2>&1 | Out-Null
        Write-Host "[$stamp] pushed snapshot"
    } catch {
        Write-Host "push error: $_"
    }
    Start-Sleep -Seconds 1800
}
