use crate::config::{AppConfig, SymbolOverride, normalize_symbol_name};
use crate::engine::Engine;
use crate::mexc::num_as_f64;
use crate::state::now_ts;
use axum::Router;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{Html, IntoResponse, Json};
use axum::routing::{get, post};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio::time::{Duration, interval};
use tower_http::services::ServeDir;
use tower_http::trace::TraceLayer;

#[derive(Clone)]
pub struct HttpState {
    pub engine: Engine,
    pub frontend_dir: PathBuf,
    exchange_trades_cache: Arc<Mutex<ExchangeTradesCache>>,
}

#[derive(Clone, Debug, Default)]
struct ExchangeTradesCache {
    ts: f64,
    items: Vec<Value>,
}

pub fn router(engine: Engine, frontend_dir: PathBuf) -> Router {
    let state = Arc::new(HttpState {
        engine,
        frontend_dir: frontend_dir.clone(),
        exchange_trades_cache: Arc::new(Mutex::new(ExchangeTradesCache::default())),
    });
    Router::new()
        .route("/", get(index))
        .route("/api/state", get(api_state))
        .route("/api/config", get(api_config_get).post(api_config_patch))
        .route("/api/universe", get(api_universe))
        .route("/api/universe/refresh", post(api_universe_refresh))
        .route("/api/universe/available", get(api_universe_available))
        .route("/api/universe/selection", post(api_universe_selection))
        .route("/api/candidates", get(api_candidates))
        .route("/api/positions", get(api_positions))
        .route("/api/trades", get(api_trades))
        .route("/api/stats", get(api_stats))
        .route("/api/equity", get(api_equity))
        .route("/api/run/start", post(api_run_start))
        .route("/api/run/stop", post(api_run_stop))
        .route("/api/run/kill", post(api_run_kill))
        .route("/api/mode", post(api_mode))
        .route(
            "/api/symbol-overrides",
            get(api_symbol_overrides_get).post(api_symbol_overrides_save),
        )
        .route(
            "/api/symbol-overrides/{symbol}/toggle",
            post(api_symbol_override_toggle),
        )
        .route("/ws", get(ws_endpoint))
        .nest_service("/static", ServeDir::new(frontend_dir))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn index(State(st): State<Arc<HttpState>>) -> impl IntoResponse {
    match tokio::fs::read_to_string(st.frontend_dir.join("index.html")).await {
        Ok(s) => Html(s).into_response(),
        Err(_) => (StatusCode::NOT_FOUND, "frontend/index.html not found").into_response(),
    }
}

async fn api_state(
    State(st): State<Arc<HttpState>>,
    Query(_q): Query<HashMap<String, String>>,
) -> impl IntoResponse {
    Json(dashboard_snapshot(&st).await)
}

async fn api_config_get(State(st): State<Arc<HttpState>>) -> Json<AppConfig> {
    Json(st.engine.cfg.lock().await.clone())
}

async fn api_config_patch(
    State(st): State<Arc<HttpState>>,
    Json(patch): Json<Value>,
) -> impl IntoResponse {
    let current = st.engine.cfg.lock().await.clone();
    let mut value = serde_json::to_value(current).unwrap_or_else(|_| json!({}));
    merge_json(&mut value, &patch);
    match serde_json::from_value::<AppConfig>(value) {
        Ok(cfg) => match st.engine.replace_config(cfg).await {
            Ok(_) => Json(json!({"success": true})).into_response(),
            Err(e) => (
                StatusCode::BAD_REQUEST,
                Json(json!({"success": false, "message": e.to_string()})),
            )
                .into_response(),
        },
        Err(e) => (
            StatusCode::BAD_REQUEST,
            Json(json!({"success": false, "message": e.to_string()})),
        )
            .into_response(),
    }
}

async fn api_universe(State(st): State<Arc<HttpState>>) -> Json<Value> {
    let state = st.engine.state.read().await;
    Json(json!({
        "size": state.universe.len(),
        "symbols": state.universe.clone(),
        "refs": state.universe_refs.clone(),
    }))
}

async fn api_universe_refresh(State(st): State<Arc<HttpState>>) -> impl IntoResponse {
    match st.engine.refresh_universe().await {
        Ok(symbols) => Json(json!({"success": true, "size": symbols.len(), "symbols": symbols}))
            .into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"success": false, "message": e.to_string()})),
        )
            .into_response(),
    }
}

async fn api_universe_available(State(st): State<Arc<HttpState>>) -> Json<Value> {
    Json(st.engine.available_universe().await)
}

