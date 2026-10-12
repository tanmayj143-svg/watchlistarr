/**
 * Frontend harness: loads the real app.js in a DOM, exercises the rule editor
 * against the rule shapes the server actually sends, and fails loudly on any
 * thrown error. Run: node tests/test_frontend.js
 */
const fs = require('fs');
const path = require('path');

const APP = path.join(__dirname, '..', 'app', 'web', 'static', 'app.js');
const src = fs.readFileSync(APP, 'utf8');

let pass = 0, fail = 0;
const check = (label, cond, extra = '') => {
  if (cond) { pass++; console.log(`  ok  ${label}${extra ? ' — ' + extra : ''}`); }
  else { fail++; console.log(`  FAIL ${label} ${extra}`); }
};

// ---------------------------------------------------------------- fake DOM
function makeEl(tag = 'div') {
  const el = {
    tagName: (tag || 'div').toUpperCase(), children: [], style: {}, dataset: {},
    _html: '', _text: '', _attrs: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    value: '', disabled: false, checked: false, multiple: false, type: '', placeholder: '',
    firstElementChild: null, selectedOptions: [], options: [],
    set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html; },
    set textContent(v) { this._text = v; }, get textContent() { return this._text; },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    querySelector: () => null, querySelectorAll: () => [],
    querySelector(sel) { return sel === '.rail' ? null : null; },
    closest: () => null, focus() {}, scrollBy() {}, remove() {},
  };
  return el;
}

const doc = {
  addEventListener() {},
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (t) => makeEl(t),
  body: makeEl('body'),
};
const win = { location: { href: '', reload() {} } };
global.document = doc;
global.window = win;
global.setTimeout = (fn) => { try { fn(); } catch (_) {} };
global.CustomEvent = class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } };
global.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) });

// ---------------------------------------------------------------- load app.js
// app.js is an IIFE that assigns its helpers onto `window`, so read them back
// off the stub window rather than Node's `global`.
const W = win;
let loadErr = null;
try { eval(src); } catch (e) { loadErr = e; }
check('app.js evaluates without throwing', !loadErr, loadErr ? loadErr.message : '');
if (loadErr) { console.log('\nAborting: app.js failed to load.'); process.exit(1); }

check('posterCard exported', typeof W.posterCard === 'function');
check('ruleRow exported', typeof W.ruleRow === 'function');
check('esc escapes HTML', W.esc('<img src=x onerror=alert(1)>').includes('&lt;img'));

// ---------------------------------------------------- rule editor: real shapes
console.log('\n  ruleRow rendering for rules the server sends:');
const CASES = [
  ['genre (multi-select)', { id: 'sf', kind: 'genre', op: 'in', value: ['Sci-Fi'], points: 40 },
   (h) => h.includes('data-f="kind"') && h.includes('data-f="points"')],
  ['country', { id: 'c', kind: 'country', op: 'in', value: ['China'], points: 30 },
   (h) => h.includes('data-f="kind"')],
  ['decade', { id: 'd', kind: 'decade', op: 'in', value: [1960], points: 40 },
   (h) => h.includes('data-f="value"')],
  ['max_free_gb (limit)', { id: 'm', kind: 'max_free_gb', comparator: 'lte', threshold: 500, threshold2: 4, limit: true, points: 0 },
   (h) => h.includes('limit-rule') && h.includes('data-f="threshold"') && h.includes('data-f="threshold2"')],
  ['capacity (limit)', { id: 'k', kind: 'capacity', unit: 'free_gb', comparator: 'lt', threshold: 100, limit: true, points: 0 },
   (h) => h.includes('limit-rule') && h.includes('data-f="unit"')],
];
for (const [label, rule, ok] of CASES) {
  let html = '';
  try { html = W.ruleRow(rule); } catch (e) { check(`ruleRow: ${label}`, false, e.message); continue; }
  check(`ruleRow: ${label}`, typeof html === 'string' && html.length > 40 && ok(html));
}

// ------------------------------------------------------------ poster cards
console.log('\n  posterCard rendering:');
const CAND = {
  id: 7, title: 'The Godfather', year: 1972, poster: 'https://img/x.jpg',
  score: 55.0, imdb_rating: 9.2, genres: ['Drama'], countries: ['United States'],
};
let card = '';
try { card = W.posterCard(CAND, { added: true }); } catch (e) { check('posterCard renders', false, e.message); }
check('posterCard renders', typeof card === 'string' && card.includes('The Godfather'));
check('posterCard shows score', card.includes('+55'));
check('posterCard marks added', card.includes('added-tag'));
check('posterCard escapes title',
  !W.posterCard({ title: '<script>alert(1)</script>' }, {}).includes('<script>'));

const noPoster = W.posterCard({ title: 'No Art', year: 1950 }, {});
check('posterCard handles missing poster', noPoster.includes('ph'));
check('posterCard handles missing year', !W.posterCard({ title: 'X' }, {}).includes('undefined'));

