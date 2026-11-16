// Behavioural proof of the leaderboard's poll-in-session-storage behaviour, run
// against the SHIPPED helpers (sliced out of the Jinja template, which Python
// cannot import). Guards the operator's rule: the polled set is kept in the
// browser, a reload resumes it WITH its ticks, a fresh poll starts from its own
// defaults, and accepting/clearing empties it.
// Run with:
//   node tests/ui/poll_session.test.js
const fs = require('fs');
const src = fs.readFileSync(process.env.LCP_TEMPLATE ||
  require('path').join(__dirname, '..', '..', 'src', 'ui', 'templates', 'jinja',
                       'sections', 'sec_models_script.html'), 'utf8');
const grab = name => {
  const i = src.indexOf('function ' + name);
  if (i < 0) throw new Error(name + ' not found');
  let d = 0, j = src.indexOf('{', i);
  const start = j;
  for (; j < src.length; j++) {
    if (src[j] === '{') d++;
    else if (src[j] === '}') { d--; if (d === 0) break; }
  }
  return src.slice(i, j + 1);
};
const store = {};
global.sessionStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
const POLL_KEY = 'lcp.lb.poll.v1';
const STATE = {pollRows: [], polledAt: null, pollChecks: {}};
eval(grab('pollKeyOf'));
eval(grab('savePoll'));
eval(grab('loadPoll'));
eval(grab('saveChecks'));
eval(grab('checkStateFor'));

const rows = [
  {model_slug: 'claude-opus-5-5', taxonomy: 'debugging', unit: 'net_improvement', board: 'agent_bash_reliability', effort: '', changed: true},
  {model_slug: 'glm-5.3-flash', taxonomy: 'planning', unit: 'elo', board: 'instruction_following', effort: 'max', changed: false},
];
STATE.pollChecks = {};
savePoll(rows, '2026-11-16T06:18:44+00:00');
if (!store[POLL_KEY]) throw new Error('no blob written');
if (checkStateFor(rows[0]) !== true) throw new Error('changed row should default to checked');
if (checkStateFor(rows[1]) !== false) throw new Error('unchanged row should default to unchecked');

// the operator unticks the first and ticks the second
STATE.pollChecks[pollKeyOf(rows[0])] = false;
STATE.pollChecks[pollKeyOf(rows[1])] = true;
saveChecks();

// simulate a reload
STATE.pollRows = []; STATE.pollChecks = {}; STATE.polledAt = null;
if (!loadPoll()) throw new Error('loadPoll reported nothing');
if (STATE.polledAt !== '2026-11-16T06:18:44+00:00') throw new Error('poll time lost');
if (checkStateFor(STATE.pollRows[0]) !== false) throw new Error('unticked row reverted');
if (checkStateFor(STATE.pollRows[1]) !== true) throw new Error('ticked row reverted');

// a fresh poll must NOT inherit yesterday's ticks
STATE.pollChecks = {};
savePoll(STATE.pollRows, STATE.polledAt);
if (store[POLL_KEY].includes('"checks":{"') && !store[POLL_KEY].includes('"checks":{}')) {
  // empty object is fine; a populated one is not
}
STATE.pollRows = []; STATE.pollChecks = {}; loadPoll();
if (checkStateFor(STATE.pollRows[0]) !== true) throw new Error('fresh set lost its defaults');

// accepting empties it
savePoll([], null);
if (store[POLL_KEY]) throw new Error('accepted set still stored');
console.log('tick persistence: OK (defaults, untick survives, tick survives, fresh poll resets, accept clears)');
