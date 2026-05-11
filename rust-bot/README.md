# Rust 0fee Scanner

This is a Rust rewrite of the Python backend. It keeps the existing dashboard
contract:

- `config.json`
- `data.sqlite`
- `frontend/`
- `/api/*`
- `/ws`

Run from the project root:

```powershell
.\start_rust.ps1
```

Or directly:

```powershell
cd rust-bot
cargo run --release -- --root ..
```

Useful overrides:

```powershell
$env:ZFEE_CONFIG_PATH = "C:\path\to\config.json"
$env:ZFEE_DB_PATH = "C:\path\to\data.sqlite"
$env:ZFEE_UNIVERSE_CACHE = "C:\path\to\.universe_cache.rust.json"
```

The Rust server binds to `host` and `port` from `config.json`, serves the
existing frontend, subscribes to Binance/MEXC public websocket feeds, scores
candidates, supports logger/paper modes, writes SQLite trades/equity, and has a
web-private MEXC real-mode path matching the Python side codes.
