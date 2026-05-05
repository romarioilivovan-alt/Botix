// ============== helpers ==============
const $ = (id) => document.getElementById(id);

async function api(url, method = 'GET', body = null) {
  const opt = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.message || r.statusText);
  return j;
}

function fmt(v, dp = 4) {
  if (v == null || isNaN(v)) return '-';
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (Math.abs(v) >= 1e3) return v.toFixed(0);
  if (Math.abs(v) >= 1) return v.toFixed(2);
  if (Math.abs(v) >= 0.0001) return v.toFixed(6);
  return v.toExponential(2);
}

function fmtTime(ts) {
  if (!ts) return '-';
  return new Date(ts * 1000).toLocaleTimeString();
}

// ============== state ==============
let cfg = null;
const STOCK_SYMBOLS = new Set(["NVDA_USDT","MSTR_USDT","TSLA_USDT","INTC_USDT"]);
let candidateFilter = 'all';

// ============== chart (equity) ==============
function drawEquity(canvas, data) {
  if (!canvas || !data || data.length < 2) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width = canvas.clientWidth * window.devicePixelRatio;
  const h = canvas.height = (canvas.clientHeight || 120) * window.devicePixelRatio;
  ctx.clearRect(0, 0, w, h);

  const ys = data.map(d => d.equity);
  const min = Math.min(...ys);
  const max = Math.max(...ys);
  const span = Math.max(1e-6, max - min);
  const xs = data.map((_, i) => (i / (data.length - 1)) * w);
  const yyy = (v) => h - ((v - min) / span) * h * 0.9 - h * 0.05;

  // baseline
  const start = data[0].balance || data[0].equity;
  const startY = yyy(start);
  ctx.strokeStyle = 'rgba(126, 138, 152, 0.4)';
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(0, startY); ctx.lineTo(w, startY); ctx.stroke();
  ctx.setLineDash([]);

  // line
  ctx.strokeStyle = ys[ys.length - 1] >= start ? '#1ec27b' : '#e34a4a';
  ctx.lineWidth = 1.5 * window.devicePixelRatio;
  ctx.beginPath();
  for (let i = 0; i < data.length; i++) {
    const x = xs[i], y = yyy(ys[i]);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
}

// ============== candidate filter ==============
function setCandidateFilter(filter) {
  candidateFilter = filter;
  document.querySelectorAll('#candidateFilterTabs .tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.filter === filter);
  });
  renderCandidates(window._lastCandidates || []);
}

function filterCandidates(items) {
  if (candidateFilter === 'crypto') return items.filter(c => !STOCK_SYMBOLS.has(c.symbol));
  if (candidateFilter === 'stocks') return items.filter(c => STOCK_SYMBOLS.has(c.symbol));
  return items;
}

// ============== rendering ==============
function renderEngine(eng) {
  const setPill = (id, ok, label) => {
    const el = $(id);
    el.classList.toggle('ok', !!ok);
    el.classList.toggle('bad', ok === false);
    el.textContent = label;
  };
  setPill('binPill', eng.binance_ok, `Binance: ${eng.binance_ok ? 'OK' : 'down'}`);
  setPill('mexcPill', eng.mexc_ok, `MEXC: ${eng.mexc_ok ? 'OK' : 'down'}`);
  if (eng.mexc_auth_ok == null)
    setPill('authPill', null, 'Auth: ?');
  else
    setPill('authPill', eng.mexc_auth_ok, `Auth: ${eng.mexc_auth_ok ? 'OK' : 'fail'}`);
  $('modePill').textContent = `Mode: ${eng.mode}${eng.running ? ' RUN' : ' STOP'}`;
  $('killPill').style.display = eng.kill ? '' : 'none';
  if (eng.kill) $('killPill').textContent = `KILL: ${eng.kill_reason || 'manual'}`;
}