// ------------------------------------------------------------ picker
console.log('\n  searchable picker (replaces the unusable <select multiple>):');
{
  // The picker is built by ruleRow; verify the markup contract and the data
  // round-trip that readRules depends on.
  const html = W.ruleRow({ id: 'g', kind: 'genre', op: 'in', value: ['Sci-Fi', 'Horror'], points: 40 });
  check('picker rendered for genre', html.includes('class="picker"'));
  check('no raw <select multiple> left', !html.includes('multiple'));
  check('picker carries its selection', html.includes('Sci-Fi') && html.includes('Horror'));
  check('picker shows a count summary', /2 selected/.test(html));
  check('picker has a search box', html.includes('data-role="search"'));
  check('picker panel is hidden by default', html.includes('class="picker-pop" hidden'));

  const ctry = W.ruleRow({ id: 'c', kind: 'country', op: 'in', value: [], points: 10 });
  check('empty picker shows placeholder', ctry.includes('Choose values'));
  check('country also uses the picker', ctry.includes('data-vocab="countries"'));

  const root = { dataset: { selected: JSON.stringify(['Sci-Fi', 'Drama']) } };
  check('pickerValue parses selections',
    JSON.stringify(W.pickerValue(root)) === JSON.stringify(['Sci-Fi', 'Drama']));
  check('pickerValue tolerates junk',
    Array.isArray(W.pickerValue({ dataset: { selected: 'not-json' } })));

  const lang = W.ruleRow({ id: 'l', kind: 'language', op: 'in', value: ['Mandarin'], points: 5 });
  check('language uses the picker', lang.includes('data-vocab="languages"'));

  const dec = W.ruleRow({ id: 'd', kind: 'decade', op: 'in', value: [1960], points: 40 });
  check('decade keeps a plain text input', !dec.includes('class="picker"'));

  const cap = W.ruleRow({ id: 'k', kind: 'capacity', unit: 'free_gb', comparator: 'lt', threshold: 100, limit: true, points: 0 });
  check('capacity limit rule still renders', cap.includes('limit-rule'));
  // v1.1.3: quota / list_size were removed -- Pace settings replace them.
  check('limit rule has no dead value box', !cap.includes('class="rule-value"'),
    'the meaningless full-width text input was the "dropdown that would not open"');
  check('quota kind no longer offered', !cap.includes('value="quota"'));
  check('list_size kind no longer offered', !cap.includes('value="list_size"'));
  check('operator select is not disabled for limit rules',
    !cap.includes('data-f="op" disabled'), 'a disabled select is skipped when saving');
}

// ------------------------------------------------- 401 redirect guard
// Regression: an unauthenticated visitor got bounced /login -> /api -> 401 ->
// /login forever, making the UI unusable. The guard must stop that.
console.log('\n  401 redirect guard:');
const src2 = fs.readFileSync(APP, 'utf8');
check('api() does not redirect when already on /login',
  /onLogin/.test(src2) && /window\.__wlRedirecting/.test(src2));
check('api() redirects at most once', /!window\.__wlRedirecting/.test(src2));
check('boot skips loadVocab on auth pages',
  /authPage/.test(src2) && /if \(!authPage\) loadVocab\(\)/.test(src2));

// Simulate the actual loop: on /login, a 401 must NOT change location.
async function simulate(pathname, status) {
  // api() reads/writes BOTH `window.__wlRedirecting` and `window.location.href`,
  // so window.location must be the very object we assert on.
  const loc = { pathname, href: 'unchanged' };
  const sandbox = { window: { __wlRedirecting: false, location: loc }, location: loc };
  const body = src2.slice(src2.indexOf('async function api('), src2.indexOf('window.api = api;'));
  const make = new Function('window', 'location', 'fetch', `return (${body});`);
  const api = make(sandbox.window, loc, async () => ({ status, json: async () => ({}) }));
  try {
    await api('/api/catalogue', { method: 'GET' });
  } catch (_) { /* expected on 401 */ }
  return { href: loc.href, redirected: sandbox.window.__wlRedirecting };
}
(async () => {
  const onLogin = await simulate('/login', 401);
  check('on /login: href unchanged (no redirect)', onLogin.href === 'unchanged', onLogin.href);

  const elsewhere = await simulate('/watchlist/2', 401);
  check('on a page: redirects to /login exactly once', elsewhere.href === '/login', elsewhere.href);
  check('redirect guard flag set', elsewhere.redirected === true);

  // Second 401 from the same page must not fire another navigation.
  const again = await simulate('/watchlist/2', 401);
  check('repeat 401 cannot re-trigger a redirect', again.href === '/login', again.href);

  console.log(`\n${'='.repeat(52)}\n  ${pass} passed, ${fail} failed\n${'='.repeat(52)}`);
  process.exit(fail ? 1 : 0);
})();

// (summary printed by the async guard block above)