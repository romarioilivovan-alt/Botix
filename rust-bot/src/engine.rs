use crate::aggregator::Aggregator;
use crate::allocator::CapitalAllocator;
use crate::config::{AppConfig, save_config};
use crate::executor::Executor;
use crate::mexc::{MexcClient, UserAccount};
use crate::opportunity::OpportunityEngine;
use crate::state::{AppState, now_ts};
use crate::store::Store;
use crate::universe::UniverseManager;
use crate::ws_clients::{WsControl, run_binance_ws, run_mexc_ws};
use anyhow::Result;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio::task::JoinHandle;
use tokio::time::{Duration, sleep};

#[derive(Clone)]
pub struct Engine {
    pub cfg: Arc<Mutex<AppConfig>>,
    pub state: Arc<AppState>,
    pub store: Store,
    trader: MexcClient,
    universe: Arc<Mutex<UniverseManager>>,
    aggregator: Arc<Mutex<Aggregator>>,
    opportunity: Arc<Mutex<OpportunityEngine>>,
    allocator: Arc<Mutex<CapitalAllocator>>,
    executor: Executor,
    binance_control: WsControl,
    mexc_control: WsControl,
    config_path: PathBuf,
    tasks: Arc<Mutex<Vec<JoinHandle<()>>>>,
}

impl Engine {
    pub async fn new(
        cfg: AppConfig,
        config_path: PathBuf,
        cache_path: PathBuf,
        store: Store,
    ) -> Result<Self> {
        let uid = cfg.mexc_web.web_uid.trim().to_string();
        let mut device_id = cfg.mexc_web.device_id.trim().to_string();
        if !uid.is_empty() && device_id.is_empty() {
            device_id = format!("{:x}", md5::compute(uid.as_bytes()));
        }
        let mut mhash = cfg.mexc_web.mhash.trim().to_string();
        if !uid.is_empty() && !device_id.is_empty() && mhash.is_empty() {
            mhash = format!("{:x}", md5::compute(format!("{uid}{device_id}")));
        }
        let trader = MexcClient::new(UserAccount::new(
            uid,
            device_id,
            mhash,
            cfg.mexc_web.proxy.clone(),
        ))?;
        let cfg_arc = Arc::new(Mutex::new(cfg.clone()));
        let state = Arc::new(AppState::new(&cfg.mode));
        let aggregator = Arc::new(Mutex::new(Aggregator::new(cfg.clone())));
        let opportunity = Arc::new(Mutex::new(OpportunityEngine::new(cfg.clone())));
        let allocator = Arc::new(Mutex::new(CapitalAllocator::new(cfg.clone())));
        let universe = Arc::new(Mutex::new(UniverseManager::new(
            trader.clone(),
            cfg.clone(),
            cache_path,
        )));
        let executor = Executor::new(
            cfg_arc.clone(),
            state.clone(),
            aggregator.clone(),
            allocator.clone(),
            store.clone(),
            trader.clone(),
        );
        Ok(Self {
            cfg: cfg_arc,
            state,
            store,
            trader,
            universe,
            aggregator,
            opportunity,
            allocator,
            executor,
            binance_control: WsControl::new(),
            mexc_control: WsControl::new(),
            config_path,
            tasks: Arc::new(Mutex::new(Vec::new())),
        })
    }

    pub async fn start(&self) -> Result<()> {
        self.configure_universe().await?;
        let binance = self.binance_control.clone();
        let mexc = self.mexc_control.clone();
        let agg = self.aggregator.clone();
        let state = self.state.clone();
        self.tasks.lock().await.push(tokio::spawn(run_binance_ws(
            binance,
            agg.clone(),
            state.clone(),
        )));
        self.tasks
            .lock()
            .await
            .push(tokio::spawn(run_mexc_ws(mexc, agg, state.clone())));

        let engine = self.clone();
        self.tasks
            .lock()
            .await
            .push(tokio::spawn(async move { engine.universe_loop().await }));
        let engine = self.clone();
        self.tasks.lock().await.push(tokio::spawn(
            async move { engine.connectivity_loop().await },
        ));
        let engine = self.clone();
        self.tasks
            .lock()
            .await
            .push(tokio::spawn(async move { engine.scoring_loop().await }));
        let executor = self.executor.clone();
        self.tasks
            .lock()
            .await
            .push(tokio::spawn(async move { executor.loop_forever().await }));

        let cfg = self.cfg.lock().await.clone();
        if !cfg.mexc_web.web_uid.trim().is_empty() {
            let res = self.trader.auth_ping().await;
            let ok = res.get("success").and_then(Value::as_bool).unwrap_or(false);
            let mut st = self.state.write().await;
            st.mexc_auth_ok = Some(ok);
            st.mexc_auth_msg = res
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or(if ok { "ok" } else { "auth failed" })
                .to_string();
        }
        self.state
            .add_log("info", format!("Engine started in mode={}", cfg.mode))
            .await;
        if cfg.autostart {
            self.run().await;
        }
        Ok(())
    }

