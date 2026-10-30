// ── HTML escape helper (prevents XSS when interpolating API data into innerHTML) ──
function escHtml(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── Welcome / onboarding modal ───────────────────────────────────────────────
let _wStep = 1;
const _wTotal = 4;
function nextWelcomeStep() {
  if (_wStep >= _wTotal) { dismissWelcome(); return; }
  document.getElementById('wstep-' + _wStep).classList.remove('active');
  document.getElementById('wd-' + _wStep).classList.remove('active');
  _wStep++;
  document.getElementById('wstep-' + _wStep).classList.add('active');
  document.getElementById('wd-' + _wStep).classList.add('active');
  if (_wStep === _wTotal) document.getElementById('wbtnNext').textContent = 'Get started →';
}
function dismissWelcome() {
  const overlay = document.getElementById('welcomeOverlay');
  if (overlay) { overlay.style.opacity = '0'; overlay.style.transition = 'opacity 0.2s'; setTimeout(() => overlay.remove(), 200); }
  fetch('/api/welcome-seen', { method: 'POST', credentials: 'same-origin' }).catch(() => {});
}

// ── TZ helper ──────────────────────────────────────────────────────────────
function _tz() { return window.PEV_TZ || 'America/Chicago'; }

function ctDateStr(d) {
  return d.toLocaleDateString('en-US', { timeZone: _tz(), year:'numeric', month:'2-digit', day:'2-digit' });
}

// ── Render times ───────────────────────────────────────────────────────────
function renderAllTimes() {
  const now = new Date();
  const dp  = now.toLocaleDateString('en-US', { timeZone: _tz(), weekday:'long', month:'long', day:'numeric', year:'numeric' });
  const tp  = now.toLocaleTimeString('en-US', { timeZone: _tz(), hour:'numeric', minute:'2-digit', timeZoneName:'short' });
  const el  = document.getElementById('today-date');
  if (el) el.textContent = dp + ' · ' + tp;

  document.querySelectorAll('[data-commence-fmt]').forEach(el => {
    try {
      const d = new Date(el.dataset.commenceFmt);
      const dp2 = d.toLocaleDateString('en-US', { timeZone: _tz(), weekday:'short', month:'short', day:'numeric' });
      const tp2 = d.toLocaleTimeString('en-US', { timeZone: _tz(), hour:'numeric', minute:'2-digit', timeZoneName:'short' });
      el.textContent = dp2 + ' · ' + tp2;
    } catch(e) { el.textContent = ''; }
  });
}
renderAllTimes();
document.addEventListener('pev:tz-change', () => { renderAllTimes(); applyFilters(); });

// ── Value Score ────────────────────────────────────────────────────────────
function computeValueScore(card) {
  const ev    = parseFloat(card.dataset.ev) || 0;
  const sharp = parseFloat(card.dataset.sharpScore) || -1;
  const hasP  = card.dataset.hasProj === '1';
  const clv   = card.dataset.clvDir || '';
  const evPts    = Math.min(40, ev * 4);
  const sharpPts = sharp < 0 ? 12 : sharp >= 80 ? 30 : sharp >= 65 ? 22 : sharp >= 50 ? 15 : 6;
  const modPts   = hasP ? 20 : 0;
  const clvPts   = clv === 'pos' ? 10 : clv === 'neg' ? 0 : 5;
  return Math.min(100, Math.round(evPts + sharpPts + modPts + clvPts));
}

// ── Countdown badges ────────────────────────────────────────────────────────
function _updateCountdowns() {
  document.querySelectorAll('.cdwn-badge[data-cdwn]').forEach(el => {
    const ms = new Date(el.dataset.cdwn) - Date.now();
    if (ms <= 0) { el.style.display = 'none'; return; }
    const h = Math.floor(ms / 3600000);
    const m = Math.floor((ms % 3600000) / 60000);
    if (ms < 900000) {                        // < 15 min
      el.textContent = '🔴 Starting soon';
      el.className = 'cdwn-badge cdwn-soon';
      el.style.display = '';
    } else if (ms < 3600000) {                // < 1 h
      el.textContent = `⏱ ${m}m`;
      el.className = 'cdwn-badge cdwn-close';
      el.style.display = '';
    } else if (h < 24) {                      // within today
      el.textContent = `⏱ ${h}h ${m}m`;
      el.className = 'cdwn-badge cdwn-day';
      el.style.display = '';
    } else {
      el.style.display = 'none';              // > 24h — hide, just show game time
    }
  });
}
const _countdownTimer = setInterval(_updateCountdowns, 30000);
_updateCountdowns();
window.addEventListener('beforeunload', () => clearInterval(_countdownTimer));

// ── Parlay Builder ──────────────────────────────────────────────────────────
let _parlayMode = false;
const _parlayLegs = [];  // {id, name, sub, odds, trueProb, book, game}

function toggleParlayMode() {
  _parlayMode ? exitParlayMode() : enterParlayMode();
}

function enterParlayMode() {
  _parlayMode = true;
  const btn = document.getElementById('parlayModeBtn');
  btn.classList.add('active');
  btn.innerHTML = '🎰 Parlay <span style="font-size:0.6rem;opacity:0.7;margin-left:2px;">ON</span>';
  document.getElementById('cardsGrid').classList.add('parlay-mode');
  document.getElementById('parlayPanel').classList.add('open');
  _renderParlayPanel();
}

function exitParlayMode() {
  _parlayMode = false;
  const btn = document.getElementById('parlayModeBtn');
  btn.classList.remove('active');
  btn.textContent = '🎰 Parlay';
  document.getElementById('cardsGrid').classList.remove('parlay-mode');
  document.getElementById('parlayPanel').classList.remove('open');
  document.querySelectorAll('.bet-card.parlay-selected').forEach(c => c.classList.remove('parlay-selected'));
  _parlayLegs.length = 0;
}

/* ── Parlay math helpers ─────────────────────────────────────── */
function _toDecimal(american) {
  return american >= 0 ? american / 100 + 1 : 100 / Math.abs(american) + 1;
}
function _toAmerican(decimal) {
  if (decimal >= 2) return Math.round((decimal - 1) * 100);
  return Math.round(-100 / (decimal - 1));
}
function _computeParlayStats() {
  const n = _parlayLegs.length;
  if (n < 2) return null;
  let dec = 1, prob = 1;
  _parlayLegs.forEach(l => { dec *= _toDecimal(l.odds); prob *= l.trueProb; });
  const evPct = (prob * dec - 1) * 100;
  const american = _toAmerican(dec);
  // Fractional Kelly: f* = (p*b - q) / b  where b = dec - 1 net odds, then × 0.25
  const b = dec - 1;
  const rawKelly = b > 0 ? (prob * dec - 1) / b : 0;
  const fracKelly = Math.max(0, rawKelly * 0.25);
  return { n, dec, prob, evPct, american, fracKelly };
}

/* ── Card click → toggle parlay leg ─────────────────────────── */
function _getLegId(card) {
  return card.dataset.betId || null;
}

function _onBetCardClick(e, card) {
  if (!_parlayMode) return;
  // Don't intercept clicks on interactive inner elements
  if (e.target.closest('button, a, input, label, select, textarea, .clv-popover')) return;
  _toggleParlayCard(card);
}

function _toggleParlayCard(card) {
  const id  = _getLegId(card);
  if (!id) return;
  const idx = _parlayLegs.findIndex(l => l.id === id);
  if (idx !== -1) {
    // Already in parlay — remove it
    _parlayLegs.splice(idx, 1);
    card.classList.remove('parlay-selected');
  } else {
    // Add it
    if (_parlayLegs.length >= 5) {
      const w = document.getElementById('parlayWarn');
      w.textContent = '⚠️ Max 5 legs — remove a leg first.';
      w.classList.remove('hidden');
      setTimeout(() => w.classList.add('hidden'), 2800);
      return;
    }
    const isProp   = card.dataset.isProp === 'true';
    const player   = card.dataset.playerName || '';
    const team     = card.dataset.team || '';
    const game     = card.dataset.game || '';
    const mkt      = card.dataset.marketDisplay || '';
    const odds     = parseInt(card.dataset.odds, 10);
    const trueProb = parseFloat(card.dataset.trueProb);
    const book     = card.dataset.book || '';
    const name     = isProp && player ? `${player} — ${team}` : team;
    const sub      = [mkt, game || '', book ? book.charAt(0).toUpperCase() + book.slice(1) : ''].filter(Boolean).join(' · ');
    _parlayLegs.push({ id, name, sub, odds, trueProb, book, game });
    card.classList.add('parlay-selected');
  }
  _renderParlayPanel();
}

/* ── Remove a single leg (called from ✕ in panel) ───────────── */
function removeParlayLeg(id) {
  const idx = _parlayLegs.findIndex(l => l.id === id);
  if (idx === -1) return;
  _parlayLegs.splice(idx, 1);
  const card = document.getElementById('card-' + id);
  if (card) card.classList.remove('parlay-selected');
  _renderParlayPanel();
}

function clearParlayLegs() {
  _parlayLegs.length = 0;
  document.querySelectorAll('.bet-card.parlay-selected').forEach(c => c.classList.remove('parlay-selected'));
  _renderParlayPanel();
}

/* ── Render the panel UI ─────────────────────────────────────── */
function _renderParlayPanel() {
  const n        = _parlayLegs.length;
  const badgeEl  = document.getElementById('parlayLegBadge');
  const emptyEl  = document.getElementById('parlayEmpty');
  const legsEl   = document.getElementById('parlayLegs');
  const oddsEl   = document.getElementById('pl-odds');
  const probEl   = document.getElementById('pl-prob');
  const evEl     = document.getElementById('pl-ev');
  const kellyEl  = document.getElementById('pl-kelly');
  const payoutEl = document.getElementById('pl-payout');
  const warnEl   = document.getElementById('parlayWarn');

  badgeEl.textContent = n === 1 ? '1 leg' : `${n} legs`;

  // Legs list
  if (n === 0) {
    emptyEl.style.display = '';
    legsEl.style.display  = 'none';
  } else {
    emptyEl.style.display = 'none';
    legsEl.style.display  = '';
    legsEl.innerHTML = _parlayLegs.map((leg, i) => {
      const oddsStr = leg.odds >= 0 ? `+${leg.odds}` : String(leg.odds);
      const odsCls  = leg.odds >= 0 ? 'pos' : 'neg';
      const safeName = leg.name.replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const safeSub  = leg.sub.replace(/</g,'&lt;').replace(/>/g,'&gt;');
      return `<div class="pl-leg" id="pl-leg-${leg.id}">
        <div class="pl-leg-num">${i + 1}</div>
        <div class="pl-leg-info">
          <div class="pl-leg-name">${safeName}</div>
          <div class="pl-leg-sub">${safeSub}</div>
        </div>
        <div class="pl-leg-odds ${odsCls}">${oddsStr}</div>
        <button class="pl-leg-del" onclick="removeParlayLeg('${leg.id}')" title="Remove leg">✕</button>
      </div>`;
    }).join('');
  }

  // Stats cells
  const stats = _computeParlayStats();
  if (!stats) {
    [oddsEl, probEl, evEl, kellyEl].forEach(el => {
      el.textContent = '—';
      el.className   = 'ps-val';
    });
    if (payoutEl) { payoutEl.textContent = '—'; payoutEl.title = ''; }
  } else {
    const oddsStr = stats.american >= 0 ? `+${stats.american}` : String(stats.american);
    oddsEl.textContent = oddsStr;
    oddsEl.className   = 'ps-val ' + (stats.american >= 0 ? 'ps-green' : '');

    probEl.textContent = `${(stats.prob * 100).toFixed(1)}%`;
    probEl.className   = 'ps-val';

    const evSign = stats.evPct >= 0 ? '+' : '';
    evEl.textContent = `${evSign}${stats.evPct.toFixed(1)}%`;
    evEl.className   = 'ps-val ' + (stats.evPct >= 2 ? 'ps-green' : stats.evPct >= 0 ? 'ps-orange' : 'ps-red');

    const _bInput = document.getElementById('bankrollInput');
    const bankroll = parseFloat(_bInput ? _bInput.value : '') || 0;
    if (bankroll > 0 && stats.fracKelly > 0) {
      const dollar = Math.round(stats.fracKelly * bankroll);
      kellyEl.textContent = `$${dollar}`;
      kellyEl.title = `${(stats.fracKelly * 100).toFixed(1)}% of bankroll`;
    } else {
      kellyEl.textContent = stats.fracKelly > 0 ? `${(stats.fracKelly * 100).toFixed(1)}%` : '0%';
      kellyEl.title = '';
    }
    kellyEl.className = 'ps-val ' + (stats.fracKelly > 0 ? 'ps-green' : 'ps-orange');

    // Payout = wager × decimal odds (includes stake return)
    const wagerEl = document.getElementById('parlayWagerInput');
    const wager = parseFloat(wagerEl ? wagerEl.value : '') || 0;
    if (payoutEl) {
      if (wager > 0) {
        const payout = wager * stats.dec;
        const profit = payout - wager;
        payoutEl.textContent = `$${payout.toFixed(2)}`;
        payoutEl.title = `Profit: $${profit.toFixed(2)} on $${wager} wager`;
      } else {
        payoutEl.textContent = '—';
        payoutEl.title = '';
      }
    }
  }

  // Warnings
  warnEl.classList.add('hidden');
  const warnings = [];
  if (n === 1) {
    warnings.push('Add at least 1 more leg (minimum 2).');
  } else if (stats) {
    // Same-game parlay detection
    const games = _parlayLegs.map(l => l.game).filter(Boolean);
    const gameFreq = {};
    games.forEach(g => { gameFreq[g] = (gameFreq[g] || 0) + 1; });
    const hasSGP = Object.values(gameFreq).some(v => v > 1);
    if (hasSGP) warnings.push('⚠️ Same-game legs detected — correlated outcomes lower actual EV.');
    if (stats.evPct < 0) warnings.push('⚠️ This parlay is −EV. Try removing the lowest-EV leg.');
    else if (stats.fracKelly === 0 && stats.evPct >= 0) warnings.push('⚠️ Kelly says pass — marginal edge after combining.');
  }
  if (warnings.length) {
    warnEl.textContent = warnings[0];
    warnEl.classList.remove('hidden');
  }
}

// ── Filter state ───────────────────────────────────────────────────────────
// (prop type chips are built after filter state + helpers are defined — see _buildPropTypeChips call below)
let activeLeague = 'all', activeMarket = 'all';
let activeBooks  = new Set();   // empty = All; multi-select (individual books + group presets)
let activeIsProp = 'all', activeDay = 'today';  // default to today; 'all' | 'today' | 'tomorrow'
let activePropMarket = 'all';                 // prop sub-type filter
let activeSort   = 'ev_desc', activeMinEv = 0, activeSearch = '';
let activeCLVDir = 'all';
let activeMinEdge = 0;  // model edge filter: true_prob% − book_implied% ≥ threshold
let activeMultiBook = false;  // when true: show only cards that have a price comparison strip
let _isCompact   = localStorage.getItem('positplusev_compact') === '1';

// ── Apply compact ──────────────────────────────────────────────────────────
function _applyCompact() {
  const grid = document.getElementById('cardsGrid');
  const btn  = document.getElementById('compactToggle');
  if (!grid) return;
  grid.classList.toggle('compact-mode', _isCompact);
  if (btn) btn.style.color = _isCompact ? 'var(--d-green)' : '';
}
function toggleCompact() {
  _isCompact = !_isCompact;
  localStorage.setItem('positplusev_compact', _isCompact ? '1' : '0');
  _applyCompact();
}
// On mobile the compact toggle button is hidden, so if compact mode was
// saved from a desktop session, clear it so the analyze panel is accessible.
if (window.innerWidth < 768 && _isCompact) {
  _isCompact = false;
  localStorage.setItem('positplusev_compact', '0');
}
_applyCompact();

// ── View: card / table ─────────────────────────────────────────────────────
let _viewMode = 'card';
function setView(mode) {
  _viewMode = mode;
  document.getElementById('cardsGrid').style.display = mode === 'card' ? '' : 'none';
  document.getElementById('tableView').style.display = mode === 'table' ? 'block' : 'none';
  document.getElementById('viewCard').classList.toggle('active', mode === 'card');
  document.getElementById('viewTable').classList.toggle('active', mode === 'table');
  if (mode === 'table') rebuildTable();
}

function rebuildTable() {
  const tbody = document.getElementById('tableBody');
  if (!tbody) return;
  const cards = Array.from(document.querySelectorAll('.bet-card')).filter(c => c.style.display !== 'none');
  tbody.innerHTML = cards.map(c => {
    const ev    = parseFloat(c.dataset.ev);
    const odds  = parseInt(c.dataset.odds);
    const prob  = parseFloat(c.dataset.trueProb);
    const oddsStr = odds > 0 ? '+' + odds : String(odds);
    const tierCls = ev >= 5 ? 'ev-high' : 'ev-med';
    const score = computeValueScore(c);
    let vsCls = 'vs-weak';
    if (score >= 80) vsCls = 'vs-elite'; else if (score >= 60) vsCls = 'vs-strong'; else if (score >= 40) vsCls = 'vs-fair';
    const kelly = halfKelly(prob, odds);
    const kellyStr = kelly > 0 ? (kelly * 100).toFixed(1) + '%' : '—';
    const betId = c.id ? c.id.replace('bet-','') : '';
    const team  = c.dataset.team || '—';
    const game  = c.dataset.game || '';
    const _lbadge = c.querySelector('.badge-league');
    const league = _lbadge ? _lbadge.textContent : '';
    const book  = c.dataset.book || '';
    const leagueKey = c.dataset.league || '';
    const bookMap = _BOOK_URLS[book];
    const betUrl  = bookMap ? (bookMap[leagueKey] || bookMap._default || '') : '';
    return `<tr>
      <td><span class="td-ev ${tierCls}">+${ev.toFixed(1)}%</span></td>
      <td>${league}</td>
      <td><div class="td-team">${team}</div><div class="td-game">${game}</div></td>
      <td class="td-odds ${odds > 0 ? 'pos' : ''}">${oddsStr}</td>
      <td style="color:var(--d-muted);font-size:0.7rem;">${book}</td>
      <td class="td-prob">${(prob*100).toFixed(1)}%</td>
      <td><span class="td-vs ${vsCls}">VS ${score}</span></td>
      <td class="td-kelly">${kellyStr}</td>
      <td class="td-action">
        <button onclick="scrollToCard(this)" data-game="${game}" data-team="${team}">View →</button>
        ${betUrl ? `<button class="td-bet-btn" onclick="window.open('${betUrl}','_blank','noopener,noreferrer')">Bet →</button>` : ''}
      </td>
    </tr>`;
  }).join('');
}

// ── Table column sort ──────────────────────────────────────────────────────
const _colAsc = {};   // tracks asc/desc state per column
function sortByCol(col) {
  _colAsc[col] = !_colAsc[col];
  const asc  = _colAsc[col];
  const grid = document.getElementById('cardsGrid');
  if (!grid) return;
  const cards = Array.from(grid.querySelectorAll('.bet-card'));
  cards.sort((a, b) => {
    let av, bv;
    switch (col) {
      case 'ev':
        av = parseFloat(a.dataset.ev);     bv = parseFloat(b.dataset.ev);     break;
      case 'odds':
        av = parseInt(a.dataset.odds, 10); bv = parseInt(b.dataset.odds, 10); break;
      case 'prob':
        av = parseFloat(a.dataset.trueProb); bv = parseFloat(b.dataset.trueProb); break;
      case 'vs':
        av = computeValueScore(a); bv = computeValueScore(b); break;
      case 'league':
        return asc
          ? (a.dataset.league || '').localeCompare(b.dataset.league || '')
          : (b.dataset.league || '').localeCompare(a.dataset.league || '');
      default: return 0;
    }
    return asc ? av - bv : bv - av;
  });
  cards.forEach(c => grid.appendChild(c));
  // Update header arrows to show active column and direction
  document.querySelectorAll('.ev-table th[onclick]').forEach(th => {
    const c = th.getAttribute('onclick').replace("sortByCol('", '').replace("')", '');
    th.style.color = c === col ? 'var(--d-green)' : '';
  });
  rebuildTable();
}

function scrollToCard(btn) {
  setView('card');
  const team = btn.dataset.team;
  const card = Array.from(document.querySelectorAll('.bet-card')).find(c => c.dataset.team === team);
  if (card) { card.scrollIntoView({ behavior: 'smooth', block: 'center' }); card.classList.add('flash'); setTimeout(() => card.classList.remove('flash'), 800); }
}

// ── Kelly Fraction ─────────────────────────────────────────────────────────
const KELLY_FRAC_KEY = 'positplusev_kelly_frac';

function _getKellyFrac() {
  return parseFloat(localStorage.getItem(KELLY_FRAC_KEY) || '0.25');
}

function _kellyFracLabel(frac) {
  if (frac >= 1)     return 'Full Kelly';
  if (frac >= 0.5)   return '½ Kelly';
  if (frac >= 0.25)  return '¼ Kelly';
  return '⅛ Kelly';
}

function _setKellyFrac(frac) {
  localStorage.setItem(KELLY_FRAC_KEY, String(frac));
  ['kellyFracSelect', 'kellyFracSelectMobile'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = frac;
  });
  // Update all static label spans
  const label = _kellyFracLabel(frac);
  document.querySelectorAll('.kelly-size-label').forEach(el => { el.textContent = label; });
  const hintLabel = document.getElementById('kellyHintLabel');
  if (hintLabel) hintLabel.textContent = label;
  updateKellyDisplays();
}

