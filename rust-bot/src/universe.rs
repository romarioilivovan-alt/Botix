use crate::config::{AppConfig, normalize_symbol_name};
use crate::mexc::MexcClient;
use crate::state::now_ts;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::PathBuf;

const BINANCE_FUTURES_INFO: &str = "https://fapi.binance.com/fapi/v1/exchangeInfo";

pub fn to_binance(mexc: &str) -> String {
    mexc.replace('_', "").to_ascii_uppercase()
}

fn price_factor_for_variant(mexc: &str, binance_symbol: &str) -> f64 {
    let base = to_binance(mexc);
    if binance_symbol.is_empty() || binance_symbol == base {
        return 1.0;
    }
    if let Some(prefix) = binance_symbol.strip_suffix(&base) {
        if let Ok(mult) = prefix.parse::<f64>() {
            if mult > 0.0 {
                return 1.0 / mult;
            }
        }
    }
    1.0
}

pub fn resolve_binance_symbol(
    mexc: &str,
    binance_set: &HashSet<String>,
    alias_map: &HashMap<String, String>,
) -> (Option<String>, f64) {
    let mexc_u = mexc.to_ascii_uppercase();
    let base = to_binance(&mexc_u);
    if let Some(alias) = alias_map.get(&mexc_u).map(|s| s.to_ascii_uppercase()) {
        if binance_set.contains(&alias) {
            let factor = price_factor_for_variant(&mexc_u, &alias);
            return (Some(alias), factor);
        }
    }
    if binance_set.contains(&base) {
        return (Some(base), 1.0);
    }
    let prefixed = format!("1000{base}");
    if binance_set.contains(&prefixed) {
        return (Some(prefixed), 0.001);
    }
    (None, 1.0)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UniverseEntry {
    pub mexc_symbol: String,
    pub binance_symbol: Option<String>,
    pub has_ref: bool,
    pub binance_price_factor: f64,
}

#[derive(Debug)]
pub struct UniverseManager {
    cfg: AppConfig,
    trader: MexcClient,
    cache_path: PathBuf,
    binance_set: HashSet<String>,
    binance_set_ts: f64,
    entries: HashMap<String, UniverseEntry>,
    client: reqwest::Client,
}

impl UniverseManager {
    pub fn new(trader: MexcClient, cfg: AppConfig, cache_path: PathBuf) -> Self {
        Self {
            cfg,
            trader,
            cache_path,
            binance_set: HashSet::new(),
            binance_set_ts: 0.0,
            entries: HashMap::new(),
            client: reqwest::Client::new(),
        }
    }

    pub fn update_config(&mut self, cfg: AppConfig) {
        self.cfg = cfg;
    }

    pub fn entries(&self) -> HashMap<String, UniverseEntry> {
        self.entries.clone()
    }

    pub fn working_set(&self) -> Vec<String> {
        let excl: HashSet<String> = self
            .cfg
            .universe
            .exclude
            .iter()
            .map(|s| s.to_ascii_uppercase())
            .collect();
        let only: HashSet<String> = self
            .cfg
            .universe
            .include_only
            .iter()
            .map(|s| s.to_ascii_uppercase())
            .collect();
        let mut out = Vec::new();
        for (sym, entry) in &self.entries {
            if excl.contains(&sym.to_ascii_uppercase()) {
                continue;
            }
            if !only.is_empty() && !only.contains(&sym.to_ascii_uppercase()) {
                continue;
            }
            if self.cfg.universe.require_binance_ref && !entry.has_ref {
                continue;
            }
            out.push(sym.clone());
            if out.len() >= self.cfg.universe.max_symbols {
                break;
            }
        }
        out
    }

    pub fn available_pool(&self) -> Vec<String> {
        self.entries
            .iter()
            .filter_map(|(sym, entry)| {
                (!self.cfg.universe.require_binance_ref || entry.has_ref).then_some(sym.clone())
            })
            .collect()
    }

    pub async fn refresh(&mut self) -> Vec<String> {
        let zero_fee = self.safe_zero_fee_list().await;
        let binance_set = self.safe_binance_set().await;
        let alias_map = self.cfg.binance_symbol_overrides.clone();
        let mut entries = HashMap::new();

        for raw in self
            .cfg
            .universe
            .force_include_symbols
            .iter()
            .chain(zero_fee.iter())
        {
            let sym = normalize_symbol_name(raw);
            if sym.is_empty() || entries.contains_key(&sym) {
                continue;
            }
            let (bsym, factor) = resolve_binance_symbol(&sym, &binance_set, &alias_map);
            entries.insert(
                sym.clone(),
                UniverseEntry {
                    mexc_symbol: sym,
                    has_ref: bsym.is_some(),
                    binance_symbol: bsym,
                    binance_price_factor: factor,
                },
            );
        }
        self.entries = entries;
        self.save_cache();
        self.working_set()
    }

    async fn safe_zero_fee_list(&self) -> Vec<String> {
        let live = self.trader.list_zero_fee_symbols().await;
        if !live.is_empty() {
            live
        } else {
            self.load_cache_symbols()
        }
    }

    async fn safe_binance_set(&mut self) -> HashSet<String> {
        let now = now_ts();
        if !self.binance_set.is_empty() && now - self.binance_set_ts < 600.0 {
            return self.binance_set.clone();
        }
        let result = async {
            let data: serde_json::Value = self
                .client
                .get(BINANCE_FUTURES_INFO)
                .send()
                .await?
                .json()
                .await?;
            Ok::<_, anyhow::Error>(data)
        }
        .await;
        let Ok(data) = result else {
            return self.binance_set.clone();
        };
        let mut out = HashSet::new();
        for item in data
            .get("symbols")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default()
        {
            let sym = item
                .get("symbol")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_ascii_uppercase();
            let quote = item
                .get("quoteAsset")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_ascii_uppercase();
            let contract_type = item
                .get("contractType")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_ascii_uppercase();
            if quote != "USDT" {
                continue;
            }
            if !contract_type.is_empty()
                && !matches!(contract_type.as_str(), "PERPETUAL" | "TRADIFI_PERPETUAL")
            {
                continue;
            }
            if !sym.is_empty() {
                out.insert(sym);
            }
        }
        if !out.is_empty() {
            self.binance_set = out;
            self.binance_set_ts = now;
        }
        self.binance_set.clone()
    }

    fn save_cache(&self) {
        let data = json!({
            "ts": now_ts(),
            "symbols": self.entries.keys().cloned().collect::<Vec<_>>(),
            "with_ref": self.entries.iter().filter_map(|(s, e)| e.has_ref.then_some(s.clone())).collect::<Vec<_>>(),
        });
        if let Some(parent) = self.cache_path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let _ = fs::write(&self.cache_path, data.to_string());
    }

    fn load_cache_symbols(&self) -> Vec<String> {
        let Ok(raw) = fs::read_to_string(&self.cache_path) else {
            return Vec::new();
        };
        let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) else {
            return Vec::new();
        };
        v.get("symbols")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|s| s.as_str().map(ToString::to_string))
                    .collect()
            })
            .unwrap_or_default()
    }
}
