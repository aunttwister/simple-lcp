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
//     {<rowsKey>: [...], total: N, filter: {per, page, pages, sort,
//                                           sorts: [{key, label}], ...}}
//
// and the module renders its controls FROM that payload — it never hardcodes a
// sort key, so adding a sort is a one-line server-side change. The server also
// owns the ordering itself: paging a table while sorting it client-side would
// sort only the page you can see, which is the bug this module exists to avoid.
//
//   LCPTable.mount({
//     ids:      {body, pager, count, controls},   // element ids
//     endpoint: '/api/work/requests',
//     rowsKey:  'rows',                           // payload key holding rows
//     noun:     'requests',                       // count-line noun
//     columns:  [{key, label, num, cell(row)}],   // cell() returns HTML
//     expand:   {param, key, colspan, render(detail, row)},  // optional, lazy
//     filters:  [{param, label, options, value}], // optional selects
//     params:   {view: 'requests'}                // extra query params
//   });
(function () {
  'use strict';

  var DEFAULTS = {
    rowsKey: 'rows',
    noun: 'rows',
    per: '20',
    perChoices: ['20', '50', '100', 'all'],
    perLabel: 'per page',
    empty: 'Nothing to show.',
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
      if (!isNaN(d.getTime())) return d.toISOString().slice(0, 19).replace('T', ' ') + 'Z';
    }
    return String(s).slice(0, 19).replace('T', ' ');
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

    var state = {
      per: urlGet('per') || o.per,
      page: parseInt(urlGet('page'), 10) || 1,
      sort: urlGet('sort') || '',
      filters: {},
      filterOptions: {},
      meta: {total: 0, pages: 1, sorts: [], per: null, page: 1, sort: ''},
      detailCache: {},
      rowData: {}
    };

    // A misconfigured mount must fail loudly at load, not render a wrong table.
    if (o.expand && !o.expand.endpoint) {
      throw new Error('logtable: expand.endpoint is required when expand is set');
    }
    (o.filters || []).forEach(function (f) {
      state.filters[f.param] = urlGet(f.param) || f.value || '';
      state.filterOptions[f.param] = f.options || [];
    });

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

    function loading(label) {
      var cols = o.columns.length + (o.expand ? 1 : 0);
      // aria-busy, not just a word: a screen reader skips a busy region and
      // announces it on the transition, which is the real semantics here.
      if (window.LCPUI) LCPUI.busy(body, true); else body.setAttribute('aria-busy', 'true');
      body.innerHTML = '<tr><td colspan="' + cols + '" class="muted">' +
        esc(label) + '</td></tr>';
    }

    // The column spec is the single source of column semantics: the header row
    // and every cell's mobile `data-label` both come from it.
    function renderHead() {
      var head = o.ids.head ? document.getElementById(o.ids.head) : null;
      if (!head) return;
      var cells = [];
      // scope="col" — without it a screen reader reads the cell, not the
      // column it belongs to.
      if (o.expand) cells.push('<th scope="col" class="task-chev-col"></th>');
      o.columns.forEach(function (c) {
        cells.push('<th scope="col"' + (c.num ? ' class="num"' : '') + '>' + esc(c.label) + '</th>');
      });
      head.innerHTML = '<tr>' + cells.join('') + '</tr>';
    }

    function renderRows(rows) {
      var out = [];
      if (!rows.length) {
        var cols = o.columns.length + (o.expand ? 1 : 0);
        body.innerHTML = '<tr><td colspan="' + cols + '" class="muted">' +
          esc(o.empty) + '</td></tr>';
        if (window.LCPUI) { LCPUI.busy(body, false); LCPUI.announce(o.empty); }
        return;
      }
      rows.forEach(function (row, i) {
        var rid = o.expand ? String(row[o.expand.key]) : String(i);
        if (o.expand) state.rowData[rid] = row;
        out.push('<tr' + (o.expand ? ' class="log-row" data-row="' + esc(rid) + '"' : '') + '>');
        if (o.expand) out.push('<td class="task-chev">▸</td>');
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
      var sorts = (m.sorts || []).filter(function (s) { return s.key === m.sort; });
      if (sorts.length) label += ' · ' + sorts[0].label.toLowerCase();
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
      parts.push('<span class="muted task-pager-label">' + esc(o.perLabel) + '</span>');
      perChoices.forEach(function (pp) {
        parts.push(btn(pp, 'per', pp, String(m.per) === String(pp)));
      });
      pagerEl.innerHTML = parts.join('');
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
      var sorts = m.sorts || [];
      if (sorts.length > 1) {
        var so = sorts.map(function (s) {
          return '<option value="' + esc(s.key) + '"' +
            (s.key === m.sort ? ' selected' : '') + '>' + esc(s.label) + '</option>';
        });
        parts.push('<select class="task-search" style="max-width:240px" data-sort="1" ' +
          'aria-label="Sort order">' + so.join('') + '</select>');
      }
      if (parts.length) controlsEl.innerHTML = parts.join('');
    }

    function fetchRows(quiet) {
      if (!quiet) loading('Loading…');
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
            sorts: m.sorts || [],
            perChoices: m.per_choices || o.perChoices
          };
          state.page = state.meta.page;
          state.per = state.meta.per;
          state.sort = state.meta.sort;
          (o.filters || []).forEach(function (f) {
            if (f.optionsKey && m[f.optionsKey]) {
              state.filterOptions[f.param] = m[f.optionsKey];
            }
          });
          renderRows(dig(payload, o.rowsKey) || []);
          renderControls();
          renderPager();
          renderCount();
          syncUrl();
          if (o.after) o.after(payload);
        })
        .catch(function (err) {
          body.innerHTML = '<tr><td colspan="' + (o.columns.length + 1) +
            '" class="muted">Could not load ' + esc(o.noun) + ': ' + esc(err.message || err) +
            '</td></tr>';
        });
    }

    function toggleDetail(rid, tr) {
      var row = body.querySelector('tr[data-detail="' + CSS.escape(rid) + '"]');
      var chev = tr.querySelector('.task-chev');
      if (!row) return;
      if (!row.hasAttribute('hidden')) {
        row.setAttribute('hidden', '');
        if (chev) chev.textContent = '▸';
        return;
      }
      row.removeAttribute('hidden');
      if (chev) chev.textContent = '▾';
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
      var link = ev.target.closest('a');
      if (link) return;   // links inside a row keep their own behaviour
      if (!o.expand) return;
      var tr = ev.target.closest('tr[data-row]');
      if (!tr) return;
      toggleDetail(tr.getAttribute('data-row'), tr);
    });

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
    }

    if (pagerEl) {
      pagerEl.addEventListener('click', function (ev) {
        var b = ev.target.closest('button');
        if (!b) return;
        if (b.getAttribute('data-page') != null) {
          state.page = parseInt(b.getAttribute('data-page'), 10) || 1;
        } else if (b.getAttribute('data-per') != null) {
          state.per = b.getAttribute('data-per');
          state.page = 1;
        } else return;
        fetchRows();
      });
    }

    fetchRows();
    renderHead();
    return {state: state, refresh: fetchRows};
  }

  window.LCPTable = {mount: mount, esc: esc, num: num, ts: ts};
})();
