// FluFlip Dashboard - Enhanced with Analytics & Speed Metrics
// Global state
let state = {};
let equityChart = null;
let pnlDistChart = null;
let winRateChart = null;
let candidateFilter = 'all';
let candidateSort = { field: 'score', dir: 'desc' };
let tradeFilters = { symbol: '', result: '' };

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
  initCharts();
  initEventListeners();
  startPolling();
});

// ========== CHARTS ==========
function initCharts() {
  // Equity chart (line)
  const equityCtx = document.getElementById('equityCanvas');
  if (equityCtx) {
    equityChart = new Chart(equityCtx, {
      type: 'line',
      data: {
        labels: [],
        datasets: [{
          label: 'Equity',
          data: [],
          borderColor: '#4ad6c5',
          backgroundColor: 'rgba(74, 214, 197, 0.1)',
          borderWidth: 2,
          tension: 0.3,
          pointRadius: 0,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { display: false },
          y: {
            ticks: { color: '#7e8a98', font: { size: 10 } },
            grid: { color: '#232c36' }
          }
        }
      }
    });
  }

  // PnL Distribution (histogram)
  const pnlCtx = document.getElementById('pnlDistChart');
  if (pnlCtx) {
    pnlDistChart = new Chart(pnlCtx, {
      type: 'bar',
      data: {
        labels: [],
        datasets: [{
          label: 'Trades',
          data: [],
          backgroundColor: '#4a8fd6',
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          title: { display: false }
        },
        scales: {
          x: {
            ticks: { color: '#7e8a98', font: { size: 10 } },
            grid: { display: false }
          },
          y: {
            ticks: { color: '#7e8a98', font: { size: 10 } },
            grid: { color: '#232c36' }
          }
        }
      }
    });
  }

  // Win Rate by Hour
  const wrCtx = document.getElementById('winRateByHourChart');
  if (wrCtx) {
    winRateChart = new Chart(wrCtx, {
      type: 'bar',
      data: {
        labels: Array.from({length: 24}, (_, i) => `${i}h`),
        datasets: [{
          label: 'Win Rate %',
          data: Array(24).fill(0),
          backgroundColor: '#1ec27b',
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: {
            ticks: { color: '#7e8a98', font: { size: 9 } },
            grid: { display: false }
          },
          y: {
            min: 0,
            max: 100,
            ticks: { color: '#7e8a98', font: { size: 10 } },
            grid: { color: '#232c36' }
          }
        }
      }
    });
  }
}

// ========== EVENT LISTENERS ==========
function initEventListeners() {
  // Mode controls
  document.getElementById('btnStart')?.addEventListener('click', () => startEngine());
  document.getElementById('btnStop')?.addEventListener('click', () => stopEngine());
  document.getElementById('btnKill')?.addEventListener('click', () => killAll());

  // Trade filters
  document.getElementById('tradeSymbolFilter')?.addEventListener('change', (e) => {
    tradeFilters.symbol = e.target.value;
    renderTrades();
  });
  document.getElementById('tradeResultFilter')?.addEventListener('change', (e) => {
    tradeFilters.result = e.target.value;
    renderTrades();
  });

  // Candidate search
  document.getElementById('candSearchInput')?.addEventListener('input', (e) => {
    renderCandidates();
  });

  // Config save
  document.getElementById('btnSaveCfg')?.addEventListener('click', () => saveConfig());
  document.getElementById('btnRefreshUniverse')?.addEventListener('click', () => refreshUniverse());
}

// ========== POLLING ==========
function startPolling() {
  poll();
  setInterval(poll, 1000); // Poll every second
}

async function poll() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) return;
    state = await res.json();
    render();
  } catch (e) {
    console.error('Poll error:', e);
  }
}

// ========== RENDERING ==========
function render() {
  renderStatus();
  renderEquity();
  renderPositions();
  renderTrades();
  renderCandidates();
  renderLogs();
  renderAnalytics();
}

