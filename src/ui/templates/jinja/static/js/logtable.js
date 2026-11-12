// The shared log-table module.
//
// One implementation of "a table of log rows": fetch a JSON view, render it,
// paginate it, sort it, and keep that state in the URL. Every log surface in LCP
// mounts this instead of hand-rolling a table, so *ordering* (newest first by
// default), paging, the count line and the filter controls are defined once and
// behave identically everywhere.
//
// Server contract (see src/ui/tables.py): the JSON view returns
//
//     {<rowsKey>: [...], total: N, filter: {per, page, pages, sort, sort_label,
//                                           sorts: [{key, label}], per_choices,
//                                           ...}}
//
// and the module renders its controls FROM that payload — it never hardcodes a
// sort key, so adding a sort is a one-line server-side change. The server also
// owns the ordering itself: paging a table while sorting it client-side would
// sort only the page you can see, which is the bug this module exists to avoid.
//
//   LCPTable.mount({
//     ids:      {body, pager, count, controls, head, term},  // element ids
//     endpoint: '/api/work/requests',
//     rowsKey:  'rows',                           // payload key holding rows
//     noun:     'requests',                       // count-line noun
//     termPrefix: 'requests',                     // term-bar label, optional
//     columns:  [{key, label, num, sort, cell(row)}],  // cell() returns HTML
//     expand:   {param, key, colspan, render(detail, row)},  // optional, lazy
//     filters:  [{param, label, options, optionsKey, value}], // optional selects
//     empty:    'No requests match.',             // empty-state title
//     emptyHint:'...',                            // empty-state second line
//     params:   {view: 'requests'}                // extra query params
//   });
//
// A column carrying `sort: '<key>'` becomes a sort button, but only when the
// server actually offers that key — the header can never invent an ordering the
// endpoint would reject.
(function () {
  'use strict';

  var DEFAULTS = {
    rowsKey: 'rows',
    noun: 'rows',
    per: '20',
    perChoices: ['20', '50', '100', 'all'],
    perLabel: 'per page',
    empty: 'Nothing to show.',
    emptyHint: '',
    termPrefix: '',
    maxPageButtons: 5
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }

  function num(n) {
    if (n == null || n === '') return '—';
    var v = Number(n);
    return isNaN(v) ? esc(n) : v.toLocaleString();
  }

  function urlGet(key) {
    try {
      return new URLSearchParams(window.location.search).get(key) || '';
    } catch (e) {
      return '';
    }
  }

  function ts(s) {
    if (s == null || s === '') return '—';
    // The decisions ledger carries POSIX epoch seconds; log rows carry ISO
    // strings. Render both as an explicit UTC stamp (the server's fmt_ts does
    // the same, and an unlabelled local-looking time invites the two to be
    // conflated).
    if (typeof s === 'number' || /^\d+(\.\d+)?$/.test(String(s))) {
      var d = new Date(Number(s) * 1000);
      if (!isNaN(d.getTime())) return d.toISOString().slice(0, 19).replace('T', ' ') + ' UTC';
    }
    return String(s).slice(0, 19).replace('T', ' ') + ' UTC';
  }

  // Latency auto-scales across the unit boundary: 812 ms, 47.4 s, 1m 04s.
  // A raw "47,445 ms" reads like 47.445 and hides the seconds magnitude.
  function latency(ms) {
    if (ms == null || ms === '' || isNaN(Number(ms))) return '—';
    var v = Number(ms);
    if (v < 1000) return Math.round(v) + ' ms';
    if (v < 60000) return (v / 1000).toFixed(1) + ' s';
    var m = Math.floor(v / 60000), s = Math.round((v % 60000) / 1000);
    return m + 'm ' + String(s).padStart(2, '0') + 's';
  }

  // rowsKey may be a path ("ledger.rows") for views that nest their rows.
  function dig(obj, path) {
    return String(path).split('.').reduce(function (acc, k) {
      return (acc == null) ? acc : acc[k];
    }, obj);
  }

  function mount(opts) {
    var o = Object.assign({}, DEFAULTS, opts);
    o.ids = o.ids || {};
    var body = document.getElementById(o.ids.body);
    if (!body) return null;
    var pagerEl = o.ids.pager ? document.getElementById(o.ids.pager) : null;
    var countEl = o.ids.count ? document.getElementById(o.ids.count) : null;
    var controlsEl = o.ids.controls ? document.getElementById(o.ids.controls) : null;
    var headEl = o.ids.head ? document.getElementById(o.ids.head) : null;
    var termEl = o.ids.term ? document.getElementById(o.ids.term) : null;

    var state = {
      per: urlGet('per') || o.per,
      page: parseInt(urlGet('page'), 10) || 1,
      sort: urlGet('sort') || '',
      filters: {},
      filterOptions: {},
      meta: {total: 0, pages: 1, sorts: [], per: null, page: 1, sort: ''},
      detailCache: {},
      rowData: {},
      lastLoaded: 0,
      inflight: false,
      error: ''
    };

    // A misconfigured mount must fail loudly at load, not render a wrong table.
    if (o.expand && !o.expand.endpoint) {
      throw new Error('logtable: expand.endpoint is required when expand is set');
    }
    (o.filters || []).forEach(function (f) {
      state.filters[f.param] = urlGet(f.param) || f.value || '';
      state.filterOptions[f.param] = f.options || [];
    });

    function activeFilterCount() {
      return (o.filters || []).filter(function (f) { return !!state.filters[f.param]; }).length;
    }

    function query() {
      var q = new URLSearchParams();
      Object.keys(o.params || {}).forEach(function (k) {
        if (o.params[k]) q.set(k, o.params[k]);
      });
      Object.keys(state.filters).forEach(function (k) {
        if (state.filters[k]) q.set(k, state.filters[k]);
      });
      if (state.sort) q.set('sort', state.sort);
      q.set('per', state.per);
      q.set('page', state.page);
      return q.toString();
    }

    // Keep the page URL in step so the view stays shareable — including params
    // this module does not own (view=, qid=).
    function syncUrl() {
      try {
        var url = new URL(window.location.href);
        if (state.sort) url.searchParams.set('sort', state.sort);
        else url.searchParams.delete('sort');
        url.searchParams.set('per', state.per);
        url.searchParams.set('page', state.page);
        Object.keys(state.filters).forEach(function (k) {
          if (state.filters[k]) url.searchParams.set(k, state.filters[k]);
          else url.searchParams.delete(k);
        });
        window.history.replaceState(null, '', url.toString());
      } catch (e) { /* non-fatal: the table still works */ }
    }

    // Disabling the pager and the selects while a fetch is in flight is not
    // cosmetic: a second click during a slow page load used to queue a second
    // request whose response could land out of order and paint stale rows.
    function setBusy(on) {
      state.inflight = !!on;
      if (pagerEl) {
        pagerEl.classList.toggle('is-busy', !!on);
        Array.prototype.forEach.call(pagerEl.querySelectorAll('button, input'), function (b) {
          b.disabled = !!on;
        });
      }
      if (controlsEl) {
        Array.prototype.forEach.call(controlsEl.querySelectorAll('select'), function (s) {
          s.disabled = !!on;
        });
      }
    }

    // How stale is what you are reading? A log console with no freshness signal
    // invites you to trust a table that may be minutes old.
    function freshness() {
      if (!state.lastLoaded) return '';
      var s = Math.round((Date.now() - state.lastLoaded) / 1000);
      if (s < 2) return 'refreshed just now';
      if (s < 60) return 'refreshed ' + s + 's ago';
      var m = Math.round(s / 60);
      if (m < 60) return 'refreshed ' + m + 'm ago';
      return 'refreshed ' + Math.round(m / 60) + 'h ago';
    }

    function loading(label) {
      var cols = o.columns.length + (o.expand ? 1 : 0);
      // aria-busy, not just a word: a screen reader skips a busy region and
      // announces it on the transition, which is the real semantics here.
      if (window.LCPUI) LCPUI.busy(body, true); else body.setAttribute('aria-busy', 'true');
      body.innerHTML = '<tr><td colspan="' + cols + '" class="muted">' +
        esc(label) + '</td></tr>';
    }

    function sortSpecFor(col) {
      if (!col.sort) return null;
      var sorts = state.meta.sorts || [];
      for (var i = 0; i < sorts.length; i++) {
        if (sorts[i].key === col.sort) return sorts[i];
      }
      return null;
    }

    // The column spec is the single source of column semantics: the header row
    // and every cell's mobile `data-label` both come from it.
    function renderHead() {
      if (!headEl) return;
      var cells = [];
      // scope="col" — without it a screen reader reads the cell, not the
      // column it belongs to.
      if (o.expand) cells.push('<th scope="col" class="task-chev-col"></th>');
      o.columns.forEach(function (c) {
        var cls = c.num ? ' class="num"' : '';
        var spec = sortSpecFor(c);
        if (!spec) {
          cells.push('<th scope="col"' + cls + '>' + esc(c.label) + '</th>');
          return;
        }
        var active = state.meta.sort === spec.key;
        // aria-sort belongs on the header cell, and only the sorted column
        // carries a direction; every other sortable column is "none".
        cells.push('<th scope="col"' + cls + ' aria-sort="' + (active ? 'descending' : 'none') + '">' +
          '<button type="button" class="th-sort' + (active ? ' active' : '') + '"' +
          ' data-sort-key="' + esc(spec.key) + '"' +
          ' title="Sort by ' + esc(spec.label.toLowerCase()) + '">' +
          esc(c.label) +
          '<span class="th-sort-arrow" aria-hidden="true">' + (active ? '↓' : '↕') + '</span>' +
          '</button></th>');
      });
      headEl.innerHTML = '<tr>' + cells.join('') + '</tr>';
    }

    function renderRows(rows) {
      var cols = o.columns.length + (o.expand ? 1 : 0);
      if (!rows.length) {
        // A bare "No requests match." leaves the reader to guess whether the
        // filter or the data is responsible, and what to do next.
        var filtered = activeFilterCount() > 0;
        body.innerHTML = '<tr><td colspan="' + cols + '" class="table-empty">' +
          '<div class="te-title">' + esc(o.empty) + '</div>' +
          (o.emptyHint ? '<div class="te-msg">' + esc(o.emptyHint) + '</div>' : '') +
          (filtered ? '<button type="button" class="btn-sm" data-clear-all="1">Clear filters</button>' : '') +
          '</td></tr>';
        if (window.LCPUI) { LCPUI.busy(body, false); LCPUI.announce(o.empty); }
        return;
      }
      var out = [];
      rows.forEach(function (row, i) {
        var rid = o.expand ? String(row[o.expand.key]) : String(i);
        if (o.expand) state.rowData[rid] = row;
        out.push('<tr' + (o.expand ? ' class="log-row" data-row="' + esc(rid) + '"' : '') + '>');
        if (o.expand) {
          out.push('<td class="task-chev">' +
            '<button type="button" class="task-chev-btn" aria-expanded="false"' +
            ' aria-label="Show details for row ' + esc(rid) + '">▸</button></td>');
        }
        o.columns.forEach(function (c) {
          out.push('<td data-label="' + esc(c.label) + '"' + (c.num ? ' class="num"' : '') +
            '>' + (c.cell ? c.cell(row) : esc(row[c.key])) + '</td>');
        });
        out.push('</tr>');
        if (o.expand) {
          out.push('<tr class="task-detail" data-detail="' + esc(rid) + '" hidden>' +
            '<td colspan="' + (o.columns.length + 1) + '"></td></tr>');
        }
      });
      body.innerHTML = out.join('');
      // The transition out of busy is the announcement: "20 rows loaded" is
      // the thing a screen-reader user needs and cannot get from a silent DOM
      // swap.
      if (window.LCPUI) {
        LCPUI.busy(body, false);
        LCPUI.announce(rows.length + ' rows loaded');
      }
    }

    function renderCount() {
      if (!countEl) return;
      var m = state.meta;
      var label = m.total
        ? 'Showing ' + num(m.first) + '–' + num(m.last) + ' of ' + num(m.total) + ' ' + o.noun
        : 'No ' + o.noun;
      // 51,861 requests is 2,593 pages. Without a page count and a way to name a
      // page, everything past the five buttons on screen is unreachable.
      if (m.pages > 1) label += ' · page ' + num(m.page) + ' of ' + num(m.pages);
      var sorts = (m.sorts || []).filter(function (s) { return s.key === m.sort; });
      var sortLabel = sorts.length ? sorts[0].label : (m.sortLabel || '');
      if (sortLabel) label += ' · ' + sortLabel.toLowerCase();
      var fresh = freshness();
      if (fresh) label += ' · ' + fresh;
      countEl.textContent = label;
    }

    function renderPager() {
      if (!pagerEl) return;
      var m = state.meta;
      var parts = [];
      var pages = m.pages || 1;
      var page = m.page || 1;
      // Page sizes the SERVER offers — the client never invents one.
      var perChoices = m.perChoices && m.perChoices.length ? m.perChoices : o.perChoices;
      var half = Math.floor(o.maxPageButtons / 2);
      var from = Math.max(1, page - half);
      var to = Math.min(pages, from + o.maxPageButtons - 1);
      if (page > 1) parts.push(btn('Prev', 'page', page - 1));
      for (var n = from; n <= to; n++) parts.push(btn(String(n), 'page', n, n === page));
      if (page < pages) parts.push(btn('Next', 'page', page + 1));
      if (pages > o.maxPageButtons) {
        parts.push('<span class="task-pager-jump">' +
          '<label class="sr-only" for="' + esc(o.ids.pager || 'lg') + '-jump">Jump to page</label>' +
          '<input type="number" id="' + esc(o.ids.pager || 'lg') + '-jump" data-jump="1"' +
          ' min="1" max="' + pages + '" value="' + page + '" inputmode="numeric"' +
          ' aria-label="Jump to page, 1 to ' + pages + '">' +
          '<span class="muted">/ ' + num(pages) + '</span></span>');
      }
      parts.push('<span class="muted task-pager-label">' + esc(o.perLabel) + '</span>');
      perChoices.forEach(function (pp) {
        parts.push(btn(pp, 'per', pp, String(m.per) === String(pp)));
      });
      pagerEl.innerHTML = parts.join('');
      // renderPager rebuilds the buttons, so the busy state has to be re-applied
      // to the new nodes rather than left on the old ones.
      if (state.inflight) setBusy(true);
    }

    function btn(label, kind, value, active) {
      return '<button type="button" class="btn-sm' + (active ? ' active' : '') +
        '" data-' + kind + '="' + esc(value) + '">' + esc(label) + '</button>';
    }

    function renderControls() {
      if (!controlsEl) return;
      var m = state.meta;
      var parts = [];
      (o.filters || []).forEach(function (f) {
        // Options come from the payload when the view can enumerate them
        // (filter.profiles), else from the mount spec.
        var options = state.filterOptions[f.param] || f.options || [];
        var opts = ['<option value="">' + esc(f.label) + '</option>'];
        options.forEach(function (v) {
          opts.push('<option value="' + esc(v) + '"' +
            (String(state.filters[f.param]) === String(v) ? ' selected' : '') + '>' +
            esc(v) + '</option>');
        });
        parts.push('<select class="task-search" style="max-width:240px" data-filter="' +
          esc(f.param) + '" aria-label="' + esc(f.label) + '">' + opts.join('') + '</select>');
      });
      // An applied filter is invisible once the select scrolls out of view, and
      // "why is this table short?" is the question it has to answer at a glance.
      (o.filters || []).forEach(function (f) {
        var v = state.filters[f.param];
        if (!v) return;
        var chipLabel = f.chipLabel || f.label.replace(/^All /i, '');
        parts.push('<span class="filter-chip">' + esc(chipLabel) + ': <b>' +
          esc(v) + '</b><button type="button" class="filter-chip-x"' +
          ' data-clear-filter="' + esc(f.param) + '"' +
          ' aria-label="Clear ' + esc(f.label) + ' filter">×</button></span>');
      });
      var sorts = m.sorts || [];
      if (sorts.length > 1) {
        var so = sorts.map(function (s) {
          return '<option value="' + esc(s.key) + '"' +
            (s.key === m.sort ? ' selected' : '') + '>' + esc(s.label) + '</option>';
        });
        parts.push('<select class="task-search" style="max-width:240px" data-sort="1" ' +
          'aria-label="Sort order">' + so.join('') + '</select>');
      }
      controlsEl.innerHTML = parts.join('');
      if (state.inflight) setBusy(true);
    }

    function renderTerm() {
      if (!termEl) return;
      var sorts = (state.meta.sorts || []).filter(function (s) { return s.key === state.meta.sort; });
      var label = sorts.length ? sorts[0].label.toLowerCase() : '';
      // The term bar used to say "newest first" unconditionally, which became a
      // lie the moment a column sort existed.
      termEl.textContent = o.termPrefix + (label ? ' · ' + label : '');
    }

    function fetchRows(quiet) {
      if (state.inflight) return Promise.resolve(null);
      if (!quiet) loading('Loading…');
      setBusy(true);
      var url = o.endpoint + '?' + query();
      return fetch(url, {headers: {'Accept': 'application/json'}})
        .then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function (payload) {
          if (payload && payload.error) throw new Error(payload.error);
          var m = payload.filter || {};
          // The server clamps page/per — trust it over our own state.
          state.meta = {
            total: m.total != null ? m.total : (payload.total || 0),
            pages: m.pages || 1,
            page: m.page || 1,
            per: m.per != null ? String(m.per) : state.per,
            first: m.first,
            last: m.last,
            sort: m.sort || '',
            sortLabel: m.sort_label || '',
            sorts: m.sorts || [],
            perChoices: m.per_choices || o.perChoices
          };
          state.error = '';
          state.page = state.meta.page;
          state.per = state.meta.per;
          state.sort = state.meta.sort;
          (o.filters || []).forEach(function (f) {
            if (f.optionsKey && m[f.optionsKey]) {
              state.filterOptions[f.param] = m[f.optionsKey];
            }
          });
          renderRows(dig(payload, o.rowsKey) || []);
          renderHead();
          renderControls();
          renderPager();
          renderCount();
          renderTerm();
          syncUrl();
          state.lastLoaded = Date.now();
          renderCount();
          setBusy(false);
          if (o.after) o.after(payload);
          return payload;
        })
        .catch(function (err) {
          state.error = String(err && err.message || err);
          var cols = o.columns.length + (o.expand ? 1 : 0);
          // A dead-end error row told the reader nothing they could act on.
          body.innerHTML = '<tr><td colspan="' + cols + '" class="table-error">' +
            '<div class="te-title">Could not load ' + esc(o.noun) + '</div>' +
            '<div class="te-msg">' + esc(state.error) + '</div>' +
            '<button type="button" class="btn-sm" data-retry="1">Retry</button>' +
            '</td></tr>';
          if (window.LCPUI) { LCPUI.busy(body, false); LCPUI.announce('Could not load ' + o.noun); }
          setBusy(false);
          return null;
        });
    }

    function toggleDetail(rid, tr) {
      var row = body.querySelector('tr[data-detail="' + CSS.escape(rid) + '"]');
      var btnEl = tr.querySelector('.task-chev-btn');
      if (!row) return;
      if (!row.hasAttribute('hidden')) {
        row.setAttribute('hidden', '');
        tr.classList.remove('is-open');
        if (btnEl) { btnEl.textContent = '▸'; btnEl.setAttribute('aria-expanded', 'false'); }
        return;
      }
      row.removeAttribute('hidden');
      // Keep the source row visibly attached to its detail: with 20 rows on
      // screen, an expanded panel with no anchor is easy to lose track of.
      tr.classList.add('is-open');
      if (btnEl) { btnEl.textContent = '▾'; btnEl.setAttribute('aria-expanded', 'true'); }
      var cell = row.firstElementChild;
      var cached = state.detailCache[rid];
      if (cached) { cell.innerHTML = cached; return; }
      cell.innerHTML = '<div class="task-detail-inner muted">Loading…</div>';
      var url = o.expand.endpoint + '?' + o.expand.param + '=' + encodeURIComponent(rid);
      fetch(url, {headers: {'Accept': 'application/json'}})
        .then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function (detail) {
          if (!detail || detail.error) throw new Error((detail && detail.error) || 'empty detail');
          var html = o.expand.render(detail, state.rowData[rid] || {}, rid);
          state.detailCache[rid] = html;
          cell.innerHTML = html;
        })
        .catch(function (err) {
          cell.innerHTML = '<div class="task-detail-inner muted">Could not load detail: ' +
            esc(err.message || err) + '</div>';
        });
    }

    body.addEventListener('click', function (ev) {
      var retry = ev.target.closest('[data-retry]');
      if (retry) { fetchRows(); return; }
      var clearAll = ev.target.closest('[data-clear-all]');
      if (clearAll) {
        Object.keys(state.filters).forEach(function (k) { state.filters[k] = ''; });
        state.page = 1;
        fetchRows();
        return;
      }
      var link = ev.target.closest('a');
      if (link) return;   // links inside a row keep their own behaviour
      if (!o.expand) return;
      var tr = ev.target.closest('tr[data-row]');
      if (!tr) return;
      toggleDetail(tr.getAttribute('data-row'), tr);
    });

    if (headEl) {
      headEl.addEventListener('click', function (ev) {
        var b = ev.target.closest('button[data-sort-key]');
        if (!b) return;
        state.sort = b.getAttribute('data-sort-key');
        state.page = 1;
        fetchRows();
      });
    }

    if (controlsEl) {
      controlsEl.addEventListener('change', function (ev) {
        var el = ev.target;
        if (el.hasAttribute('data-sort')) state.sort = el.value;
        else if (el.hasAttribute('data-filter')) {
          state.filters[el.getAttribute('data-filter')] = el.value;
        } else return;
        state.page = 1;
        fetchRows();
      });
      controlsEl.addEventListener('click', function (ev) {
        var x = ev.target.closest('[data-clear-filter]');
        if (!x) return;
        state.filters[x.getAttribute('data-clear-filter')] = '';
        state.page = 1;
        fetchRows();
      });
    }

    if (pagerEl) {
      pagerEl.addEventListener('click', function (ev) {
        var b = ev.target.closest('button');
        if (!b || b.disabled) return;
        if (b.getAttribute('data-page') != null) {
          state.page = parseInt(b.getAttribute('data-page'), 10) || 1;
        } else if (b.getAttribute('data-per') != null) {
          state.per = b.getAttribute('data-per');
          state.page = 1;
        } else return;
        fetchRows();
      });
      // The jump field commits on Enter or blur rather than on every keystroke:
      // typing "12" would otherwise fetch page 1 and then page 12.
      pagerEl.addEventListener('change', function (ev) {
        var el = ev.target;
        if (!el.hasAttribute('data-jump') || el.disabled) return;
        var want = parseInt(el.value, 10);
        var pages = (state.meta && state.meta.pages) || 1;
        if (!want || want < 1 || want > pages) { el.value = state.meta.page; return; }
        if (want === state.meta.page) return;
        state.page = want;
        fetchRows();
      });
      pagerEl.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' && ev.target.hasAttribute('data-jump')) {
          ev.preventDefault();
          ev.target.blur();
        }
      });
    }

    // The freshness line is a clock, so it has to tick on its own; a stamp that
    // only moved when you clicked something would be worse than none.
    window.setInterval(function () {
      if (state.lastLoaded) renderCount();
    }, 1000);

    renderHead();
    fetchRows();
    return {state: state, refresh: fetchRows};
  }

  window.LCPTable = {mount: mount, esc: esc, num: num, ts: ts, latency: latency};
})();
