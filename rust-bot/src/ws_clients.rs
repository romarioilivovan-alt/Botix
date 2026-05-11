use crate::aggregator::Aggregator;
use crate::state::{AppState, now_ts};
use futures_util::{Sink, SinkExt, StreamExt};
use serde_json::{Value, json};
use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use tokio::sync::{Mutex, RwLock};
use tokio::time::{Duration, interval, sleep};
use tokio_tungstenite::tungstenite::Message;

const BINANCE_FUTURES_WS_BASE: &str = "wss://fstream.binance.com/stream";
const MEXC_WS_ENDPOINT: &str = "wss://contract.mexc.com/edge";

#[derive(Clone)]
pub struct WsControl {
    desired: Arc<RwLock<HashSet<String>>>,
    connected: Arc<AtomicBool>,
}

impl WsControl {
    pub fn new() -> Self {
        Self {
            desired: Arc::new(RwLock::new(HashSet::new())),
            connected: Arc::new(AtomicBool::new(false)),
        }
    }

    pub async fn set_symbols(&self, symbols: impl IntoIterator<Item = String>) {
        let mut desired = self.desired.write().await;
        *desired = symbols
            .into_iter()
            .map(|s| s.to_ascii_uppercase())
            .collect();
    }

    pub async fn symbols(&self) -> HashSet<String> {
        self.desired.read().await.clone()
    }

    pub fn is_connected(&self) -> bool {
        self.connected.load(Ordering::Relaxed)
    }
}

fn parse_levels(v: &Value) -> Vec<[f64; 2]> {
    v.as_array()
        .into_iter()
        .flatten()
        .filter_map(|row| {
            let arr = row.as_array()?;
            let p = arr.first().and_then(parse_num)?;
            let q = arr.get(1).and_then(parse_num)?;
            (p > 0.0 && q > 0.0).then_some([p, q])
        })
        .collect()
}

fn parse_num(v: &Value) -> Option<f64> {
    v.as_f64().or_else(|| v.as_str()?.parse().ok())
}

fn rest_to_symbols(streams: &[String]) -> HashSet<String> {
    streams
        .iter()
        .filter_map(|s| s.split_once('@').map(|(sym, _)| sym.to_ascii_uppercase()))
        .collect()
}

async fn send_binance_sub<S>(
    sink: &mut S,
    syms: &HashSet<String>,
    subscribe: bool,
    req_id: &mut i64,
) where
    S: Sink<Message> + Unpin,
    <S as Sink<Message>>::Error: std::fmt::Display,
{
    if syms.is_empty() {
        return;
    }
    let params: Vec<String> = syms
        .iter()
        .flat_map(|s| {
            let ls = s.to_ascii_lowercase();
            [format!("{ls}@depth20@100ms"), format!("{ls}@trade")]
        })
        .collect();
    *req_id += 1;
    let msg = json!({
        "method": if subscribe { "SUBSCRIBE" } else { "UNSUBSCRIBE" },
        "params": params,
        "id": *req_id,
    });
    let _ = sink.send(Message::Text(msg.to_string().into())).await;
}

pub async fn run_binance_ws(control: WsControl, agg: Arc<Mutex<Aggregator>>, state: Arc<AppState>) {
    let mut backoff = 1.0;
    loop {
        let desired = control.symbols().await;
        let streams: Vec<String> = desired
            .iter()
            .flat_map(|s| {
                let ls = s.to_ascii_lowercase();
                [format!("{ls}@depth20@100ms"), format!("{ls}@trade")]
            })
            .collect();
        let first_chunk = if streams.is_empty() {
            vec!["btcusdt@trade".to_string()]
        } else {
            streams.iter().take(200).cloned().collect()
        };
        let url = format!(
            "{BINANCE_FUTURES_WS_BASE}?streams={}",
            first_chunk.join("/")
        );
        match tokio_tungstenite::connect_async(&url).await {
            Ok((ws, _)) => {
                control.connected.store(true, Ordering::Relaxed);
                state.add_log("info", "Binance WS connected").await;
                backoff = 1.0;
                let (mut sink, mut stream) = ws.split();
                let mut subscribed = rest_to_symbols(&first_chunk);
                let rest = rest_to_symbols(&streams.iter().skip(200).cloned().collect::<Vec<_>>());
                let mut req_id = 0;
                send_binance_sub(&mut sink, &rest, true, &mut req_id).await;
                subscribed.extend(rest);
                let mut tick = interval(Duration::from_secs(5));
                loop {
                    tokio::select! {
                        _ = tick.tick() => {
                            let desired = control.symbols().await;
                            let to_add: HashSet<_> = desired.difference(&subscribed).cloned().collect();
                            let to_remove: HashSet<_> = subscribed.difference(&desired).cloned().collect();
                            send_binance_sub(&mut sink, &to_add, true, &mut req_id).await;
                            send_binance_sub(&mut sink, &to_remove, false, &mut req_id).await;
                            subscribed.extend(to_add);
                            for s in to_remove { subscribed.remove(&s); }
                        }
                        next = stream.next() => {
                            let Some(Ok(msg)) = next else { break; };
                            let Ok(text) = msg.into_text() else { continue; };
                            let Ok(v) = serde_json::from_str::<Value>(&text) else { continue; };
                            handle_binance_msg(&v, &agg).await;
                        }
                    }
                }
            }
            Err(e) => {
                state
                    .add_log("warn", format!("Binance WS reconnect: {e}"))
                    .await;
            }
        }
        control.connected.store(false, Ordering::Relaxed);
        sleep(Duration::from_secs_f64(backoff)).await;
        backoff = (backoff * 1.7).min(10.0);
    }
}

