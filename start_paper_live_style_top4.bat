@echo off
setlocal
cd /d "%~dp0"
set "ZFEE_CONFIG_PATH=%~dp0config.paper_live_style_top4.json"
set "ZFEE_DB_PATH=%~dp0data.paper_live_style_top4_iocfix.sqlite"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
)
call ".venv\Scripts\activate.bat"
python -m pip install -r requirements.txt
python run.py
