// ── Sidebar behaviour ───────────────────────────────────────────────────
// Collapse/expand, and the alert badge on the Alerts entry.
//
// The usage widgets that used to live here are gone (M2b): the provider-credit
// submenu duplicated what the Usage page shows, and its ↻ button posted the same
// /api/settings/cache/refresh the Usage page's own "Refresh now" posts — so
// removing the sidebar shortcuts removed no capability, only the second copy.
// Usage is a page again, and it owns its refresh controls.

// Sidebar toggle — shared across all pages.
//
// aria-expanded tracks whether the navigation is actually visible, which is a
// different question on each breakpoint: on mobile the sidebar is off-canvas
// and `open` means visible; on desktop it is a column and `collapsed` means
// hidden. A single boolean cannot be derived from one class name, so the
// state is computed from both.
function syncSidebarAria() {
  var sb = document.getElementById('sidebar');
  var btn = document.getElementById('sidebarToggle');
  if (!sb || !btn) return;
  var visible = window.innerWidth <= 768
    ? sb.classList.contains('open')
    : !sb.classList.contains('collapsed');
  btn.setAttribute('aria-expanded', visible ? 'true' : 'false');
}

function toggleSidebar() {
  var sb = document.getElementById('sidebar');
  var ov = document.getElementById('sidebarOverlay');
  if (window.innerWidth <= 768) {
    sb.classList.toggle('open');
    ov.classList.toggle('show');
  } else {
    sb.classList.toggle('collapsed');
    localStorage.setItem('lcp-sidebar', sb.classList.contains('collapsed') ? 'collapsed' : 'pinned');
  }
  syncSidebarAria();
}

function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebarOverlay').classList.remove('show');
  syncSidebarAria();
}

(function () {
  if (window.innerWidth > 768 && localStorage.getItem('lcp-sidebar') === 'collapsed') {
    document.getElementById('sidebar').classList.add('collapsed');
  }
  syncSidebarAria();
  window.addEventListener('resize', syncSidebarAria);
})();

// Alert badge polling — updates active alert count on all pages.
(function() {
  function pollAlertBadge() {
    var badge = document.getElementById('sidebarAlertBadge');
    if (!badge) return;
    try {
      fetch('/api/alerts/active').then(function(r) { return r.json(); }).then(function(d) {
        var count = (d.alerts || []).length;
        if (count > 0) {
          badge.textContent = count;
          badge.style.display = 'inline-block';
        } else {
          badge.style.display = 'none';
        }
      });
    } catch(e) {}
  }
  pollAlertBadge();
  setInterval(pollAlertBadge, 15000);
})();
