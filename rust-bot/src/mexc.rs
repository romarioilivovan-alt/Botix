use anyhow::Result;
use rand::seq::SliceRandom;
use rust_decimal::Decimal;
use rust_decimal::prelude::ToPrimitive;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::str::FromStr;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::Mutex;

#[derive(Debug, Clone)]
pub struct UserAccount {
    pub uid: String,
    pub device_id: String,
    pub mhash: String,
    pub chash: String,
    pub proxy: Option<String>,
}

impl UserAccount {
    pub fn new(uid: String, device_id: String, mhash: String, proxy: Option<String>) -> Self {
        Self {
            uid,
            device_id,
            mhash,
            chash: "d6c64d28e362f314071b3f9d78ff7494d9cd7177ae0465e772d1840e9f7905d8".to_string(),
            proxy,
        }
    }
}

#[derive(Debug, Clone)]
struct Cached<T> {
    value: T,
    ts: Instant,
}

#[derive(Clone, Debug)]
pub struct MexcClient {
    account: UserAccount,
    public_client: reqwest::Client,
    private_client: reqwest::Client,
    contract_cache: std::sync::Arc<Mutex<HashMap<String, Cached<Value>>>>,
    ticker_cache: std::sync::Arc<Mutex<HashMap<String, Cached<Value>>>>,
}

impl MexcClient {
    pub fn new(account: UserAccount) -> Result<Self> {
        let mut builder = reqwest::Client::builder()
            .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            .timeout(Duration::from_secs(12));
        if let Some(proxy) = account.proxy.as_deref().filter(|p| p.starts_with("http")) {
            builder = builder.proxy(reqwest::Proxy::all(proxy)?);
        }
        let public_client = builder.build()?;
        let mut private_builder = reqwest::Client::builder()
            .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            .timeout(Duration::from_secs(12));
        if let Some(proxy) = account.proxy.as_deref().filter(|p| p.starts_with("http")) {
            private_builder = private_builder.proxy(reqwest::Proxy::all(proxy)?);
        }
        Ok(Self {
            account,
            public_client,
            private_client: private_builder.build()?,
            contract_cache: std::sync::Arc::new(Mutex::new(HashMap::new())),
            ticker_cache: std::sync::Arc::new(Mutex::new(HashMap::new())),
        })
    }