function openHelpModal() {
  document.getElementById('helpModalOverlay').classList.add('open');
  document.body.style.overflow = 'hidden';
}
function closeHelpModal(e) {
  if (e && e.target !== document.getElementById('helpModalOverlay')) return;
  document.getElementById('helpModalOverlay').classList.remove('open');
  document.body.style.overflow = '';
}
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeHelpModal();
});

function openKellyInfo() {
  document.getElementById('kellyModal').style.display = 'block';
  document.getElementById('kellyModalOverlay').style.display = 'block';
}
function closeKellyInfo() {
  document.getElementById('kellyModal').style.display = 'none';
  document.getElementById('kellyModalOverlay').style.display = 'none';
}

// ── Kelly calc ─────────────────────────────────────────────────────────────
// Uses stored fraction (default ¼). Function name kept for DOM compatibility.
function halfKelly(prob, americanOdds) {
  const frac = _getKellyFrac();
  const odds = parseInt(americanOdds, 10);
  const b = odds > 0 ? odds / 100 : 100 / Math.abs(odds);
  const k = (prob * b - (1 - prob)) / b;
  return Math.max(0, k * frac);
}

// ── Bankroll ───────────────────────────────────────────────────────────────
const bankrollInput = document.getElementById('bankrollInput');
const bankrollHint  = document.getElementById('bankrollHint');
if (bankrollInput) {
  const _saved = localStorage.getItem('positplusev_bankroll');
  if (_saved) bankrollInput.value = _saved;
}

function updateKellyDisplays() {
  const bankroll = parseFloat(bankrollInput ? bankrollInput.value : '0');
  const hasBr    = bankroll > 0;
  const frac     = _getKellyFrac();
  const label    = _kellyFracLabel(frac);
  document.querySelectorAll('.kelly-val').forEach(el => {
    const pct    = halfKelly(parseFloat(el.dataset.prob), el.dataset.odds);
    const dolEl  = el.nextElementSibling;
    el.textContent = pct > 0 ? (pct * 100).toFixed(1) + '%' : '—';
    if (dolEl && dolEl.classList.contains('kelly-dollar')) {
      if (hasBr && pct > 0) { dolEl.textContent = '$' + Math.round(bankroll * pct).toLocaleString(); dolEl.style.display = 'block'; }
      else dolEl.style.display = 'none';
    }
  });
  // Update dynamic labels
  document.querySelectorAll('.kelly-size-label').forEach(el => { el.textContent = label; });
  const kellyTh = document.getElementById('kellyTh');
  if (kellyTh) kellyTh.textContent = label;
  const hintLabel = document.getElementById('kellyHintLabel');
  if (hintLabel) hintLabel.textContent = label;
  if (bankrollHint) bankrollHint.textContent = hasBr ? '→ unit sizes shown in green' : `Enter → see ${label} on each pick`;
}
if (bankrollInput) {
  bankrollInput.addEventListener('input', () => {
    const v = bankrollInput.value;
    v && parseFloat(v) > 0 ? localStorage.setItem('positplusev_bankroll', v) : localStorage.removeItem('positplusev_bankroll');
    updateKellyDisplays();
  });
}

// ── Kelly selector init ─────────────────────────────────────────────────────
(function initKellySelectors() {
  const savedFrac = _getKellyFrac();
  ['kellyFracSelect', 'kellyFracSelectMobile'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = savedFrac;
    el.addEventListener('change', () => _setKellyFrac(parseFloat(el.value)));
  });
  // Set initial labels
  const label = _kellyFracLabel(savedFrac);
  document.querySelectorAll('.kelly-size-label').forEach(el => { el.textContent = label; });
  const kellyTh = document.getElementById('kellyTh');
  if (kellyTh) kellyTh.textContent = label;
  const hintLabel = document.getElementById('kellyHintLabel');
  if (hintLabel) hintLabel.textContent = label;
})();

updateKellyDisplays();

// ── Sort ───────────────────────────────────────────────────────────────────
function sortCards() {
  const grid  = document.getElementById('cardsGrid');
  if (!grid) return;
  const cards = Array.from(grid.querySelectorAll('.bet-card'));
  const nflFirst = activeLeague === 'all';
  cards.sort((a, b) => {
    // In "All Picks" view, NFL cards float to the top as a group
    if (nflFirst) {
      const aNfl = a.dataset.league === 'americanfootball_nfl' ? 0 : 1;
      const bNfl = b.dataset.league === 'americanfootball_nfl' ? 0 : 1;
      if (aNfl !== bNfl) return aNfl - bNfl;
    }
    switch (activeSort) {
      case 'ev_asc':   return parseFloat(a.dataset.ev) - parseFloat(b.dataset.ev);
      case 'time_asc': { const ta = a.dataset.commence ? new Date(a.dataset.commence).getTime() : Infinity; const tb = b.dataset.commence ? new Date(b.dataset.commence).getTime() : Infinity; return ta - tb; }
      case 'odds_best':return parseInt(b.dataset.odds,10) - parseInt(a.dataset.odds,10);
      default:         return parseFloat(b.dataset.ev) - parseFloat(a.dataset.ev);
    }
  });
  cards.forEach(c => grid.appendChild(c));
  // Ribbons track sort order — reassign after reorder
  _updateTopPickRibbon();
}
document.getElementById('sortSelect').addEventListener('change', function() { activeSort = this.value; sortCards(); });

// ── Faceted filter counts ──────────────────────────────────────────────────
// Returns how many cards would be visible if [dimension] were set to [value]
// while all other active filters remain the same.
function _countFor(dimension, value) {
  const todayStr    = ctDateStr(new Date());
  const tomorrowStr = ctDateStr(new Date(Date.now() + 86400000));
  let count = 0;
  document.querySelectorAll('.bet-card').forEach(card => {
    const ip   = dimension === 'is_prop'     ? value : activeIsProp;
    const mkt  = dimension === 'market'      ? value : activeMarket;
    const pm   = dimension === 'prop_market' ? value : activePropMarket;
    const clv  = dimension === 'clvdir'      ? value : activeCLVDir;
    const lg   = dimension === 'league'      ? value : activeLeague;

    const leagueOk = lg === 'all' || (lg === 'soccer' && card.dataset.league.startsWith('soccer_')) || card.dataset.league === lg;
    const marketOk = mkt === 'all' || card.dataset.market === mkt;
    const bookOk   = activeBooks.size === 0 || activeBooks.has(card.dataset.book || '');
    const clvDirOk = clv === 'all' || (card.dataset.clvDir || '') === clv;
    const propOk   = ip === 'all' || card.dataset.isProp === ip;
    const propMktOk= pm === 'all' || card.dataset.propMarket === pm;
    const evOk       = parseFloat(card.dataset.ev) >= activeMinEv;
    const edgeCOk   = parseFloat(card.dataset.edge || '0') >= activeMinEdge;
    const mbOk      = !activeMultiBook || card.dataset.hasCompare === '1';
    const srchOk   = !activeSearch ||
      (card.dataset.team   || '').toLowerCase().includes(activeSearch) ||
      (card.dataset.game   || '').toLowerCase().includes(activeSearch) ||
      (card.dataset.playerName || '').toLowerCase().includes(activeSearch);
    let dayOk = true;
    const cIso = card.dataset.commence;
    if (cIso) {
      const cDate = new Date(cIso);
      const cd    = ctDateStr(cDate);
      const _dnum = s => { const [m,d,y] = s.split('/'); return y*10000 + parseInt(m)*100 + parseInt(d); };
      // Always hide games that have already started, regardless of tab
      if (cDate <= new Date()) { dayOk = false; }
      else if (activeDay === 'today')    { dayOk = cd === todayStr; }
      else if (activeDay === 'tomorrow') { dayOk = cd === tomorrowStr; }
      else if (activeDay === 'all')      { dayOk = _dnum(cd) >= _dnum(todayStr); }
    }
    if (leagueOk && marketOk && bookOk && clvDirOk && propOk && propMktOk && evOk && edgeCOk && mbOk && srchOk && dayOk) count++;
  });
  return count;
}

function updateChipCounts() {
  // Bet Type
  ['all','false','true'].forEach(val => {
    const n = _countFor('is_prop', val);
    document.querySelectorAll(`.qfilter-btn[data-filter="is_prop"][data-value="${val}"] .chip-count`).forEach(el => {
      el.textContent = n > 0 ? n : '';
    });
  });
  // Market
  ['all','h2h','spreads','totals'].forEach(val => {
    const n = _countFor('market', val);
    document.querySelectorAll(`.qfilter-btn[data-filter="market"][data-value="${val}"] .chip-count`).forEach(el => {
      el.textContent = n > 0 ? n : '';
    });
  });
  // CLV direction
  ['all','pos','neg'].forEach(val => {
    const n = _countFor('clvdir', val);
    document.querySelectorAll(`.qfilter-btn[data-filter="clvdir"][data-value="${val}"] .chip-count`).forEach(el => {
      el.textContent = n > 0 ? n : '';
    });
  });
  // Prop market sub-types
  document.querySelectorAll('.qfilter-btn[data-filter="prop-market"]').forEach(btn => {
    const n = _countFor('prop_market', btn.dataset.value);
    const el = btn.querySelector('.chip-count');
    if (el) el.textContent = n > 0 ? n : '';
  });
}

function updateSportPillCounts() {
  const allCards = document.querySelectorAll('.bet-card');
  const counts = { all: 0, nfl: 0, ncaaf: 0, nhl: 0, nba: 0, mlb: 0, soccer: 0, props: 0, sharp: 0, div: 0 };
  allCards.forEach(card => {
    if (card.style.display === 'none') return;
    const league = card.dataset.league || '';
    const isProp = card.dataset.prop === '1';
    counts.all++;
    if (league === 'americanfootball_nfl') counts.nfl++;
    else if (league === 'americanfootball_ncaaf') counts.ncaaf++;
    else if (league === 'icehockey_nhl') counts.nhl++;
    else if (league === 'basketball_nba') counts.nba++;
    else if (league === 'baseball_mlb') counts.mlb++;
    else if (league.includes('soccer')) counts.soccer++;
    if (isProp) counts.props++;
    if (parseFloat(card.dataset.sharpScore || '0') >= 70) counts.sharp++;
    if (parseFloat(card.dataset.divergence || '0') >= 3) counts.div++;
  });
  const midCards = document.querySelectorAll('.middle-card');
  const hrCards  = document.querySelectorAll('[data-hr-model="1"]');
  const idMap = {
    all:'pc-all', nfl:'pc-nfl', ncaaf:'pc-ncaaf', nhl:'pc-nhl',
    nba:'pc-nba', mlb:'pc-mlb', soccer:'pc-soccer',
    props:'pc-props', sharp:'pc-sharp', div:'pc-div'
  };
  Object.entries(counts).forEach(([key, count]) => {
    const el = document.getElementById(idMap[key]);
    if (el) el.textContent = count > 0 ? count : '';
  });
  const hrEl  = document.getElementById('pc-hr');
  const midEl = document.getElementById('pc-mid');
  if (hrEl)  hrEl.textContent  = hrCards.length  > 0 ? hrCards.length  : '';
  if (midEl) midEl.textContent = midCards.length > 0 ? midCards.length : '';
}

function updateClearBtn() {
  const btn = document.getElementById('clearAllBtn');
  if (!btn) return;
  const hasFilter = (
    activeLeague !== 'all' || activeIsProp !== 'all' || activeMarket !== 'all' ||
    activeCLVDir !== 'all' || activeMinEv > 0 || activeMinEdge > 0 ||
    activeMultiBook || activeBooks.size > 0
  );
  btn.style.display = hasFilter ? 'inline-flex' : 'none';
}

// ── Top Pick ribbon — tracks the first two VISIBLE cards with CLV+ or no CLV ─
function _updateTopPickRibbon() {
  // Remove any existing ribbon divs
  document.querySelectorAll('.card-ribbon').forEach(r => r.remove());
  // Eligible: visible cards that do NOT have confirmed negative CLV
  const eligible = Array.from(document.querySelectorAll('.bet-card'))
    .filter(c => c.style.display !== 'none' && c.dataset.clvDir !== 'neg');
  if (eligible[0]) {
    const r1 = document.createElement('div');
    r1.className = 'card-ribbon ribbon-top1';
    r1.textContent = '🏆 Top Pick — Highest Model Edge';
    eligible[0].insertBefore(r1, eligible[0].firstChild);
  }
  if (eligible[1]) {
    const r2 = document.createElement('div');
    r2.className = 'card-ribbon ribbon-top2';
    r2.textContent = '✅ Strong Pick — High Model Edge';
    eligible[1].insertBefore(r2, eligible[1].firstChild);
  }
}

// ── Apply filters ──────────────────────────────────────────────────────────
function applyFilters() {
  const todayStr    = ctDateStr(new Date());
  const tomorrowStr = ctDateStr(new Date(Date.now() + 86400000));
  const cards       = document.querySelectorAll('.bet-card');
  let visible = 0;
  const countByDay  = { all: 0, today: 0, tomorrow: 0 };

  cards.forEach(card => {
    const leagueOk =
      activeLeague === 'all' ||
      (activeLeague === 'soccer' && card.dataset.league.startsWith('soccer_')) ||
      card.dataset.league === activeLeague;
    const marketOk = activeMarket === 'all' || card.dataset.market === activeMarket;
    const bookOk   = activeBooks.size === 0 || activeBooks.has(card.dataset.book || '');
    const clvDirOk = activeCLVDir === 'all' || (card.dataset.clvDir || '') === activeCLVDir;
    const propOk       = activeIsProp === 'all' || card.dataset.isProp === activeIsProp;
    const propMarketOk = activePropMarket === 'all' || card.dataset.propMarket === activePropMarket;
    const evOk         = parseFloat(card.dataset.ev) >= activeMinEv;
    const edgeOk       = parseFloat(card.dataset.edge || '0') >= activeMinEdge;
    const multiBookOk  = !activeMultiBook || card.dataset.hasCompare === '1';
    const notHrProp    = card.dataset.propMarket !== 'batter_home_runs';
    const sharpFlowOk  = !_sharpFlowActive ||
      (parseFloat(card.dataset.moneyPct || '0') - parseFloat(card.dataset.betPct || '0') >= 15);
    const searchOk = !activeSearch ||
      (card.dataset.team || '').toLowerCase().includes(activeSearch) ||
      (card.dataset.game || '').toLowerCase().includes(activeSearch) ||
      (card.dataset.playerName || '').toLowerCase().includes(activeSearch);

    let dayOk = true;
    const cIso = card.dataset.commence;
    if (cIso) {
      const cDate = new Date(cIso);
      const cd    = ctDateStr(cDate);
      const _dnum = s => { const [m,d,y] = s.split('/'); return y*10000 + parseInt(m)*100 + parseInt(d); };
      // Always hide games that have already started, regardless of tab
      if (cDate <= new Date()) { dayOk = false; }
      else if (activeDay === 'today')    { dayOk = cd === todayStr; }
      else if (activeDay === 'tomorrow') { dayOk = cd === tomorrowStr; }
      else if (activeDay === 'all')      { dayOk = _dnum(cd) >= _dnum(todayStr); }
    }

    const divergenceOk = !_divergenceActive || parseFloat(card.dataset.divergence || '0') >= 3.0;
    const passOther = leagueOk && marketOk && bookOk && clvDirOk && propOk && propMarketOk && evOk && edgeOk && multiBookOk && searchOk && notHrProp && sharpFlowOk && divergenceOk;
    const show = passOther && dayOk;
    card.style.display = show ? '' : 'none';
    if (show) visible++;

    // Day button counts ignore the active day filter so every button always
    // shows how many picks exist for that day given all other active filters.
    // Games that have already started are excluded from all counts.
    if (passOther) {
      if (cIso) {
        const cDate2 = new Date(cIso);
        if (cDate2 > new Date()) {  // only count games that haven't started
          const cd2   = ctDateStr(cDate2);
          const _dn   = s => { const [m,d,y] = s.split('/'); return y*10000 + parseInt(m)*100 + parseInt(d); };
          if (_dn(cd2) >= _dn(todayStr)) countByDay.all++;
          if (cd2 === todayStr)    countByDay.today++;
          if (cd2 === tomorrowStr) countByDay.tomorrow++;
        }
      } else {
        countByDay.all++;  // no commence time — include in upcoming
      }
    }
  });

  // Update counts
  const badge = document.getElementById('stat-total');
  if (badge) badge.textContent = visible;
  ['all', 'today', 'tomorrow'].forEach(d => {
    const el = document.getElementById('sdCount-' + d);
    if (el) el.textContent = countByDay[d];
  });
  const nr = document.getElementById('no-results-filtered');
  if (nr) nr.style.display = (visible === 0 && cards.length > 0) ? '' : 'none';

  // Keep table view in sync with the filtered card set
  if (_viewMode === 'table') rebuildTable();

  // Update count badges on all filter chips
  updateChipCounts();
  updateSportPillCounts();
  updateClearBtn();

  // Reapply prop grouping if active
  if (_propsGrouped && activeIsProp === 'true') _applyPropGrouping();
  else if (!_propsGrouped || activeIsProp !== 'true') _clearPropGroupHeaders();

  // Reapply game grouping if active (games mode or all mode)
  if (_gamesGrouped && activeIsProp !== 'true') _applyGameGrouping();
  else _clearGameGroupHeaders();

  // When divergence mode is active, sort visible cards by divergence score descending
  if (_divergenceActive) {
    const _grid = document.getElementById('cardsGrid');
    if (_grid) {
      const _vis = Array.from(_grid.querySelectorAll('.bet-card')).filter(c => c.style.display !== 'none');
      _vis.sort((a, b) => parseFloat(b.dataset.divergence || '0') - parseFloat(a.dataset.divergence || '0'));
      _vis.forEach(c => _grid.appendChild(c));
    }
  }

  _saveFilters();

  // Ribbons must be re-stamped last, after grouping headers are in place
  _updateTopPickRibbon();
  // Re-init tooltips so any newly visible cards pick up listeners
  if (typeof initTooltips === 'function') initTooltips();
}