async fn api_universe_selection(
    State(st): State<Arc<HttpState>>,
    Json(payload): Json<Value>,
) -> impl IntoResponse {
    let raw = payload
        .get("symbols")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let cleaned: Vec<String> = raw
        .iter()
        .filter_map(Value::as_str)
        .map(normalize_symbol_name)
        .filter(|s| !s.is_empty())
        .collect();
    let mut cfg = st.engine.cfg.lock().await.clone();
    cfg.universe.include_only = cleaned.clone();
    if let Err(e) = st.engine.replace_config(cfg).await {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"success": false, "message": e.to_string()})),
        )
            .into_response();
    }
    match st.engine.refresh_universe().await {
        Ok(_) => Json(json!({"success": true, "selected": cleaned})).into_response(),
        Err(e) => (
            StatusCode::BAD_REQUEST,
            Json(json!({"success": false, "message": e.to_string()})),
        )
            .into_response(),
    }
}

async fn api_candidates(State(st): State<Arc<HttpState>>) -> Json<Value> {
    Json(json!({"items": st.engine.state.read().await.candidates.clone()}))
}

async fn api_positions(State(st): State<Arc<HttpState>>) -> Json<Value> {
    let snap = st.engine.state.snapshot().await;
    Json(json!({"items": snap.get("positions").cloned().unwrap_or_else(|| json!([]))}))
}

async fn api_trades(
    State(st): State<Arc<HttpState>>,
    Query(q): Query<HashMap<String, String>>,
) -> Json<Value> {
    let limit = q
        .get("limit")
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(200);
    let mode = q.get("mode").filter(|s| !s.is_empty()).map(String::as_str);
    let rows = st
        .engine
        .store
        .list_trades(limit, mode)
        .await
        .unwrap_or_default();
    let mut items: Vec<_> = rows.into_iter().rev().map(normalize_trade_row).collect();
    let current_mode = st.engine.cfg.lock().await.mode.clone();
    let include_exchange = mode
        .map(|m| m == "real")
        .unwrap_or_else(|| current_mode == "real");
    if include_exchange {
        items.extend(cached_exchange_trades(&st, limit.max(50) as usize).await);
    }
    let items = merge_trade_items(items, limit.max(0) as usize);
    Json(json!({"items": items}))
}

async fn api_stats(
    State(st): State<Arc<HttpState>>,
    Query(q): Query<HashMap<String, String>>,
) -> Json<Value> {
    let mode = q.get("mode").filter(|s| !s.is_empty()).map(String::as_str);
    Json(
        st.engine
            .store
            .stats_summary(mode)
            .await
            .unwrap_or_else(|_| json!({})),
    )
}

async fn api_equity(
    State(st): State<Arc<HttpState>>,
    Query(q): Query<HashMap<String, String>>,
) -> Json<Value> {
    let limit = q
        .get("limit")
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(500);
    let mode = q.get("mode").filter(|s| !s.is_empty()).map(String::as_str);
    Json(json!({"items": st.engine.store.list_equity(limit, mode).await.unwrap_or_default()}))
}

async fn api_run_start(State(st): State<Arc<HttpState>>) -> Json<Value> {
    st.engine.run().await;
    Json(json!({"success": true}))
}

async fn api_run_stop(State(st): State<Arc<HttpState>>) -> Json<Value> {
    st.engine.run_stop().await;
    Json(json!({"success": true}))
}

async fn api_run_kill(State(st): State<Arc<HttpState>>) -> Json<Value> {
    Json(st.engine.kill_all().await)
}

async fn api_mode(State(st): State<Arc<HttpState>>, Json(payload): Json<Value>) -> Json<Value> {
    let mode = payload.get("mode").and_then(Value::as_str).unwrap_or("");
    Json(st.engine.set_mode(mode).await)
}

async fn api_symbol_overrides_get(State(st): State<Arc<HttpState>>) -> Json<Value> {
    Json(json!({"items": st.engine.cfg.lock().await.symbol_overrides.clone()}))
}

async fn api_symbol_overrides_save(
    State(st): State<Arc<HttpState>>,
    Json(payload): Json<Value>,
) -> impl IntoResponse {
    let raw = payload
        .get("items")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut parsed = Vec::new();
    for item in raw {
        let Ok(mut ov) = serde_json::from_value::<SymbolOverride>(item) else {
            continue;
        };
        ov.symbol = normalize_symbol_name(&ov.symbol);
        if !ov.symbol.is_empty() {
            parsed.push(ov);
        }
    }
    let mut cfg = st.engine.cfg.lock().await.clone();
    cfg.symbol_overrides = parsed.clone();
    match st.engine.replace_config(cfg).await {
        Ok(_) => Json(json!({"success": true, "count": parsed.len()})).into_response(),
        Err(e) => (
            StatusCode::BAD_REQUEST,
            Json(json!({"success": false, "message": e.to_string()})),
        )
            .into_response(),
    }
}

