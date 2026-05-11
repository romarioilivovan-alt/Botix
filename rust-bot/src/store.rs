use anyhow::{Context, Result};
use serde_json::{Value, json};
use sqlx::sqlite::{SqliteConnectOptions, SqlitePoolOptions};
use sqlx::{Row, SqlitePool};
use std::fs;
use std::path::{Path, PathBuf};

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry REAL NOT NULL,
    exit REAL,
    qty REAL NOT NULL,
    notional REAL NOT NULL,
    margin REAL NOT NULL,
    leverage REAL NOT NULL,
    open_ts REAL NOT NULL,
    close_ts REAL,
    duration_sec REAL,
    pnl_usdt REAL,
    pnl_pct REAL,
    fair_at_open REAL,
    sigma_at_open REAL,
    z_at_open REAL,
    close_reason TEXT,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS ix_trades_symbol ON trades(symbol);

CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    balance REAL NOT NULL,
    equity REAL NOT NULL,
    open_positions INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_equity_ts ON equity(ts);

CREATE TABLE IF NOT EXISTS candidates_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT,
    score REAL,
    z REAL,
    spread_bps REAL,
    fair REAL,
    mexc REAL,
    depth REAL,
    blocked TEXT,
    accepted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_cand_ts ON candidates_log(ts);
"#;

#[derive(Clone)]
pub struct Store {
    pool: SqlitePool,
    managed_positions_path: PathBuf,
}