// ── HR Model section toggle ────────────────────────────────────────────────
let _hrModelActive = false;
let _sharpFlowActive = false;
let _divergenceActive = false;

function _setHrModel(active) {
  _hrModelActive = active;
  const hrSection  = document.getElementById('hrModelSection');
  const cardsGrid  = document.getElementById('cardsGrid');
  const tableView  = document.getElementById('tableView');
  const hrPill     = document.getElementById('hrModelPill');

  if (active) {
    if (hrSection)  hrSection.style.display  = 'block';
    if (cardsGrid)  cardsGrid.style.display  = 'none';
    if (tableView)  tableView.style.display  = 'none';
    document.querySelectorAll('.sport-pill').forEach(b => b.classList.remove('active'));
    if (hrPill) hrPill.classList.add('active');
    // Hide filter bar sub-controls that don't apply to HR section
    const qfilterRow = document.querySelector('.qfilter-row');
    if (qfilterRow) qfilterRow.style.display = 'none';
  } else {
    if (hrSection) hrSection.style.display = 'none';
    if (cardsGrid) { cardsGrid.style.display = ''; cardsGrid.style.visibility = ''; }
    const qfilterRow = document.querySelector('.qfilter-row');
    if (qfilterRow) qfilterRow.style.display = '';
    if (_viewMode === 'table' && tableView) tableView.style.display = '';
    if (hrPill) hrPill.classList.remove('active');
  }
}

// ── Sport pills ────────────────────────────────────────────────────────────
document.querySelectorAll('.sport-pill').forEach(btn => {
  btn.addEventListener('click', () => {
    const ft = btn.dataset.filter, val = btn.dataset.value;
    if (ft === 'sharp_flow') {
      _sharpFlowActive = !_sharpFlowActive;
      document.getElementById('sharpFlowPill').classList.toggle('active', _sharpFlowActive);
      applyFilters();
      return;
    }
    if (ft === 'divergence') {
      _divergenceActive = !_divergenceActive;
      document.getElementById('divergencePill').classList.toggle('active', _divergenceActive);
      const _dfh = document.getElementById('divergenceFeedHeader');
      if (_dfh) _dfh.classList.toggle('visible', _divergenceActive);
      applyFilters();
      return;
    }
    if (_sharpFlowActive) { _sharpFlowActive = false; document.getElementById('sharpFlowPill').classList.remove('active'); }
    if (_divergenceActive) {
      _divergenceActive = false;
      document.getElementById('divergencePill').classList.remove('active');
      const _dfh2 = document.getElementById('divergenceFeedHeader');
      if (_dfh2) _dfh2.classList.remove('visible');
    }
    if (ft === 'hr_model') {
      // Toggle HR model section
      if (_hrModelActive) {
        _setHrModel(false);
        document.querySelector('.sport-pill[data-filter="league"][data-value="all"]').classList.add('active');
        activeLeague = 'all';
        applyFilters();
      } else {
        _setHrModel(true);
      }
      return;
    }
    // Any other pill: exit HR model mode first
    if (_hrModelActive) _setHrModel(false);
    if (ft === 'is_prop') {
      // Toggle: if already filtering props-only → back to all; else → props-only
      _setIsProp(activeIsProp === 'true' ? 'all' : 'true');
    } else {
      document.querySelectorAll('.sport-pill[data-filter="league"]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeLeague = val;
      // Rebuild prop type chips scoped to the newly selected league
      _buildPropTypeChips();
    }
    applyFilters();
  });
});

// ── Global search ──────────────────────────────────────────────────────────
document.getElementById('globalSearch').addEventListener('input', function() {
  activeSearch = this.value.trim().toLowerCase();
  applyFilters();
});

// ── Sidebar day ────────────────────────────────────────────────────────────
function _setSideDay(day) {
  activeDay = day;
  ['all','today','tomorrow'].forEach(d => {
    const _sd = document.getElementById('sideDay-' + d);
    if (_sd) _sd.classList.toggle('active', d === day);
    const _bn = document.getElementById('bn-' + d);
    if (_bn) _bn.classList.toggle('active', d === day);
  });
  applyFilters();
}

// ── Mobile bottom nav ──────────────────────────────────────────────────────
function mobileDayFilter(day) { _setSideDay(day); }

// ── Book chip visual sync (individual + group chips) ──────────────────────
function _syncBookChipVisuals() {
  // Individual book chips
  document.querySelectorAll('.qfilter-btn[data-filter="book"]').forEach(b => {
    b.classList.toggle('active',
      b.dataset.value === 'all' ? activeBooks.size === 0 : activeBooks.has(b.dataset.value)
    );
  });
  // Group chips: active when ALL books of that type are selected
  document.querySelectorAll('.qfilter-btn[data-filter="booktype"]').forEach(b => {
    const booksOfType = Array.from(
      document.querySelectorAll(`.qfilter-btn[data-filter="book"][data-book-type="${b.dataset.value}"]`)
    ).map(c => c.dataset.value);
    b.classList.toggle('active', booksOfType.length > 0 && booksOfType.every(bk => activeBooks.has(bk)));
  });
}

// ── Prop type chip builder ─────────────────────────────────────────────────
// Friendly display names for known Odds API prop market keys
// Nested label map: league key → { propMarket key → label }
// Keys that appear in multiple sports are kept separate per sport, with no duplicate JS keys.
const _PROP_LABEL_MAP = {
  basketball_nba: {
    player_points:                  '🏀 Points',
    player_rebounds:                '🏀 Rebounds',
    player_assists:                 '🏀 Assists',
    player_threes:                  '🏀 3-Pointers',
    player_blocks:                  '🏀 Blocks',
    player_steals:                  '🏀 Steals',
    player_turnovers:               '🏀 Turnovers',
    player_points_rebounds_assists: '🏀 PRA',
    player_points_rebounds:         '🏀 Pts+Reb',
    player_points_assists:          '🏀 Pts+Ast',
    player_rebounds_assists:        '🏀 Reb+Ast',
    player_double_double:           '🏀 Double-Double',
    player_triple_double:           '🏀 Triple-Double',
    player_first_basket:            '🏀 First Basket',
  },
  baseball_mlb: {
    batter_hits:                    '⚾ Hits',
    batter_home_runs:               '⚾ Home Runs',
    batter_rbis:                    '⚾ RBIs',
    batter_runs_scored:             '⚾ Runs Scored',
    batter_total_bases:             '⚾ Total Bases',
    batter_singles:                 '⚾ Singles',
    batter_doubles:                 '⚾ Doubles',
    batter_triples:                 '⚾ Triples',
    batter_stolen_bases:            '⚾ Stolen Bases',
    batter_strikeouts:              "⚾ Batter K's",
    batter_walks:                   '⚾ Walks',
    batter_hits_runs_rbis:          '⚾ H+R+RBI',
    pitcher_strikeouts:             "⚾ Pitcher K's",
    pitcher_outs:                   '⚾ Outs Recorded',
    pitcher_hits_allowed:           '⚾ Hits Allowed',
    pitcher_walks:                  '⚾ Pitcher Walks',
    pitcher_earned_runs:            '⚾ Earned Runs',
    pitcher_strikeouts_over_under:  "⚾ Pitcher K's O/U",
    player_hits:                    '⚾ Hits',         // alternate key
    player_home_runs:               '⚾ Home Runs',
    player_strikeouts:              "⚾ Pitcher K's",
    player_total_bases:             '⚾ Total Bases',
  },
  icehockey_nhl: {
    player_shots_on_goal:           '🏒 Shots on Goal',
    player_goals:                   '🏒 Goals',
    player_assists:                 '🏒 Assists',
    player_points:                  '🏒 Points',
    player_saves:                   '🥅 Saves',
    player_time_on_ice:             '🏒 Time on Ice',
    player_blocked_shots:           '🏒 Blocked Shots',
  },
  // Soccer: covers any league key starting with 'soccer_'
  soccer: {
    player_shots:                   '⚽ Shots',
    player_shots_on_target:         '⚽ Shots on Target',
    player_goals:                   '⚽ Goals',
    player_assists:                 '⚽ Assists',
    player_anytime_score:           '⚽ Anytime Scorer',
    player_first_goal_scorer:       '⚽ First Scorer',
    player_last_goal_scorer:        '⚽ Last Scorer',
    player_cards:                   '⚽ Cards',
    player_tackles:                 '⚽ Tackles',
    player_passes:                  '⚽ Passes',
  },
};

/** Look up the display label for a market key given a specific league. */
function _propLabel(league, marketKey) {
  const exact = _PROP_LABEL_MAP[league] ? _PROP_LABEL_MAP[league][marketKey] : undefined;
  if (exact) return exact;
  if (league.startsWith('soccer_')) {
    const soc = _PROP_LABEL_MAP.soccer ? _PROP_LABEL_MAP.soccer[marketKey] : undefined;
    if (soc) return soc;
  }
  // Fallback: title-case the raw key (strip player_ / batter_ / pitcher_ prefix)
  return marketKey
    .replace(/^(player_|batter_|pitcher_)/, '')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, ch => ch.toUpperCase());
}

function _buildPropTypeChips() {
  // Collect prop market keys scoped to the active league.
  // Map key: propMarket value. Map value: { label, count }.
  // When activeLeague === 'all', shared keys (e.g. player_points across NBA+NHL)
  // get grouped under a single chip — label comes from the first league seen.
  const markets = new Map(); // propMarket key → label string
  document.querySelectorAll('.bet-card[data-is-prop="true"]').forEach(c => {
    const lg = c.dataset.league || '';
    const lgOk = activeLeague === 'all'
      || (activeLeague === 'soccer' && lg.startsWith('soccer_'))
      || lg === activeLeague;
    if (!lgOk) return;
    const m = c.dataset.propMarket;
    if (!m || markets.has(m)) return;
    markets.set(m, _propLabel(lg, m));
  });

  // Sort: put "All Props" first, then alphabetically by label
  const sorted = [...markets.entries()].sort((a, b) => a[1].localeCompare(b[1]));

  // If current activePropMarket no longer exists in new market set, reset to 'all'
  if (activePropMarket !== 'all' && !markets.has(activePropMarket)) {
    activePropMarket = 'all';
  }

  ['propTypeSection','mfPropTypeSection'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = '';

    // "All Props" button
    const allBtn = document.createElement('button');
    allBtn.className = 'qfilter-btn' + (activePropMarket === 'all' ? ' active' : '');
    allBtn.dataset.filter = 'prop-market';
    allBtn.dataset.value  = 'all';
    allBtn.textContent    = 'All Props';
    const allCc = document.createElement('span');
    allCc.className = 'chip-count';
    allBtn.appendChild(allCc);
    el.appendChild(allBtn);

    sorted.forEach(([key, label]) => {
      const btn     = document.createElement('button');
      btn.className = 'qfilter-btn' + (activePropMarket === key ? ' active' : '');
      btn.dataset.filter = 'prop-market';
      btn.dataset.value  = key;
      btn.textContent    = label;
      const cc = document.createElement('span');
      cc.className = 'chip-count';
      btn.appendChild(cc);
      el.appendChild(btn);
    });

    el.querySelectorAll('.qfilter-btn[data-filter="prop-market"]').forEach(btn => {
      btn.addEventListener('click', () => {
        activePropMarket = btn.dataset.value;
        document.querySelectorAll('.qfilter-btn[data-filter="prop-market"]').forEach(b =>
          b.classList.toggle('active', b.dataset.value === activePropMarket)
        );
        applyFilters();
        updateFilterBadge();
      });
    });
  });
}

// ── isProp unified setter ──────────────────────────────────────────────────
function _setIsProp(val) {
  activeIsProp = val;
  // sport-pill: active only when showing props-only
  document.querySelectorAll('.sport-pill[data-filter="is_prop"]').forEach(b =>
    b.classList.toggle('active', activeIsProp === 'true')
  );
  // sidebar + mobile sheet chips: match exact value
  document.querySelectorAll('.qfilter-btn[data-filter="is_prop"]').forEach(b =>
    b.classList.toggle('active', b.dataset.value === activeIsProp)
  );
  // show/hide prop type sub-sections (only when Props selected)
  const showProps = activeIsProp === 'true';
  if (showProps) _buildPropTypeChips(); // rebuild for current active league before revealing
  ['propTypeWrap','mfPropTypeWrap'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = showProps ? '' : 'none';
  });
  // show/hide game markets section (only when Games or All selected)
  const showGameMkts = activeIsProp !== 'true';
  ['gameMarketsWrap','mfGameMarketsWrap'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = showGameMkts ? '' : 'none';
  });
  // reset market filter when switching to props-only
  if (!showGameMkts) {
    activeMarket = 'all';
    document.querySelectorAll('.qfilter-btn[data-filter="market"]').forEach(b =>
      b.classList.toggle('active', b.dataset.value === 'all')
    );
  }
  // reset prop market filter when switching away from props
  if (!showProps) {
    activePropMarket = 'all';
    document.querySelectorAll('.qfilter-btn[data-filter="prop-market"]').forEach(b =>
      b.classList.toggle('active', b.dataset.value === 'all')
    );
  }
  // Show/hide group toggles based on mode
  const propsGroupBtn = document.getElementById('propsGroupToggle');
  const gamesGroupBtn = document.getElementById('gamesGroupToggle');
  const showGames = (val === 'false' || val === 'all');
  if (propsGroupBtn) propsGroupBtn.classList.toggle('visible', showProps);
  if (gamesGroupBtn) gamesGroupBtn.classList.toggle('visible', showGames);
  // Apply or remove grid props-mode class
  const grid = document.getElementById('cardsGrid');
  if (grid) grid.classList.toggle('props-mode', showProps);
  // Remove headers if leaving a mode
  if (!showProps) { _clearPropGroupHeaders(); _propsGrouped = false; if (propsGroupBtn) propsGroupBtn.classList.remove('active'); }
  if (!showGames) { _clearGameGroupHeaders(); _gamesGrouped = false; if (gamesGroupBtn) gamesGroupBtn.classList.remove('active'); }
  // Re-apply grouping if still in the right mode
  if (showProps && _propsGrouped) _applyPropGrouping();
  if (showGames && _gamesGrouped) _applyGameGrouping();
}

// ── Props grouping by player ───────────────────────────────────────────────
let _propsGrouped = false;

function _clearPropGroupHeaders() {
  document.querySelectorAll('.prop-player-hdr').forEach(el => el.remove());
}

function _applyPropGrouping() {
  const grid = document.getElementById('cardsGrid');
  if (!grid) return;
  _clearPropGroupHeaders();
  if (!_propsGrouped) return;

  // Collect visible prop cards
  const propCards = Array.from(grid.querySelectorAll('.bet-card[data-is-prop="true"]'))
    .filter(c => c.style.display !== 'none');
  if (!propCards.length) return;

  // Sort by player name, then EV desc
  propCards.sort((a, b) => {
    const pA = (a.dataset.playerName || '').toLowerCase();
    const pB = (b.dataset.playerName || '').toLowerCase();
    if (pA !== pB) return pA.localeCompare(pB);
    return parseFloat(b.dataset.ev) - parseFloat(a.dataset.ev);
  });

  // Group cards by player name first
  const groups = [];  // [{player, cards: [...]}]
  propCards.forEach(card => {
    const player = card.dataset.playerName || card.dataset.team || '—';
    const last = groups[groups.length - 1];
    if (last && last.player === player) {
      last.cards.push(card);
    } else {
      groups.push({ player, cards: [card] });
    }
  });

  // Re-append in new order and insert player headers with group track button
  groups.forEach(({ player, cards: groupCards }) => {
    const hdr = document.createElement('div');
    hdr.className = 'prop-player-hdr';

    // Player name text node
    hdr.appendChild(document.createTextNode(player));

    // Horizontal rule
    const line = document.createElement('span');
    line.className = 'pph-line';
    hdr.appendChild(line);

    grid.appendChild(hdr);
    groupCards.forEach(card => grid.appendChild(card));
  });
}

function togglePropsGrouping() {
  _propsGrouped = !_propsGrouped;
  const btn = document.getElementById('propsGroupToggle');
  if (btn) btn.classList.toggle('active', _propsGrouped);
  _applyPropGrouping();
}

// ── Game grouping by matchup ──────────────────────────────────────────────
let _gamesGrouped = false;

function _clearGameGroupHeaders() {
  document.querySelectorAll('.game-group-hdr').forEach(el => el.remove());
}

function _applyGameGrouping() {
  const grid = document.getElementById('cardsGrid');
  if (!grid) return;
  _clearGameGroupHeaders();
  if (!_gamesGrouped) return;

  // Collect visible non-prop cards
  const gameCards = Array.from(grid.querySelectorAll('.bet-card'))
    .filter(c => c.dataset.isProp !== 'true' && c.style.display !== 'none');
  if (!gameCards.length) return;

  // Sort by game name, then EV desc
  gameCards.sort((a, b) => {
    const gA = (a.dataset.game || '').toLowerCase();
    const gB = (b.dataset.game || '').toLowerCase();
    if (gA !== gB) return gA.localeCompare(gB);
    return parseFloat(b.dataset.ev) - parseFloat(a.dataset.ev);
  });

  // Group by game
  const groups = [];
  gameCards.forEach(card => {
    const game = card.dataset.game || '—';
    const last = groups[groups.length - 1];
    if (last && last.game === game) {
      last.cards.push(card);
    } else {
      groups.push({ game, cards: [card] });
    }
  });

  // Re-append with game headers
  groups.forEach(({ game, cards: groupCards }) => {
    const hdr = document.createElement('div');
    hdr.className = 'game-group-hdr';

    // Game name text
    hdr.appendChild(document.createTextNode(game));

    // Horizontal rule
    const line = document.createElement('span');
    line.className = 'ggh-line';
    hdr.appendChild(line);

    grid.appendChild(hdr);
    groupCards.forEach(card => grid.appendChild(card));
  });
}

function toggleGamesGrouping() {
  _gamesGrouped = !_gamesGrouped;
  const btn = document.getElementById('gamesGroupToggle');
  if (btn) btn.classList.toggle('active', _gamesGrouped);
  _applyGameGrouping();
}

// ── Sidebar + mobile sheet qfilter btns ───────────────────────────────────
document.querySelectorAll('.qfilter-btn[data-filter]').forEach(btn => {
  btn.addEventListener('click', () => {
    const ft = btn.dataset.filter, val = btn.dataset.value;

    if (ft === 'book') {
      // ── Individual book chip: multi-select toggle ─────────────────────────
      if (val === 'all') {
        activeBooks.clear();
      } else {
        activeBooks.has(val) ? activeBooks.delete(val) : activeBooks.add(val);
      }
      _syncBookChipVisuals();

    } else if (ft === 'booktype') {
      // ── Group chip: toggle all books of this type ─────────────────────────
      const booksOfType = Array.from(
        document.querySelectorAll(`.qfilter-btn[data-filter="book"][data-book-type="${val}"]`)
      ).map(b => b.dataset.value);
      const allSelected = booksOfType.length > 0 && booksOfType.every(b => activeBooks.has(b));
      // If all of this type are already selected → deselect them; else select all
      booksOfType.forEach(b => allSelected ? activeBooks.delete(b) : activeBooks.add(b));
      _syncBookChipVisuals();

    } else if (ft === 'is_prop') {
      // ── Bet type (games / props / all) ────────────────────────────────────
      _setIsProp(val);

    } else {
      // ── All other filters: single mutual-exclusion ────────────────────────
      document.querySelectorAll(`.qfilter-btn[data-filter="${ft}"]`).forEach(b => b.classList.remove('active'));
      document.querySelectorAll(`.qfilter-btn[data-filter="${ft}"][data-value="${val}"]`).forEach(b => b.classList.add('active'));
      if (ft === 'market')  activeMarket  = val;
      if (ft === 'clvdir')  activeCLVDir  = val;
    }

    applyFilters();
    updateFilterBadge();
  });
});

