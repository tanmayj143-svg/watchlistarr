/**
 * Per-row Request button test (v1.6.3).
 *
 * The button rendered and the API worked, but the page threw
 * "requestOne is not defined" on click: the onclick attribute referenced a
 * handler that had never actually been written into the template. Curl against
 * the endpoint passed, so only a real click could catch this.
 *
 * Run: node tests/test_request_button.js
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const TPL = path.join(__dirname, '..', 'app', 'web', 'templates', 'watchlist.html');
const APP = path.join(__dirname, '..', 'app', 'web', 'static', 'app.js');

let pass = 0, fail = 0;
const check = (label, cond, extra = '') => {
  if (cond) { pass++; console.log(`  ok  ${label}${extra ? ' — ' + extra : ''}`); }
  else { fail++; console.log(`  FAIL ${label} ${extra}`); }
};

function main() {
  const tpl = fs.readFileSync(TPL, 'utf8');

  console.log('\n1. The button exists in the markup');
  check('Request button rendered', /onclick="requestOne\(/.test(tpl));
  check('it passes the candidate id', /onclick="requestOne\(\{\{ c\.id \}\}, this\)"/.test(tpl));

  console.log('\n2. The handler it calls is actually defined');
  check('requestOne is defined', /async function requestOne\s*\(/.test(tpl));
  const m = /async function requestOne\s*\([\s\S]*?\n\}/.exec(tpl);
  check('handler body was captured', !!m);

  console.log('\n3. Every onclick handler in the template is defined');
  const called = new Set();
  for (const mm of tpl.matchAll(/onclick="([A-Za-z_$][\w$]*)\s*\(/g)) called.add(mm[1]);
  const defined = new Set(
    [...tpl.matchAll(/(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/g)].map((x) => x[1])
  );
  // handlers supplied by app.js rather than inline
  const appJs = fs.readFileSync(APP, 'utf8');
  for (const mm of appJs.matchAll(/(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/g)) {
    defined.add(mm[1]);
  }
  const missing = [...called].filter((n) => !defined.has(n));
  check('no undefined onclick handlers', missing.length === 0, missing.join(', '));

  console.log('\n4. requestOne posts to the per-candidate endpoint');
  check("uses /api/candidates/{id}/request",
    /api\('\/api\/candidates\/' \+ id \+ '\/request'/.test(tpl));

  console.log('\n5. Status column shows only the two states requested');
  check('has a pending state', />pending</.test(tpl));
  check('has a cached state for unreleased films', />cached</.test(tpl));
  check('cached is tied to not_released', /blocked_by == 'not_released'/.test(tpl));
  // The operator asked for no reasoning in this column.
  check('no gate reason rendered in the status cell',
    !/\{\{ c\.status_line or 'on hold' \}\}/.test(tpl),
    'status_line text must not be shown');

  console.log(`\n${'='.repeat(58)}\n  ${pass} passed, ${fail} failed\n${'='.repeat(58)}`);
  process.exit(fail ? 1 : 0);
}

main();