impl Store {
    pub async fn open(path: PathBuf) -> Result<Self> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).ok();
        }
        let options = SqliteConnectOptions::new()
            .filename(&path)
            .create_if_missing(true);
        let pool = SqlitePoolOptions::new()
            .max_connections(5)
            .connect_with(options)
            .await
            .with_context(|| format!("failed to open sqlite {}", path.display()))?;
        for pragma in [
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA temp_store=MEMORY",
            "PRAGMA busy_timeout=3000",
        ] {
            let _ = sqlx::query(pragma).execute(&pool).await;
        }
        for stmt in SCHEMA.split(';').map(str::trim).filter(|s| !s.is_empty()) {
            sqlx::query(stmt).execute(&pool).await?;
        }
        let _ = sqlx::query("ALTER TABLE trades ADD COLUMN entry_latency_sec REAL")
            .execute(&pool)
            .await;
        Ok(Self {
            managed_positions_path: path
                .parent()
                .unwrap_or(Path::new("."))
                .join("managed_positions.json"),
            pool,
        })
    }

    pub async fn insert_trade(&self, row: &Value) -> Result<()> {
        let extra = row
            .get("extra")
            .cloned()
            .unwrap_or_else(|| json!({}))
            .to_string();
        sqlx::query(
            r#"INSERT INTO trades(
                ts, mode, symbol, side, entry, exit, qty, notional, margin, leverage,
                open_ts, close_ts, duration_sec, pnl_usdt, pnl_pct,
                fair_at_open, sigma_at_open, z_at_open, close_reason, extra, entry_latency_sec
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"#,
        )
        .bind(
            row.get("ts")
                .and_then(Value::as_f64)
                .unwrap_or_else(crate::state::now_ts),
        )
        .bind(row.get("mode").and_then(Value::as_str).unwrap_or("paper"))
        .bind(row.get("symbol").and_then(Value::as_str).unwrap_or(""))
        .bind(row.get("side").and_then(Value::as_str).unwrap_or(""))
        .bind(row.get("entry").and_then(Value::as_f64).unwrap_or(0.0))
        .bind(row.get("exit").and_then(Value::as_f64))
        .bind(row.get("qty").and_then(Value::as_f64).unwrap_or(0.0))
        .bind(row.get("notional").and_then(Value::as_f64).unwrap_or(0.0))
        .bind(row.get("margin").and_then(Value::as_f64).unwrap_or(0.0))
        .bind(row.get("leverage").and_then(Value::as_f64).unwrap_or(1.0))
        .bind(row.get("open_ts").and_then(Value::as_f64).unwrap_or(0.0))
        .bind(row.get("close_ts").and_then(Value::as_f64))
        .bind(row.get("duration_sec").and_then(Value::as_f64))
        .bind(row.get("pnl_usdt").and_then(Value::as_f64))
        .bind(row.get("pnl_pct").and_then(Value::as_f64))
        .bind(row.get("fair_at_open").and_then(Value::as_f64))
        .bind(row.get("sigma_at_open").and_then(Value::as_f64))
        .bind(row.get("z_at_open").and_then(Value::as_f64))
        .bind(row.get("close_reason").and_then(Value::as_str))
        .bind(extra)
        .bind(row.get("entry_latency_sec").and_then(Value::as_f64))
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn insert_equity(
        &self,
        ts: f64,
        mode: &str,
        balance: f64,
        equity: f64,
        open_positions: usize,
    ) -> Result<()> {
        sqlx::query(
            "INSERT INTO equity(ts, mode, balance, equity, open_positions) VALUES (?,?,?,?,?)",
        )
        .bind(ts)
        .bind(mode)
        .bind(balance)
        .bind(equity)
        .bind(open_positions as i64)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn insert_candidate(
        &self,
        ts: f64,
        symbol: &str,
        side: Option<&str>,
        score: f64,
        z: Option<f64>,
        spread_bps: Option<f64>,
        fair: Option<f64>,
        mexc: Option<f64>,
        depth: Option<f64>,
        blocked: Option<&str>,
        accepted: bool,
    ) -> Result<()> {
        sqlx::query(
            r#"INSERT INTO candidates_log(
                ts, symbol, side, score, z, spread_bps, fair, mexc, depth, blocked, accepted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"#,
        )
        .bind(ts)
        .bind(symbol)
        .bind(side)
        .bind(score)
        .bind(z)
        .bind(spread_bps)
        .bind(fair)
        .bind(mexc)
        .bind(depth)
        .bind(blocked)
        .bind(if accepted { 1_i64 } else { 0_i64 })
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn list_trades(&self, limit: i64, mode: Option<&str>) -> Result<Vec<Value>> {
        let rows = if let Some(mode) = mode {
            sqlx::query("SELECT * FROM trades WHERE mode=? ORDER BY id DESC LIMIT ?")
                .bind(mode)
                .bind(limit)
                .fetch_all(&self.pool)
                .await?
        } else {
            sqlx::query("SELECT * FROM trades ORDER BY id DESC LIMIT ?")
                .bind(limit)
                .fetch_all(&self.pool)
                .await?
        };
        Ok(rows
            .into_iter()
            .map(|r| {
                json!({
                    "id": r.try_get::<i64, _>("id").ok(),
                    "ts": r.try_get::<f64, _>("ts").ok(),
                    "mode": r.try_get::<String, _>("mode").ok(),
                    "symbol": r.try_get::<String, _>("symbol").ok(),
                    "side": r.try_get::<String, _>("side").ok(),
                    "entry": r.try_get::<f64, _>("entry").ok(),
                    "exit": r.try_get::<f64, _>("exit").ok(),
                    "qty": r.try_get::<f64, _>("qty").ok(),
                    "notional": r.try_get::<f64, _>("notional").ok(),
                    "margin": r.try_get::<f64, _>("margin").ok(),
                    "leverage": r.try_get::<f64, _>("leverage").ok(),
                    "open_ts": r.try_get::<f64, _>("open_ts").ok(),
                    "close_ts": r.try_get::<f64, _>("close_ts").ok(),
                    "duration_sec": r.try_get::<f64, _>("duration_sec").ok(),
                    "pnl_usdt": r.try_get::<f64, _>("pnl_usdt").ok(),
                    "pnl_pct": r.try_get::<f64, _>("pnl_pct").ok(),
                    "fair_at_open": r.try_get::<f64, _>("fair_at_open").ok(),
                    "sigma_at_open": r.try_get::<f64, _>("sigma_at_open").ok(),
                    "z_at_open": r.try_get::<f64, _>("z_at_open").ok(),
                    "close_reason": r.try_get::<String, _>("close_reason").ok(),
                    "extra": r.try_get::<String, _>("extra").ok(),
                    "entry_latency_sec": r.try_get::<f64, _>("entry_latency_sec").ok(),
                })
            })
            .collect())
    }

    pub async fn stats_summary(&self, mode: Option<&str>) -> Result<Value> {
        let row = if let Some(mode) = mode {
            sqlx::query(
                r#"SELECT
                    COUNT(*) AS n,
                    SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN pnl_usdt <= 0 THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(pnl_usdt), 0) AS total_pnl,
                    COALESCE(AVG(pnl_usdt), 0) AS avg_pnl,
                    COALESCE(AVG(duration_sec), 0) AS avg_duration
                FROM trades WHERE mode=?"#,
            )
            .bind(mode)
            .fetch_one(&self.pool)
            .await?
        } else {
            sqlx::query(
                r#"SELECT
                    COUNT(*) AS n,
                    SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN pnl_usdt <= 0 THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(pnl_usdt), 0) AS total_pnl,
                    COALESCE(AVG(pnl_usdt), 0) AS avg_pnl,
                    COALESCE(AVG(duration_sec), 0) AS avg_duration
                FROM trades"#,
            )
            .fetch_one(&self.pool)
            .await?
        };
        Ok(json!({
            "n": row.try_get::<i64, _>("n").unwrap_or(0),
            "wins": row.try_get::<i64, _>("wins").unwrap_or(0),
            "losses": row.try_get::<i64, _>("losses").unwrap_or(0),
            "total_pnl": row.try_get::<f64, _>("total_pnl").unwrap_or(0.0),
            "avg_pnl": row.try_get::<f64, _>("avg_pnl").unwrap_or(0.0),
            "avg_duration": row.try_get::<f64, _>("avg_duration").unwrap_or(0.0),
        }))
    }

    pub async fn list_equity(&self, limit: i64, mode: Option<&str>) -> Result<Vec<Value>> {
        let rows = if let Some(mode) = mode {
            sqlx::query("SELECT ts, balance, equity, open_positions FROM equity WHERE mode=? ORDER BY id DESC LIMIT ?")
                .bind(mode)
                .bind(limit)
                .fetch_all(&self.pool)
                .await?
        } else {
            sqlx::query(
                "SELECT ts, balance, equity, open_positions FROM equity ORDER BY id DESC LIMIT ?",
            )
            .bind(limit)
            .fetch_all(&self.pool)
            .await?
        };
        let mut out: Vec<_> = rows
            .into_iter()
            .map(|r| {
                json!({
                    "ts": r.try_get::<f64, _>("ts").ok(),
                    "balance": r.try_get::<f64, _>("balance").ok(),
                    "equity": r.try_get::<f64, _>("equity").ok(),
                    "open_positions": r.try_get::<i64, _>("open_positions").ok(),
                })
            })
            .collect();
        out.reverse();
        Ok(out)
    }

    fn load_managed_blob(&self) -> Value {
        fs::read_to_string(&self.managed_positions_path)
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_else(|| json!({}))
    }

    fn save_managed_blob(&self, payload: &Value) -> Result<()> {
        if let Some(parent) = self.managed_positions_path.parent() {
            fs::create_dir_all(parent).ok();
        }
        let tmp = self.managed_positions_path.with_extension("json.tmp");
        fs::write(&tmp, serde_json::to_string_pretty(payload)?)?;
        fs::rename(tmp, &self.managed_positions_path)?;
        Ok(())
    }

    pub async fn upsert_managed_position(
        &self,
        mode: &str,
        symbol: &str,
        payload: &Value,
    ) -> Result<()> {
        let mut root = self.load_managed_blob();
        if !root.is_object() {
            root = json!({});
        }
        let obj = root.as_object_mut().unwrap();
        let bucket = obj.entry(mode.to_string()).or_insert_with(|| json!({}));
        if !bucket.is_object() {
            *bucket = json!({});
        }
        bucket
            .as_object_mut()
            .unwrap()
            .insert(symbol.to_ascii_uppercase(), payload.clone());
        self.save_managed_blob(&root)
    }

    pub async fn delete_managed_position(&self, mode: &str, symbol: &str) -> Result<()> {
        let mut root = self.load_managed_blob();
        if let Some(bucket) = root.get_mut(mode).and_then(Value::as_object_mut) {
            bucket.remove(&symbol.to_ascii_uppercase());
        }
        self.save_managed_blob(&root)
    }
}