// ── EV slider (desktop + mobile, kept in sync) ────────────────────────────
const evSlider        = document.getElementById('evSlider');
const evSliderMobile  = document.getElementById('evSliderMobile');
const evSliderVal     = document.getElementById('evSliderVal');
const evSliderValMob  = document.getElementById('evSliderValMobile');

function _setEvSlider(val) {
  activeMinEv = val;
  if (evSlider)       evSlider.value       = val;
  if (evSliderMobile) evSliderMobile.value = val;
  if (evSliderVal)    evSliderVal.textContent    = val + '%';
  if (evSliderValMob) evSliderValMob.textContent = val + '%';
  document.querySelectorAll('.qfilter-btn[data-ev]').forEach(b => b.classList.toggle('active', parseFloat(b.dataset.ev) === val));
  applyFilters();
  updateFilterBadge();
}
evSlider.addEventListener('input', function() { _setEvSlider(parseFloat(this.value)); });
if (evSliderMobile) evSliderMobile.addEventListener('input', function() { _setEvSlider(parseFloat(this.value)); });

document.querySelectorAll('.qfilter-btn[data-ev]').forEach(btn => {
  btn.addEventListener('click', () => { _setEvSlider(parseFloat(btn.dataset.ev)); });
});

// ── Model Edge filter buttons ─────────────────────────────────────────────
function _setEdgeFilter(val) {
  activeMinEdge = val;
  document.querySelectorAll('.qfilter-btn[data-edge]').forEach(b => b.classList.toggle('active', parseFloat(b.dataset.edge) === val));
  applyFilters();
  updateFilterBadge();
}
document.querySelectorAll('.qfilter-btn[data-edge]').forEach(btn => {
  btn.addEventListener('click', () => { _setEdgeFilter(parseFloat(btn.dataset.edge)); });
});

// ── Multi-book toggle ─────────────────────────────────────────────────────
function _setMultiBook(val) {
  activeMultiBook = val;
  document.querySelectorAll('[data-multibook]').forEach(b => b.classList.toggle('active', val));
  applyFilters();
  updateFilterBadge();
}
document.querySelectorAll('[data-multibook]').forEach(btn => {
  btn.addEventListener('click', () => { _setMultiBook(!activeMultiBook); });
});

function clearFilters() {
  try { localStorage.removeItem('positplusev_filters'); } catch(e) {}
  activeLeague = 'all'; activeMarket = 'all';
  activeBooks.clear(); activeDay = 'today'; activeCLVDir = 'all';
  activeMinEv = 0; activeMinEdge = 0; activeMultiBook = false; activeSearch = '';
  document.querySelectorAll('.qfilter-btn[data-edge]').forEach(b => b.classList.toggle('active', b.dataset.edge === '0'));
  document.querySelectorAll('[data-multibook]').forEach(b => b.classList.remove('active'));
  document.getElementById('globalSearch').value = '';
  _setEvSlider(0);
  _setIsProp('all');
  document.querySelectorAll('.sport-pill').forEach(b => b.classList.toggle('active', b.dataset.value === 'all' && b.dataset.filter === 'league'));
  document.querySelectorAll('.qfilter-btn[data-filter]').forEach(b => b.classList.toggle('active', b.dataset.value === 'all'));
  _syncBookChipVisuals();
  _setSideDay('all');
  _buildPropTypeChips(); // rebuild with all leagues visible again
  applyFilters();
  updateFilterBadge();
  updateClearBtn();
}

// ── Filter state persistence ───────────────────────────────────────────────
function _saveFilters() {
  try {
    localStorage.setItem('positplusev_filters', JSON.stringify({
      league:     activeLeague,
      isProp:     activeIsProp,
      market:     activeMarket,
      propMarket: activePropMarket,
      sort:       activeSort,
      minEv:      activeMinEv,
      minEdge:    activeMinEdge,
      clvDir:     activeCLVDir,
      multiBook:  activeMultiBook,
      day:        activeDay,
      books:      [...activeBooks],
    }));
  } catch(e) {}
}

function _restoreFilters() {
  try {
    const raw = localStorage.getItem('positplusev_filters');
    if (!raw) return;
    const s = JSON.parse(raw);

    // Restore scalar state
    activeLeague    = s.league    || 'all';
    activeCLVDir    = s.clvDir    || 'all';
    activeDay       = s.day       || 'today';
    activeSort      = s.sort      || 'ev_desc';
    activeMinEv     = typeof s.minEv   === 'number' ? s.minEv   : 0;
    activeMinEdge   = typeof s.minEdge === 'number' ? s.minEdge : 0;
    activeMultiBook = !!s.multiBook;
    activePropMarket = s.propMarket || 'all';
    if (Array.isArray(s.books)) { activeBooks.clear(); s.books.forEach(b => activeBooks.add(b)); }

    // League pills
    document.querySelectorAll('.sport-pill[data-filter="league"]').forEach(b =>
      b.classList.toggle('active', b.dataset.value === activeLeague)
    );

    // isProp — handles section show/hide and prop type chip rebuild
    _setIsProp(s.isProp || 'all');

    // Market chips — restore after _setIsProp (which may reset it for props mode)
    activeMarket = s.market || 'all';
    document.querySelectorAll('.qfilter-btn[data-filter="market"]').forEach(b =>
      b.classList.toggle('active', b.dataset.value === activeMarket)
    );

    // CLV direction chips
    document.querySelectorAll('.qfilter-btn[data-filter="clvdir"]').forEach(b =>
      b.classList.toggle('active', b.dataset.value === activeCLVDir)
    );

    // Day buttons (sidebar + mobile bottom nav)
    ['all','today','tomorrow'].forEach(d => {
      const _rsd = document.getElementById('sideDay-' + d);
      if (_rsd) _rsd.classList.toggle('active', d === activeDay);
      const _rbn = document.getElementById('bn-' + d);
      if (_rbn) _rbn.classList.toggle('active', d === activeDay);
    });

    // EV slider + chips
    if (evSlider)       evSlider.value             = activeMinEv;
    if (evSliderMobile) evSliderMobile.value        = activeMinEv;
    if (evSliderVal)    evSliderVal.textContent     = activeMinEv + '%';
    if (evSliderValMob) evSliderValMob.textContent  = activeMinEv + '%';
    document.querySelectorAll('.qfilter-btn[data-ev]').forEach(b =>
      b.classList.toggle('active', parseFloat(b.dataset.ev) === activeMinEv)
    );

    // Edge chips
    document.querySelectorAll('.qfilter-btn[data-edge]').forEach(b =>
      b.classList.toggle('active', parseFloat(b.dataset.edge) === activeMinEdge)
    );

    // Multi-book toggle
    document.querySelectorAll('[data-multibook]').forEach(b =>
      b.classList.toggle('active', activeMultiBook)
    );

    // Book chips
    _syncBookChipVisuals();

    // Sort select
    const sortSel = document.getElementById('sortSelect');
    if (sortSel) sortSel.value = activeSort;

  } catch(e) {}
}

// ── Mobile filter sheet ────────────────────────────────────────────────────
function updateFilterBadge() {
  const count = [
    activeMarket      !== 'all',
    activeBooks.size  > 0,
    activeCLVDir      !== 'all',
    activeMinEv       > 0,
    activeMinEdge     > 0,
    activeMultiBook,
    activeIsProp      !== 'all',
    activePropMarket  !== 'all',
  ].filter(Boolean).length;
  const badge = document.getElementById('mfBadge');
  if (!badge) return;
  badge.textContent = count;
  badge.style.display = count > 0 ? 'flex' : 'none';
}

function openMobileFilters() {
  // Sync bankroll
  const br = localStorage.getItem('positplusev_bankroll') || '';
  const mob = document.getElementById('bankrollInputMobile');
  if (mob) mob.value = br;
  // Sync Kelly fraction
  const kfMob = document.getElementById('kellyFracSelectMobile');
  if (kfMob) kfMob.value = _getKellyFrac();
  document.getElementById('mfSheet').classList.add('open');
  document.getElementById('mfOverlay').classList.add('open');
  document.body.style.overflow = 'hidden';
}

function closeMobileFilters() {
  document.getElementById('mfSheet').classList.remove('open');
  document.getElementById('mfOverlay').classList.remove('open');
  document.body.style.overflow = '';
}

// Mobile bankroll input sync
const bankrollInputMobile = document.getElementById('bankrollInputMobile');
if (bankrollInputMobile) {
  bankrollInputMobile.addEventListener('input', () => {
    const v = bankrollInputMobile.value;
    const mainInput = document.getElementById('bankrollInput');
    if (mainInput) mainInput.value = v;
    v && parseFloat(v) > 0
      ? localStorage.setItem('positplusev_bankroll', v)
      : localStorage.removeItem('positplusev_bankroll');
    updateKellyDisplays();
  });
}


// ── Click-to-Bet deep-link map ─────────────────────────────────────────────
// URLs point to the live betting interface, not marketing pages.
// Mobile-only apps (NoVig, ProphetX) link to their app/download page.
const _BOOK_URLS = {
  // ── Sportsbooks (full web app) ──
  draftkings: {
    icehockey_nhl:        "https://sportsbook.draftkings.com/leagues/hockey/nhl",
    basketball_nba:       "https://sportsbook.draftkings.com/leagues/basketball/nba",
    baseball_mlb:         "https://sportsbook.draftkings.com/leagues/baseball/mlb",
    soccer_epl:                  "https://sportsbook.draftkings.com/leagues/soccer/england---premier-league",
    soccer_spain_la_liga:        "https://sportsbook.draftkings.com/leagues/soccer/la-liga",
    soccer_usa_mls:              "https://sportsbook.draftkings.com/leagues/soccer/mls",
    soccer_uefa_champs_league:   "https://sportsbook.draftkings.com/leagues/soccer/uefa-champions-league",
    _default:                    "https://sportsbook.draftkings.com",
  },
  fanduel: {
    icehockey_nhl:               "https://sportsbook.fanduel.com/navigation/nhl",
    basketball_nba:              "https://sportsbook.fanduel.com/navigation/nba",
    baseball_mlb:                "https://sportsbook.fanduel.com/navigation/mlb",
    soccer_epl:                  "https://sportsbook.fanduel.com/soccer",
    soccer_spain_la_liga:        "https://sportsbook.fanduel.com/soccer",
    soccer_usa_mls:              "https://sportsbook.fanduel.com/soccer",
    soccer_uefa_champs_league:   "https://sportsbook.fanduel.com/soccer",
    _default:                    "https://sportsbook.fanduel.com",
  },
  // BetMGM: sports.betmgm.com is a blog; actual sportsbook is www.betmgm.com/en/sports
  // Geo-redirect to state subdomain is handled automatically by their server
  betmgm: {
    icehockey_nhl:               "https://www.betmgm.com/en/sports/hockey-15",
    basketball_nba:              "https://www.betmgm.com/en/sports/basketball-7",
    baseball_mlb:                "https://www.betmgm.com/en/sports/baseball-23",
    soccer_epl:                  "https://www.betmgm.com/en/sports/soccer-4",
    soccer_spain_la_liga:        "https://www.betmgm.com/en/sports/soccer-4",
    soccer_usa_mls:              "https://www.betmgm.com/en/sports/soccer-4",
    soccer_uefa_champs_league:   "https://www.betmgm.com/en/sports/soccer-4",
    _default:                    "https://www.betmgm.com/en/sports",
  },
  // Caesars: actual sportsbook app is at sportsbook.caesars.com with /odds/ paths
  caesars: {
    icehockey_nhl:               "https://sportsbook.caesars.com/odds/nhl",
    basketball_nba:              "https://sportsbook.caesars.com/odds/nba",
    baseball_mlb:                "https://sportsbook.caesars.com/odds/mlb",
    soccer_epl:                  "https://sportsbook.caesars.com/odds/soccer",
    soccer_spain_la_liga:        "https://sportsbook.caesars.com/odds/soccer",
    soccer_usa_mls:              "https://sportsbook.caesars.com/odds/soccer",
    soccer_uefa_champs_league:   "https://sportsbook.caesars.com/odds/soccer",
    _default:                    "https://sportsbook.caesars.com",
  },
  // PointsBet US was acquired by Fanatics (betfanatics.com) — PointsBet brand is closed
  pointsbet: {
    icehockey_nhl:               "https://betfanatics.com/gaming/sports/icehockey",
    basketball_nba:              "https://betfanatics.com/gaming/sports/basketball",
    baseball_mlb:                "https://betfanatics.com/gaming/sports/baseball",
    soccer_epl:                  "https://betfanatics.com/gaming/sports/soccer",
    soccer_spain_la_liga:        "https://betfanatics.com/gaming/sports/soccer",
    soccer_usa_mls:              "https://betfanatics.com/gaming/sports/soccer",
    soccer_uefa_champs_league:   "https://betfanatics.com/gaming/sports/soccer",
    _default:                    "https://betfanatics.com",
  },
  betfair_ex_uk: {
    icehockey_nhl:               "https://www.betfair.com/exchange/plus/ice-hockey",
    basketball_nba:              "https://www.betfair.com/exchange/plus/basketball",
    baseball_mlb:                "https://www.betfair.com/exchange/plus/baseball",
    soccer_epl:                  "https://www.betfair.com/exchange/plus/football",
    soccer_spain_la_liga:        "https://www.betfair.com/exchange/plus/football",
    soccer_usa_mls:              "https://www.betfair.com/exchange/plus/football",
    soccer_uefa_champs_league:   "https://www.betfair.com/exchange/plus/football",
    _default:                    "https://www.betfair.com/exchange/plus/",
  },
  williamhill_us: {
    icehockey_nhl:               "https://www.williamhill.com/us/en/bet/sports/ice-hockey",
    basketball_nba:              "https://www.williamhill.com/us/en/bet/sports/basketball",
    baseball_mlb:                "https://www.williamhill.com/us/en/bet/sports/baseball",
    soccer_epl:                  "https://www.williamhill.com/us/en/bet/sports/soccer",
    _default:                    "https://www.williamhill.com/us/en/bet",
  },
  mybookie: {
    icehockey_nhl:               "https://mybookie.ag/sportsbook/hockey",
    basketball_nba:              "https://mybookie.ag/sportsbook/basketball",
    baseball_mlb:                "https://mybookie.ag/sportsbook/baseball",
    soccer_epl:                  "https://mybookie.ag/sportsbook/soccer",
    _default:                    "https://mybookie.ag/sportsbook",
  },
  bovada: {
    icehockey_nhl:               "https://www.bovada.lv/sports/hockey",
    basketball_nba:              "https://www.bovada.lv/sports/basketball",
    baseball_mlb:                "https://www.bovada.lv/sports/baseball",
    soccer_epl:                  "https://www.bovada.lv/sports/soccer",
    soccer_spain_la_liga:        "https://www.bovada.lv/sports/soccer",
    soccer_usa_mls:              "https://www.bovada.lv/sports/soccer",
    soccer_uefa_champs_league:   "https://www.bovada.lv/sports/soccer",
    _default:                    "https://www.bovada.lv/sports",
  },
  pinnacle: {
    icehockey_nhl:               "https://www.pinnacle.com/en/hockey/nhl/matchups",
    basketball_nba:              "https://www.pinnacle.com/en/basketball/nba/matchups",
    baseball_mlb:                "https://www.pinnacle.com/en/baseball/mlb/matchups",
    soccer_epl:                  "https://www.pinnacle.com/en/soccer/england-premier-league/matchups",
    soccer_spain_la_liga:        "https://www.pinnacle.com/en/soccer/spain-la-liga/matchups",
    soccer_usa_mls:              "https://www.pinnacle.com/en/soccer/usa-mls/matchups",
    soccer_uefa_champs_league:   "https://www.pinnacle.com/en/soccer/champions-league/matchups",
    _default:                    "https://www.pinnacle.com/en/sports",
  },
  betfair_ex_us: {
    icehockey_nhl:               "https://www.betfair.com/exchange/plus/ice-hockey",
    basketball_nba:              "https://www.betfair.com/exchange/plus/basketball",
    baseball_mlb:                "https://www.betfair.com/exchange/plus/baseball",
    soccer_epl:                  "https://www.betfair.com/exchange/plus/football",
    _default:                    "https://www.betfair.com/exchange/plus/",
  },
  betrivers: {
    baseball_mlb:                "https://www.betrivers.com/sports/baseball",
    basketball_nba:              "https://www.betrivers.com/sports/basketball",
    icehockey_nhl:               "https://www.betrivers.com/sports/ice-hockey",
    _default:                    "https://www.betrivers.com/sports",
  },
  espnbet: {
    baseball_mlb:                "https://www.espnbet.com/sport/baseball",
    basketball_nba:              "https://www.espnbet.com/sport/basketball",
    icehockey_nhl:               "https://www.espnbet.com/sport/ice-hockey",
    _default:                    "https://www.espnbet.com",
  },
  hardrockbet: {
    baseball_mlb:                "https://hardrockbet.com/sports/baseball",
    basketball_nba:              "https://hardrockbet.com/sports/basketball",
    icehockey_nhl:               "https://hardrockbet.com/sports/ice-hockey",
    _default:                    "https://hardrockbet.com/sports",
  },
  betonlineag: {
    baseball_mlb:                "https://www.betonline.ag/sportsbook/baseball",
    basketball_nba:              "https://www.betonline.ag/sportsbook/basketball",
    icehockey_nhl:               "https://www.betonline.ag/sportsbook/hockey",
    _default:                    "https://www.betonline.ag/sportsbook",
  },
  // ── Prediction markets ──
  // Kalshi: /markets/sports is invalid; /category/all is the browse interface
  kalshi:    { _default: "https://kalshi.com/category/all" },
  // NoVig: mobile-only — handled via _MOBILE_APP_STORES below
  novig:     { _default: "https://novig.com" },
  // Polymarket: /sports/live is the verified sports trading interface
  polymarket:{ _default: "https://polymarket.com/sports/live" },
  // ProphetX: mobile-only — handled via _MOBILE_APP_STORES below
  prophetx:  { _default: "https://www.prophetx.co" },
  // BetOpenly: P2P betting exchange — handled via _MOBILE_APP_STORES below
  betopenly: { _default: "https://betopenly.com" },
};

// Mobile-only apps: try native URI scheme first (opens app directly if installed),
// fall back to App Store / Play Store after 1.5s if app isn't installed.
const _MOBILE_APP_STORES = {
  novig: {
    scheme:  "novigapp://",   // NoVig's registered URI scheme — opens app instantly
    ios:     "https://apps.apple.com/us/app/novig/id6443958997",
    android: "https://play.google.com/store/apps/details?id=us.novig.app",
    desktop: "https://novig.com",
  },
  prophetx: {
    scheme:  null,
    ios:     "https://apps.apple.com/us/app/prophetx-prediction-market/id6504584166",
    android: "https://play.google.com/store/apps/details?id=com.prophetxsweepstake",
    desktop: "https://www.prophetx.co",
  },
  betopenly: {
    scheme:  null,
    ios:     "https://apps.apple.com/us/app/betopenly/id6471480892",
    android: "https://play.google.com/store/apps/details?id=com.opengames.betopenly",
    desktop: "https://betopenly.com",
  },
};