async fn api_symbol_override_toggle(
    State(st): State<Arc<HttpState>>,
    Path(symbol): Path<String>,
) -> impl IntoResponse {
    let symbol = normalize_symbol_name(&symbol);
    let mut cfg = st.engine.cfg.lock().await.clone();
    if let Some(ov) = cfg.symbol_overrides.iter_mut().find(|o| o.symbol == symbol) {
        ov.enabled = !ov.enabled;
    } else {
        let mut ov = SymbolOverride::default();
        ov.symbol = symbol.clone();
        ov.enabled = false;
        cfg.symbol_overrides.push(ov);
    }
    let enabled = cfg
        .symbol_overrides
        .iter()
        .find(|o| o.symbol == symbol)
        .map(|o| o.enabled)
        .unwrap_or(false);
    match st.engine.replace_config(cfg).await {
        Ok(_) => {
            Json(json!({"success": true, "symbol": symbol, "enabled": enabled})).into_response()
        }
        Err(e) => (
            StatusCode::BAD_REQUEST,
            Json(json!({"success": false, "message": e.to_string()})),
        )
            .into_response(),
    }
}

async fn ws_endpoint(State(st): State<Arc<HttpState>>, ws: WebSocketUpgrade) -> impl IntoResponse {
    ws.on_upgrade(move |socket| ws_loop(st, socket))
}

async fn ws_loop(st: Arc<HttpState>, socket: WebSocket) {
    let (mut sender, mut receiver) = socket.split();
    let snap = dashboard_snapshot(&st).await;
    if sender
        .send(Message::Text(
            json!({"type": "snapshot", "data": snap}).to_string().into(),
        ))
        .await
        .is_err()
    {
        return;
    }
    let mut tick = interval(Duration::from_millis(500));
    loop {
        tokio::select! {
            _ = tick.tick() => {
                let snap = dashboard_snapshot(&st).await;
                if sender.send(Message::Text(json!({"type": "snapshot", "data": snap}).to_string().into())).await.is_err() {
                    break;
                }
            }
            msg = receiver.next() => {
                match msg {
                    Some(Ok(Message::Close(_))) | None => break,
                    _ => {}
                }
            }
        }
    }
}

async fn dashboard_snapshot(st: &Arc<HttpState>) -> Value {
    let mut snap = st.engine.state.snapshot().await;
    let mode = snap
        .get("engine")
        .and_then(|e| e.get("mode"))
        .and_then(Value::as_str)
        .map(str::to_string);
    let mut items = st
        .engine
        .store
        .list_trades(200, mode.as_deref())
        .await
        .unwrap_or_default()
        .into_iter()
        .rev()
        .map(normalize_trade_row)
        .collect::<Vec<_>>();
    if mode.as_deref() == Some("real") {
        items.extend(cached_exchange_trades(st, 50).await);
    }
    snap["recent_trades"] = Value::Array(merge_trade_items(items, 50));
    snap
}

async fn cached_exchange_trades(st: &Arc<HttpState>, limit: usize) -> Vec<Value> {
    let now = now_ts();
    {
        let cache = st.exchange_trades_cache.lock().await;
        if now - cache.ts < 5.0 {
            return cache.items.clone();
        }
    }
    let items = st
        .engine
        .real_history_positions(limit)
        .await
        .into_iter()
        .filter_map(normalize_exchange_position)
        .collect::<Vec<_>>();
    let mut cache = st.exchange_trades_cache.lock().await;
    cache.ts = now;
    cache.items = items.clone();
    items
}

fn merge_trade_items(mut items: Vec<Value>, limit: usize) -> Vec<Value> {
    items.sort_by(|a, b| {
        b.get("ts")
            .and_then(Value::as_f64)
            .unwrap_or(0.0)
            .total_cmp(&a.get("ts").and_then(Value::as_f64).unwrap_or(0.0))
    });
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for item in items {
        if seen.insert(trade_key(&item)) {
            out.push(item);
        }
        if out.len() >= limit {
            break;
        }
    }
    out
}

