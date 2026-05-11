# Live VPS Setup

## Chto nuzhno na novom Windows VPS

- Python 3.11+
- internet access do `www.mexc.com`, `contract.mexc.com`, `stream.binance.com`
- prava na zapusk PowerShell skriptov

## Python biblioteki

Ustanavlivayutsya iz `requirements.txt`:

- `fastapi>=0.115.6`
- `uvicorn>=0.30.6`
- `websockets>=13.0`
- `aiohttp>=3.10.11,<4.0`
- `aiohttp-socks>=0.9.1`
- `aiosqlite>=0.20.0`
- `python-dotenv>=1.0.1`

## Bystryy start

1. Raspakovat proekt na VPS.
2. Otkryt PowerShell ot Administrator.
3. V papke proekta zapustit:

```powershell
.\vps_setup.ps1
.\vps_run.ps1
```

## Chto podnimetsya po umolchaniyu

- config: `config.real_lineA_contract_v2.json`
- port: `8086`
- run db: `runs\lineA_real_v2\real_YYYYMMDD_HHMMSS.sqlite`

## Pered Start v UI

1. Otkryt `http://VPS_IP:8086`
2. Vstavit svezhiy `MEXC Web UID`
3. Ubeditsya, chto `Auth: OK`
4. Tolko potom нажимать `Start`

## Vazhno

- Dlya pervogo live smoke zapuska podnimay tolko `lineA`
- Esli `Auth: fail`, ne startuy torgovlyu
- Если нужен `lineB`, запускай его отдельной копией процесса на `8087`