async fn handle_binance_msg(v: &Value, agg: &Arc<Mutex<Aggregator>>) {
    let Some(stream) = v.get("stream").and_then(Value::as_str) else {
        return;
    };
    let data = v.get("data").unwrap_or(v);
    let Some((sym, kind)) = stream.split_once('@') else {
        return;
    };
    let sym_u = sym.to_ascii_uppercase();
    if kind.starts_with("depth") {
        let bids = parse_levels(data.get("b").unwrap_or(&Value::Null));
        let asks = parse_levels(data.get("a").unwrap_or(&Value::Null));
        agg.lock()
            .await
            .on_binance_depth(&sym_u, bids, asks, now_ts());
    } else if kind == "trade" || kind == "aggTrade" {
        let price = data.get("p").and_then(parse_num).unwrap_or(0.0);
        let qty = data.get("q").and_then(parse_num).unwrap_or(0.0);
        let buyer_is_maker = data.get("m").and_then(Value::as_bool).unwrap_or(false);
        let ts = data
            .get("T")
            .and_then(parse_num)
            .map(|v| v / 1000.0)
            .unwrap_or_else(now_ts);
        agg.lock()
            .await
            .on_binance_trade(&sym_u, price, qty, buyer_is_maker, ts);
    }
}

async fn send_mexc_sub<S>(sink: &mut S, sym: &str, subscribe: bool)
where
    S: Sink<Message> + Unpin,
    <S as Sink<Message>>::Error: std::fmt::Display,
{
    let msg = json!({
        "method": if subscribe { "sub.depth.full" } else { "unsub.depth.full" },
        "param": {"symbol": sym, "limit": 20},
        "gzip": false,
    });
    let _ = sink.send(Message::Text(msg.to_string().into())).await;
}

pub async fn run_mexc_ws(control: WsControl, agg: Arc<Mutex<Aggregator>>, state: Arc<AppState>) {
    let mut backoff = 1.0;
    loop {
        match tokio_tungstenite::connect_async(MEXC_WS_ENDPOINT).await {
            Ok((ws, _)) => {
                control.connected.store(true, Ordering::Relaxed);
                state.add_log("info", "MEXC WS connected").await;
                backoff = 1.0;
                let (mut sink, mut stream) = ws.split();
                let mut subscribed = HashSet::new();
                for sym in control.symbols().await {
                    send_mexc_sub(&mut sink, &sym, true).await;
                    subscribed.insert(sym);
                }
                let mut tick = interval(Duration::from_secs(5));
                loop {
                    tokio::select! {
                        _ = tick.tick() => {
                            let desired = control.symbols().await;
                            let to_add: Vec<_> = desired.difference(&subscribed).cloned().collect();
                            let to_remove: Vec<_> = subscribed.difference(&desired).cloned().collect();
                            for sym in to_add {
                                send_mexc_sub(&mut sink, &sym, true).await;
                                subscribed.insert(sym);
                            }
                            for sym in to_remove {
                                send_mexc_sub(&mut sink, &sym, false).await;
                                subscribed.remove(&sym);
                            }
                            let _ = sink.send(Message::Text(json!({"method": "ping"}).to_string().into())).await;
                        }
                        next = stream.next() => {
                            let Some(Ok(msg)) = next else { break; };
                            let Ok(text) = msg.into_text() else { continue; };
                            let Ok(v) = serde_json::from_str::<Value>(&text) else { continue; };
                            handle_mexc_msg(&v, &agg).await;
                        }
                    }
                }
            }
            Err(e) => {
                state
                    .add_log("warn", format!("MEXC WS reconnect: {e}"))
                    .await;
            }
        }
        control.connected.store(false, Ordering::Relaxed);
        sleep(Duration::from_secs_f64(backoff)).await;
        backoff = (backoff * 1.7).min(10.0);
    }
}

async fn handle_mexc_msg(v: &Value, agg: &Arc<Mutex<Aggregator>>) {
    let channel = v.get("channel").and_then(Value::as_str).unwrap_or("");
    if channel != "push.depth" && channel != "push.depth.full" {
        return;
    }
    let payload = v.get("data").unwrap_or(&Value::Null);
    let sym = v
        .get("symbol")
        .or_else(|| payload.get("symbol"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_uppercase();
    if sym.is_empty() {
        return;
    }
    let bids = parse_levels(
        payload
            .get("bids")
            .or_else(|| payload.get("b"))
            .unwrap_or(&Value::Null),
    );
    let asks = parse_levels(
        payload
            .get("asks")
            .or_else(|| payload.get("a"))
            .unwrap_or(&Value::Null),
    );
    agg.lock().await.on_mexc_depth(&sym, bids, asks, now_ts());
}
