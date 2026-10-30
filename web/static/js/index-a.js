(function () {
  const form   = document.getElementById('nlForm');
  const btn    = document.getElementById('nlBtn');
  const result = document.getElementById('nlResult');

  form.addEventListener('submit', async function (e) {
    e.preventDefault();
    const email = document.getElementById('nlEmail').value;
    btn.disabled = true;
    btn.textContent = 'Setting up…';
    try {
      const body = new URLSearchParams({ email });
      const res  = await fetch('/newsletter/subscribe', { method: 'POST', body });
      if (res.ok) {
        form.closest('.nl-form-box').style.display = 'none';
        result.style.display = 'block';
        result.style.background = 'rgba(74,222,128,0.1)';
        result.style.color      = '#4ADE80';
        result.style.border     = '1px solid rgba(74,222,128,0.3)';
        result.innerHTML =
          '<strong style="font-size:1rem; color:#fff;">✓ You\'re in — pick is on its way!</strong><br>'
          + '<span style="font-size:0.82rem; color:rgba(255,255,255,0.55);">📬 Don\'t see it? Check your <strong style="color:rgba(255,255,255,0.75);">spam or junk folder</strong> and mark us as "Not Spam".</span>'
          + '<div style="margin-top:16px; padding-top:16px; border-top:1px solid rgba(74,222,128,0.2); display:flex; gap:12px; flex-wrap:wrap; align-items:center; justify-content:center;">'
          + '  <div style="flex:1; min-width:180px; text-align:left;">'
          + '    <p style="margin:0 0 3px; font-weight:700; color:#fff; font-size:0.9rem;">Want every pick, not just one?</p>'
          + '    <p style="margin:0; font-size:0.78rem; color:rgba(255,255,255,0.5);">Unlock the full dashboard — 50+ picks/day, all markets, Kelly sizing.</p>'
          + '  </div>'
          + '  <a href="/register" style="display:inline-block; background:#7C3AED; color:#fff; font-weight:700; font-size:0.88rem;'
          + '     padding:10px 22px; border-radius:8px; text-decoration:none; white-space:nowrap; flex-shrink:0;'
          + '     box-shadow:0 4px 14px rgba(124,58,237,0.4);">'
          + '    Start 7-Day Free Trial &rarr;'
          + '  </a>'
          + '</div>';
      } else {
        result.style.display = 'block';
        result.style.background = 'rgba(239,68,68,0.1)';
        result.style.color      = '#FCA5A5';
        result.style.border     = '1px solid rgba(239,68,68,0.3)';
        result.innerHTML = 'Something went wrong — please try again.';
        btn.disabled = false;
        btn.textContent = 'Get My Free Pick →';
      }
    } catch {
      result.style.display = 'block';
      result.style.background = 'rgba(239,68,68,0.1)';
      result.style.color      = '#FCA5A5';
      result.style.border     = '1px solid rgba(239,68,68,0.3)';
      result.innerHTML = 'Something went wrong — please try again.';
      btn.disabled = false;
      btn.textContent = 'Get My Free Pick →';
    }
  });
})();
