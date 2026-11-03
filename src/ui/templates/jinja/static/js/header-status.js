// Global top-right header widget — latest active provider + usage/credits.
// Rendered on EVERY page (base.html) and refreshed every 60s. It fetches the
// cost-plugin data itself, so it works on any page without server injection.
(function () {
  'use strict';

  var hdrBadge = document.getElementById('pluginHeaderBadge');
  var hdrDot = document.getElementById('pluginHeaderDot');
  var hdrText = document.getElementById('pluginHeaderText');
  if (!hdrBadge || !hdrText) return; // page didn't include the widget

  var hpUsage = document.getElementById('hpUsage');
  var hpFill = document.getElementById('hpUsageFill');
  var hpLabel = document.getElementById('hpUsageLabel');
  var hpPop = document.getElementById('hpUsagePopover');

  function barColor(pct) { return pct >= 90 ? 'red' : pct >= 70 ? 'orange' : pct >= 40 ? 'yellow' : 'green'; }

  function fmtCountdown(sec) {
    if (!sec || sec <= 0) return '';
    var d = Math.floor(sec / 86400);
    var h = Math.floor((sec % 86400) / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var parts = [];
    if (d > 0) parts.push(d + 'd');
    if (h > 0) parts.push(h + 'h');
    if (m > 0 || parts.length === 0) parts.push(m + 'm');
    return parts.join(' ');
  }

  function fmtResetAt(sec) {
    if (!sec || sec <= 0) return '';
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    var resetDate = new Date(Date.now() + sec * 1000);
    return months[resetDate.getMonth()] + ' ' + resetDate.getDate() + ' ' +
      String(resetDate.getHours()).padStart(2, '0') + ':' + String(resetDate.getMinutes()).padStart(2, '0');
  }

  function fmtTokens(n) {
    if (n == null) return '—';
    if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n);
  }

  function hideUsage() { if (hpUsage) hpUsage.style.display = 'none'; }

  // OpenCode: rolling/weekly/monthly usage bar + popover, plus an
  // "Available credits" row from the billing snapshot when present.
  function renderOpencodeUsage(ocSub, ocBal) {
    if (!hpUsage) return;
    var rows = '';
    if (ocBal && ocBal.balance != null) {
      rows += '<div class="hp-usage-popover-row"><div class="hp-usage-pop-row-head">' +
        '<span class="hp-usage-pop-name">Available credits</span>' +
        '<span class="hp-usage-pop-pct">$' + ocBal.balance.toFixed(2) + '</span></div>' +
        (ocBal.plan ? '<div class="hp-usage-pop-sub">' + escH(ocBal.plan) + '</div>' : '') +
        '</div>';
    }
    if (!ocSub || ocSub._error || ocSub.rolling_pct == null) {
      if (rows) {
        hpFill.style.width = '0%';
        hpFill.className = 'hp-usage-fill';
        hpLabel.textContent = (ocBal && ocBal.balance != null) ? '$' + ocBal.balance.toFixed(2) + ' credits' : '';
        hpPop.innerHTML = rows;
        hpUsage.style.display = 'flex';
      } else {
        hideUsage();
      }
      return;
    }
    var windows = [
      { name: 'Rolling (5h)', pct: ocSub.rolling_pct, reset: ocSub.rolling_reset_sec },
      { name: 'Weekly',       pct: ocSub.weekly_pct,  reset: ocSub.weekly_reset_sec },
      { name: 'Monthly',      pct: ocSub.monthly_pct, reset: ocSub.monthly_reset_sec }
    ];
    var top = windows.reduce(function (a, b) { return (b.pct > a.pct) ? b : a; });
    hpFill.style.width = Math.min(top.pct, 100) + '%';
    hpFill.className = 'hp-usage-fill ' + barColor(top.pct);
    hpLabel.textContent = top.name + ' ' + top.pct.toFixed(0) + '%';
    windows.forEach(function (w) {
      if (w.pct == null) return;
      rows += '<div class="hp-usage-popover-row">' +
        '<div class="hp-usage-pop-row-head">' +
          '<span class="hp-usage-pop-name">' + w.name + '</span>' +
          '<span class="hp-usage-pop-pct">' + w.pct.toFixed(0) + '%</span>' +
        '</div>' +
        '<div class="hp-usage-pop-track"><div class="hp-usage-pop-fill ' + barColor(w.pct) + '" style="width:' + Math.min(w.pct, 100) + '%"></div></div>' +
        '<div class="hp-usage-pop-sub"><span>reset ' + fmtCountdown(w.reset) + '</span><span>' + fmtResetAt(w.reset) + '</span></div>' +
      '</div>';
    });
    hpPop.innerHTML = rows;
    hpUsage.style.display = 'flex';
  }

  // Command Code: rolling/weekly/monthly bar + popover with credits + tokens.
  function renderCommandCodeUsage(ccSub) {
    if (!hpUsage) return;
    if (!ccSub || ccSub._error || ccSub.five_hour_pct == null) { hideUsage(); return; }
    var windows = [
      { name: 'Rolling (5h)', pct: ccSub.five_hour_pct, reset: ccSub.five_hour_reset_sec },
      { name: 'Weekly',       pct: ccSub.weekly_pct,  reset: ccSub.weekly_reset_sec }
    ];
    if (ccSub.monthly_pct != null && ccSub.monthly_pct > 0) {
      windows.push({ name: 'Monthly', pct: ccSub.monthly_pct, reset: ccSub.monthly_reset_sec });
    }
    var top = windows.reduce(function (a, b) { return (b.pct > a.pct) ? b : a; });
    hpFill.style.width = Math.min(top.pct, 100) + '%';
    hpFill.className = 'hp-usage-fill ' + barColor(top.pct);
    hpLabel.textContent = top.name + ' ' + top.pct.toFixed(0) + '%';
    var rows = '';
    windows.forEach(function (w) {
      if (w.pct == null) return;
      rows += '<div class="hp-usage-popover-row">' +
        '<div class="hp-usage-pop-row-head">' +
          '<span class="hp-usage-pop-name">' + w.name + '</span>' +
          '<span class="hp-usage-pop-pct">' + w.pct.toFixed(0) + '%</span>' +
        '</div>' +
        '<div class="hp-usage-pop-track"><div class="hp-usage-pop-fill ' + barColor(w.pct) + '" style="width:' + Math.min(w.pct, 100) + '%"></div></div>' +
        '<div class="hp-usage-pop-sub"><span>reset ' + fmtCountdown(w.reset) + '</span><span>' + fmtResetAt(w.reset) + '</span></div>' +
      '</div>';
    });
    if (ccSub.monthly_credits_remaining != null) {
      rows += '<div class="hp-usage-popover-row"><div class="hp-usage-pop-row-head">' +
        '<span class="hp-usage-pop-name">Monthly credits</span>' +
        '<span class="hp-usage-pop-pct">$' + ccSub.monthly_credits_remaining.toFixed(2) + '</span></div></div>';
    }
    var ccTotals = ccSub.usage_summary || {};
    if (ccTotals.total_tokens != null) {
      rows += '<div class="hp-usage-popover-row"><div class="hp-usage-pop-row-head">' +
        '<span class="hp-usage-pop-name">Total tokens</span>' +
        '<span class="hp-usage-pop-pct">' + fmtTokens(ccTotals.total_tokens) + '</span></div></div>';
    }
    hpPop.innerHTML = rows;
    hpUsage.style.display = 'flex';
  }

  function escH(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Provider with the most recent successful request (from usage rows).
  function latestProviderFromUsage(allUsage) {
    var best = null, bestDate = '';
    Object.keys(allUsage || {}).forEach(function (prov) {
      var rows = allUsage[prov] || [];
      var maxDate = '';
      rows.forEach(function (r) {
        if ((r.request_count || 0) > 0 && r.date > maxDate) maxDate = r.date;
      });
      if (maxDate && maxDate >= bestDate) { bestDate = maxDate; best = prov; }
    });
    return best;
  }

  function loadHeaderStatus() {
    Promise.all([
      fetch('/api/cost-plugins/usage').then(function (r) { return r.json(); }).catch(function () { return {}; }),
      fetch('/api/cost-plugins/summary').then(function (r) { return r.json(); }).catch(function () { return {}; }),
      fetch('/api/cost-plugins/subscriptions').then(function (r) { return r.json(); }).catch(function () { return {}; }),
      fetch('/api/cost-plugins/balances').then(function (r) { return r.json(); }).catch(function () { return {}; })
    ]).then(function (results) {
      var allUsage = (results[0].plugin_usage || {});
      var pluginSummaries = (results[1].plugin_summaries || {});
      var subscriptions = (results[2].plugin_subscriptions || {});
      var balances = (results[3].plugin_balances || {});

      var latest = latestProviderFromUsage(allUsage);
      if (!latest && window.configuredProviders && window.configuredProviders.length) {
        latest = window.configuredProviders[0];
      }

      if (!latest) {
        hideUsage();
        hdrDot.className = 'header-plugin-dot off';
        hdrText.textContent = 'No requests yet';
        hdrBadge.title = '';
        return;
      }

      var sum = pluginSummaries[latest];
      var hdrLabel = providerDisplayName(latest);
      // Branch on the provider's TYPE (declared or inferred), so the header
      // chip reads the same way for a second account of a vendor and for a
      // second local box as it does for the first of each.
      var hdrType = (typeof providerTypeOf === 'function') ? (providerTypeOf(latest) || latest) : latest;

      if (hdrType === 'deepseek' && sum && sum.balance) {
        hideUsage();
        hdrLabel += ' \u00b7 ' + (sum.balance.currency || 'USD') + ' ' + sum.balance.available.toFixed(2) + ' available';
      } else if (hdrType === 'opencode') {
        var ocBal = balances[latest];
        if (ocBal && ocBal.balance != null) hdrLabel += ' \u00b7 $' + ocBal.balance.toFixed(2) + ' credits';
        renderOpencodeUsage(subscriptions[latest], ocBal);
      } else if (hdrType === 'commandcode') {
        var ccSub = subscriptions[latest];
        if (ccSub && ccSub.monthly_credits_remaining != null) {
          hdrLabel += ' \u00b7 $' + ccSub.monthly_credits_remaining.toFixed(2) + ' credits';
        }
        renderCommandCodeUsage(ccSub);
      } else if (hdrType === 'local') {
        hideUsage();
        var mt = (window.monthly && window.monthly[latest]) ? (window.monthly[latest].tokens || 0) : 0;
        hdrLabel += ' \u00b7 ' + fmtTokens(mt) + ' tokens';
      } else {
        hideUsage();
        var usg = allUsage[latest] || [];
        var totalCost = usg.reduce(function (s, r) { return s + (r.cost || 0); }, 0);
        hdrLabel += ' \u00b7 $' + totalCost.toFixed(4);
      }

      hdrDot.className = 'header-plugin-dot on';
      hdrText.textContent = hdrLabel;
      hdrBadge.title = providerDisplayName(latest) + ' \u2014 latest active provider';
    });
  }

  loadHeaderStatus();
  setInterval(loadHeaderStatus, 60000);
})();