fn trade_key(item: &Value) -> String {
    if let Some(id) = item.get("source_id").and_then(Value::as_i64) {
        return format!("exchange:{id}");
    }
    let ts = item
        .get("ts")
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
        .round();
    let entry = item.get("entry").and_then(Value::as_f64).unwrap_or(0.0);
    let exit = item.get("exit").and_then(Value::as_f64).unwrap_or(0.0);
    format!(
        "{}|{}|{entry:.10}|{exit:.10}|{ts:.0}",
        item.get("symbol").and_then(Value::as_str).unwrap_or(""),
        item.get("side").and_then(Value::as_str).unwrap_or("")
    )
}

fn merge_json(dst: &mut Value, patch: &Value) {
    match (dst, patch) {
        (Value::Object(dst), Value::Object(src)) => {
            for (k, v) in src {
                merge_json(dst.entry(k.clone()).or_insert(Value::Null), v);
            }
        }
        (dst, src) => *dst = src.clone(),
    }
}

fn normalize_trade_row(row: Value) -> Value {
    let extra = row
        .get("extra")
        .and_then(Value::as_str)
        .and_then(|s| serde_json::from_str::<Value>(s).ok())
        .unwrap_or_else(|| json!({}));
    json!({
        "ts": row.get("close_ts").or_else(|| row.get("ts")).and_then(Value::as_f64).unwrap_or(0.0),
        "symbol": row.get("symbol").cloned().unwrap_or(Value::Null),
        "side": row.get("side").cloned().unwrap_or(Value::Null),
        "entry": row.get("entry").cloned().unwrap_or(Value::Null),
        "exit": row.get("exit").cloned().unwrap_or(Value::Null),
        "pnl": row.get("pnl_usdt").cloned().unwrap_or(Value::Null),
        "pnl_pct": row.get("pnl_pct").cloned().unwrap_or(Value::Null),
        "reason": row.get("close_reason").cloned().unwrap_or(Value::Null),
        "duration": row.get("duration_sec").cloned().unwrap_or(Value::Null),
        "entry_latency_ms": extra.get("entry_latency_ms").cloned().unwrap_or(Value::Null),
        "exit_latency_ms": extra.get("exit_latency_ms").cloned().unwrap_or(Value::Null),
        "entry_algo": extra.get("entry_algo").cloned().unwrap_or(Value::Null),
        "entry_score": extra.get("entry_score").cloned().unwrap_or(Value::Null),
        "price_source": extra.get("price_source").cloned().unwrap_or(Value::Null),
    })
}

fn normalize_exchange_position(row: Value) -> Option<Value> {
    let symbol = row.get("symbol")?.as_str()?.to_string();
    let side = match row
        .get("positionType")
        .and_then(num_as_f64)
        .unwrap_or_default() as i64
    {
        1 => "LONG",
        2 => "SHORT",
        _ => return None,
    };
    let open_ts = time_sec(first_f64(
        &row,
        &["createTime", "openTime", "openTs", "holdTime"],
    ));
    let close_ts = time_sec(first_f64(
        &row,
        &["updateTime", "closeTime", "closeTs", "finishTime"],
    ))
    .or(open_ts)?;
    let entry = first_f64(
        &row,
        &[
            "openAvgPrice",
            "newOpenAvgPrice",
            "holdAvgPrice",
            "holdAvgPriceFullyScale",
        ],
    )?;
    let exit = first_f64(&row, &["closeAvgPrice", "newCloseAvgPrice"])?;
    let pnl = first_f64(
        &row,
        &["closeProfitLoss", "realised", "realizedPnl", "profit"],
    )
    .unwrap_or(0.0);
    let pnl_pct = first_f64(&row, &["profitRatio", "roe"]).map(|v| v * 100.0);
    let duration = open_ts.map(|open| (close_ts - open).max(0.0));
    Some(json!({
        "ts": close_ts,
        "source": "mexc_history",
        "source_id": row.get("positionId").and_then(Value::as_i64),
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "exit": exit,
        "qty": first_f64(&row, &["closeVol", "holdVol"]),
        "leverage": first_f64(&row, &["leverage"]),
        "pnl": pnl,
        "pnl_usdt": pnl,
        "pnl_pct": pnl_pct,
        "reason": row.get("positionShowStatus").and_then(Value::as_str).unwrap_or("exchange_history"),
        "duration": duration,
        "duration_sec": duration,
        "price_source": "mexc_history",
    }))
}

fn first_f64(row: &Value, keys: &[&str]) -> Option<f64> {
    keys.iter()
        .find_map(|key| row.get(*key).and_then(num_as_f64))
}

fn time_sec(value: Option<f64>) -> Option<f64> {
    value.map(|v| if v > 10_000_000_000.0 { v / 1000.0 } else { v })
}