// ── Deep-link / event URL builder ─────────────────────────────────────────
// Returns the most specific URL we can construct for the given book + card.
//
// Books with true event-level links (no partnership needed):
//   • Pinnacle   — /en/{sport}/{league}/matchups/{away-slug}-vs-{home-slug}
//   • Kalshi     — /markets?query={search} (search-scoped to their market list)
//   • Polymarket — /sports/live?query={search}
//
// Books that need an affiliate partnership for event-level links:
//   • DraftKings  — join affiliates.draftkings.com → event IDs exposed in feed
//   • FanDuel     — join fanduelaffiliate.com       → event-level betslip links
//   • BetMGM      — Commission Junction (CJ)        → event-level links
//   • Caesars     — Commission Junction (CJ)        → event-level links
//   • BetRivers   — Betrivers affiliate program     → event-level links
//   • ESPN Bet    — ESPN affiliate program          → event-level links
// Until those partnerships are in place, those books open the sport section
// + the search clipboard helper tells the user what to find.

const _PINNACLE_LEAGUE_PATH = {
  baseball_mlb:                    'baseball/mlb',
  basketball_nba:                  'basketball/nba',
  icehockey_nhl:                   'hockey/nhl',
  americanfootball_nfl:            'football/nfl',
  americanfootball_ncaaf:          'football/ncaa',
  soccer_epl:                      'soccer/england-premier-league',
  soccer_spain_la_liga:            'soccer/spain-la-liga',
  soccer_germany_bundesliga:       'soccer/germany-bundesliga',
  soccer_france_ligue_one:         'soccer/france-ligue-1',
  soccer_italy_serie_a:            'soccer/italy-serie-a',
  soccer_usa_mls:                  'soccer/usa-mls',
  soccer_uefa_champs_league:       'soccer/champions-league',
  soccer_uefa_europa_league:       'soccer/europa-league',
  soccer_conmebol_copa_libertadores: 'soccer/copa-libertadores',
  tennis_atp_us_open:              'tennis/us-open',
  basketball_ncaab:                'basketball/ncaa',
  mma_mixed_martial_arts:          'fighting/ufc',
  boxing_boxing:                   'fighting/boxing',
};

function _slugify(s) {
  return (s || '').toLowerCase().trim()
    .replace(/['']/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '');
}

function _buildEventUrl(book, league, card) {
  const game     = card?.dataset?.game || '';
  const isProp   = card?.dataset?.isProp === 'true';
  const mkt      = card?.dataset?.market || '';
  const team     = card?.dataset?.team || '';
  const player   = card?.dataset?.playerName || '';
  const oddsStr  = (parseInt(card?.dataset?.odds, 10) || '');

  // Build a concise search string for books that use it
  let q = '';
  if (game.includes(' @ ')) {
    const [away, home] = game.split(' @ ').map(s => s.trim());
    q = encodeURIComponent(`${away} vs ${home}`);
  } else if (isProp && player) {
    q = encodeURIComponent(player);
  } else {
    q = encodeURIComponent(team || game);
  }

  if (book === 'pinnacle' && game.includes(' @ ') && !isProp) {
    const [away, home] = game.split(' @ ').map(s => s.trim());
    const leaguePath   = _PINNACLE_LEAGUE_PATH[league];
    if (leaguePath) {
      return `https://www.pinnacle.com/en/${leaguePath}/matchups/${_slugify(away)}-vs-${_slugify(home)}`;
    }
  }

  if (book === 'kalshi') {
    return `https://kalshi.com/markets?query=${q}`;
  }

  if (book === 'polymarket') {
    return `https://polymarket.com/sports/live?query=${q}`;
  }

  if (book === 'betfair_ex_uk' || book === 'betfair_ex_us') {
    // Betfair search works across their exchange
    return `https://www.betfair.com/exchange/plus/en/search?q=${q}`;
  }

  // All other books: fall back to sport-section URL
  const map = _BOOK_URLS[book];
  if (!map) return null;
  return map[league] || map._default || null;
}

// ── Bet search helper toast ────────────────────────────────────────────────

function _buildSearchString(card) {
  if (!card) return { search: '', detail: '' };
  const game      = card.dataset.game     || '';
  const team      = card.dataset.team     || '';
  const player    = card.dataset.playerName || '';
  const mkt       = card.dataset.marketDisplay || card.dataset.market || '';
  const odds      = parseInt(card.dataset.odds, 10);
  const isProp    = card.dataset.isProp === 'true';
  const oddsStr   = odds > 0 ? `+${odds}` : String(odds);

  // For game bets: "Boston Celtics vs Los Angeles Lakers"
  // For props:     "LeBron James — Points Over 26.5"
  let search = '', detail = '';
  if (isProp && player) {
    const parts = team.toLowerCase().includes('over') ? ['Over', team.replace(/over/i,'').trim()]
                : team.toLowerCase().includes('under') ? ['Under', team.replace(/under/i,'').trim()]
                : [team, ''];
    search = player;
    detail = `${mkt} ${parts[0]} ${parts[1]} · ${oddsStr}`.trim();
  } else if (game.includes(' @ ')) {
    const [away, home] = game.split(' @ ');
    search = `${away.trim()} vs ${home.trim()}`;
    detail = `${team} · ${mkt} · ${oddsStr}`;
  } else {
    search = game || team;
    detail = `${mkt} · ${oddsStr}`;
  }
  return { search, detail };
}

function _showBetToast(search, detail, copied) {
  // Remove any existing toast
  document.querySelectorAll('.bsh-toast').forEach(e => {
    e.classList.add('bsh-exit');
    setTimeout(() => e.remove(), 350);
  });
  const t = document.createElement('div');
  t.className = 'bsh-toast';
  t.innerHTML = `
    <div class="bsh-label">Bet to place${copied ? ' — copied to clipboard' : ''}</div>
    <div class="bsh-search">${search}</div>
    <div class="bsh-sub">${detail}</div>
    ${copied ? '<div class="bsh-copied">✓ Paste to search on the book\'s site</div>' : ''}
  `;
  document.body.appendChild(t);
  setTimeout(() => {
    t.classList.add('bsh-exit');
    setTimeout(() => t.remove(), 350);
  }, 4000);
}

function openBetLink(book, league, btn) {
  const card = btn ? btn.closest('.bet-card') : null;
  const { search, detail } = _buildSearchString(card);

  // Copy search string to clipboard (best-effort)
  let copied = false;
  if (search && navigator.clipboard) {
    navigator.clipboard.writeText(search).then(() => {}).catch(() => {});
    copied = true;
  }

  const mobileApp = _MOBILE_APP_STORES[book];
  if (mobileApp) {
    // Always target the website — never the App Store.
    // If a native URI scheme is registered (e.g. NoVig), try to open the app
    // first on mobile; if the app isn't installed the scheme silently fails and
    // we open the website after 1.5 s.  Without a scheme we go straight to web.
    const webUrl = mobileApp.desktop;
    const ua     = navigator.userAgent;
    const isMobile = /iPhone|iPad|iPod|Android/.test(ua);

    if (search) _showBetToast(search, detail, copied);

    if (isMobile && mobileApp.scheme) {
      // Try to hand off to the native app
      const fallback = setTimeout(() => window.open(webUrl, '_blank', 'noopener,noreferrer'), 1500);
      const cancel = () => clearTimeout(fallback);
      window.addEventListener('blur', cancel, { once: true });
      document.addEventListener('visibilitychange', () => { if (document.hidden) cancel(); }, { once: true });
      window.location.href = mobileApp.scheme;
    } else {
      // No URI scheme (ProphetX, BetOpenly) or desktop — open website directly
      window.open(webUrl, '_blank', 'noopener,noreferrer');
    }
    return;
  }

  // Web sportsbooks & prediction markets
  const url = _buildEventUrl(book, league, card);
  if (!url) return;

  // Show search helper before opening the book.
  // For books with event-level URLs (Pinnacle, Kalshi, Polymarket, Betfair),
  // the clipboard content helps the user confirm the right market once they land.
  if (search) _showBetToast(search, detail, copied);

  window.open(url, '_blank', 'noopener,noreferrer');
}

// ── Projection panel ───────────────────────────────────────────────────────
const _projCache = {};

function toggleProjPanel(betId, btn) {
  const panel = document.getElementById('proj-panel-' + betId);
  if (!panel) return;
  if (panel.style.display !== 'none') {
    panel.style.display = 'none';
    btn.textContent = '📊 Projection';
    return;
  }
  if (_projCache[betId]) {
    panel.innerHTML = _buildProjHtml(_projCache[betId]);
    panel.style.display = 'block';
    btn.textContent = '📊 Hide';
    return;
  }
  _loadProjection(betId, btn, panel);
}