    pub async fn shutdown(&self) {
        for task in self.tasks.lock().await.drain(..) {
            task.abort();
        }
    }

    pub async fn run(&self) {
        let mut st = self.state.write().await;
        st.engine_running = true;
        st.kill_switch = false;
        drop(st);
        self.executor.init_balance().await;
        self.state.add_log("info", "Engine: RUN").await;
    }

    pub async fn run_stop(&self) {
        self.state.write().await.engine_running = false;
        self.state.add_log("info", "Engine: STOP").await;
    }

    pub async fn kill_all(&self) -> Value {
        self.executor.kill_all().await
    }

    pub async fn real_history_positions(&self, limit: usize) -> Vec<Value> {
        let cfg = self.cfg.lock().await.clone();
        if cfg.mode != "real" {
            return Vec::new();
        }
        self.trader.get_history_positions(None, limit).await
    }

    pub async fn set_mode(&self, mode: &str) -> Value {
        let mode = mode.trim().to_ascii_lowercase();
        if !matches!(mode.as_str(), "paper" | "real" | "logger") {
            return json!({"success": false, "message": "mode must be paper|real|logger"});
        }
        self.run_stop().await;
        let mut cfg = self.cfg.lock().await.clone();
        cfg.mode = mode.clone();
        if let Err(e) = self.replace_config(cfg).await {
            return json!({"success": false, "message": e.to_string()});
        }
        self.state.write().await.engine_mode = mode.clone();
        self.executor.init_balance().await;
        self.state
            .add_log("info", format!("Mode switched to {mode}"))
            .await;
        json!({"success": true})
    }

    pub async fn replace_config(&self, cfg: AppConfig) -> Result<()> {
        let cfg = cfg.normalize();
        save_config(&self.config_path, &cfg)?;
        *self.cfg.lock().await = cfg.clone();
        self.aggregator.lock().await.update_config(cfg.clone());
        self.opportunity.lock().await.update_config(cfg.clone());
        self.allocator.lock().await.update_config(cfg.clone());
        self.universe.lock().await.update_config(cfg);
        Ok(())
    }

    pub async fn refresh_universe(&self) -> Result<Vec<String>> {
        self.configure_universe().await
    }

    pub async fn available_universe(&self) -> Value {
        let u = self.universe.lock().await;
        let selected = self.cfg.lock().await.universe.include_only.clone();
        json!({
            "available": u.available_pool(),
            "selected": selected,
            "working": u.working_set(),
        })
    }

    async fn configure_universe(&self) -> Result<Vec<String>> {
        let working_set = self.universe.lock().await.refresh().await;
        let entries = self.universe.lock().await.entries();
        let mut m2b: HashMap<String, Option<String>> = HashMap::new();
        let mut factors = HashMap::new();
        let mut sizes = HashMap::new();
        for sym in &working_set {
            let entry = entries.get(sym);
            m2b.insert(sym.clone(), entry.and_then(|e| e.binance_symbol.clone()));
            factors.insert(
                sym.clone(),
                entry.map(|e| e.binance_price_factor).unwrap_or(1.0),
            );
            let size = self
                .trader
                .get_contract_detail(sym, Duration::from_secs(60))
                .await
                .and_then(|d| d.get("contractSize").and_then(crate::mexc::num_as_f64))
                .unwrap_or(1.0)
                .max(1e-18);
            sizes.insert(sym.clone(), size);
        }
        self.aggregator
            .lock()
            .await
            .configure_symbols(&m2b, &factors, &sizes);
        self.binance_control
            .set_symbols(m2b.values().filter_map(Clone::clone).collect::<Vec<_>>())
            .await;
        self.mexc_control.set_symbols(working_set.clone()).await;
        {
            let mut st = self.state.write().await;
            st.universe = working_set.clone();
            st.universe_refs = m2b
                .iter()
                .filter_map(|(k, v)| v.clone().map(|vv| (k.clone(), vv)))
                .collect();
        }
        self.state
            .add_log("info", format!("universe -> {} symbols", working_set.len()))
            .await;
        Ok(working_set)
    }