function renderCandidates(items) {
  window._lastCandidates = items;
  const tbody = $('candTable').querySelector('tbody');
  tbody.innerHTML = '';
  for (const c of filterCandidates(items).slice(0, 20)) {
    const tr = document.createElement('tr');
    const sideCls = c.side === 'LONG' ? 'green' : c.side === 'SHORT' ? 'red' : 'muted';
    tr.innerHTML = `
      <td>${c.symbol || '-'}</td>
      <td class="${sideCls}">${c.side || '-'}</td>
      <td>${fmt(c.score, 2)}</td>
      <td>${fmt(c.z, 2)}</td>
      <td>${fmt(c.spread_bps, 2)}</td>
      <td>${fmt(c.fair, 6)}</td>
      <td>${fmt(c.mexc, 6)}</td>
      <td>${fmt(c.depth, 0)}</td>
      <td class="muted">${c.blocked || ''}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderPositions(items) {
  const tbody = $('posTable').querySelector('tbody');
  tbody.innerHTML = '';
  for (const p of items) {
    const tr = document.createElement('tr');
    const pnlCls = p.pnl > 0 ? 'green' : p.pnl < 0 ? 'red' : '';
    const sideCls = p.side === 'LONG' ? 'green' : 'red';
    tr.innerHTML = `
      <td>${p.symbol}</td>
      <td class="${sideCls}">${p.side}</td>
      <td>${fmt(p.entry, 6)}</td>
      <td>${fmt(p.stop, 6)}</td>
      <td>${fmt(p.tp, 6)}</td>
      <td>${fmt(p.qty, 6)}</td>
      <td>${p.lev}</td>
      <td class="${pnlCls}">${fmt(p.pnl, 4)}</td>
      <td class="${pnlCls}">${fmt(p.pnl_pct, 2)}%</td>
    `;
    tbody.appendChild(tr);
  }
  $('openCount').textContent = items.length;
}

function renderTrades(items) {
  const tbody = $('tradesTable').querySelector('tbody');
  tbody.innerHTML = '';
  const recent = [...items].reverse();
  for (const t of recent.slice(0, 50)) {
    const tr = document.createElement('tr');
    const pnlCls = t.pnl > 0 ? 'green' : t.pnl < 0 ? 'red' : '';
    const sideCls = t.side === 'LONG' ? 'green' : 'red';
    tr.innerHTML = `
      <td>${fmtTime(t.ts)}</td>
      <td>${t.symbol}</td>
      <td class="${sideCls}">${t.side}</td>
      <td>${fmt(t.entry, 6)}</td>
      <td>${fmt(t.exit, 6)}</td>
      <td class="${pnlCls}">${fmt(t.pnl, 4)}</td>
      <td class="${pnlCls}">${fmt(t.pnl_pct, 2)}%</td>
      <td class="muted">${t.reason || '-'}</td>
      <td>${fmt(t.duration, 1)}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderLogs(items) {
  const box = $('logs');
  const wasNearBottom = box.scrollTop + box.clientHeight + 30 >= box.scrollHeight;
  box.innerHTML = '';
  for (const l of items) {
    const div = document.createElement('div');
    div.className = `log-entry log-${l.level}`;
    div.textContent = `${fmtTime(l.t)} ${l.level.toUpperCase().padEnd(5)} ${l.msg}`;
    box.appendChild(div);
  }
  if (wasNearBottom) box.scrollTop = box.scrollHeight;
}

function applySnapshot(s) {
  if (!s) return;
  if (s.engine) renderEngine(s.engine);
  $('balanceVal').textContent = fmt(s.balance, 4) + ' USDT';
  $('sessStart').textContent = fmt(s.session_starting_balance, 4);
  $('sessPeak').textContent = fmt(s.session_peak_balance, 4);
  $('univSize').textContent = s.universe_size ?? '-';

  renderCandidates(s.candidates || []);
  renderPositions(s.positions || []);
  renderTrades(s.recent_trades || []);
  renderLogs(s.logs || []);

  drawEquity($('equityCanvas'), s.equity || []);
}

// ============== controls ==============
async function loadConfig() {
  cfg = await api('/api/config');
  $('cfgWebUid').value = cfg.mexc_web?.web_uid || '';
  $('cfgK').value = cfg.risk?.max_concurrent_positions || 5;
  $('cfgMarginPct').value = cfg.risk?.margin_pct_per_slot || 0.18;
  $('cfgLevMode').value = cfg.risk?.leverage_mode || 'max';
  $('cfgFixedLev').value = cfg.risk?.fixed_leverage || 20;
  $('cfgDailyKill').value = cfg.risk?.daily_loss_pct_kill || 0.30;
  $('cfgDDKill').value = cfg.risk?.max_drawdown_pct_kill || 0.50;
  $('cfgEntryZ').value = cfg.strategy?.entry_z || 1.8;
  $('cfgCancelZ').value = cfg.strategy?.cancel_z || 0.5;
  $('cfgHardSL').value = cfg.strategy?.hard_sl_pct || 0.004;
  $('cfgMaxHold').value = cfg.strategy?.max_hold_sec || 30;
  $('cfgMaxSyms').value = cfg.universe?.max_symbols || 60;
  $('cfgRefreshSec').value = cfg.universe?.refresh_sec || 300;
  $('cfgPaperBal').value = cfg.paper_starting_balance || 1000;
  $('modeSelect').value = cfg.mode || 'paper';
}

async function saveConfig() {
  const patch = {
    mexc_web: { web_uid: $('cfgWebUid').value.trim() },
    risk: {
      max_concurrent_positions: parseInt($('cfgK').value) || 5,
      margin_pct_per_slot: parseFloat($('cfgMarginPct').value) || 0.18,
      leverage_mode: $('cfgLevMode').value,
      fixed_leverage: parseInt($('cfgFixedLev').value) || 20,
      daily_loss_pct_kill: parseFloat($('cfgDailyKill').value) || 0.30,
      max_drawdown_pct_kill: parseFloat($('cfgDDKill').value) || 0.50,
    },
    strategy: {
      entry_z: parseFloat($('cfgEntryZ').value) || 1.8,
      cancel_z: parseFloat($('cfgCancelZ').value) || 0.5,
      hard_sl_pct: parseFloat($('cfgHardSL').value) || 0.004,
      max_hold_sec: parseFloat($('cfgMaxHold').value) || 30,
    },
    universe: {
      max_symbols: parseInt($('cfgMaxSyms').value) || 60,
      refresh_sec: parseInt($('cfgRefreshSec').value) || 300,
    },
    paper_starting_balance: parseFloat($('cfgPaperBal').value) || 1000,
  };
  await api('/api/config', 'POST', patch);
  alert('Config saved. Restart engine to apply changes that affect WS subscriptions.');
}

// ============== symbol selector ==============
let symAvailable = [];   // all (MEXC ∩ Binance)
let symSelected = new Set();   // current include_only

function renderSymbolList() {
  const filter = ($('symbolFilter').value || '').trim().toUpperCase();
  const box = $('symbolList');
  box.innerHTML = '';
  let visible = 0;
  for (const s of symAvailable) {
    if (filter && !s.includes(filter)) continue;
    visible++;
    const id = `sym_${s}`;
    const lbl = document.createElement('label');
    lbl.htmlFor = id;
    const checked = symSelected.has(s) || symSelected.size === 0;
    lbl.innerHTML = `<input type="checkbox" id="${id}" ${checked ? 'checked' : ''} data-sym="${s}" /> ${s}`;
    box.appendChild(lbl);
  }
  $('symCount').textContent = `${symSelected.size === 0 ? 'all' : symSelected.size}/${symAvailable.length} (${visible} shown)`;
}

async function loadSymbols() {
  try {
    const j = await api('/api/universe/available');
    symAvailable = (j.available || []).slice().sort();
    symSelected = new Set(j.selected || []);
    renderSymbolList();
  } catch (e) {
    console.warn('loadSymbols error', e);
  }
}

function readCheckedSymbols() {
  const sels = new Set();
  for (const cb of $('symbolList').querySelectorAll('input[type="checkbox"]')) {
    if (cb.checked) sels.add(cb.dataset.sym);
  }
  return sels;
}

async function saveSymbols() {
  // If everything is checked → empty list (= trade all). Otherwise persist explicit set.
  const sels = readCheckedSymbols();
  // include hidden-by-filter selections too
  for (const s of symSelected) sels.add(s);
  // trick: if all visible are checked AND user hasn't filtered, treat as "all"
  const allChecked = sels.size >= symAvailable.length;
  const out = allChecked ? [] : Array.from(sels);
  await api('/api/universe/selection', 'POST', { symbols: out });
  symSelected = new Set(out);
  renderSymbolList();
}

// ============== ws ==============
function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'snapshot') applySnapshot(msg.data);
    } catch {}
  };
  ws.onclose = () => setTimeout(connectWS, 1500);
}

