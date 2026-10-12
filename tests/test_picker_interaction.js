/**
 * Real-DOM interaction test (jsdom). Mounts the actual rule-row markup produced
 * by app.js, loads the real app.js, waits for the vocabulary fetch to resolve,
 * then performs genuine clicks.
 *
 * This exists because markup-only assertions cannot catch "the dropdown does
 * not open" — two regressions in this area were invisible to them.
 *
 * Run: node tests/test_picker_interaction.js
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const APP = path.join(__dirname, '..', 'app', 'web', 'static', 'app.js');
const src = fs.readFileSync(APP, 'utf8');

let pass = 0, fail = 0;
const check = (label, cond, extra = '') => {
  if (cond) { pass++; console.log(`  ok  ${label}${extra ? ' — ' + extra : ''}`); }
  else { fail++; console.log(`  FAIL ${label} ${extra}`); }
};

const VOCAB = {
  genres: ['Drama', 'Sci-Fi', 'Horror', 'Comedy', 'Romance', 'Zombie'],
  countries: ['China', 'France', 'Iceland', 'Taiwan', 'Japan', 'United States'],
  languages: ['English', 'Mandarin', 'Japanese'],
};

async function main() {
  const dom = new JSDOM('<!doctype html><html><body><div id="ruleBox"><div id="ruleList"></div></div></body></html>', {
    url: 'http://127.0.0.1:7777/watchlist/2',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const { window } = dom;
  const { document } = window;

  window.fetch = async () => ({ ok: true, status: 200, json: async () => VOCAB });

  try {
    window.eval(src);
    check('app.js loads in a real DOM', true);
  } catch (e) {
    check('app.js loads in a real DOM', false, e.message);
    process.exit(1);
  }

  await window.loadVocab();

  const list = document.getElementById('ruleList');
  const box = document.getElementById('ruleBox');

  function addRule(rule) {
    const tmp = document.createElement('div');
    tmp.innerHTML = window.ruleRow(rule);
    const row = tmp.firstElementChild;
    list.appendChild(row);
    window.initAllPickers(row);
    return row;
  }

  console.log('\n  picker opens on click:');
  const row = addRule({ id: 'g', kind: 'genre', op: 'in', value: ['Sci-Fi'], points: 40 });
  const picker = row.querySelector('.picker');
  const btn = row.querySelector('[data-role="toggle"]');
  const pop = row.querySelector('.picker-pop');
  const search = row.querySelector('[data-role="search"]');
  const optList = row.querySelector('[data-role="list"]');

  check('picker exists', !!picker);
  check('toggle button exists', !!btn);
  check('panel starts hidden', pop.hidden === true);

  btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('click OPENS the panel', pop.hidden === false, `hidden=${pop.hidden}`);
  check('options rendered from vocabulary', optList.querySelectorAll('.picker-opt').length === 6,
    `${optList.querySelectorAll('.picker-opt').length} options`);

  console.log('\n  search filters:');
  search.value = 'sc';
  search.dispatchEvent(new window.Event('input', { bubbles: true }));
  const filtered = optList.querySelectorAll('.picker-opt');
  check('typing narrows the list', filtered.length === 1, `${filtered.length} option(s)`);
  check('correct option survives the filter', filtered[0] && filtered[0].dataset.v === 'Sci-Fi',
    filtered[0] ? filtered[0].dataset.v : 'none');

  console.log('\n  selecting values:');
  search.value = '';
  search.dispatchEvent(new window.Event('input', { bubbles: true }));
  const horror = Array.from(optList.querySelectorAll('.picker-opt')).find((o) => o.dataset.v === 'Horror');
  check('unselected option is present', !!horror);
  horror.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const sel = window.pickerValue(picker);
  check('clicking selects the value', sel.includes('Horror'), JSON.stringify(sel));
  check('existing selection retained', sel.includes('Sci-Fi'), JSON.stringify(sel));
  check('button shows a count', /2 selected/.test(btn.textContent), btn.textContent.trim().slice(0, 44));

  const sciFi = Array.from(optList.querySelectorAll('.picker-opt')).find((o) => o.dataset.v === 'Sci-Fi');
  check('already-selected option is marked', sciFi.className.includes('on'));
  sciFi.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('clicking again deselects', !window.pickerValue(picker).includes('Sci-Fi'),
    JSON.stringify(window.pickerValue(picker)));

  console.log('\n  closing:');
  // The toggle flips state, so it must also be able to close an open panel.
  btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('second click CLOSES the panel', pop.hidden === true, `hidden=${pop.hidden}`);

  // Click-away: clicking outside an OPEN picker must close it.
  btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('re-opened for click-away test', pop.hidden === false);
  document.body.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('click outside closes the panel', pop.hidden === true, `hidden=${pop.hidden}`);

  // The Done button must also close it.
  btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const done = row.querySelector('[data-role="done"]');
  done.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('Done button closes the panel', pop.hidden === true, `hidden=${pop.hidden}`);

  console.log('\n  limit-rule row (no dead value box):');
  const lim = addRule({ id: 'cap', kind: 'capacity', unit: 'free_gb', comparator: 'lt',
                        threshold: 100, limit: true, points: 0 });
  check('threshold input present', !!lim.querySelector('[data-f="threshold"]'));
  check('unit select present', !!lim.querySelector('[data-f="unit"]'));
  check('limits tag present', !!lim.querySelector('.limit-tag'));
  check('no picker on a limit rule', !lim.querySelector('.picker'));
  // THE BUG: a full-width text input rendered for every rule, including limit
  // rules where it is meaningless -- it looked like a dropdown that would not open.
  check('NO value row on a limit rule', !lim.querySelector('.rule-value'));
  check('NO text input on a limit rule', !lim.querySelector('.rule-value input'));
  check('no time-window selector (quota removed)', !lim.querySelector('[data-f="window"]'));
  check('operator dropdown is enabled', (() => {
    const op = lim.querySelector('[data-f="op"]');
    return op && !op.disabled;
  })());
  check('limit kinds excluded from the dropdown', (() => {
    const opts = Array.from(lim.querySelectorAll('[data-f="kind"] option')).map((o) => o.value);
    return !opts.includes('quota') && !opts.includes('list_size');
  })(), 'quota/list_size must not be selectable');


  console.log('\n  letterboxd_rating rule:');
  {
    const lb = addRule({ id: 'lb', kind: 'letterboxd_rating', op: 'gte', value: 4, points: 20 });
    const sel = lb.querySelector('[data-f="kind"]');
    check('letterboxd_rating is a selectable kind', sel.value === 'letterboxd_rating');
    const opts = Array.from(sel.options).map((o) => o.value);
    check('letterboxd_rating appears in the options list', opts.includes('letterboxd_rating'));
    check('operator defaults to gte for a numeric rule',
      lb.querySelector('[data-f="op"]').value === 'gte');
    check('value input present for the 0-5 threshold', !!lb.querySelector('[data-f="value"]'));
    check('points input present', lb.querySelector('[data-f="points"]').value === '20');
    check('no picker on a numeric rule', !lb.querySelector('.picker'));
  }

  console.log('\n  saved-rule round-trip:');
  // bindRuleEditor scopes to a container holding .rules
  const holder = document.createElement('div');
  holder.innerHTML = '<div class="rules"></div>';
  const rules = holder.querySelector('.rules');
  document.body.appendChild(holder);
  rules.appendChild(row);
  rules.appendChild(lim);
  window.bindRuleEditor(holder);
  const read = holder._readRules ? holder._readRules() : null;
  check('readRules exposed by bindRuleEditor', Array.isArray(read));
  if (read) {
    const g = read.find((r) => r.kind === 'genre');
    check('picker selections serialise', g && Array.isArray(g.value) && g.value.length === 1,
      g ? JSON.stringify(g.value) : 'no genre rule');
    check('points serialise', g && Number(g.points) === 40, g ? String(g.points) : '');
    const c = read.find((r) => r.kind === 'capacity');
    check('limit rule keeps limit=true', c && c.limit === true);
    check('limit threshold serialises', c && Number(c.threshold) === 100);
  }

  console.log(`\n${'='.repeat(52)}\n  ${pass} passed, ${fail} failed\n${'='.repeat(52)}`);
  process.exit(fail ? 1 : 0);
}

main().catch((e) => {
  console.error('harness error:', e);
  process.exit(1);
});