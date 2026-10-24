// Shared UI plumbing: clipboard, screen-reader announcements, aria-busy.
//
// Vendored, no dependency, ES5 like the rest of static/js. These three helpers
// exist because the admin UI loads almost every table after paint, and a page
// that fills in silently is invisible to a screen reader: the DOM changes, no
// announcement is made, and focus never lands on the new content.
(function () {
  'use strict';

  // ── One polite live region for the document ──
  // Created lazily: a page that never announces anything carries no extra node.
  var liveEl = null;

  function live() {
    if (liveEl && document.body.contains(liveEl)) return liveEl;
    liveEl = document.getElementById('lcpLive');
    if (!liveEl) {
      liveEl = document.createElement('div');
      liveEl.id = 'lcpLive';
      liveEl.className = 'sr-only';
      liveEl.setAttribute('role', 'status');
      liveEl.setAttribute('aria-live', 'polite');
      liveEl.setAttribute('aria-atomic', 'true');
      document.body.appendChild(liveEl);
    }
    return liveEl;
  }

  function announce(msg) {
    if (!msg) return;
    var el = live();
    // Clear first. A live region only re-announces when its text changes, so
    // the same message twice in a row would otherwise be silent.
    el.textContent = '';
    setTimeout(function () { el.textContent = msg; }, 30);
  }

  // ── Clipboard ──
  // navigator.clipboard needs a secure context, and this admin UI is served
  // over plain http on the LAN — so the legacy path is the common path here,
  // not a rare fallback.
  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:-1000px;left:-1000px;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
      if (ok) resolve(); else reject(new Error('copy failed'));
    });
  }

  // Wire a button to copy `text`. The glyph changes in place and the live
  // region says what happened; the button keeps an aria-label so its purpose
  // survives the glyph swap.
  function copyButton(btn, text, opts) {
    if (!btn) return;
    opts = opts || {};
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      copy(text).then(function () {
        btn.classList.add('copied');
        btn.textContent = opts.copiedGlyph || '✓';
        announce(opts.copiedMsg || 'Copied to clipboard');
        setTimeout(function () {
          btn.classList.remove('copied');
          btn.textContent = opts.glyph || '⧉';
        }, 1400);
      }).catch(function () {
        announce(opts.failMsg || 'Copy failed — select the text manually');
      });
    });
  }

  // ── aria-busy ──
  // true while a region is filling, removed once it holds content. Screen
  // readers skip a busy region and announce it on the transition, which is
  // exactly the semantics of a table that loads late.
  function busy(el, isBusy) {
    if (!el) return;
    if (isBusy) el.setAttribute('aria-busy', 'true');
    else el.removeAttribute('aria-busy');
  }

  window.LCPUI = {
    announce: announce,
    copy: copy,
    copyButton: copyButton,
    busy: busy
  };
})();