// ============== boot ==============
window.addEventListener('DOMContentLoaded', async () => {
  $('btnStart').addEventListener('click', () => api('/api/run/start', 'POST'));
  $('btnStop').addEventListener('click', () => api('/api/run/stop', 'POST'));
  $('btnKill').addEventListener('click', async () => {
    if (!confirm('KILL ALL — close every open position and stop the engine?')) return;
    await api('/api/run/kill', 'POST');
  });
  $('modeSelect').addEventListener('change', () => api('/api/mode', 'POST', { mode: $('modeSelect').value }));
  $('btnSaveCfg').addEventListener('click', saveConfig);
  $('btnRefreshUniverse').addEventListener('click', async () => {
    await api('/api/universe/refresh', 'POST');
    await loadSymbols();
  });

  $('btnSymAll').addEventListener('click', () => {
    for (const cb of $('symbolList').querySelectorAll('input[type="checkbox"]')) cb.checked = true;
  });
  $('btnSymNone').addEventListener('click', () => {
    for (const cb of $('symbolList').querySelectorAll('input[type="checkbox"]')) cb.checked = false;
  });
  $('symbolFilter').addEventListener('input', renderSymbolList);
  $('btnSaveSymbols').addEventListener('click', saveSymbols);

  await loadConfig();
  await loadSymbols();
  connectWS();

  // Fallback poll if WS dies
  setInterval(async () => {
    try {
      const s = await api('/api/state');
      applySnapshot(s);
    } catch {}
  }, 3000);
});
