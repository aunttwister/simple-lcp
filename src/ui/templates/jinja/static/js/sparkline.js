// Sparkline + radial gauge primitives.
//
// Vendored on purpose: no CDN, no npm, no build step. LCP is server-rendered
// Jinja with plain ES5 modules, and a control plane that sees every API key
// should not pull a new third-party script off the internet to draw a trend.
//
// Two renderers, because they answer different questions:
//
//   ASCII   " .:-=+*#%@"  10 levels in one character cell — a coarse shape.
//   BRAILLE 2x4 dots per cell, so 4x the vertical resolution in the same
//           character width — the right tool for latency jitter, where the
//           difference between two adjacent samples matters.
//
// Both are text, so they live in a <pre>: selectable, copyable, screen-reader
// readable, and they cost one DOM node instead of a canvas per card.
//
//   LCPSpark.ascii(values, {width: 40})      -> string
//   LCPSpark.braille(values, {width: 20})    -> string
//   LCPSpark.mount(el, values, {mode: 'braille'})
//   LCPSpark.gauge(canvasEl, 62, {label: 'weekly'})   // radial, 0..100
(function () {
  'use strict';

  var ASCII_RAMP = ' .:-=+*#%@';   // 10 levels, lightest to densest

  // Braille bit masks. A cell is 2 columns x 4 rows:
  //   dot1(0x01) dot4(0x08)
  //   dot2(0x02) dot5(0x10)
  //   dot3(0x04) dot6(0x20)
  //   dot7(0x40) dot8(0x80)
  // Bottom-up, per column, is the order a sparkline fills.
  var BRAILLE_LEFT = [0x40, 0x04, 0x02, 0x01];
  var BRAILLE_RIGHT = [0x80, 0x20, 0x10, 0x08];

  function isNum(v) { return typeof v === 'number' && isFinite(v); }

  function clean(values) {
    if (!values || !values.length) return [];
    var out = [];
    for (var i = 0; i < values.length; i++) {
      var v = Number(values[i]);
      out.push(isFinite(v) ? v : 0);
    }
    return out;
  }

  // Resample to exactly n points by bucketing (mean of each bucket), so a
  // 30-day series and a 7-day series render at the same character width
  // without silently dropping the tail.
  function resample(values, n) {
    if (!values.length || n <= 0) return [];
    if (values.length === n) return values.slice();
    var out = [], step = values.length / n;
    for (var i = 0; i < n; i++) {
      var start = Math.floor(i * step), end = Math.floor((i + 1) * step);
      if (end <= start) end = start + 1;
      var sum = 0, count = 0;
      for (var j = start; j < end && j < values.length; j++) { sum += values[j]; count++; }
      out.push(count ? sum / count : 0);
    }
    return out;
  }

  function ascii(values, opts) {
    opts = opts || {};
    var width = opts.width || 40;
    var v = resample(clean(values), width);
    if (!v.length) return '';
    var min = Math.min.apply(null, v), max = Math.max.apply(null, v);
    var span = max - min;
    var out = '';
    for (var i = 0; i < v.length; i++) {
      // A flat series is drawn at the middle of the ramp, not at the bottom:
      // "no change" should look like a line, not like an absence of data.
      var t = span === 0 ? 0.5 : (v[i] - min) / span;
      var idx = Math.round(t * (ASCII_RAMP.length - 1));
      out += ASCII_RAMP.charAt(idx);
    }
    return out;
  }

  function braille(values, opts) {
    opts = opts || {};
    var cells = opts.width || 20;        // each cell = 2 samples
    var v = resample(clean(values), cells * 2);
    if (!v.length) return '';
    var min = Math.min.apply(null, v), max = Math.max.apply(null, v);
    var span = max - min;
    var out = '';
    for (var i = 0; i < v.length; i += 2) {
      var left = v[i];
      var right = (i + 1 < v.length) ? v[i + 1] : left;
      var lh = span === 0 ? 2 : Math.max(1, Math.round(((left - min) / span) * 4));
      var rh = span === 0 ? 2 : Math.max(1, Math.round(((right - min) / span) * 4));
      var bits = 0;
      for (var k = 0; k < lh; k++) bits |= BRAILLE_LEFT[k];
      for (var m = 0; m < rh; m++) bits |= BRAILLE_RIGHT[m];
      out += String.fromCharCode(0x2800 + bits);
    }
    return out;
  }

  // Fill an element with a sparkline plus a one-line summary. `values` is the
  // raw series; the summary reports the real numbers so the picture never has
  // to be trusted for magnitude.
  function mount(el, values, opts) {
    if (!el) return null;
    opts = opts || {};
    var v = clean(values);
    if (!v.length) {
      el.textContent = opts.empty || 'no data';
      el.className = (el.className || '') + ' spark-empty';
      return null;
    }
    var mode = opts.mode === 'braille' ? 'braille' : 'ascii';
    var text = mode === 'braille' ? braille(v, opts) : ascii(v, opts);
    el.textContent = text;
    // Re-mounting the same element (a range change) must not stack duplicate
    // mode classes on it.
    var cls = (el.className || '').replace(/\bspark-empty\b/, '').replace(/\s+/g, ' ').trim();
    if (cls.indexOf('spark-' + mode) === -1) cls += ' spark-' + mode;
    el.className = cls.trim();
    if (opts.title) el.title = opts.title;
    return {
      min: Math.min.apply(null, v),
      max: Math.max.apply(null, v),
      last: v[v.length - 1],
      points: v.length,
      mode: mode
    };
  }

  // Radial gauge. Drawn with two arcs on a canvas; the numeric value is the
  // DOM text beside it, so the canvas is decoration and carries aria-hidden.
  // Colour thresholds mirror the app's existing barColor() bands.
  var GAUGE_SWEEP = Math.PI * 1.5;      // 270 degrees
  var GAUGE_START = Math.PI * 0.75;     // lower-left

  function gaugeColor(pct) {
    if (pct >= 90) return '#ef4444';
    if (pct >= 70) return '#f97316';
    if (pct >= 40) return '#eab308';
    return '#22c55e';
  }

  function gauge(canvas, pct, opts) {
    if (!canvas || !canvas.getContext) return;
    opts = opts || {};
    var p = Math.max(0, Math.min(100, Number(pct) || 0));
    var cssSize = opts.size || 92;
    var dpr = window.devicePixelRatio || 1;
    canvas.width = cssSize * dpr;
    canvas.height = cssSize * dpr;
    canvas.style.width = cssSize + 'px';
    canvas.style.height = cssSize + 'px';
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssSize, cssSize);

    var cx = cssSize / 2, cy = cssSize / 2;
    var lineWidth = opts.lineWidth || 8;
    var radius = (cssSize - lineWidth) / 2 - 2;

    ctx.lineCap = 'butt';
    // Track: the token colour the app already uses for inert surfaces.
    ctx.beginPath();
    ctx.arc(cx, cy, radius, GAUGE_START, GAUGE_START + GAUGE_SWEEP);
    ctx.strokeStyle = opts.track || '#1e293b';
    ctx.lineWidth = lineWidth;
    ctx.stroke();

    if (p > 0) {
      ctx.beginPath();
      ctx.arc(cx, cy, radius, GAUGE_START, GAUGE_START + GAUGE_SWEEP * (p / 100));
      ctx.strokeStyle = opts.color || gaugeColor(p);
      ctx.lineWidth = lineWidth;
      ctx.stroke();
    }

    if (opts.label !== false) {
      ctx.fillStyle = opts.text || '#f8fafc';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.font = '700 ' + Math.round(cssSize * 0.22) + 'px "SF Mono", "Fira Code", monospace';
      ctx.fillText(Math.round(p) + '%', cx, cy - cssSize * 0.02);
      if (opts.sub) {
        ctx.font = Math.round(cssSize * 0.1) + 'px "SF Mono", "Fira Code", monospace';
        ctx.fillStyle = opts.subText || '#94a3b8';
        ctx.fillText(opts.sub, cx, cy + cssSize * 0.2);
      }
    }
    return p;
  }

  window.LCPSpark = {
    ascii: ascii,
    braille: braille,
    mount: mount,
    gauge: gauge,
    gaugeColor: gaugeColor
  };
})();
