@echo off
setlocal
cd /d "%~dp0"
set "RUN_DIR=%~dp0runs\hype_solo_v1"
if not exist "%RUN_DIR%" mkdir "%RUN_DIR%"
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).ToString(\"yyyyMMdd_HHmmss\")"') do set "RUNSTAMP=%%i"
set "ZFEE_CONFIG_PATH=%~dp0config.paper_hype_solo_v1.json"
set "ZFEE_DB_PATH=%RUN_DIR%\paper_%RUNSTAMP%.sqlite"
> "%RUN_DIR%\last_paper_db.txt" echo %ZFEE_DB_PATH%
> "%RUN_DIR%\last_config.txt" echo %ZFEE_CONFIG_PATH%
powershell -NoProfile -Command "$p = Get-NetTCPConnection -State Listen -LocalPort 8115 -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess; if ($p) { Stop-Process -Id $p -Force }"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
)
call ".venv\Scripts\activate.bat"
python -m pip install -r requirements.txt
python run.py