function _loadProjection(betId, btn, panel, attempt) {
  attempt = attempt || 1;
  btn.disabled = true;
  btn.textContent = attempt > 1 ? 'Retrying…' : 'Loading…';
  panel.style.display = 'block';
  panel.innerHTML = '<div style="padding:10px 12px;"><div class="skel-bar skel-w90"></div><div class="skel-bar skel-w75"></div><div class="skel-bar skel-w60"></div></div>';

  fetch('/api/projection/' + betId, { credentials: 'same-origin' })
    .then(r => {
      if (r.status === 422) return null;          // prop / no game data
      if (r.status === 503 || r.status === 500) throw Object.assign(new Error('service'), {retryable: true, status: r.status});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(data => {
      btn.disabled = false;
      if (!data) {
        panel.innerHTML = '<div style="padding:8px 12px;font-size:0.72rem;color:var(--d-muted);">No projection data available for this game.</div>';
        btn.textContent = '📊 Projection';
        panel.style.display = 'block';
        return;
      }
      _projCache[betId] = data;
      panel.innerHTML = _buildProjHtml(data);
      btn.textContent = '📊 Hide';
    })
    .catch(err => {
      btn.disabled = false;
      btn.textContent = '📊 Projection';
      if (err.retryable && attempt < 3) {
        // Auto-retry once silently, then show retry button
        if (attempt === 1) {
          panel.innerHTML = '<div style="padding:8px 12px;font-size:0.72rem;color:var(--d-muted);">Connecting…</div>';
          setTimeout(() => _loadProjection(betId, btn, panel, attempt + 1), 1200);
          return;
        }
      }
      // Show refresh prompt — projection data loads with the page, so a reload fixes it
      panel.innerHTML = `<div style="padding:10px 14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        <span style="font-size:0.75rem;color:var(--d-muted);">Projection unavailable — refresh to load.</span>
        <button id="proj-retry-${betId}"
          style="background:var(--d-bright);border:1px solid var(--d-border);border-radius:6px;color:var(--d-sub);font-size:0.72rem;font-weight:600;padding:5px 10px;cursor:pointer;font-family:inherit;">
          🔄 Refresh page
        </button>
      </div>`;
      const retryBtn = document.getElementById('proj-retry-' + betId);
      if (retryBtn) retryBtn.addEventListener('click', () => location.reload());
    });
}

function _buildProjHtml(d) {
  let h = '<div class="proj-panel-hdr"><span>🤖 Game Projection</span><span style="font-style:italic;font-weight:400;text-transform:none;letter-spacing:0;color:var(--d-muted);">' + (d.updated_at ? new Date(d.updated_at).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'}) : '') + '</span></div>';
  if (d.home_score_mean != null && d.away_score_mean != null) {
    const aw = parseFloat(d.away_score_mean) > parseFloat(d.home_score_mean);
    const aTs = parseFloat(d.away_score_mean).toFixed(1);
    const hTs = parseFloat(d.home_score_mean).toFixed(1);
    const away = escHtml(d.away_display || d.away_team || '?');
    const home = escHtml(d.home_display || d.home_team || '?');
    h += `<div class="proj-scoreline">
      <div class="proj-team"><div class="proj-score${aw?' winner':''}">${aTs}</div><div class="proj-name">${away.split(' ').pop()}</div></div>
      <div class="proj-sep">–</div>
      <div class="proj-team"><div class="proj-score${!aw?' winner':''}">${hTs}</div><div class="proj-name">${home.split(' ').pop()}</div></div>
    </div>`;
    if (d.model_agrees_with_bet === true) h += '<div class="proj-align-badge proj-align-yes">✅ Model agrees</div>';
    else if (d.model_agrees_with_bet === false) h += '<div class="proj-align-badge" style="background:rgba(255,91,91,0.08);color:var(--d-red);border:1px solid rgba(255,91,91,0.2);">⚠️ Model contradicts</div>';
  }
  // Stats row — shown even when individual scores aren't available (e.g. NHL/soccer)
  const stats = [];
  if (d.total_mean != null) stats.push(['O/U', parseFloat(d.total_mean).toFixed(1), '']);
  if (d.home_win_probability != null) { const fh = d.home_win_probability >= 0.5; const wp = fh ? d.home_win_probability : 1 - d.home_win_probability; const lbl = escHtml((fh ? (d.home_display||d.home_team||'Home') : (d.away_display||d.away_team||'Away'))).split(' ').pop(); stats.push(['Win%', Math.round(wp*100)+'%', lbl+' fav']); }
  if (d.consensus_line != null) stats.push(['Spread', (d.consensus_line>0?'+':'')+d.consensus_line, 'consensus']);
  if (stats.length && !h.includes('proj-stats')) h += '<div class="proj-stats">' + stats.map(([l,v,s]) => `<div class="proj-stat"><div class="ps-val">${v}</div><div class="ps-lbl">${l}</div>${s?`<div style="font-size:0.55rem;color:var(--d-muted);">${escHtml(s)}</div>`:''}</div>`).join('') + '</div>';
  const away = escHtml(d.away_team||'?'), home = escHtml(d.home_team||'?');
  const aSh = away.split(' ').pop(), hSh = home.split(' ').pop();
  if (d.away_record || d.home_record) {
    h += '<div class="proj-ctx-row">';
    if (d.away_record) h += `<span class="pctx-team">${aSh}</span><span class="pctx-val">${escHtml(d.away_record)}${d.away_streak?' <span class="pctx-streak">'+escHtml(d.away_streak)+'</span>':''}</span>`;
    if (d.away_record && d.home_record) h += '<span style="color:var(--d-muted);">·</span>';
    if (d.home_record) h += `<span class="pctx-team">${hSh}</span><span class="pctx-val">${escHtml(d.home_record)}${d.home_streak?' <span class="pctx-streak">'+escHtml(d.home_streak)+'</span>':''}</span>`;
    h += '</div>';
  }
  if (d.away_last10 || d.home_last10) {
    h += '<div class="proj-ctx-row">';
    if (d.away_last10) h += `<span style="color:var(--d-muted);font-size:0.62rem;">L10 ${aSh}:</span> <span class="pctx-val">${escHtml(d.away_last10)}</span>${d.away_pts!=null?` <span class="pctx-pts">${escHtml(String(d.away_pts))} pts</span>`:''}`;
    if (d.away_last10 && d.home_last10) h += '<span style="color:var(--d-muted);">·</span>';
    if (d.home_last10) h += `<span style="color:var(--d-muted);font-size:0.62rem;">L10 ${hSh}:</span> <span class="pctx-val">${escHtml(d.home_last10)}</span>${d.home_pts!=null?` <span class="pctx-pts">${escHtml(String(d.home_pts))} pts</span>`:''}`;
    h += '</div>';
  }
  if (d.away_playoff_note || d.home_playoff_note) h += `<div class="proj-ctx-row">${d.away_playoff_note?`<span>📋 ${aSh}: ${escHtml(d.away_playoff_note)}</span>`:''}${d.home_playoff_note?`<span>📋 ${hSh}: ${escHtml(d.home_playoff_note)}</span>`:''}</div>`;
  if (d.away_playoff_seed || d.home_playoff_seed) h += `<div class="proj-ctx-row">${d.away_playoff_seed?`<span>${aSh}: #${escHtml(String(d.away_playoff_seed))} seed</span>`:''}${d.home_playoff_seed?`<span>${hSh}: #${escHtml(String(d.home_playoff_seed))} seed</span>`:''}</div>`;
  if (d.away_goalie || d.home_goalie) h += `<div class="proj-ctx-row">${d.away_goalie?`<span style="color:var(--d-muted);">🥅 ${aSh}:</span> <span class="pctx-val">${escHtml(d.away_goalie)}${d.away_goalie_confirmed?' ✓':' (unconf.)'}</span>`:''}${d.home_goalie?`<span style="color:var(--d-muted);">🥅 ${hSh}:</span> <span class="pctx-val">${escHtml(d.home_goalie)}${d.home_goalie_confirmed?' ✓':' (unconf.)'}</span>`:''}</div>`;
  if (d.away_pitcher || d.home_pitcher) h += `<div class="proj-ctx-row">${d.away_pitcher?`<span style="color:var(--d-muted);">⚾ ${aSh}:</span> <span class="pctx-val">${escHtml(d.away_pitcher)}</span>`:''}${d.home_pitcher?`<span style="color:var(--d-muted);">⚾ ${hSh}:</span> <span class="pctx-val">${escHtml(d.home_pitcher)}</span>`:''}</div>`;
  const awInj = d.away_injuries||[], hwInj = d.home_injuries||[];
  if (awInj.length || hwInj.length) h += `<div class="proj-ctx-row pctx-inj"><span style="font-size:0.62rem;font-weight:700;">🚑 Out/DTD:</span> ${[awInj.length?aSh+': '+awInj.map(escHtml).join(', '):'', hwInj.length?hSh+': '+hwInj.map(escHtml).join(', '):''].filter(Boolean).join(' · ')}</div>`;
  if (!h.includes('proj-scoreline') && !h.includes('proj-ctx-row')) h += '<div style="padding:8px 12px;font-size:0.72rem;color:var(--d-muted);">No context data yet.</div>';
  return h;
}

// ── AI panel ───────────────────────────────────────────────────────────────
const _aiCache = {};

function _loadAnalysis(betId) {
  delete _aiCache[betId];
  const btn  = document.getElementById('analyze-btn-' + betId);
  const wrap = document.getElementById('analysis-wrap-' + betId);
  if (wrap && wrap.classList.contains('open')) wrap.classList.remove('open');
  if (btn) { btn.classList.remove('open'); btn.textContent = '🤖 +EV Insight'; toggleAnalysisPanel(betId, btn); }
}

function scrollAndAnalyze(betId) {
  const card = document.getElementById('card-' + betId);
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  setTimeout(() => {
    const btn  = document.getElementById('analyze-btn-' + betId);
    const wrap = document.getElementById('analysis-wrap-' + betId);
    if (!wrap) return;
    if (wrap.classList.contains('open')) {
      // Panel already open — just scroll to it
      wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      return;
    }
    // Force-open via toggleAnalysisPanel (btn may be CSS-hidden in compact mode,
    // but it's still in the DOM so closest('.bet-card') works)
    if (btn) {
      toggleAnalysisPanel(betId, btn);
    } else {
      // btn not in DOM at all — open the wrap directly and trigger fetch
      wrap.classList.add('open');
      setTimeout(() => wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 60);
      const loadEl = document.getElementById('ai-loading-' + betId);
      const contentEl = document.getElementById('ai-content-' + betId);
      const errorEl = document.getElementById('ai-error-' + betId);
      if (loadEl) loadEl.style.display = 'block';
      if (contentEl) contentEl.style.display = 'none';
      if (errorEl) errorEl.style.display = 'none';
    }
  }, card ? 450 : 0);
}

function toggleAiPanel(btn) {
  const betId = btn.dataset.betId;
  const panel = document.getElementById('ai-panel-' + betId);
  if (!panel) return;
  const card  = btn.closest('.bet-card');
  const isOpen = panel.classList.contains('open');
  panel.classList.toggle('open', !isOpen);
  btn.style.transition = 'opacity 0.15s';
  btn.style.opacity = '0';
  setTimeout(() => {
    btn.classList.toggle('ai-open', !isOpen);
    btn.textContent = isOpen ? '🤖 +EV Insight' : '✕ Close';
    btn.style.opacity = '1';
  }, 120);
  if (isOpen) return;
  if (_aiCache[betId]) return;
  const loadEl    = document.getElementById('ai-loading-' + betId);
  const contentEl = document.getElementById('ai-content-' + betId);
  const errorEl   = document.getElementById('ai-error-'   + betId);
  if (loadEl)    loadEl.style.display    = 'block';
  if (contentEl) contentEl.style.display = 'none';
  if (errorEl)   errorEl.style.display   = 'none';
  fetch('/api/analysis/' + betId, { credentials: 'same-origin' })
    .then(r => {
      if (!r.ok) {
        return r.json().catch(() => ({})).then(body => {
          throw new Error(r.status + ':' + (body.detail || ''));
        });
      }
      return r.json();
    })
    .then(data => {
      _aiCache[betId] = data;
      if (loadEl)    loadEl.style.display    = 'none';
      if (contentEl) contentEl.style.display = 'block';
      const metaEl  = document.getElementById('ai-meta-'   + betId);
      const bodyEl  = document.getElementById('ai-body-'   + betId);
      const footEl  = document.getElementById('ai-footer-' + betId);
      _renderAiMeta(metaEl, betId, data);
      if (bodyEl) bodyEl.innerHTML = _buildAnalysisHtml(data, card);
      if (footEl) {
        footEl.innerHTML = data.rule_based
          ? 'Powered by model signals'
          : data.generated_at
            ? `Generated at ${new Date(data.generated_at).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'})} — Sharp Analysis`
            : 'Powered by Sharp Analysis';
      }
    })
    .catch(err => {
      if (loadEl)  loadEl.style.display  = 'none';
      if (errorEl) {
        const msg = String(err.message || '');
        if (msg.startsWith('404')) {
          errorEl.textContent = '🔄 Picks refreshed — reload the page to analyze.';
        } else if (msg.startsWith('504')) {
          errorEl.textContent = 'Analysis timed out — tap to retry.';
        } else if (msg.startsWith('500') || msg.startsWith('502')) {
          errorEl.textContent = 'Analysis service error — tap to retry.';
        } else {
          errorEl.textContent = 'Analysis unavailable — tap to retry.';
        }
        errorEl.style.display = 'block';
        errorEl.style.cursor  = 'pointer';
        errorEl.onclick = () => _loadAnalysis(betId);
      }
    });
}

// ── Prop Context Panel ─────────────────────────────────────────────────────
const _propCtxCache = {};

function _loadPropContext(betId, panel) {
  panel.style.display = 'block';
  panel.innerHTML = '<div style="padding:10px 12px;"><div class="skel-bar skel-w90"></div><div class="skel-bar skel-w75"></div><div class="skel-bar skel-w60"></div></div>';

  fetch('/api/prop-context/' + betId, { credentials: 'same-origin' })
    .then(r => {
      if (r.status === 422 || r.status === 403) return null;   // no data or not subscribed
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(data => {
      if (!data) {
        panel.style.display = 'none';
        return;
      }
      _propCtxCache[betId] = data;
      panel.innerHTML = _buildPropContextHtml(data);
    })
    .catch(() => {
      panel.innerHTML = '<div style="padding:8px 12px;font-size:0.72rem;color:var(--d-muted);">⚠️ Context data unavailable.</div>';
    });
}

function _buildPropContextHtml(d) {
  const t = d.prop_context_type;
  const _sv = v => (v == null || v === '---') ? '<span style="color:var(--d-muted);">—</span>' : v;
  const yr  = new Date().getFullYear();

  // ── MLB Pitcher Platoon Splits ──────────────────────────────────────────
  if (t === 'mlb_pitcher_splits') {
    const lhb  = d.vs_lhb || {};
    const rhb  = d.vs_rhb || {};
    const name = d.player_name || 'Pitcher';
    let h = `<div class="prop-ctx-hdr"><span>⚾ ${name} — Platoon Splits</span><span style="font-weight:400;text-transform:none;letter-spacing:0;color:var(--d-muted);">${yr} Season</span></div>`;
    h += `<table class="splits-table"><thead><tr>
      <th>vs</th><th>PA</th><th>BA</th><th>ERA</th><th>WHIP</th><th>K%</th><th>BB%</th><th>HR</th>
    </tr></thead><tbody>`;
    if (lhb.pa != null) {
      const kHi = parseFloat(lhb.k_pct) >= 25 ? ' class="st-highlight"' : '';
      h += `<tr><td>LHB</td><td>${_sv(lhb.pa)}</td><td>${_sv(lhb.ba)}</td><td>${_sv(lhb.era)}</td><td>${_sv(lhb.whip)}</td><td${kHi}>${_sv(lhb.k_pct)}</td><td>${_sv(lhb.bb_pct)}</td><td>${_sv(lhb.hr)}</td></tr>`;
    }
    if (rhb.pa != null) {
      const kHi = parseFloat(rhb.k_pct) >= 25 ? ' class="st-highlight"' : '';
      h += `<tr><td>RHB</td><td>${_sv(rhb.pa)}</td><td>${_sv(rhb.ba)}</td><td>${_sv(rhb.era)}</td><td>${_sv(rhb.whip)}</td><td${kHi}>${_sv(rhb.k_pct)}</td><td>${_sv(rhb.bb_pct)}</td><td>${_sv(rhb.hr)}</td></tr>`;
    }
    h += '</tbody></table>';
    if (!lhb.pa && !rhb.pa) h += '<div style="padding:8px 12px;font-size:0.72rem;color:var(--d-muted);">No split data yet this season.</div>';
    return h;
  }

  // ── MLB Batter Platoon Splits ──────────────────────────────────────────
  if (t === 'mlb_batter_splits') {
    const lhp  = d.vs_lhp || {};
    const rhp  = d.vs_rhp || {};
    const name = d.player_name || 'Batter';
    let h = `<div class="prop-ctx-hdr"><span>⚾ ${name} — Platoon Splits</span><span style="font-weight:400;text-transform:none;letter-spacing:0;color:var(--d-muted);">${yr} Season</span></div>`;
    h += `<table class="splits-table"><thead><tr>
      <th>vs</th><th>PA</th><th>BA</th><th>OBP</th><th>SLG</th><th>OPS</th><th>K%</th><th>HR</th>
    </tr></thead><tbody>`;
    if (lhp.pa != null) {
      const opsHi = parseFloat(lhp.ops) >= 0.9 ? ' class="st-highlight"' : '';
      h += `<tr><td>vs LHP</td><td>${_sv(lhp.pa)}</td><td>${_sv(lhp.ba)}</td><td>${_sv(lhp.obp)}</td><td>${_sv(lhp.slg)}</td><td${opsHi}>${_sv(lhp.ops)}</td><td>${_sv(lhp.k_pct)}</td><td>${_sv(lhp.hr)}</td></tr>`;
    }
    if (rhp.pa != null) {
      const opsHi = parseFloat(rhp.ops) >= 0.9 ? ' class="st-highlight"' : '';
      h += `<tr><td>vs RHP</td><td>${_sv(rhp.pa)}</td><td>${_sv(rhp.ba)}</td><td>${_sv(rhp.obp)}</td><td>${_sv(rhp.slg)}</td><td${opsHi}>${_sv(rhp.ops)}</td><td>${_sv(rhp.k_pct)}</td><td>${_sv(rhp.hr)}</td></tr>`;
    }
    h += '</tbody></table>';
    if (!lhp.pa && !rhp.pa) h += '<div style="padding:8px 12px;font-size:0.72rem;color:var(--d-muted);">No split data yet this season.</div>';

    // Game log streak section
    const gl = d.game_log;
    if (gl && gl.game_log && gl.game_log.length) {
      const label  = gl.stat_label || '';
      const streak = gl.consecutive_streak || 0;
      const hits   = gl.hit_count_last7   || 0;
      const n      = gl.games_checked     || gl.game_log.length;
      const streakColor = streak >= 3 ? 'var(--d-green)' : streak >= 1 ? 'var(--d-primary)' : 'var(--d-muted)';
      h += `<div class="prop-ctx-hdr" style="margin-top:10px;"><span>📊 Recent ${label} — Last ${n} Games</span>
        <span style="font-weight:600;color:${streakColor};">${hits}/${n} games</span></div>`;
      if (streak >= 2) {
        h += `<div style="padding:4px 12px 6px;font-size:0.72rem;color:${streakColor};font-weight:600;">🔥 ${streak}-game ${label} streak</div>`;
      }
      h += '<div>';
      for (const g of gl.game_log) {
        const date = g.date ? g.date.slice(5) : '';
        const ha   = g.home ? 'vs' : '@';
        const hit  = g.val >= 1;
        const dot  = hit ? `<span style="color:var(--d-green);font-weight:700;">✓</span>` : `<span style="color:var(--d-muted);">–</span>`;
        const valCls = hit ? ' class="pgl-val st-highlight"' : ' class="pgl-val"';
        h += `<div class="prop-ctx-gamelog-row">
          <span class="pgl-date">${date}</span>
          <span class="pgl-dec">${dot}</span>
          <span class="pgl-opp">${ha} ${g.opp||'?'}</span>
          <span${valCls}>${g.val} ${label}</span>
        </div>`;
      }
      h += '</div>';
    }
    return h;
  }

  // ── NHL Goalie Stats ────────────────────────────────────────────────────
  if (t === 'nhl_goalie') {
    const name = d.player_name || 'Goalie';
    const rec  = d.recent || [];
    const rec5 = d.wins != null
      ? `${d.wins}-${d.losses||0}${d.ot_losses ? '-' + d.ot_losses : ''}`
      : '—';
    let h = `<div class="prop-ctx-hdr"><span>🥅 ${name} — Goalie Stats</span><span style="font-weight:400;text-transform:none;letter-spacing:0;color:var(--d-muted);">2024–25 Season</span></div>`;
    h += `<div class="prop-ctx-stat-row">
      <div class="prop-ctx-stat"><div class="pcs-val">${d.save_pct != null ? d.save_pct.toFixed(3) : '—'}</div><div class="pcs-lbl">SV%</div></div>
      <div class="prop-ctx-stat"><div class="pcs-val">${d.gaa != null ? d.gaa.toFixed(2) : '—'}</div><div class="pcs-lbl">GAA</div></div>
      <div class="prop-ctx-stat"><div class="pcs-val">${d.shots_against_per_gm != null ? d.shots_against_per_gm : '—'}</div><div class="pcs-lbl">SA/gm</div></div>
      <div class="prop-ctx-stat"><div class="pcs-val">${rec5}</div><div class="pcs-lbl">Record</div></div>
    </div>`;
    if (rec.length) {
      h += '<div>';
      for (const g of rec) {
        const svp  = g.save_pct != null ? g.save_pct.toFixed(3) : '—';
        const dec  = g.decision === 'W' ? '🟢' : g.decision === 'L' ? '🔴' : g.decision === 'O' ? '🟡' : '◻';
        const date = g.date ? g.date.slice(5) : '';
        const ha   = g.home_or_away === 'H' ? 'vs' : '@';
        h += `<div class="prop-ctx-gamelog-row">
          <span class="pgl-date">${date}</span>
          <span class="pgl-dec">${dec}</span>
          <span class="pgl-opp">${ha} ${g.opponent||'?'}</span>
          <span style="color:var(--d-muted);font-size:0.62rem;">${g.shots_against != null ? g.shots_against + ' SA' : ''}</span>
          <span class="pgl-val">SV% ${svp}</span>
        </div>`;
      }
      h += '</div>';
    }
    return h;
  }

  // ── NHL Player Shots on Goal ────────────────────────────────────────────
  if (t === 'nhl_player_sog') {
    const name = d.player_name || 'Player';
    const rec  = d.recent || [];
    let h = `<div class="prop-ctx-hdr"><span>🏒 ${name} — Shots on Goal</span><span style="font-weight:400;text-transform:none;letter-spacing:0;color:var(--d-muted);">2024–25 Season</span></div>`;
    h += `<div class="prop-ctx-stat-row">
      <div class="prop-ctx-stat"><div class="pcs-val">${d.season_sog_per_gm != null ? d.season_sog_per_gm : '—'}</div><div class="pcs-lbl">Season avg</div></div>
      <div class="prop-ctx-stat"><div class="pcs-val">${d.last5_sog_per_gm != null ? d.last5_sog_per_gm : '—'}</div><div class="pcs-lbl">Last 5</div></div>
      <div class="prop-ctx-stat"><div class="pcs-val">${d.last10_sog_per_gm != null ? d.last10_sog_per_gm : '—'}</div><div class="pcs-lbl">Last 10</div></div>
    </div>`;
    if (rec.length) {
      h += '<div>';
      for (const g of rec.slice(0, 7)) {
        const date    = g.date ? g.date.slice(5) : '';
        const ha      = g.home_or_away === 'H' ? 'vs' : '@';
        const sogHi   = (g.sog != null && g.sog >= 4) ? ' class="pgl-val st-highlight"' : ' class="pgl-val"';
        const goalMrk = g.goals > 0 ? ` <span style="color:var(--d-green);font-size:0.6rem;">⚽${g.goals}</span>` : '';
        h += `<div class="prop-ctx-gamelog-row">
          <span class="pgl-date">${date}</span>
          <span class="pgl-opp">${ha} ${g.opponent||'?'}</span>
          ${g.toi ? `<span style="color:var(--d-muted);font-size:0.62rem;">${g.toi}</span>` : ''}
          <span${sogHi}>${g.sog != null ? g.sog + ' SOG' : '—'}${goalMrk}</span>
        </div>`;
      }
      h += '</div>';
    }
    return h;
  }

  return '<div style="padding:8px 12px;font-size:0.72rem;color:var(--d-muted);">No prop context available.</div>';
}

// ── Monte Carlo Simulation Panel ─────────────────────────────────────────
async function toggleSimPanel(betId, btn) {
  const panel = document.getElementById('sim-panel-' + betId);
  if (!panel) return;

  if (panel.style.display !== 'none') {
    panel.style.display = 'none';
    btn.classList.remove('sim-btn-open');
    return;
  }

  panel.style.display = 'block';
  btn.classList.add('sim-btn-open');

  if (panel.dataset.loaded) return;

  panel.innerHTML = '<div class="sim-panel-loading"><div class="skel-bar skel-w90" style="margin:12px 16px 6px;"></div><div class="skel-bar skel-w75" style="margin:0 16px 6px;"></div><div class="skel-bar skel-w60" style="margin:0 16px 16px;"></div></div>';

  try {
    const resp = await fetch('/api/simulation/' + betId, { credentials: 'same-origin' });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || resp.statusText);
    }
    const d = await resp.json();
    panel.innerHTML = _buildSimHtml(d);
    panel.dataset.loaded = '1';
  } catch (e) {
    panel.innerHTML = '<div class="sim-panel-error">Simulation data unavailable: ' + (e.message || 'retry later') + '</div>';
  }
}

function _buildSimHtml(d) {
  const isSoccer  = d.draw_pct > 0;
  const scoreUnit = d.sport_key === 'baseball_mlb' ? 'runs' : 'goals';
  const confCls   = d.confidence >= 65 ? 'sim-conf-high' : d.confidence >= 55 ? 'sim-conf-med' : 'sim-conf-low';

  const homeTeamE = escHtml(d.home_team||'?');
  const awayTeamE = escHtml(d.away_team||'?');

  // Outcome distribution bars
  let bars = `<div class="sim-dist-bars">`;
  bars += _simBar(homeTeamE, d.home_win_pct, 'sim-fill-home', d.projected_outcome === 'Home Win');
  if (isSoccer) bars += _simBar('Draw', d.draw_pct, 'sim-fill-draw', d.projected_outcome === 'Draw');
  bars += _simBar(awayTeamE, d.away_win_pct, 'sim-fill-away', d.projected_outcome === 'Away Win');
  bars += `</div>`;

  // Stats row
  let stats = `<div class="sim-stats-strip">
    <div class="sim-stat-cell">
      <div class="sim-stat-val">${d.avg_home_score.toFixed(1)}</div>
      <div class="sim-stat-lbl">Avg ${escHtml(scoreUnit)}<br>${homeTeamE}</div>
    </div>
    <div class="sim-stat-cell">
      <div class="sim-stat-val">${d.avg_away_score.toFixed(1)}</div>
      <div class="sim-stat-lbl">Avg ${escHtml(scoreUnit)}<br>${awayTeamE}</div>
    </div>`;
  if (d.total_line != null && d.over_pct != null) {
    stats += `<div class="sim-stat-cell">
      <div class="sim-stat-val">${Math.round(d.over_pct)}%</div>
      <div class="sim-stat-lbl">Over ${escHtml(String(d.total_line))}</div>
    </div>
    <div class="sim-stat-cell">
      <div class="sim-stat-val">${Math.round(d.under_pct)}%</div>
      <div class="sim-stat-lbl">Under ${escHtml(String(d.total_line))}</div>
    </div>`;
  }
  stats += `</div>`;

  // Sim-vs-bet verdict badge (primary) + market alignment note (secondary context)
  let mktNote = '';
  if (d.sim_agrees_with_bet === true) {
    mktNote += `<div class="sim-mkt-note sim-mkt-agree">✅ Simulation agrees with this pick</div>`;
  } else if (d.sim_agrees_with_bet === false) {
    mktNote += `<div class="sim-mkt-note sim-mkt-diverge">⚠️ Simulation projects the opposite side</div>`;
  }
  if (d.market_home_win_prob != null) {
    const mktLeader = d.market_home_win_prob > d.market_away_win_prob ? d.home_team : d.away_team;
    const simLeader = d.home_win_pct > d.away_win_pct ? d.home_team : d.away_team;
    const mktSimAligned = mktLeader === simLeader;
    mktNote += `<div class="sim-mkt-note" style="opacity:0.7;font-size:0.68rem;">
      ${mktSimAligned ? '✓ Market aligned with sim' : '⚠ Market diverges from sim'} — ${homeTeamE} ${escHtml(String(d.market_home_win_prob))}% · ${awayTeamE} ${escHtml(String(d.market_away_win_prob))}%
    </div>`;
  }

  // AI summary
  const summaryHtml = d.summary
    ? `<div class="sim-summary">"${escHtml(d.summary)}"</div>`
    : '';

  const updatedAt = d.updated_at
    ? new Date(d.updated_at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
    : '';

  return `
    <div class="sim-panel-hdr">
      <span>🎲 Simulation Analysis</span>
      <span class="sim-conf-chip ${confCls}">${d.confidence.toFixed(1)}% confidence</span>
    </div>
    ${bars}
    ${stats}
    ${mktNote}
    ${summaryHtml}
    <div class="sim-panel-footer">${(d.n_sims || 10000).toLocaleString()} Monte Carlo simulations · Poisson ${isSoccer ? 'goal' : 'run'} model · The Odds API calibrated${updatedAt ? ' · Updated ' + updatedAt : ''}</div>`;
}

function _simBar(label, pct, fillCls, isWinner) {
  // All rows use identical structure (label + track + pct) so the track
  // width is consistent and width:X% maps correctly to the displayed pct.
  // Winner styling is applied via CSS class only — no extra flex children.
  return `<div class="sim-dist-row ${isWinner ? 'sim-row-winner' : ''}">
    <span class="sim-dist-lbl">${isWinner ? '▶ ' : ''}${label}</span>
    <div class="sim-dist-track"><div class="sim-dist-fill ${fillCls}" style="width:${Math.min(pct, 100)}%"></div></div>
    <span class="sim-dist-pct ${isWinner ? 'sim-dist-pct-winner' : ''}">${pct.toFixed(1)}%</span>
  </div>`;
}

// ── Combined Analysis Panel (Projection + AI) ────────────────────────────
function toggleAnalysisPanel(betId, btn) {
  try {
  const wrap = document.getElementById('analysis-wrap-' + betId);
  if (!wrap) return;
  const card   = btn ? btn.closest('.bet-card') : document.getElementById('card-' + betId);
  const isOpen = wrap.classList.contains('open');

  if (isOpen) {
    wrap.classList.remove('open');
    btn.classList.remove('open');
    { const t = btn.querySelector('.btn-analyze-top'); if (t) t.textContent = '🎯 Should I Bet This?'; }
    { const s = btn.querySelector('.btn-analyze-sub'); if (s) s.style.display = ''; }
    return;
  }

  // Open the panel
  wrap.classList.add('open');
  btn.classList.add('open');
  { const t = btn.querySelector('.btn-analyze-top'); if (t) t.textContent = '✕ Close Analysis'; }
  { const s = btn.querySelector('.btn-analyze-sub'); if (s) s.style.display = 'none'; }
  // Bring the panel into view so the user can see it
  setTimeout(() => wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 60);

  // Load projection data if not already loaded (game bets)
  const projPanel = document.getElementById('proj-panel-' + betId);
  if (projPanel && projPanel.innerHTML.trim() === '') {
    if (_projCache[betId]) {
      projPanel.innerHTML = _buildProjHtml(_projCache[betId]);
    } else {
      // Use a silent dummy btn — _loadProjection sets .textContent/.disabled on it harmlessly
      const _db = { textContent: '', disabled: false };
      _loadProjection(betId, _db, projPanel);
    }
  }

  // Load prop context if not already loaded (prop bets)
  const propCtxPanel = document.getElementById('prop-ctx-panel-' + betId);
  if (propCtxPanel && propCtxPanel.style.display === 'none') {
    if (_propCtxCache[betId]) {
      propCtxPanel.innerHTML = _buildPropContextHtml(_propCtxCache[betId]);
      propCtxPanel.style.display = 'block';
    } else {
      _loadPropContext(betId, propCtxPanel);
    }
  }

  // Load AI analysis if not cached
  const loadEl    = document.getElementById('ai-loading-' + betId);
  const contentEl = document.getElementById('ai-content-' + betId);
  const errorEl   = document.getElementById('ai-error-'   + betId);

  if (_aiCache[betId]) {
    // Already cached — ensure visible
    if (loadEl)    loadEl.style.display    = 'none';
    if (contentEl) contentEl.style.display = 'block';
    return;
  }

  // Show skeleton loader
  if (loadEl)    loadEl.style.display    = 'block';
  if (contentEl) contentEl.style.display = 'none';
  if (errorEl)   errorEl.style.display   = 'none';

  fetch('/api/analysis/' + betId)
    .then(r => {
      if (!r.ok) {
        return r.json().catch(() => ({})).then(b => {
          throw new Error(r.status + ':' + (b.detail || ''));
        });
      }
      return r.json();
    })
    .then(data => {
      _aiCache[betId] = data;
      if (loadEl)    loadEl.style.display    = 'none';
      if (contentEl) contentEl.style.display = 'block';
      const metaEl = document.getElementById('ai-meta-'   + betId);
      const bodyEl = document.getElementById('ai-body-'   + betId);
      const footEl = document.getElementById('ai-footer-' + betId);
      _renderAiMeta(metaEl, betId, data);
      if (bodyEl) bodyEl.innerHTML = _buildAnalysisHtml(data, card);
      if (footEl) {
        footEl.innerHTML = data.rule_based
          ? 'Powered by model signals'
          : data.generated_at
            ? `Generated at ${new Date(data.generated_at).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'})} — Claude Opus 4.6`
            : 'Powered by Sharp Analysis';
      }
    })
    .catch(err => {
      if (loadEl)  loadEl.style.display  = 'none';
      if (errorEl) {
        const msg = String(err.message || '');
        errorEl.textContent = msg.startsWith('404') ? '🔄 Picks refreshed — reload the page to analyze.'
                            : msg.startsWith('504') ? 'Analysis timed out — tap to retry.'
                            : msg.startsWith('5')   ? 'Analysis service error — tap to retry.'
                            : 'Analysis unavailable — tap to retry.';
        errorEl.style.display = 'block';
        errorEl.style.cursor  = 'pointer';
        errorEl.onclick = () => {
          delete _aiCache[betId];
          errorEl.style.display   = 'none';
          if (loadEl)    loadEl.style.display    = 'block';
          if (contentEl) contentEl.style.display = 'none';
          fetch('/api/analysis/' + betId, { credentials: 'same-origin' })
            .then(r => r.ok ? r.json() : r.json().catch(()=>({})).then(b=>{throw new Error(r.status+':'+(b.detail||''));}))
            .then(data => {
              _aiCache[betId] = data;
              if (loadEl)    loadEl.style.display    = 'none';
              if (contentEl) contentEl.style.display = 'block';
              const metaEl = document.getElementById('ai-meta-'   + betId);
              const bodyEl = document.getElementById('ai-body-'   + betId);
              const footEl = document.getElementById('ai-footer-' + betId);
              _renderAiMeta(metaEl, betId, data);
              if (bodyEl) bodyEl.innerHTML = _buildAnalysisHtml(data, card);
              if (footEl) footEl.innerHTML = data.rule_based ? 'Powered by model signals' : 'Powered by Sharp Analysis';
            })
            .catch(() => { if (loadEl) loadEl.style.display='none'; if (errorEl) errorEl.style.display='block'; });
        };
      }
    });
  } catch (e) {
    console.error('toggleAnalysisPanel error:', e);
    const errorEl = document.getElementById('ai-error-' + betId);
    const wrap    = document.getElementById('analysis-wrap-' + betId);
    if (wrap) wrap.classList.add('open');
    if (errorEl) { errorEl.textContent = 'Analysis unavailable — tap to retry.'; errorEl.style.display = 'block'; }
  }
}

// ── EV Modal ───────────────────────────────────────────────────────────────
function impliedProb(o) { o=parseInt(o,10); return o<0?Math.abs(o)/(Math.abs(o)+100):100/(o+100); }
function openEvModal(card) {
  const odds=parseInt(card.dataset.odds,10), tp=parseFloat(card.dataset.trueProb);
  const ev=parseFloat(card.dataset.ev), team=card.dataset.team, game=card.dataset.game;
  const market=card.dataset.marketDisplay, adjFlags=card.dataset.adjFlags||'', openingOdds=card.dataset.openingOdds?parseInt(card.dataset.openingOdds,10):null;
  const bookProb=impliedProb(odds), edgePp=tp-bookProb, oddsStr=odds>0?'+'+odds:String(odds);
  const payout=odds>0?odds/100:100/Math.abs(odds), evPerHundo=((tp*payout)-(1-tp))*100;
  document.getElementById('evModalGame').textContent=game?game+' · '+team:team;
  document.getElementById('evModalBookProb').textContent=(bookProb*100).toFixed(1)+'%';
  document.getElementById('evModalTrueProb').textContent=(tp*100).toFixed(1)+'%';
  document.getElementById('evModalEdge').textContent='+'+(edgePp*100).toFixed(1)+' pp';
  const ctx={Moneyline:'win outright',Spread:'cover the spread',Total:'hit the total'}[market]||'hit this outcome';
  document.getElementById('evModalExplain').innerHTML=`The book prices <strong>${oddsStr}</strong> at <strong>${(bookProb*100).toFixed(1)}%</strong>. The sharp market consensus puts fair probability at <strong>${(tp*100).toFixed(1)}%</strong>. That <strong>${(edgePp*100).toFixed(1)} pp gap</strong> is your edge — the book is underpricing the probability this ${ctx}.`;
  document.getElementById('evModalFormula').innerHTML=`<strong style="color:var(--d-green);">EV per $100 wagered</strong><br>(${(tp*100).toFixed(1)}% × $${(payout*100).toFixed(2)}) − (${((1-tp)*100).toFixed(1)}% × $100) = <strong style="color:var(--d-green);">+$${evPerHundo.toFixed(2)}</strong>`;
  const flags=adjFlags?adjFlags.split('|').filter(f=>f.trim()):[];
  const fe=document.getElementById('evModalFlags'), fp=document.getElementById('evModalFlagPills');
  if (flags.length){fp.innerHTML=flags.map(f=>`<span style="background:rgba(255,122,69,0.1);color:var(--d-orange);border:1px solid rgba(255,122,69,0.25);border-radius:10px;font-size:0.68rem;padding:2px 10px;white-space:nowrap;">${f.trim()}</span>`).join('');fe.style.display='block';}else{fe.style.display='none';}
  const ci=document.getElementById('evModalClvInfo');
  if(openingOdds&&openingOdds!==odds){const oStr=openingOdds>0?'+'+openingOdds:String(openingOdds);const cStr=odds>0?'+'+odds:String(odds);const shorter=openingOdds>odds;ci.innerHTML=`<div style="padding:8px 10px;border-radius:8px;background:${shorter?'rgba(0,214,143,0.06)':'rgba(255,122,69,0.06)'};border:1px solid ${shorter?'rgba(0,214,143,0.2)':'rgba(255,122,69,0.2)'};font-size:0.75rem;color:var(--d-sub);">📊 Line: <strong>${oStr}</strong> → <strong>${cStr}</strong> — ${shorter?'<span style="color:var(--d-green);">CLV confirmed ✓</span>':'<span style="color:var(--d-orange);">CLV monitoring ⚠️</span>'}</div>`;ci.style.display='block';}else{ci.style.display='none';}
  document.getElementById('evModalOverlay').classList.add('open');
  document.body.style.overflow='hidden';
}
function closeEvModal(e){if(e&&e.target!==document.getElementById('evModalOverlay'))return;document.getElementById('evModalOverlay').classList.remove('open');document.body.style.overflow='';}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeEvModal();hideClvPopover();}});

// ── CLV Popover ────────────────────────────────────────────────────────────
const _clvPos='<strong>Line moved in your favor — strong signal.</strong><br><br>The closing line is the most efficient price the market produces. When it shortens after your bet appears, sharp money agreed with the edge.<br><br><span class="clv-highlight">Beating the closing line consistently is the gold standard proof of a real edge.</span>';
const _clvNeg='<strong>Line moved against this bet — monitor it.</strong><br><br>The market hasn\'t confirmed this edge yet. Sharp money moved the other direction. The bet may still be +EV if the book is slow to adjust, but confirmation is pending.';
function showClvPopover(e,isPos){
  e.stopPropagation();
  const pop=document.getElementById('clvPopover');
  document.getElementById('clvPopBody').innerHTML=isPos?_clvPos:_clvNeg;
  document.getElementById('clvPopTitle').innerHTML=isPos?'📈 CLV Confirmed':'⚠️ CLV — Monitor';
  pop.classList.add('visible');
  const popW=300,popH=pop.offsetHeight,rect=e.target.getBoundingClientRect();
  let left=rect.left+rect.width/2-popW/2, top=rect.top-popH-10;
  if(top<8)top=rect.bottom+10;
  left=Math.max(8,Math.min(left,window.innerWidth-popW-8));
  pop.style.left=left+'px';pop.style.top=top+'px';pop.style.maxWidth=popW+'px';
}
function hideClvPopover(){document.getElementById('clvPopover').classList.remove('visible');}
document.addEventListener('click',e=>{const p=document.getElementById('clvPopover');if(p.classList.contains('visible')&&!p.contains(e.target))hideClvPopover();});
document.addEventListener('scroll',hideClvPopover,true);

// ── Sharp Action Popover ────────────────────────────────────────────────────
function showSharpPopover(e, score){
  e.stopPropagation();
  hideClvPopover();
  const strong = score >= 80;
  const title  = strong ? '⚡ Strong Sharp Action' : '⚡ Sharp Action';
  const body   = strong
    ? '<strong>Professional money is heavily on this side.</strong><br><br>A high sharp score means two things are true: (1) the public is mostly betting the other side, and (2) the line has moved <em>toward</em> this bet since opening. That combination means large, sophisticated bettors are driving the price — and they agree with our model.<br><br><span class="clv-highlight">Sharp consensus + model edge = the strongest setup we surface.</span>'
    : '<strong>Professional bettors appear to be on this side.</strong><br><br>Sharps are the small percentage of bettors that books actually fear — they bet based on edge, not gut. When the public is mostly on the other side but the line moves <em>this</em> way, it\'s sharp money pushing it.<br><br>Combined with our +EV model finding an edge here, this is a meaningful confirmation signal.';
  document.getElementById('sharpPopTitle').textContent = title;
  document.getElementById('sharpPopBody').innerHTML    = body;
  const pop=document.getElementById('sharpPopover');
  pop.classList.add('visible');
  const popW=300,popH=pop.offsetHeight,rect=e.target.getBoundingClientRect();
  let left=rect.left+rect.width/2-popW/2, top=rect.top-popH-10;
  if(top<8)top=rect.bottom+10;
  left=Math.max(8,Math.min(left,window.innerWidth-popW-8));
  pop.style.left=left+'px'; pop.style.top=top+'px'; pop.style.maxWidth=popW+'px';
}
function hideSharpPopover(){document.getElementById('sharpPopover').classList.remove('visible');}
document.addEventListener('click',e=>{const p=document.getElementById('sharpPopover');if(p.classList.contains('visible')&&!p.contains(e.target))hideSharpPopover();});
document.addEventListener('scroll',hideSharpPopover,true);
document.addEventListener('keydown',e=>{if(e.key==='Escape')hideSharpPopover();});

// ── NCAAF Saturday slate banner ───────────────────────────────────────────
(function _ncaafSaturdayBanner() {
  if (new Date().getDay() !== 6) return;  // only Saturdays
  const cards = document.querySelectorAll('.bet-card[data-league="americanfootball_ncaaf"]');
  const count = cards.length;
  if (!count) return;
  const banner = document.createElement('div');
  banner.id = 'ncaafSaturdayBanner';
  banner.style.cssText = 'display:flex;align-items:center;gap:14px;background:rgba(83,74,183,0.08);border:1px solid rgba(83,74,183,0.4);border-radius:10px;padding:12px 18px;margin-bottom:16px;';
  banner.innerHTML = `
    <span style="font-size:1.3rem;flex-shrink:0;">🏟</span>
    <span style="color:#a78bfa;font-weight:700;font-size:0.93rem;">Saturday College Football Slate —</span>
    <span style="color:rgba(255,255,255,.65);font-size:0.88rem;"> ${count} +EV pick${count !== 1 ? 's' : ''} across today's games</span>
    <button onclick="document.getElementById('ncaafSaturdayBanner').remove()" style="margin-left:auto;background:none;border:none;color:rgba(255,255,255,.35);cursor:pointer;font-size:1rem;flex-shrink:0;">✕</button>
  `;
  const grid = document.getElementById('cardsGrid');
  if (grid && grid.parentNode) grid.parentNode.insertBefore(banner, grid);
})();

// ── Cross-book price comparison renderer ──────────────────────────────────
(function _renderBookComparisons() {
  const NAMES = {
    draftkings:'DraftKings', fanduel:'FanDuel', betmgm:'BetMGM',
    caesars:'Caesars', williamhill_us:'Caesars', pointsbet:'Fanatics',
    betrivers:'BetRivers', espnbet:'ESPN Bet', hardrockbet:'Hard Rock',
    betonlineag:'BetOnline', mybookie:'MyBookie', bovada:'Bovada',
    pinnacle:'Pinnacle', novig:'NoVig', prophetx:'ProphetX',
    betopenly:'BetOpenly', betfair_ex_uk:'Betfair', betfair_ex_us:'Betfair US',
    kalshi:'Kalshi', polymarket:'Polymarket',
  };

  function fmtOdds(n) { return n > 0 ? '+' + n : '' + n; }

  document.querySelectorAll('.bet-card[data-all-book-odds]').forEach(card => {
    const wrap = card.querySelector('.odds-compare-wrap');
    if (!wrap) return;

    let map;
    try { map = JSON.parse(card.dataset.allBookOdds); } catch(e) { return; }

    const entries = Object.entries(map)
      .filter(([, o]) => typeof o === 'number' && isFinite(o))
      .sort((a, b) => b[1] - a[1]);   // highest American odds first = best price first

    const currentBook = (card.dataset.book || '').toLowerCase();
    const currentOdds = parseInt(card.dataset.odds, 10);

    // Identify the card's own book/odds so we can exclude it from the comparison
    const isOurs = ([book, odds]) =>
      book.toLowerCase() === currentBook || odds === currentOdds;

    // Comparison shows only the *other* books — the card header already names ours
    // Only show books whose odds are equal-to or worse-than the card's odds.
    // If another book somehow has a better price, the card should be showing
    // that book — showing it here would confuse users about where to bet.
    const alts = entries.filter(e => !isOurs(e) && e[1] <= currentOdds);
    if (alts.length < 1) return;   // no alternatives to compare against

    // Show up to 4 alternative pills, best price first
    const shown = alts.slice(0, 4);

    // Pills HTML — no highlighting needed since the card's book is excluded
    const pillsHtml = shown.map(([book, odds]) => {
      const name = NAMES[book.toLowerCase()] || (book.charAt(0).toUpperCase() + book.slice(1));
      const label = name.length > 11 ? name.slice(0, 10) + '…' : name;
      return `<div class="oc-pill">
        <span class="oc-book">${label}</span>
        <span class="oc-odds">${fmtOdds(odds)}</span>
      </div>`;
    }).join('');

    // Savings note: compare our price to the best alternative
    let noteHtml = '';
    const [nextBook, nextOdds] = alts[0];
    const gap = currentOdds - nextOdds;  // positive = we have the better price
    const nextName = NAMES[nextBook.toLowerCase()] || nextBook;
    if (gap > 0) {
      noteHtml = `<div class="odds-compare-note">
        <span class="oc-note-pos">+${gap} pts</span> better than ${nextName} (${fmtOdds(nextOdds)})
      </div>`;
    } else if (gap < 0) {
      noteHtml = `<div class="odds-compare-note">
        ${nextName} has <span class="oc-note-pos">${fmtOdds(nextOdds)}</span> — best available
      </div>`;
    } else {
      noteHtml = `<div class="odds-compare-note">Same price at ${nextName} — shop for line movement</div>`;
    }

    wrap.innerHTML = `
      <div class="odds-compare-label">Odds comparison</div>
      <div class="odds-compare-pills">${pillsHtml}</div>
      ${noteHtml}`;
    card.dataset.hasCompare = '1';   // flag used by the "Multi-book" filter
  });
})();

// ── Analysis panel meta renderer ──────────────────────────────────────────
// Computes Kelly directly from the card's data-prob / data-odds so it always
// matches the metric cell on the card (same formula, same fraction selector).
// ── Visual analysis panel builder ──────────────────────────────────────────
function _buildAnalysisHtml(data, card) {
  const conf     = data.confidence_score || 0;
  const confCls  = conf >= 75 ? 'acf-high' : conf >= 50 ? 'acf-med' : 'acf-low';
  const confColour = conf >= 75 ? '#00D68F' : conf >= 50 ? '#F5C542' : '#FF5B5B';

  // Verdict badge
  const rec    = data.recommended_action || _extractVerdict(data.analysis) || 'Value Bet';
  const recCls = rec.toLowerCase().includes('strong')   ? 'avb-strong'
               : rec.toLowerCase().includes('moderate') ? 'avb-moderate'
               : rec.toLowerCase().includes('value')    ? 'avb-value'
               : 'avb-lean';

  // Probabilities — read from card dataset (already rendered server-side)
  const trueProb = parseFloat((card && card.dataset.trueProb) || '0');
  const rawOdds  = parseInt((card && card.dataset.odds) || '-110', 10);
  const impliedProb = rawOdds > 0 ? 100 / (rawOdds + 100) : Math.abs(rawOdds) / (Math.abs(rawOdds) + 100);
  const edgePP   = ((trueProb - impliedProb) * 100).toFixed(1);
  const truePct  = (trueProb * 100).toFixed(1);
  const implPct  = (impliedProb * 100).toFixed(1);
  // Scale bars relative to whichever is larger
  const maxP     = Math.max(trueProb, impliedProb);
  const modelW   = maxP > 0 ? ((trueProb / maxP) * 100).toFixed(1) : '100';
  const bookW    = maxP > 0 ? ((impliedProb / maxP) * 100).toFixed(1) : '100';
  const edgeSign = parseFloat(edgePP) >= 0 ? '+' : '';

  // ── Signal chips ─────────────────────────────────────────────────────────
  let chips = '';

  // EV chip — always
  const evPct = parseFloat((card && card.dataset.ev) || '0');
  chips += `<div class="ai-sig sig-ev">
    <span class="ai-sig-icon">⚡</span>
    <div class="ai-sig-body">
      <span class="ai-sig-label">EV Edge</span>
      <span class="ai-sig-val">+${evPct.toFixed(1)}%</span>
    </div>
  </div>`;

  // Sharp money chip
  const moneyPct = (card && card.dataset.moneyPct != null) ? parseFloat(card.dataset.moneyPct) : null;
  const betPct   = (card && card.dataset.betPct   != null) ? parseFloat(card.dataset.betPct)   : null;
  if (moneyPct != null) {
    const isRev  = betPct != null && moneyPct > betPct + 8;
    const icon   = isRev ? '🔄' : moneyPct >= 60 ? '💰' : moneyPct < 40 ? '🔻' : '💵';
    const cls    = moneyPct >= 55 ? 'sig-sharp' : moneyPct < 40 ? 'sig-neg' : '';
    const label  = isRev ? 'Reverse LM' : 'Sharp $';
    const val    = isRev ? `${Math.round(betPct)}% bets / ${Math.round(moneyPct)}% $` : `${Math.round(moneyPct)}% on side`;
    chips += `<div class="ai-sig ${cls}">
      <span class="ai-sig-icon">${icon}</span>
      <div class="ai-sig-body">
        <span class="ai-sig-label">${label}</span>
        <span class="ai-sig-val">${val}</span>
      </div>
    </div>`;
  }

  // Sharp score chip
  const sharpSc = (card && card.dataset.sharpScore != null) ? parseFloat(card.dataset.sharpScore) : null;
  if (sharpSc != null) {
    const icon = sharpSc >= 70 ? '🎯' : sharpSc >= 50 ? '📊' : '📉';
    const cls  = sharpSc >= 65 ? 'sig-sharp' : sharpSc < 35 ? 'sig-neg' : '';
    chips += `<div class="ai-sig ${cls}">
      <span class="ai-sig-icon">${icon}</span>
      <div class="ai-sig-body">
        <span class="ai-sig-label">Sharp Score</span>
        <span class="ai-sig-val">${Math.round(sharpSc)}/100</span>
      </div>
    </div>`;
  }

  // Line movement chip
  const openOdds = (card && card.dataset.openingOdds) ? parseInt(card.dataset.openingOdds, 10) : null;
  if (openOdds != null) {
    const curOdds = rawOdds;
    const favorable = openOdds > curOdds;  // line shortened = market confirmed bet = positive CLV
    const opStr  = openOdds > 0 ? `+${openOdds}` : String(openOdds);
    const cuStr  = curOdds  > 0 ? `+${curOdds}`  : String(curOdds);
    chips += `<div class="ai-sig ${favorable ? 'sig-clv' : 'sig-neg'}">
      <span class="ai-sig-icon">${favorable ? '📈' : '📉'}</span>
      <div class="ai-sig-body">
        <span class="ai-sig-label">Line Move</span>
        <span class="ai-sig-val">${opStr} → ${cuStr}</span>
      </div>
    </div>`;
  }

  // Projection chip (game bets)
  const homeWp = (card && card.dataset.projHomeWp) ? parseFloat(card.dataset.projHomeWp) : null;
  if (homeWp != null) {
    chips += `<div class="ai-sig sig-proj">
      <span class="ai-sig-icon">🎲</span>
      <div class="ai-sig-body">
        <span class="ai-sig-label">Model Proj</span>
        <span class="ai-sig-val">Home ${(homeWp*100).toFixed(0)}%</span>
      </div>
    </div>`;
  }

  // Team form chips — parse home_trend / away_trend ("7-3 W4" format)
  function _parseTrend(s) {
    if (!s) return null;
    const m = s.match(/^(\d+)-(\d+)\s+([WL])(\d+)$/);
    if (!m) return null;
    return { wins: +m[1], losses: +m[2], dir: m[3], streak: +m[4] };
  }
  const homeTrendRaw = (card && card.dataset.homeTrend) || '';
  const awayTrendRaw = (card && card.dataset.awayTrend) || '';
  const gameStr      = (card && card.dataset.game) || '';
  const isPropCard   = (card && card.dataset.isProp) === 'true';
  const parts        = gameStr.includes(' @ ') ? gameStr.split(' @ ') : [];
  const awayName     = parts[0] || '';
  const homeName     = parts[1] || '';

  if (!isPropCard && (homeTrendRaw || awayTrendRaw)) {
    for (const [side, raw, name] of [['home', homeTrendRaw, homeName], ['away', awayTrendRaw, awayName]]) {
      const t = _parseTrend(raw);
      if (!t) continue;
      const icon  = t.dir === 'W' && t.streak >= 3 ? '🔥' : t.dir === 'L' && t.streak >= 3 ? '❄️' : t.dir === 'W' ? '📈' : '📉';
      const cls   = t.dir === 'W' ? (t.streak >= 3 ? 'sig-sharp' : '') : 'sig-neg';
      const label = `${name || (side === 'home' ? 'Home' : 'Away')} L10`;
      const val   = `${t.wins}-${t.losses} · ${t.dir}${t.streak}`;
      chips += `<div class="ai-sig ${cls}">
        <span class="ai-sig-icon">${icon}</span>
        <div class="ai-sig-body">
          <span class="ai-sig-label">${label}</span>
          <span class="ai-sig-val">${val}</span>
        </div>
      </div>`;
    }
  }

  // ── H2H matchup block — ML picks only ───────────────────────────────────
  let h2hHtml = '';
  const isMLPick = (card && card.dataset.market) === 'h2h';
  if (isMLPick && (data.home_record || data.away_record)) {
    const aName  = data.away_team  || awayName || 'Away';
    const hName  = data.home_team  || homeName || 'Home';
    const aRec   = data.away_record  || '—';
    const hRec   = data.home_record  || '—';
    const aStrk  = data.away_streak  || '';
    const hStrk  = data.home_streak  || '';
    const _srkIcon = s => {
      if (!s) return '';
      return s.startsWith('W') ? ' 🔥' : s.startsWith('L') ? ' ❄️' : '';
    };
    h2hHtml = `<div style="margin:10px 0 4px;padding:8px 12px;background:var(--d-card-bg,rgba(255,255,255,0.04));border-radius:8px;border:1px solid var(--d-border,rgba(255,255,255,0.08));">
      <div style="font-size:0.65rem;text-transform:uppercase;letter-spacing:.07em;color:var(--d-muted);margin-bottom:6px;">Season Records</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <div style="flex:1;min-width:110px;">
          <div style="font-size:0.7rem;color:var(--d-muted);margin-bottom:2px;">@ ${hName}</div>
          <div style="font-size:0.9rem;font-weight:700;font-variant-numeric:tabular-nums;">${hRec}<span style="font-size:0.7rem;font-weight:400;color:var(--d-muted);margin-left:4px;">${hStrk}${_srkIcon(hStrk)}</span></div>
        </div>
        <div style="flex:1;min-width:110px;">
          <div style="font-size:0.7rem;color:var(--d-muted);margin-bottom:2px;">${aName} (away)</div>
          <div style="font-size:0.9rem;font-weight:700;font-variant-numeric:tabular-nums;">${aRec}<span style="font-size:0.7rem;font-weight:400;color:var(--d-muted);margin-left:4px;">${aStrk}${_srkIcon(aStrk)}</span></div>
        </div>
      </div>
    </div>`;
  }

  // ── Why-bet narrative ─────────────────────────────────────────────────────
  // Strip the leading **Verdict**\n\n from the analysis text
  const rawText = (data.analysis || '');
  const whyRaw  = rawText.replace(/^\*\*[^*]+\*\*\n\n?/, '').trim();
  const whyHtml = whyRaw
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\n\n/g, '</p><p style="margin-top:7px;">')
    .replace(/\n/g, '<br>');

  return `<div class="ai-vis">
    <div class="ai-verdict-row">
      <span class="ai-verdict-badge ${recCls}">${rec}</span>
      <div class="ai-conf-wrap">
        <div class="ai-conf-track">
          <div class="ai-conf-fill ${confCls}" style="width:${conf}%"></div>
        </div>
        <span class="ai-conf-num" style="color:${confColour}">Confidence ${conf}%</span>
      </div>
    </div>

    ${h2hHtml}

    ${whyHtml ? `<div class="ai-why">
      <div class="ai-why-body"><p>${whyHtml}</p></div>
    </div>` : ''}

    <div class="ai-signals">${chips}</div>
  </div>`;
}

