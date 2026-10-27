// LCP landing — motion and navigation. No scroll listeners: IntersectionObserver only.

(function () {
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ── section reveals ────────────────────────────────────────────────────
  var revealables = document.querySelectorAll('.reveal');
  if (reduce || !('IntersectionObserver' in window)) {
    revealables.forEach(function (el) { el.classList.add('is-in'); });
  } else {
    var revealObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        var siblings = Array.prototype.slice.call(
          entry.target.parentElement.querySelectorAll(':scope > .reveal')
        );
        var index = Math.max(0, siblings.indexOf(entry.target));
        entry.target.style.transitionDelay = Math.min(index * 80, 320) + 'ms';
        entry.target.classList.add('is-in');
        revealObserver.unobserve(entry.target);
      });
    }, { rootMargin: '0px 0px -12% 0px', threshold: 0.1 });
    revealables.forEach(function (el) { revealObserver.observe(el); });
  }

  // ── tagline: each word lights as it crosses the trigger line ───────────
  var tagline = document.querySelector('[data-reveal-words]');
  if (tagline) {
    var text = tagline.textContent.trim();
    tagline.textContent = '';
    var words = text.split(/\s+/);
    words.forEach(function (word, i) {
      var span = document.createElement('span');
      span.className = 'word';
      span.textContent = word;
      tagline.appendChild(span);
      if (i < words.length - 1) tagline.appendChild(document.createTextNode(' '));
    });

    var wordEls = tagline.querySelectorAll('.word');
    if (reduce || !('IntersectionObserver' in window)) {
      wordEls.forEach(function (w) { w.classList.add('is-lit'); });
    } else {
      var wordObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          entry.target.classList.add('is-lit');
          wordObserver.unobserve(entry.target);
        });
      }, { rootMargin: '-45% 0px -45% 0px', threshold: 0 });
      wordEls.forEach(function (w) { wordObserver.observe(w); });
    }
  }

  // ── mobile menu ────────────────────────────────────────────────────────
  var toggle = document.getElementById('navToggle');
  var overlay = document.getElementById('navOverlay');
  if (!toggle || !overlay) return;

  function openMenu() {
    overlay.hidden = false;
    // force a reflow so the transition has a resolved starting state, without
    // depending on requestAnimationFrame (throttled in background tabs)
    void overlay.offsetWidth;
    overlay.classList.add('is-open');
    toggle.setAttribute('aria-expanded', 'true');
    toggle.setAttribute('aria-label', 'Close menu');
    document.body.style.overflow = 'hidden';
    var first = overlay.querySelector('a');
    if (first) first.focus();
  }

  function closeMenu(returnFocus) {
    overlay.classList.remove('is-open');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', 'Open menu');
    document.body.style.overflow = '';
    window.setTimeout(function () { overlay.hidden = true; }, 500);
    if (returnFocus) toggle.focus();
  }

  toggle.addEventListener('click', function () {
    if (toggle.getAttribute('aria-expanded') === 'true') closeMenu(true);
    else openMenu();
  });

  overlay.addEventListener('click', function (event) {
    if (event.target.closest('a')) closeMenu(false);
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && toggle.getAttribute('aria-expanded') === 'true') {
      closeMenu(true);
    }
  });

  // keep focus inside the overlay while it is open
  overlay.addEventListener('keydown', function (event) {
    if (event.key !== 'Tab') return;
    var focusables = overlay.querySelectorAll('a, button');
    if (!focusables.length) return;
    var first = focusables[0];
    var last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  });

  window.addEventListener('resize', function () {
    if (window.innerWidth > 640 && toggle.getAttribute('aria-expanded') === 'true') {
      closeMenu(false);
    }
  });
})();