function renderStatus() {
  const eng = state.engine || {};

  // Pills
  document.getElementById('binPill').className = 'pill ' + (eng.binance_ok ? 'ok' : 'bad');
  document.getElementById('binPill').textContent = 'Binance: ' + (eng.binance_ok ? 'OK' : 'DOWN');

  document.getElementById('mexcPill').className = 'pill ' + (eng.mexc_ok ? 'ok' : 'bad');
  document.getElementById('mexcPill').textContent = 'MEXC: ' + (eng.mexc_ok ? 'OK' : 'DOWN');

  const authOk = eng.mexc_auth_ok === true;
  document.getElementById('authPill').className = 'pill ' + (authOk ? 'ok' : 'bad');
  document.getElementById('authPill').textContent = 'Auth: ' + (authOk ? 'OK' : eng.mexc_auth_msg || '?');

  document.getElementById('modePill').textContent = 'Mode: ' + (eng.mode || '?').toUpperCase();

  const killPill = document.getElementById('killPill');
  if (eng.kill) {
    killPill.style.display = 'block';
    killPill.textContent = 'KILL: ' + (eng.kill_reason || 'active');
  } else {
    killPill.style.display = 'none';
  }
}

function renderEquity() {
  const bal = state.balance || 0;
  const start = state.session_starting_balance || 0;
  const peak = state.session_peak_balance || 0;
  const pnl = bal - start;
  const pnlPct = start > 0 ? (pnl / start * 100) : 0;

  document.getElementById('balanceVal').textContent = bal.toFixed(2) + ' USDT';
  document.getElementById('balanceVal').className = 'big ' + (pnl >= 0 ? 'green' : 'red');

  document.getElementById('equityHint').textContent =
    (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + ' (' + (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(2) + '%)';

  document.getElementById('sessStart').textContent = start.toFixed(2);
  document.getElementById('sessPeak').textContent = peak.toFixed(2);
  document.getElementById('openCount').textContent = (state.positions || []).length;
  document.getElementById('univSize').textContent = state.universe_size || 0;

  // Update equity chart
  if (equityChart && state.equity) {
    const points = state.equity.slice(-100); // Last 100 points
    equityChart.data.labels = points.map((_, i) => i);
    equityChart.data.datasets[0].data = points.map(p => p.balance || 0);
    equityChart.update('none');
  }
}

function renderPositions() {
  const tbody = document.querySelector('#posTable tbody');
  if (!tbody) return;

  const positions = state.positions || [];
  document.getElementById('posHint').textContent = positions.length + ' open';

  tbody.innerHTML = positions.map(p => {
    const age = Math.floor((Date.now() / 1000) - p.open_ts);
    const pnlClass = p.pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
    const latency = p.entry_latency_ms ? p.entry_latency_ms.toFixed(0) + 'ms' : '-';
    const algo = p.entry_algo || '-';
    const score = p.entry_score ? p.entry_score.toFixed(2) : '-';

    return `<tr>
      <td>${p.symbol}</td>
      <td class="${p.side === 'LONG' ? 'green' : 'red'}">${p.side}</td>
      <td>${p.entry.toFixed(6)}</td>
      <td>${p.stop ? p.stop.toFixed(6) : '-'}</td>
      <td class="muted">${algo}</td>
      <td class="muted">${score}</td>
      <td class="muted">${age}s</td>
      <td class="muted">${latency}</td>
      <td class="${pnlClass}">${p.pnl >= 0 ? '+' : ''}${p.pnl.toFixed(2)}</td>
      <td class="${pnlClass}">${p.pnl_pct >= 0 ? '+' : ''}${p.pnl_pct.toFixed(2)}%</td>
    </tr>`;
  }).join('');
}

function renderTrades() {
  const tbody = document.querySelector('#tradesTable tbody');
  if (!tbody) return;

  let trades = state.recent_trades || [];

  // Apply filters
  if (tradeFilters.symbol) {
    trades = trades.filter(t => t.symbol === tradeFilters.symbol);
  }
  if (tradeFilters.result === 'profit') {
    trades = trades.filter(t => t.pnl > 0);
  } else if (tradeFilters.result === 'loss') {
    trades = trades.filter(t => t.pnl < 0);
  }

  // Update symbol filter dropdown
  const symbolFilter = document.getElementById('tradeSymbolFilter');
  if (symbolFilter && state.recent_trades) {
    const symbols = [...new Set(state.recent_trades.map(t => t.symbol))].sort();
    const currentVal = symbolFilter.value;
    symbolFilter.innerHTML = '<option value="">All Symbols</option>' +
      symbols.map(s => `<option value="${s}">${s}</option>`).join('');
    symbolFilter.value = currentVal;
  }

  // Show last 20
  trades = trades.slice(-20).reverse();

  tbody.innerHTML = trades.map(t => {
    const time = new Date(t.ts * 1000).toLocaleTimeString();
    const pnlClass = t.pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
    const entryLat = t.entry_latency_ms ? t.entry_latency_ms.toFixed(0) + 'ms' : '-';
    const exitLat = t.exit_latency_ms ? t.exit_latency_ms.toFixed(0) + 'ms' : '-';
    const algo = t.entry_algo || '-';
    const score = t.entry_score ? t.entry_score.toFixed(2) : '-';
    const dur = t.duration ? t.duration.toFixed(1) + 's' : '-';

    return `<tr>
      <td class="muted">${time}</td>
      <td>${t.symbol}</td>
      <td class="${t.side === 'LONG' ? 'green' : 'red'}">${t.side}</td>
      <td class="muted">${algo}</td>
      <td class="muted">${score}</td>
      <td class="muted">${entryLat}</td>
      <td class="muted">${exitLat}</td>
      <td class="${pnlClass}">${t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(2)}</td>
      <td class="${pnlClass}">${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct.toFixed(2)}%</td>
      <td class="muted">${t.reason}</td>
      <td class="muted">${dur}</td>
    </tr>`;
  }).join('');

  // Auto-scroll to bottom
  const scroll = document.getElementById('tradesScroll');
  if (scroll) {
    scroll.scrollTop = scroll.scrollHeight;
  }
}

function renderCandidates() {
  const tbody = document.querySelector('#candTable tbody');
  if (!tbody) return;

  let candidates = state.candidates || [];

  // Apply search filter
  const searchInput = document.getElementById('candSearchInput');
  if (searchInput && searchInput.value) {
    const search = searchInput.value.toLowerCase();
    candidates = candidates.filter(c => c.symbol.toLowerCase().includes(search));
  }

  // Apply type filter
  if (candidateFilter === 'crypto') {
    candidates = candidates.filter(c => !c.symbol.includes('STOCK') && !isStockSymbol(c.symbol));
  } else if (candidateFilter === 'stocks') {
    candidates = candidates.filter(c => c.symbol.includes('STOCK') || isStockSymbol(c.symbol));
  }

  // Sort
  candidates.sort((a, b) => {
    const aVal = a[candidateSort.field] || 0;
    const bVal = b[candidateSort.field] || 0;
    return candidateSort.dir === 'desc' ? bVal - aVal : aVal - bVal;
  });

  tbody.innerHTML = candidates.slice(0, 20).map(c => {
    const scoreClass = c.score > 2 ? 'score-high' : c.score > 1 ? 'score-med' : 'score-low';
    const sideClass = c.side === 'LONG' ? 'green' : c.side === 'SHORT' ? 'red' : 'muted';

    return `<tr>
      <td>${c.symbol}</td>
      <td class="${sideClass}">${c.side || '-'}</td>
      <td class="${scoreClass}">${c.score ? c.score.toFixed(2) : '-'}</td>
      <td>${c.z ? c.z.toFixed(2) : '-'}</td>
      <td>${c.spread_bps ? c.spread_bps.toFixed(1) : '-'}</td>
      <td>${c.depth ? (c.depth / 1000).toFixed(1) + 'k' : '-'}</td>
      <td class="muted">${c.blocked || '-'}</td>
    </tr>`;
  }).join('');
}

function renderLogs() {
  const logbox = document.getElementById('logs');
  if (!logbox) return;

  const logs = state.logs || [];
  logbox.innerHTML = logs.slice(-50).map(l => {
    const time = new Date(l.t * 1000).toLocaleTimeString();
    return `<div class="log-entry log-${l.level}">[${time}] ${l.msg}</div>`;
  }).join('');
  logbox.scrollTop = logbox.scrollHeight;
}

function renderAnalytics() {
  if (!document.getElementById('panel-analytics')) return;

  const trades = state.recent_trades || [];
  if (trades.length === 0) return;

  // Calculate statistics
  const profitable = trades.filter(t => t.pnl > 0);
  const winRate = (profitable.length / trades.length * 100).toFixed(1);
  const bestTrade = Math.max(...trades.map(t => t.pnl));
  const worstTrade = Math.min(...trades.map(t => t.pnl));
  const avgDuration = (trades.reduce((sum, t) => sum + (t.duration || 0), 0) / trades.length).toFixed(1);

  const tradesWithEntryLat = trades.filter(t => t.entry_latency_ms > 0);
  const avgEntryLat = tradesWithEntryLat.length > 0
    ? (tradesWithEntryLat.reduce((sum, t) => sum + t.entry_latency_ms, 0) / tradesWithEntryLat.length).toFixed(0)
    : '-';

  const tradesWithExitLat = trades.filter(t => t.exit_latency_ms > 0);
  const avgExitLat = tradesWithExitLat.length > 0
    ? (tradesWithExitLat.reduce((sum, t) => sum + t.exit_latency_ms, 0) / tradesWithExitLat.length).toFixed(0)
    : '-';

  // Update stat boxes
  document.getElementById('statBestTrade').textContent = '+' + bestTrade.toFixed(2) + ' USDT';
  document.getElementById('statWorstTrade').textContent = worstTrade.toFixed(2) + ' USDT';
  document.getElementById('statAvgDuration').textContent = avgDuration + 's';
  document.getElementById('statWinRate').textContent = winRate + '%';
  document.getElementById('statAvgEntryLatency').textContent = avgEntryLat + (avgEntryLat !== '-' ? 'ms' : '');
  document.getElementById('statAvgExitLatency').textContent = avgExitLat + (avgExitLat !== '-' ? 'ms' : '');

  // PnL Distribution
  if (pnlDistChart) {
    const bins = [-10, -5, -2, -1, 0, 1, 2, 5, 10];
    const counts = Array(bins.length - 1).fill(0);
    trades.forEach(t => {
      for (let i = 0; i < bins.length - 1; i++) {
        if (t.pnl >= bins[i] && t.pnl < bins[i + 1]) {
          counts[i]++;
          break;
        }
      }
    });
    pnlDistChart.data.labels = bins.slice(0, -1).map((b, i) => `${b} to ${bins[i + 1]}`);
    pnlDistChart.data.datasets[0].data = counts;
    pnlDistChart.update('none');
  }

  // Win Rate by Hour
  if (winRateChart) {
    const hourlyStats = Array(24).fill(0).map(() => ({ total: 0, wins: 0 }));
    trades.forEach(t => {
      const hour = new Date(t.ts * 1000).getHours();
      hourlyStats[hour].total++;
      if (t.pnl > 0) hourlyStats[hour].wins++;
    });
    const hourlyWR = hourlyStats.map(h => h.total > 0 ? (h.wins / h.total * 100) : 0);
    winRateChart.data.datasets[0].data = hourlyWR;
    winRateChart.update('none');
  }

  // Symbol Activity Heatmap
  const heatmapContainer = document.getElementById('heatmapContainer');
  if (heatmapContainer) {
    const symbolCounts = {};
    trades.forEach(t => {
      symbolCounts[t.symbol] = (symbolCounts[t.symbol] || 0) + 1;
    });
    const sorted = Object.entries(symbolCounts).sort((a, b) => b[1] - a[1]).slice(0, 12);
    heatmapContainer.innerHTML = sorted.map(([sym, count]) => {
      const intensity = Math.min(count / 10, 1);
      const color = `rgba(74, 214, 197, ${intensity * 0.5})`;
      return `<div class="heatmap-cell" style="background: ${color}">
        <div class="heatmap-symbol">${sym}</div>
        <div class="heatmap-count">${count}</div>
      </div>`;
    }).join('');
  }
}

// ========== HELPERS ==========
function isStockSymbol(sym) {
  const stocks = ['NVDA', 'MSTR', 'TSLA', 'INTC', 'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NFLX'];
  return stocks.some(s => sym.includes(s));
}

function setCandidateFilter(filter) {
  candidateFilter = filter;
  document.querySelectorAll('#candidateFilterTabs .tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.filter === filter);
  });
  renderCandidates();
}

function sortCandidates(field) {
  if (candidateSort.field === field) {
    candidateSort.dir = candidateSort.dir === 'desc' ? 'asc' : 'desc';
  } else {
    candidateSort.field = field;
    candidateSort.dir = 'desc';
  }
  renderCandidates();
}

function showTab(tab) {
  document.querySelectorAll('.panel-wrap').forEach(p => p.style.display = 'none');
  document.getElementById('panel-' + tab).style.display = 'block';
  document.querySelectorAll('.main-tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.textContent.toLowerCase().includes(tab));
  });
}

// ========== API CALLS ==========
async function startEngine() {
  const mode = document.getElementById('modeSelect').value;
  await fetch('/api/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }) });
}

async function stopEngine() {
  await fetch('/api/stop', { method: 'POST' });
}

async function killAll() {
  if (!confirm('Kill all positions and stop engine?')) return;
  await fetch('/api/kill', { method: 'POST' });
}

async function saveConfig() {
  // TODO: Implement config save
  alert('Config save not yet implemented');
}

async function refreshUniverse() {
  await fetch('/api/universe/refresh', { method: 'POST' });
}

async function saveSymbolOverrides() {
  // TODO: Implement symbol overrides save
  alert('Symbol overrides save not yet implemented');
}