function _extractVerdict(text) {
  const m = (text || '').match(/^\*\*([^*]+)\*\*/);
  return m ? m[1] : '';
}

function _renderAiMeta(metaEl, betId, data) {
  // Confidence + verdict now live inside _buildAnalysisHtml — meta only shows
  // the Kelly chip and edge tag so the header stays uncluttered.
  if (!metaEl) return;
  let html = '';
  const card    = document.querySelector(`.bet-card[data-bet-id="${betId}"]`);
  const kellyEl = card ? card.querySelector('.kelly-val') : null;
  if (kellyEl) {
    const kPct   = halfKelly(parseFloat(kellyEl.dataset.prob), kellyEl.dataset.odds);
    const kLabel = _kellyFracLabel(_getKellyFrac());
    if (kPct > 0)
      html += `<span class="ai-kelly-chip">${kLabel}: ${(kPct * 100).toFixed(1)}%</span>`;
  }
  if (data.edge_tag) html += ` <span class="ai-edge-tag">⚡ ${data.edge_tag}</span>`;
  metaEl.innerHTML = html;
}

// ── Tooltip: body-appended, position:fixed (escapes overflow:hidden) ───────
function initTooltips() {
  var activeTooltip = null;
  document.querySelectorAll('.tooltip-wrap').forEach(function(wrap) {
    var icon = wrap.querySelector('.tooltip-icon');
    var box  = wrap.querySelector('.tooltip-box');
    if (!icon || !box) return;
    // Move tooltip to body so it's never clipped by any ancestor overflow
    box.remove();
    document.body.appendChild(box);
    box.style.position   = 'fixed';
    box.style.zIndex     = '99999';
    box.style.pointerEvents = 'none';
    box.style.opacity    = '0';
    box.style.transition = 'opacity 0.15s ease';
    // Desktop: mouseenter / mouseleave
    icon.addEventListener('mouseenter', function() {
      if (activeTooltip && activeTooltip !== box) activeTooltip.style.opacity = '0';
      var r    = icon.getBoundingClientRect();
      var tipW = window.innerWidth < 600 ? 180 : 220;
      var left = r.left + r.width / 2 - tipW / 2;
      var top  = r.top - 8;
      if (left < 8) left = 8;
      if (left + tipW > window.innerWidth - 8) left = window.innerWidth - tipW - 8;
      box.style.width  = tipW + 'px';
      box.style.left   = left + 'px';
      box.style.bottom = (window.innerHeight - top) + 'px';
      box.style.top    = 'auto';
      box.style.opacity = '1';
      activeTooltip = box;
    });
    icon.addEventListener('mouseleave', function() {
      box.style.opacity = '0';
      activeTooltip = null;
    });
    // Mobile: tap-to-show for 3 seconds
    icon.addEventListener('touchstart', function(e) {
      e.preventDefault();
      if (activeTooltip && activeTooltip !== box) activeTooltip.style.opacity = '0';
      var r    = icon.getBoundingClientRect();
      var tipW = 180;
      var left = r.left + r.width / 2 - tipW / 2;
      var top  = r.top - 8;
      if (left < 8) left = 8;
      if (left + tipW > window.innerWidth - 8) left = window.innerWidth - tipW - 8;
      box.style.width  = tipW + 'px';
      box.style.left   = left + 'px';
      box.style.bottom = (window.innerHeight - top) + 'px';
      box.style.top    = 'auto';
      box.style.opacity = '1';
      activeTooltip = box;
      setTimeout(function() { box.style.opacity = '0'; activeTooltip = null; }, 3000);
    }, { passive: false });
  });
}