    async fn universe_loop(self) {
        loop {
            let refresh_sec = self.cfg.lock().await.universe.refresh_sec.max(30);
            sleep(Duration::from_secs(refresh_sec)).await;
            let prev = self.state.read().await.universe.clone();
            if let Ok(next) = self.configure_universe().await {
                if prev != next {
                    self.state
                        .add_log("info", format!("universe changed -> {}", next.len()))
                        .await;
                }
            }
        }
    }

    async fn connectivity_loop(self) {
        loop {
            {
                let mut st = self.state.write().await;
                st.binance_ws_ok = self.binance_control.is_connected();
                st.mexc_ws_ok = self.mexc_control.is_connected();
            }
            sleep(Duration::from_secs(1)).await;
        }
    }

    async fn scoring_loop(self) {
        let mut last_emit: HashMap<String, f64> = HashMap::new();
        let mut cleanup_last = 0.0;
        loop {
            let cfg = self.cfg.lock().await.clone();
            let tick = cfg.strategy.paper_tick_sec.max(0.05);
            let engine_running = self.state.read().await.engine_running;
            let kill_switch = self.state.read().await.kill_switch;
            let symbols = self.aggregator.lock().await.symbols();
            let mut stats = HashMap::new();
            for sym in symbols {
                let ov = cfg
                    .symbol_overrides
                    .iter()
                    .find(|o| o.symbol == sym)
                    .cloned();
                if ov.as_ref().is_some_and(|o| !o.enabled) {
                    continue;
                }
                let mut st = self.aggregator.lock().await.compute_stats(&sym);
                if let Some(ov) = ov
                    .as_ref()
                    .filter(|o| o.algorithms.as_ref().is_some_and(|a| !a.is_empty()))
                {
                    self.opportunity
                        .lock()
                        .await
                        .evaluate_multi(&sym, &mut st, ov);
                } else {
                    self.opportunity.lock().await.evaluate(&sym, &mut st);
                }
                stats.insert(sym.clone(), st.clone());
                self.state.write().await.stats.insert(sym, st);
            }
            let ranked = self.opportunity.lock().await.rank(&stats);
            self.state.write().await.candidates = ranked.clone();

            if !engine_running || kill_switch {
                let idle_sleep = if kill_switch { 0.5 } else { tick.max(0.3) };
                sleep(Duration::from_secs_f64(idle_sleep)).await;
                continue;
            }

            let now = now_ts();
            let mut emitted = 0;
            for c in ranked {
                let Some(sym) = c.get("symbol").and_then(Value::as_str) else {
                    continue;
                };
                if c.get("side").and_then(Value::as_str).is_none()
                    || c.get("blocked").is_some_and(|v| !v.is_null())
                    || c.get("score").and_then(Value::as_f64).unwrap_or(0.0) <= 0.0
                {
                    continue;
                }
                if now - last_emit.get(sym).copied().unwrap_or(0.0) < 0.5 {
                    continue;
                }
                let Some(st) = stats.get(sym) else {
                    continue;
                };
                let Some(opp) = self.opportunity.lock().await.make_opportunity(sym, st) else {
                    continue;
                };
                last_emit.insert(sym.to_string(), now);
                if cfg.mode == "logger" {
                    let _ = self
                        .store
                        .insert_candidate(
                            now,
                            sym,
                            Some(&opp.side),
                            opp.score,
                            Some(opp.z),
                            st.spread_bps,
                            st.fair,
                            st.mexc_mid,
                            st.mexc_book_top10_notional,
                            None,
                            true,
                        )
                        .await;
                    self.state
                        .add_log(
                            "info",
                            format!(
                                "[logger] {} {} score={:.2} z={:.2}",
                                sym, opp.side, opp.score, opp.z
                            ),
                        )
                        .await;
                } else {
                    self.executor.on_signal(opp.clone()).await;
                    let _ = self
                        .store
                        .insert_candidate(
                            now,
                            sym,
                            Some(&opp.side),
                            opp.score,
                            Some(opp.z),
                            st.spread_bps,
                            st.fair,
                            st.mexc_mid,
                            st.mexc_book_top10_notional,
                            None,
                            true,
                        )
                        .await;
                }
                emitted += 1;
                if emitted >= 10 {
                    break;
                }
            }
            if now - cleanup_last > 5.0 {
                cleanup_last = now;
                self.aggregator.lock().await.cleanup_old_samples();
            }
            sleep(Duration::from_secs_f64(tick)).await;
        }
    }
}