    fn now_ms() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0)
    }

    fn sign(&self, method: &str, endpoint: &str, body: Option<&Value>) -> (i64, String) {
        let timestamp = Self::now_ms();
        let first = format!(
            "{:x}",
            md5::compute(format!("{}{}", self.account.uid, timestamp))
        );
        let first_tail = first.get(7..).unwrap_or("");
        let body_string = if matches!(method, "POST" | "PUT") {
            body.map(|v| serde_json::to_string(v).unwrap_or_default())
                .unwrap_or_default()
        } else {
            String::new()
        };
        let final_input = format!("{timestamp}{body_string}{first_tail}");
        let signature = format!("{:x}", md5::compute(final_input));
        let _ = endpoint;
        (timestamp, signature)
    }

    pub async fn market_request(&self, path: &str) -> Result<Value> {
        let url = format!("https://contract.mexc.com/{}", path.trim_start_matches('/'));
        let resp = self.public_client.get(url).send().await?;
        let status = resp.status();
        let text = resp.text().await?;
        let v: Value = serde_json::from_str(&text)
            .unwrap_or_else(|_| json!({"success": false, "message": text}));
        if !status.is_success() {
            return Ok(json!({"success": false, "code": status.as_u16(), "message": v}));
        }
        Ok(v)
    }

    pub async fn private_request(
        &self,
        method: &str,
        endpoint: &str,
        body: Option<Value>,
        params: Option<Vec<(String, String)>>,
    ) -> Result<Value> {
        if self.account.uid.trim().is_empty() {
            return Ok(json!({"success": false, "message": "empty MEXC web_uid"}));
        }
        let (nonce, sign) = self.sign(method, endpoint, body.as_ref());
        let url = format!("https://www.mexc.com/api/platform/futures/api/v1/{endpoint}");
        let mut req = match method {
            "POST" => self.private_client.post(&url),
            "PUT" => self.private_client.put(&url),
            _ => self.private_client.get(&url),
        };
        if let Some(params) = params {
            req = req.query(&params);
        }
        let mut mtoken = self.account.device_id.clone();
        if mtoken.trim().is_empty() {
            mtoken = format!("{:x}", md5::compute(self.account.uid.as_bytes()));
        }
        req = req
            .header("authorization", &self.account.uid)
            .header("x-mxc-nonce", nonce.to_string())
            .header("x-mxc-sign", sign)
            .header("content-type", "application/json")
            .header("x-language", "en-US")
            .header("language", "English")
            .header("platform", "H5-web")
            .header("origin", "https://www.mexc.com")
            .header("referer", "https://www.mexc.com/")
            .header("mtoken", &mtoken);
        if !self.account.device_id.trim().is_empty() {
            req = req.header("device-id", &self.account.device_id);
        }
        if let Some(body) = body {
            req = req.body(serde_json::to_string(&body)?);
        }
        let resp = req.send().await?;
        let status = resp.status();
        let text = resp.text().await?;
        let v: Value = serde_json::from_str(&text)
            .unwrap_or_else(|_| json!({"success": false, "message": text}));
        if !status.is_success() {
            return Ok(json!({"success": false, "code": status.as_u16(), "message": v}));
        }
        Ok(v)
    }

    pub async fn auth_ping(&self) -> Value {
        self.private_request("GET", "private/account/assets", None, None)
            .await
            .unwrap_or_else(|e| json!({"success": false, "message": e.to_string()}))
    }

    pub async fn get_contract_detail(&self, symbol: &str, ttl: Duration) -> Option<Value> {
        let full = norm_symbol(symbol);
        {
            let cache = self.contract_cache.lock().await;
            if let Some(c) = cache.get(&full) {
                if c.ts.elapsed() < ttl {
                    return Some(c.value.clone());
                }
            }
        }
        let data = self
            .market_request(&format!("api/v1/contract/detail?symbol={full}"))
            .await
            .ok()?;
        if data.get("success").and_then(Value::as_bool) != Some(true) {
            return None;
        }
        let raw = data.get("data")?;
        let item = if let Some(arr) = raw.as_array() {
            arr.first().cloned()
        } else {
            Some(raw.clone())
        }?;
        self.contract_cache.lock().await.insert(
            full,
            Cached {
                value: item.clone(),
                ts: Instant::now(),
            },
        );
        Some(item)
    }

    pub async fn is_zero_fee_symbol(&self, symbol: &str) -> bool {
        let Some(d) = self
            .get_contract_detail(symbol, Duration::from_secs(60))
            .await
        else {
            return false;
        };
        zero_fee_flag(&d)
    }

    pub async fn get_max_leverage(&self, symbol: &str) -> Option<i64> {
        let d = self
            .get_contract_detail(symbol, Duration::from_secs(60))
            .await?;
        for key in ["maxLeverage", "max_leverage", "maxLever", "maxLeveraged"] {
            if let Some(v) = d.get(key).and_then(num_as_f64) {
                return Some(v as i64);
            }
        }
        None
    }

    pub async fn list_zero_fee_symbols(&self) -> Vec<String> {
        let Ok(data) = self.market_request("api/v1/contract/detail").await else {
            return Vec::new();
        };
        if data.get("success").and_then(Value::as_bool) != Some(true) {
            return Vec::new();
        }
        let raw = data
            .get("data")
            .and_then(|v| v.get("data").or(Some(v)))
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut out: Vec<String> = raw
            .iter()
            .filter_map(|item| {
                let sym = item.get("symbol")?.as_str()?.to_ascii_uppercase();
                (sym.ends_with("_USDT") && zero_fee_flag(item)).then_some(sym)
            })
            .collect();
        out.sort();
        out.dedup();
        if let Some(idx) = out.iter().position(|s| s == "BTC_USDT") {
            let btc = out.remove(idx);
            out.insert(0, btc);
        }
        out
    }

    pub async fn ticker_cached(&self, symbol: &str) -> Option<Value> {
        let full = norm_symbol(symbol);
        {
            let cache = self.ticker_cache.lock().await;
            if let Some(c) = cache.get(&full) {
                if c.ts.elapsed() < Duration::from_millis(700) {
                    return Some(c.value.clone());
                }
            }
        }
        let data = self
            .market_request(&format!("api/v1/contract/ticker?symbol={full}"))
            .await
            .ok()?;
        if data.get("success").and_then(Value::as_bool) != Some(true) {
            return None;
        }
        let raw = data.get("data").cloned()?;
        let item = if raw.is_array() {
            raw.as_array().and_then(|a| a.first()).cloned()
        } else {
            Some(raw)
        }?;
        self.ticker_cache.lock().await.insert(
            full,
            Cached {
                value: item.clone(),
                ts: Instant::now(),
            },
        );
        Some(item)
    }

    pub async fn get_price_for_side(
        &self,
        symbol: &str,
        side: i64,
        fallback: Option<f64>,
    ) -> Option<f64> {
        let ticker = self.ticker_cached(symbol).await?;
        let keys = if matches!(side, 1 | 2) {
            ["ask1", "ask", "bestAsk", "lastPrice", "fairPrice"]
        } else {
            ["bid1", "bid", "bestBid", "lastPrice", "fairPrice"]
        };
        for key in keys {
            if let Some(v) = ticker.get(key).and_then(num_as_f64).filter(|v| *v > 0.0) {
                return Some(v);
            }
        }
        fallback
    }

    pub async fn calc_volume_from_usdt(
        &self,
        symbol: &str,
        notional_usdt: f64,
        price_override: Option<f64>,
        side: i64,
    ) -> Option<f64> {
        if notional_usdt <= 0.0 {
            return None;
        }
        let info = self
            .get_contract_detail(symbol, Duration::from_secs(3600))
            .await?;
        let contract_size = info
            .get("contractSize")
            .and_then(num_as_f64)
            .unwrap_or(1.0)
            .max(1e-18);
        let vol_unit = info.get("volUnit").and_then(num_as_f64).unwrap_or(0.0);
        let min_vol = info.get("minVol").and_then(num_as_f64).unwrap_or(0.0);
        let vol_scale = info
            .get("volScale")
            .and_then(num_as_f64)
            .unwrap_or(0.0)
            .max(0.0) as i32;
        let price = match price_override.filter(|p| *p > 0.0) {
            Some(p) => p,
            None => self.get_price_for_side(symbol, side, None).await?,
        };
        if price <= 0.0 {
            return None;
        }
        let raw = notional_usdt / (price * contract_size);
        let mut vol = if vol_unit > 0.0 {
            (raw / vol_unit).floor().max(1.0) * vol_unit
        } else {
            raw
        };
        if min_vol > 0.0 && vol < min_vol {
            vol = min_vol;
        }
        let factor = 10_f64.powi(vol_scale);
        vol = (vol * factor).floor() / factor;
        (vol > 0.0).then_some(vol)
    }

    pub async fn create_order(
        &self,
        symbol: &str,
        side: i64,
        order_type: &str,
        price: Option<String>,
        vol: f64,
        leverage: i64,
        margin_mode: i64,
    ) -> Value {
        let symbol_full = norm_symbol(symbol);
        let ts = Self::now_ms();
        let mut payload = json!({
            "symbol": symbol_full,
            "side": side,
            "openType": if matches!(margin_mode, 1 | 2) { margin_mode } else { 1 },
            "type": order_type,
            "vol": vol,
            "leverage": leverage,
            "marketCeiling": false,
            "priceProtect": "0",
            "p0": security_param(400),
            "k0": security_param(400),
            "chash": self.account.chash,
            "ts": ts,
            "mhash": self.account.mhash,
            "mtoken": self.account.device_id,
        });
        if matches!(side, 2 | 4) {
            payload["flashClose"] = json!(true);
        }
        if let Some(p) = price {
            if ["1", "2", "3", "4", "6"].contains(&order_type) {
                payload["price"] = json!(p);
            }
        }
        self.private_request("POST", "private/order/create", Some(payload), None)
            .await
            .unwrap_or_else(|e| json!({"success": false, "message": e.to_string()}))
    }

    pub async fn open_ioc(
        &self,
        symbol_full: &str,
        side: &str,
        notional_usdt: f64,
        leverage: i64,
        price: f64,
    ) -> Value {
        let side_code = if side.eq_ignore_ascii_case("LONG") {
            1
        } else {
            3
        };
        let Some((snapped_price, snapped_price_s)) =
            self.snap_entry_price(symbol_full, price, side, true).await
        else {
            return json!({"success": false, "message": "Could not snap price"});
        };
        let Some(vol) = self
            .calc_volume_from_usdt(symbol_full, notional_usdt, Some(snapped_price), side_code)
            .await
        else {
            return json!({"success": false, "message": "Could not calc volume"});
        };
        let mut resp = self
            .create_order(
                symbol_full,
                side_code,
                "3",
                Some(snapped_price_s.clone()),
                vol,
                leverage,
                1,
            )
            .await;
        if let Some(obj) = resp.as_object_mut() {
            obj.insert("_requested_vol".to_string(), json!(vol));
            obj.insert("_requested_notional".to_string(), json!(notional_usdt));
            obj.insert("_requested_price".to_string(), json!(snapped_price_s));
        }
        resp
    }

    pub async fn open_limit(
        &self,
        symbol_full: &str,
        side: &str,
        notional_usdt: f64,
        leverage: i64,
        price: f64,
    ) -> Value {
        let side_code = if side.eq_ignore_ascii_case("LONG") {
            1
        } else {
            3
        };
        let Some((snapped_price, snapped_price_s)) =
            self.snap_entry_price(symbol_full, price, side, false).await
        else {
            return json!({"success": false, "message": "Could not snap price"});
        };
        let Some(vol) = self
            .calc_volume_from_usdt(symbol_full, notional_usdt, Some(snapped_price), side_code)
            .await
        else {
            return json!({"success": false, "message": "Could not calc volume"});
        };
        self.create_order(
            symbol_full,
            side_code,
            "2",
            Some(snapped_price_s),
            vol,
            leverage,
            1,
        )
        .await
    }

    pub async fn close_market(
        &self,
        symbol_full: &str,
        side: &str,
        hold_vol: f64,
        leverage: i64,
    ) -> Value {
        let side_code = if side.eq_ignore_ascii_case("LONG") {
            4
        } else {
            2
        };
        self.create_order(symbol_full, side_code, "5", None, hold_vol, leverage, 1)
            .await
    }

    async fn snap_entry_price(
        &self,
        symbol: &str,
        price: f64,
        side: &str,
        aggressive: bool,
    ) -> Option<(f64, String)> {
        if !price.is_finite() || price <= 0.0 {
            return None;
        }
        let info = self
            .get_contract_detail(symbol, Duration::from_secs(3600))
            .await?;
        let tick = price_tick_from_info(&info)?;
        if tick <= Decimal::ZERO {
            return None;
        }
        let price_d = Decimal::from_str(&plain_decimal(price)).ok()?;
        let units = price_d / tick;
        let is_long = side.eq_ignore_ascii_case("LONG");
        let snapped_units = match (is_long, aggressive) {
            (true, true) => units.ceil(),
            (false, true) => units.floor(),
            (true, false) => units.floor(),
            (false, false) => units.ceil(),
        };
        let snapped = snapped_units * tick;
        let scale = info
            .get("priceScale")
            .and_then(num_as_f64)
            .unwrap_or(12.0)
            .max(0.0) as u32;
        let s = trim_decimal_string(snapped.round_dp(scale).to_string());
        let f = Decimal::from_str(&s).ok()?.to_f64()?;
        Some((f, s))
    }

    pub async fn cancel_all_for(&self, symbol: &str) -> Value {
        self.private_request(
            "POST",
            "private/order/cancel_all",
            Some(json!({"symbol": norm_symbol(symbol)})),
            None,
        )
        .await
        .unwrap_or_else(|e| json!({"success": false, "message": e.to_string()}))
    }

    pub async fn query_order(&self, order_id: i64) -> Value {
        self.private_request("GET", &format!("private/order/get/{order_id}"), None, None)
            .await
            .unwrap_or_else(|e| json!({"success": false, "message": e.to_string()}))
    }

    pub async fn get_positions_raw(&self) -> Vec<Value> {
        self.get_positions_raw_checked().await.unwrap_or_default()
    }

    pub async fn get_positions_raw_checked(&self) -> Option<Vec<Value>> {
        let raw = self
            .private_request("GET", "private/position/open_positions", None, None)
            .await
            .unwrap_or_else(|e| json!({"success": false, "message": e.to_string()}));
        if raw.get("success").and_then(Value::as_bool) != Some(true) {
            return None;
        }
        Some(normalize_list_data(&raw))
    }

    pub async fn get_history_positions(&self, symbol: Option<&str>, limit: usize) -> Vec<Value> {
        self.get_history_positions_window(symbol, None, None, limit)
            .await
    }

    pub async fn get_history_positions_window(
        &self,
        symbol: Option<&str>,
        start_time_ms: Option<i64>,
        end_time_ms: Option<i64>,
        limit: usize,
    ) -> Vec<Value> {
        let mut params = vec![
            ("page_num".to_string(), "1".to_string()),
            ("page_size".to_string(), limit.to_string()),
        ];
        if let Some(symbol) = symbol {
            params.push(("symbol".to_string(), norm_symbol(symbol)));
        }
        if let Some(start_time_ms) = start_time_ms {
            params.push(("start_time".to_string(), start_time_ms.to_string()));
        }
        if let Some(end_time_ms) = end_time_ms {
            params.push(("end_time".to_string(), end_time_ms.to_string()));
        }
        let raw = self
            .private_request(
                "GET",
                "private/position/list/history_positions",
                None,
                Some(params),
            )
            .await
            .unwrap_or_else(|e| json!({"success": false, "message": e.to_string()}));
        if raw.get("success").and_then(Value::as_bool) != Some(true) {
            return Vec::new();
        }
        let data = raw.get("data").cloned().unwrap_or(Value::Null);
        if let Some(arr) = data.as_array() {
            return arr.clone();
        }
        for key in ["resultList", "data"] {
            if let Some(arr) = data.get(key).and_then(Value::as_array) {
                return arr.clone();
            }
        }
        Vec::new()
    }

    pub async fn get_usdt_balance_snapshot(&self) -> (f64, f64) {
        let raw = self
            .private_request("GET", "private/account/assets", None, None)
            .await
            .unwrap_or_else(|e| json!({"success": false, "message": e.to_string()}));
        if raw.get("success").and_then(Value::as_bool) != Some(true) {
            return (0.0, 0.0);
        }
        let rows = normalize_list_data(&raw);
        for item in rows {
            let cur = item
                .get("currency")
                .or_else(|| item.get("asset"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_ascii_uppercase();
            if cur != "USDT" {
                continue;
            }
            let mut available: f64 = 0.0;
            for key in [
                "availableBalance",
                "availableCash",
                "availableOpen",
                "available",
                "availableMargin",
                "marginAvailable",
                "maxAvailable",
                "positionMarginFree",
                "balance",
            ] {
                if let Some(v) = item.get(key).and_then(num_as_f64) {
                    available = available.max(v);
                }
            }
            let equity = ["equity", "balance"]
                .iter()
                .find_map(|k| item.get(*k).and_then(num_as_f64))
                .unwrap_or(available);
            return (available.max(0.0), equity.max(0.0));
        }
        (0.0, 0.0)
    }
}

pub fn norm_symbol(symbol: &str) -> String {
    let s = symbol.trim().to_ascii_uppercase();
    if s.is_empty() {
        return s;
    }
    if s.ends_with("_USDT") {
        s
    } else if s.ends_with("USDT") && !s.contains('_') {
        format!("{}_USDT", &s[..s.len() - 4])
    } else if s.contains('_') {
        s
    } else {
        format!("{s}_USDT")
    }
}

pub fn num_as_f64(v: &Value) -> Option<f64> {
    if let Some(f) = v.as_f64() {
        Some(f)
    } else if let Some(s) = v.as_str() {
        s.parse::<f64>().ok()
    } else {
        None
    }
}

fn zero_fee_flag(item: &Value) -> bool {
    for key in ["isZeroFeeSymbol", "zeroFee", "isZeroFee"] {
        if let Some(v) = item.get(key) {
            if let Some(b) = v.as_bool() {
                return b;
            }
            if let Some(n) = num_as_f64(v) {
                return n != 0.0;
            }
        }
    }
    let maker = ["makerFeeRate", "makerFee", "openFeeRate"]
        .iter()
        .find_map(|k| item.get(*k).and_then(num_as_f64));
    let taker = ["takerFeeRate", "takerFee", "closeFeeRate"]
        .iter()
        .find_map(|k| item.get(*k).and_then(num_as_f64));
    let fee = ["feeRate", "fee_rate"]
        .iter()
        .find_map(|k| item.get(*k).and_then(num_as_f64));
    maker.zip(taker).is_some_and(|(m, t)| m == 0.0 && t == 0.0) || fee == Some(0.0)
}

fn normalize_list_data(raw: &Value) -> Vec<Value> {
    let Some(data) = raw.get("data") else {
        return Vec::new();
    };
    if let Some(arr) = data.as_array() {
        return arr.clone();
    }
    for key in ["data", "list", "resultList", "rows"] {
        if let Some(arr) = data.get(key).and_then(Value::as_array) {
            return arr.clone();
        }
    }
    Vec::new()
}

fn security_param(length: usize) -> String {
    let chars: Vec<char> = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/="
        .chars()
        .collect();
    let mut rng = rand::thread_rng();
    (0..length)
        .map(|_| *chars.choose(&mut rng).unwrap_or(&'a'))
        .collect()
}

fn plain_decimal(v: f64) -> String {
    if !v.is_finite() {
        return "0".to_string();
    }
    let s = format!("{v:.16}");
    s.trim_end_matches('0').trim_end_matches('.').to_string()
}

fn price_tick_from_info(info: &Value) -> Option<Decimal> {
    if let Some(unit) = info
        .get("priceUnit")
        .and_then(num_as_f64)
        .filter(|v| *v > 0.0)
    {
        return Decimal::from_str(&plain_decimal(unit)).ok();
    }
    let scale = info.get("priceScale").and_then(num_as_f64).unwrap_or(0.0);
    (scale >= 0.0).then(|| Decimal::new(1, scale as u32))
}

fn trim_decimal_string(mut s: String) -> String {
    if s.contains('.') {
        while s.ends_with('0') {
            s.pop();
        }
        if s.ends_with('.') {
            s.pop();
        }
    }
    if s.is_empty() { "0".to_string() } else { s }
}

pub fn extract_order_id(resp: &Value) -> Option<i64> {
    let data = resp.get("data")?;
    if let Some(id) = data.get("orderId").and_then(num_as_f64) {
        return Some(id as i64);
    }
    if let Some(id) = data.get("order_id").and_then(num_as_f64) {
        return Some(id as i64);
    }
    if let Some(id) = data.as_str().and_then(|s| s.parse::<i64>().ok()) {
        return Some(id);
    }
    if let Some(row) = data.as_array().and_then(|rows| rows.first()) {
        if let Some(id) = row.get("orderId").and_then(num_as_f64) {
            return Some(id as i64);
        }
        if let Some(id) = row.get("order_id").and_then(num_as_f64) {
            return Some(id as i64);
        }
        if let Some(id) = row.as_str().and_then(|s| s.parse::<i64>().ok()) {
            return Some(id);
        }
    }
    data.as_i64()
}
