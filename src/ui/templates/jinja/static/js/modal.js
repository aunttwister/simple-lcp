// shadcn-style modal helpers — shared across all LCP pages.
// Provides: openDialog(id), closeDialog(id), closeAllDialogs()
// Features: Escape key, click-outside dismiss, body scroll lock, ARIA attributes.
(function () {
  'use strict';

  var DIALOG_DATA = '__lcp_dialog';
  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  // Where focus was before the dialog opened. Restored on close, otherwise
  // dismissing a dialog dumps the keyboard user at the top of the document and
  // they have to tab back to the control they came from.
  var _returnFocus = null;

  function focusables(overlay) {
    var content = overlay.querySelector('.dialog-content') || overlay;
    return Array.prototype.filter.call(
      content.querySelectorAll(FOCUSABLE),
      function (el) { return el.offsetParent !== null || el === document.activeElement; }
    );
  }

  /**
   * Open a dialog by ID. If the element doesn't have ARIA attributes yet,
   * the first open patches them in. Returns the overlay element.
   */
  window.openDialog = function (overlayId) {
    var overlay = document.getElementById(overlayId);
    if (!overlay) return null;

    // Patch ARIA on first open
    if (!overlay[DIALOG_DATA]) {
      patchAria(overlay);
      overlay[DIALOG_DATA] = true;
    }

    if (!_returnFocus) _returnFocus = document.activeElement;

    overlay.classList.add('open');
    document.body.classList.add('lcp-dialog-open');
    lockBodyScroll();

    // Focus the first focusable element
    requestAnimationFrame(function () {
      var content = overlay.querySelector('.dialog-content');
      if (content) {
        var first = content.querySelector(FOCUSABLE);
        if (first) first.focus();
      }
    });

    return overlay;
  };

  /**
   * Close a dialog by ID. Removes 'open' class and unlocks body if no others.
   */
  window.closeDialog = function (overlayId) {
    var overlay = document.getElementById(overlayId);
    if (!overlay) return;
    overlay.classList.remove('open');
    if (!document.querySelector('.dialog-overlay.open')) {
      document.body.classList.remove('lcp-dialog-open');
      unlockBodyScroll();
      // Restore focus only once every dialog is closed — closing one of two
      // stacked dialogs should leave focus in the other, not back on the page.
      if (_returnFocus && document.body.contains(_returnFocus)) {
        try { _returnFocus.focus(); } catch (e) { /* detached node */ }
      }
      _returnFocus = null;
    }
  };

  /** Close all open dialogs. */
  window.closeAllDialogs = function () {
    document.querySelectorAll('.dialog-overlay.open').forEach(function (o) {
      o.classList.remove('open');
    });
    document.body.classList.remove('lcp-dialog-open');
    unlockBodyScroll();
    if (_returnFocus && document.body.contains(_returnFocus)) {
      try { _returnFocus.focus(); } catch (e) { /* detached node */ }
    }
    _returnFocus = null;
  };

  // ── body scroll lock ──
  var _scrollY = 0;
  function lockBodyScroll() {
    _scrollY = window.scrollY;
    document.body.style.position = 'fixed';
    document.body.style.top = '-' + _scrollY + 'px';
    document.body.style.width = '100%';
    document.body.style.overflowY = 'scroll'; // preserve scrollbar width
  }
  function unlockBodyScroll() {
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.width = '';
    document.body.style.overflowY = '';
    window.scrollTo(0, _scrollY);
  }

  // ── ARIA patching ──
  function patchAria(overlay) {
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');

    // Find title/description elements and wire up aria-labelledby/describedby
    var title = overlay.querySelector('.dialog-header h2, .dialog-title');
    if (title && !title.id) {
      title.id = 'dlg-title-' + Math.random().toString(36).slice(2, 8);
    }
    if (title) {
      overlay.setAttribute('aria-labelledby', title.id);
    }

    var body = overlay.querySelector('.dialog-body');
    if (body && !body.id) {
      body.id = 'dlg-desc-' + Math.random().toString(36).slice(2, 8);
      overlay.setAttribute('aria-describedby', body.id);
    }
  }

  // ── Global event listeners ──

  // Escape key
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    // Close the top-most open dialog
    var dialogs = document.querySelectorAll('.dialog-overlay.open');
    if (dialogs.length > 0) {
      closeDialog(dialogs[dialogs.length - 1].id);
    }
  });

  // Tab trap. `aria-modal="true"` tells assistive tech the rest of the page is
  // inert, but the browser does not enforce it: without this handler Tab walks
  // straight out of the dialog into the page behind it, and the aria-modal
  // claim becomes a lie.
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Tab') return;
    var dialogs = document.querySelectorAll('.dialog-overlay.open');
    if (!dialogs.length) return;
    var overlay = dialogs[dialogs.length - 1];
    var items = focusables(overlay);
    if (!items.length) { e.preventDefault(); return; }

    var first = items[0];
    var last = items[items.length - 1];
    var active = document.activeElement;

    if (!overlay.contains(active)) {
      // Focus escaped (or never entered): pull it back to the first control.
      e.preventDefault();
      first.focus();
      return;
    }
    if (e.shiftKey && active === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  });

  // Click outside (on overlay backdrop)
  document.addEventListener('click', function (e) {
    var overlay = e.target.closest('.dialog-overlay');
    if (!overlay) return;
    if (!overlay.classList.contains('open')) return;
    // Only close if the click was on the overlay itself, not on content
    if (e.target === overlay) {
      closeDialog(overlay.id);
    }
  });

})();
