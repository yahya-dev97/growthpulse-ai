// Smoke tests for the dashboard UI: every screen renders, navigation and deep links work,
// and key states (multi-currency client, account scope, demo labelling) appear.
//
// Run: node --test frontend/tests/
// Needs Python 3 (static server) and Google Chrome (headless). Skips if Chrome is missing.

import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CHROME = [
  process.env.CHROME_PATH,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].find((candidate) => candidate && existsSync(candidate));
const PORT = 8800 + Math.floor(Math.random() * 150);
let server;

before(async () => {
  if (!CHROME) return;
  server = spawn('python3', ['-m', 'http.server', String(PORT), '--bind', '127.0.0.1'], {
    cwd: ROOT,
    stdio: 'ignore',
  });
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}/index.html`);
      if (response.ok) return;
    } catch { /* not up yet */ }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error('static server did not start');
});

after(() => server?.kill());

function render(route) {
  return execFileSync(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--virtual-time-budget=4000',
    '--dump-dom', `http://127.0.0.1:${PORT}/${route}`,
  ], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 60000 });
}

const count = (html, needle) => html.split(needle).length - 1;
const selectedTab = (html) => html.match(/aria-selected="true">([^<]+)/)?.[1];
const skip = !CHROME && 'Google Chrome not found';

test('performance overview renders KPIs, charts and the Milestone 4 numbers', { skip }, () => {
  const html = render('#overview');
  assert.match(html, /data-ready="true"/);
  assert.equal(selectedTab(html), 'Performance Overview');
  assert.equal(count(html, 'card tile'), 7); // 4 delivery + 3 conversion bases
  assert.ok(count(html, 'class="chart"') >= 2);
  assert.match(html, /£62,289/); // spend from report feb1e176…
  assert.match(html, /40,669/);
  assert.match(html, /View data as table/);
});

test('AI insights shows findings and a clearly labelled demo workflow', { skip }, () => {
  const html = render('#insights');
  assert.equal(selectedTab(html), 'AI Insights &amp; Experiments');
  assert.equal(count(html, 'class="finding"'), 14);
  assert.equal(count(html, 'class="exp-card"'), 6);
  assert.match(html, /Demonstration only/);
  assert.match(html, /Requires human approval — never auto-applied/);
  assert.match(html, /findings suppressed by the noise guard/);
});

test('accounts and reports screens render, sync is marked illustrative', { skip }, () => {
  const accounts = render('#accounts');
  assert.equal(selectedTab(accounts), 'Accounts &amp; Sync');
  assert.match(accounts, /Illustrative\./);
  assert.match(accounts, /synthetic-google-account-001/);
  const reports = render('#reports');
  assert.equal(selectedTab(reports), 'Reports');
  assert.match(reports, /feb1e17675c3/);
  assert.match(reports, /Stated limitations/);
});

test('multi-currency client renders one portfolio per currency', { skip }, () => {
  const html = render('?client=examplescan-eu#overview');
  for (const currency of ['EUR', 'GBP', 'USD']) {
    assert.match(html, new RegExp(`<h2>${currency} portfolio</h2>`));
  }
  assert.match(html, /never summed across currencies/);
});

test('account scope narrows the view and drops client-level deltas', { skip }, () => {
  const html = render('?account=synthetic-meta-account-001#overview');
  assert.match(html, /Meta Ads · GBP · Europe\/London/);
  assert.match(html, /period deltas shown at client scope/);
  assert.equal(count(html, 'card tile'), 6); // 4 delivery + Meta's 2 conversion bases
});

test('unknown deep-link values fall back safely', { skip }, () => {
  const html = render('?client=nope&account=nope#nowhere');
  assert.equal(selectedTab(html), 'Performance Overview');
  assert.match(html, /ExampleScan UK \(fictional\)/);
});
