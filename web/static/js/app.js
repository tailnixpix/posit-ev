// ── Posit+EV — global client-side utilities ───────────────────────────────

// ── Timezone preference ───────────────────────────────────────────────────
// Reads/writes to localStorage under 'pev_tz'.
// Exposes window.PEV_TZ (string) for any page script to consume.
// Dispatches 'pev:tz-change' on document when the user switches zones.

(function () {
  const TZ_KEY = 'pev_tz';
  const DEFAULT_TZ = 'America/Chicago';

  // Resolve saved TZ (fall back to CT)
  window.PEV_TZ = localStorage.getItem(TZ_KEY) || DEFAULT_TZ;

  function applyTZ(tz) {
    window.PEV_TZ = tz;
    localStorage.setItem(TZ_KEY, tz);
    // Refresh active styling on all pickers (there may be one in every page nav)
    document.querySelectorAll('.tz-btn').forEach(btn => {
      btn.classList.toggle('tz-btn-active', btn.dataset.tz === tz);
    });
    document.dispatchEvent(new CustomEvent('pev:tz-change', { detail: { tz } }));
  }

  // Wire up picker buttons — runs after DOM is available
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.tz-btn').forEach(btn => {
      btn.classList.toggle('tz-btn-active', btn.dataset.tz === window.PEV_TZ);
      btn.addEventListener('click', () => applyTZ(btn.dataset.tz));
    });
  });
})();
