(function () {
  /* ── Sticky nav: show after hero scrolls past ── */
  var banner = document.getElementById('sticky-free-pick');
  var hero   = document.querySelector('.hero');
  function checkStickyState () {
    if (!banner || !hero) return;
    var heroBottom = hero.getBoundingClientRect().bottom;
    if (heroBottom < 0) {
      banner.classList.add('visible');
      banner.removeAttribute('aria-hidden');
    } else {
      banner.classList.remove('visible');
      banner.setAttribute('aria-hidden', 'true');
    }
  }
  window.addEventListener('scroll', checkStickyState, { passive: true });
  checkStickyState();

  /* ── FAQ accordion ── */
  window.toggleFaq = function (btn) {
    var item   = btn.closest('.faq-item');
    var isOpen = item.classList.contains('faq-open');
    document.querySelectorAll('.faq-item.faq-open').forEach(function (el) {
      el.classList.remove('faq-open');
    });
    if (!isOpen) item.classList.add('faq-open');
  };

  /* ── Track record picks toggle ── */
  window.toggleTrackPicks = function () {
    var wrap  = document.getElementById('trPicksWrap');
    var btn   = document.getElementById('trToggleBtn');
    if (!wrap || !btn) return;
    var hidden = wrap.style.display === 'none';
    wrap.style.display = hidden ? '' : 'none';
    var count = btn.dataset.count;
    btn.innerHTML = hidden
      ? 'Hide picks &#9650;'
      : 'Show all picks (' + count + ') &#9660;';
  };

  window.filterTrackBySport = function (pill) {
    var league = pill.dataset.league;

    // Update active pill
    document.querySelectorAll('.tr-filter-pill').forEach(function (p) {
      p.classList.toggle('active', p === pill);
    });

    // Show/hide rows
    var rows = document.querySelectorAll('#trPicksWrap .tr-table tbody tr');
    var won = 0, lost = 0, push = 0, pl = 0;
    rows.forEach(function (row) {
      var rowLeague = (row.dataset.league || '').trim();
      var show = league === 'all' || rowLeague === league.trim();
      row.style.display = show ? '' : 'none';
      if (!show) return;
      var result = row.dataset.result;
      var rowPl  = parseFloat(row.dataset.pl) || 0;
      if (result === 'won')  { won++;  pl += rowPl; }
      if (result === 'lost') { lost++; pl += rowPl; }
      if (result === 'push') { push++;              }
    });

    // Recompute stats
    var settled = won + lost + push;
    var units   = pl / 20;
    var roi     = settled > 0 ? (pl / (settled * 20)) * 100 : 0;

    var elTotal  = document.getElementById('trStatTotal');
    var elROI    = document.getElementById('trStatROI');
    var elRecord = document.getElementById('trStatRecord');
    var elPL     = document.getElementById('trStatPL');
    var elUnits  = document.getElementById('trStatUnits');

    if (elTotal)  elTotal.textContent  = settled;
    if (elROI) {
      elROI.textContent  = (roi >= 0 ? '+' : '') + roi.toFixed(1) + '%';
      elROI.style.color  = roi >= 0 ? '#16A34A' : '#DC2626';
    }
    if (elRecord) elRecord.textContent = 'ROI · ' + won + 'W – ' + lost + 'L';
    if (elPL) {
      elPL.textContent = (pl >= 0 ? '+$' : '-$') + Math.abs(pl).toFixed(2);
      elPL.style.color = pl >= 0 ? '#16A34A' : '#DC2626';
    }
    if (elUnits) {
      elUnits.textContent = (units >= 0 ? '+' : '') + units.toFixed(1) + ' u';
      elUnits.style.color = units >= 0 ? '#16A34A' : '#DC2626';
    }
  };
})();
