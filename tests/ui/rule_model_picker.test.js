// Behavioural check of the rule-picker's model-identity helpers, extracted
// verbatim from sec_providers_script.html and run against a realistic registry.
// Run with:
//   docker run --rm \
//     -v <repo>/src/ui/templates/jinja/sections:/s:ro \
//     -v <repo>/tests/ui/rule_model_picker.test.js:/t.js:ro \
//     node:22-alpine node /t.js
//
// pytest does not execute this file (it is not a test_*.py); it guards the
// rule-picker's model-identity helpers, which live in a Jinja template and so
// cannot be imported by Python. It slices the SHIPPED functions out of
// sec_providers_script.html and drives them, so it fails if the template's
// behaviour drifts even when the Python suite is green.
const fs = require('fs');
const src = fs.readFileSync(process.env.LCP_TEMPLATE || '/s/sec_providers_script.html', 'utf8');

// Pull the rule-picker code out of the template so we test the SHIPPED code.
// From `var PROV_MODELS` up to `renderRoutingRules`, which is the last function
// that needs DOM/network access to define.
const start = src.indexOf('var PROV_MODELS');
const end = src.indexOf('function renderRoutingRules');
if (start < 0 || end < 0) throw new Error('rule-picker block not found');
const escLine = src.split('\n').find(l => l.startsWith('const escC = '));
if (!escLine) throw new Error('escC not found');
const js = escLine + '\n' + src.slice(start, end);

const api = new Function(js + `
  return {canonFor, registryEntryForCanon, ruleModelLabel,
          ruleModelOptions, ruleModelOptionsCompat, ruleTaskOptions, usedIntents,
          setState: (r, a, p) => { RULE_REGISTRY = r; ALIAS_TO_CANON = a; CANON_NAMES = p.canon; PROV_CANONS = p.prov; },
          setProvModels: (m) => { PROV_MODELS = m; },
          setRouting: (r) => { ROUTING = r; }};
`)();

// The registry as it exists after the fix: one canonical model, three spellings.
const registry = {
  'deepseek-flash': {
    logical_name: 'deepseek-flash',
    benchmark_key: 'deepseek-v4.1-flash',
    provider_mappings: {
      deepseek: 'deepseek-flash',
      opencode: 'deepseek-v4.1-flash',
      commandcode: 'deepseek/deepseek-v4.1-flash',
    },
  },
  'deepseek-v4-pro': {
    logical_name: 'deepseek-v4-pro',
    benchmark_key: 'deepseek-v4-pro',
    provider_mappings: {deepseek: 'deepseek-v4-pro', opencode: 'deepseek-v4-pro',
                        commandcode: 'deepseek/deepseek-v4-pro'},
  },
};

// Rebuild the indexes exactly as loadRuleRegistry() does.
const RULE_REGISTRY = {}, ALIAS_TO_CANON = {}, CANON = [], PROV = {};
for (const [logical, e] of Object.entries(registry)) {
  const canon = e.benchmark_key || e.logical_name;
  RULE_REGISTRY[logical] = e;
  ALIAS_TO_CANON[String(e.logical_name).toLowerCase()] = canon;
  ALIAS_TO_CANON[String(canon).toLowerCase()] = canon;
  if (!CANON.includes(canon)) CANON.push(canon);
  for (const [p, v] of Object.entries(e.provider_mappings || {})) {
    if (v) ALIAS_TO_CANON[String(v).toLowerCase()] = canon;
    (PROV[p] = PROV[p] || []).includes(canon) || PROV[p].push(canon);
  }
}
CANON.sort();
api.setState(RULE_REGISTRY, ALIAS_TO_CANON, {canon: CANON, prov: PROV});
api.setProvModels({
  deepseek: ['deepseek-v4-pro', 'deepseek-v4-flash'],
  opencode: ['deepseek-v4-pro', 'deepseek-v4-flash'],
  commandcode: ['deepseek-v4-pro', 'deepseek-v4-flash', 'claude-sonnet-5'],
});
api.setRouting({
  per_task: {agentic_multi_step: 1, code_generation: 1, debugging: 1},
  rules: [{task: 'debugging', profile: 'l2', action: 'prefer',
           provider: '*', model: 'deepseek-v4.1-flash'}],
  profiles: ['l2'],
});

let fails = 0;
function check(name, cond, detail) {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + name + (detail ? '   ' + detail : ''));
  if (!cond) fails++;
}

// 1. Every provider spelling collapses to the ONE canonical option.
check('all provider spellings map to one canonical name',
  ['deepseek-flash', 'deepseek-v4.1-flash', 'deepseek/deepseek-v4.1-flash']
    .every(m => api.canonFor(m) === 'deepseek-v4.1-flash'));

// 2. The "provider = *" dropdown has exactly one option per MODEL, not per name.
const all = api.ruleModelOptions('*', '*');
const allValues = all.map(o => o.value);
check('unscoped model list has one entry per model',
  allValues.join(',') === '*,deepseek-v4-pro,deepseek-v4.1-flash',
  JSON.stringify(allValues));

// 3. A pinned provider offers only the models mapped to it — still canonical.
const cc = api.ruleModelOptions('commandcode', '*');
check('provider-scoped list uses canonical names',
  cc.map(o => o.value).join(',') === '*,deepseek-v4.1-flash,deepseek-v4-pro',
  JSON.stringify(cc.map(o => o.value)));
check('label shows the canonical name with the provider-side ID in brackets',
  cc.find(o => o.value === 'deepseek-v4.1-flash').label ===
    'deepseek-v4.1-flash (deepseek/deepseek-v4.1-flash)',
  cc.find(o => o.value === 'deepseek-v4.1-flash').label);

// 4. A legacy/unregistered value in an existing rule stays selectable.
const legacy = api.ruleModelOptions('*', 'some-old-model');
check('unregistered current value is preserved, not reset to *',
  legacy.map(o => o.value).includes('some-old-model'));

// 5. The task picker hides intents that already have a rule in this scope.
const taken = api.usedIntents('l2');
check('usedIntents sees the existing debugging rule', taken.debugging === 1);
const tasks = api.ruleTaskOptions('*', taken).map(o => o.value);
check('draft task list omits intents already ruled in this scope',
  !tasks.includes('debugging') && tasks.includes('code_generation'),
  JSON.stringify(tasks));

// 6. Fallback path: with no registry the picker still populates.
api.setState({}, {}, {canon: [], prov: {}});
const fb = api.ruleModelOptionsCompat('commandcode', '*');
check('falls back to raw provider models when the registry is unavailable',
  fb.map(o => o.value).includes('claude-sonnet-5'),
  JSON.stringify(fb.map(o => o.value)));

console.log(fails ? `\n${fails} CHECK(S) FAILED` : '\nALL CHECKS PASSED');
process.exit(fails ? 1 : 0);