// ── Middles pill ───────────────────────────────────────────────────────────
const middlesPill = document.getElementById('middlesPill');
const middlesGrid = document.getElementById('middlesGrid');
const _cardsGridEl = document.getElementById('cardsGrid');
let _middlesActive = false;
if (middlesPill) {
  middlesPill.addEventListener('click', function() {
    _middlesActive = !_middlesActive;
    middlesPill.classList.toggle('active', _middlesActive);
    if (_middlesActive) {
      _cardsGridEl.style.display = 'none';
      middlesGrid.style.display = 'grid';
      document.querySelectorAll('.sport-pill').forEach(function(p) { if (p !== middlesPill) p.classList.remove('active'); });
    } else {
      _cardsGridEl.style.display = '';
      middlesGrid.style.display = 'none';
    }
  });
}
// When any other pill is clicked, deactivate middles
document.querySelectorAll('.sport-pill').forEach(function(p) {
  if (p.id !== 'middlesPill') {
    p.addEventListener('click', function() {
      if (_middlesActive) {
        _middlesActive = false;
        middlesPill && middlesPill.classList.remove('active');
        _cardsGridEl.style.display = '';
        middlesGrid.style.display = 'none';
      }
    });
  }
});

// ── scrollToCardById (leaderboard) ────────────────────────────────────────
function scrollToCardById(betId) {
  var card = document.querySelector('[data-bet-id="' + betId + '"]');
  if (!card) return;
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  card.style.outline = '2px solid #534AB7';
  card.style.outlineOffset = '3px';
  setTimeout(function() { card.style.outline = ''; card.style.outlineOffset = ''; }, 2000);
}

function showToast(msg) {
  const t = document.createElement('div');
  t.className = 'posit-toast';
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.classList.add('toast-show'), 10);
  setTimeout(() => { t.classList.remove('toast-show'); setTimeout(() => t.remove(), 300); }, 2500);
}

function toggleWatchlist(betId, btn) {
  console.log('toggleWatchlist fired for bet', betId);
  if (btn.classList.contains('added') || btn.disabled) return;
  btn.disabled = true;
  fetch('/watchlist/add', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bet_cache_id: betId })
  }).then(async function (resp) {
    if (!resp.ok) {
      console.error('Watchlist failed:', resp.status, await resp.text());
      btn.disabled = false;
      showToast('Could not add to Paper Trail');
      return;
    }
    const data = await resp.json();
    if (data.success) {
      btn.classList.add('added');
      btn.title = 'Added to Paper Trail';
      showToast('⭐ Added to paper trail');
    } else {
      btn.disabled = false;
      showToast('Could not add to Paper Trail');
    }
  }).catch(function (err) {
    console.error('Watchlist failed:', err);
    btn.disabled = false;
    showToast('Could not add to Paper Trail');
  });
}

function sharePick(betId, btn) {
  const card = document.querySelector('.bet-card[data-bet-id="' + betId + '"]');
  if (!card) return;
  const league = card.querySelector('.badge-league, .badge-nfl, .badge-ncaaf')?.textContent?.trim() || '';
  const teamEl = card.querySelector('.card-bet-name, .card-matchup');
  const team = teamEl ? teamEl.textContent.trim().replace(/\s+/g, ' ') : '';
  const evEl = card.querySelector('.ev-badge');
  const ev = evEl ? evEl.textContent.trim().split('\n')[0].trim() : '';
  const bookEl = card.querySelector('.bs-bookname, .best-book-name');
  const book = bookEl ? bookEl.textContent.trim() : '';
  const shareText = [league, team, ev, book].filter(Boolean).join(' · ') + ' · posit-ev.com';
  if (navigator.share) {
    navigator.share({ title: 'Posit+EV Pick', text: shareText, url: 'https://posit-ev.com' }).catch(() => {});
  } else {
    navigator.clipboard.writeText(shareText).then(() => {
      btn.classList.add('copied');
      showToast('📋 Pick copied to clipboard');
      setTimeout(() => btn.classList.remove('copied'), 2000);
    }).catch(() => {
      showToast('Could not copy — try manually');
    });
  }
}

// ── Init ───────────────────────────────────────────────────────────────────
_restoreFilters();   // re-apply saved filter state before first render
sortCards();
applyFilters();
updateSportPillCounts();
updateClearBtn();
updateFilterBadge();
initTooltips();
// Reveal grid only after filters have been applied — prevents the flash
// where all picks appear briefly before JS narrows to today's picks.
document.getElementById('cardsGrid').style.visibility = '';
_buildPropTypeChips();

// Initial visibility for group toggles (default: 'all' mode → show game group toggle)
(function() {
  const gg = document.getElementById('gamesGroupToggle');
  const pg = document.getElementById('propsGroupToggle');
  if (gg) gg.classList.toggle('visible', activeIsProp !== 'true');
  if (pg) pg.classList.toggle('visible', activeIsProp === 'true');
})();




// ── "Starts within 3 hrs" tile ───────────────────────────────────────────────
(function () {
  function refreshSoon() {
    var el = document.getElementById('glanceSoon');
    if (!el) return;
    var now = Date.now(), n = 0;
    document.querySelectorAll('.bet-card[data-commence]').forEach(function (c) {
      var t = Date.parse(c.getAttribute('data-commence'));
      if (t > now && t - now <= 3 * 3600 * 1000) n++;
    });
    el.textContent = n;
  }
  refreshSoon();
  setInterval(refreshSoon, 60000);
})();

// ── Web Push pick alerts ─────────────────────────────────────────────────────
(function () {
  var btn = document.getElementById('pushBtn');
  var lbl = document.getElementById('pushBtnLbl');
  if (!btn || !('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) return;

  function b64ToBytes(b64) {
    var pad = '='.repeat((4 - b64.length % 4) % 4);
    var raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function paint(on) {
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    lbl.textContent = on ? 'Alerts on' : 'Get pick alerts';
  }
  function post(url, body) {
    return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  }

  var cfg = null;
  fetch('/api/push/config').then(function (r) { return r.ok ? r.json() : null; }).then(function (c) {
    if (!c || !c.enabled) return;
    cfg = c;
    btn.hidden = false;
    return navigator.serviceWorker.ready.then(function (reg) {
      return reg.pushManager.getSubscription();
    }).then(function (sub) { paint(!!sub && Notification.permission === 'granted'); });
  }).catch(function () {});

  btn.addEventListener('click', function () {
    if (!cfg) return;
    btn.disabled = true;
    navigator.serviceWorker.ready.then(function (reg) {
      return reg.pushManager.getSubscription().then(function (sub) {
        if (sub) {   // turn off
          return post('/api/push/unsubscribe', { endpoint: sub.endpoint })
            .then(function () { return sub.unsubscribe(); })
            .then(function () { paint(false); if (window.showToast) showToast('Alerts off'); });
        }
        return Notification.requestPermission().then(function (perm) {
          if (perm !== 'granted') {
            if (window.showToast) showToast('Alerts blocked — allow notifications for this site in your browser settings');
            return;
          }
          return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(cfg.key) })
            .then(function (s) {
              var j = s.toJSON();
              return post('/api/push/subscribe', { endpoint: j.endpoint, keys: j.keys, min_ev: 5 });
            })
            .then(function (r) {
              if (r && r.ok) { paint(true); if (window.showToast) showToast('Alerts on — we\'ll ping you for 5%+ EV picks'); }
            });
        });
      });
    }).catch(function () {
      if (window.showToast) showToast('Could not change alerts — on iPhone, add Posit+EV to your Home Screen first');
    }).then(function () { btn.disabled = false; });
  });
})();
