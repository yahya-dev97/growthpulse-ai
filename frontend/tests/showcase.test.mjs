// Showcase layouts and the screenshot export.
//
// Run: node --test frontend/tests/showcase.test.mjs
// Needs Python 3 and Google Chrome; skips if Chrome is missing.

import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { exportShowcase, pngSize, SHOTS } from '../tools/export_showcase.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CHROME = [
  process.env.CHROME_PATH,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].find((candidate) => candidate && existsSync(candidate));
const skip = !CHROME && 'Google Chrome not found';
const PORT = 9000 + Math.floor(Math.random() * 150);
const data = JSON.parse(readFileSync(path.join(ROOT, 'data', 'dashboard_data.json'), 'utf8'));
const legacy = data.clients.find((client) => client.key === 'examplescan-uk');
let server;

before(async () => {
  if (!CHROME) return;
  server = spawn('python3', ['-m', 'http.server', String(PORT), '--bind', '127.0.0.1'], { cwd: ROOT, stdio: 'ignore' });
  for (let i = 0; i < 50; i += 1) {
    try { if ((await fetch(`http://127.0.0.1:${PORT}/showcase.html`)).ok) return; } catch { /* starting */ }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error('static server did not start');
});
after(() => server?.kill());

const render = (layout) => execFileSync(CHROME, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--window-size=1920,1080',
  '--virtual-time-budget=5000', '--dump-dom', `http://127.0.0.1:${PORT}/showcase.html#${layout}`,
], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 60000 });
const text = (html) => html.replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/\s+/g, ' ');

// Claims the showcase must never make: AI inference is not implemented.
const FORBIDDEN = [/AI[- ]generated (?!—)/i, /powered by AI/i, /AI recommends/i, /AI-driven/i, /approved and applied/i];

test('performance layout shows pipeline values, every basis separately, and the disclosure', { skip }, () => {
  const html = render('performance');
  const body = text(html);
  assert.match(html, /data-layout="performance"/);
  assert.match(body, /SYNTHETIC DEMO|Synthetic demo/i);
  assert.match(body, /ExampleScan UK \(fictional\)/);
  assert.match(body, /1–30 Oct 2026/);
  assert.match(body, new RegExp(legacy.report.report_id.slice(0, 12)));
  assert.match(body, /£62,289/);
  assert.match(body, /40,669/);
  for (const basis of Object.values(legacy.daily.bases)) assert.ok(body.includes(basis.label), basis.label);
  assert.match(body, /never summed across bases/);
  assert.match(body, /platform-attributed, not bookings or true CAC/);
  assert.equal((html.match(/class="sc-finding"/g) ?? []).length, 3);
  for (const pattern of FORBIDDEN) assert.doesNotMatch(body, pattern);
});

test('findings layout pairs three findings with three labelled drafts', { skip }, () => {
  const html = render('findings');
  const body = text(html);
  assert.match(html, /data-layout="findings"/);
  assert.equal((html.match(/class="flow-row"/g) ?? []).length, 3);
  assert.equal((body.match(/Requires human approval/g) ?? []).length, 3);
  assert.match(body, /Deterministic experiment templates — not AI-generated\./);
  assert.match(body, /Approvals in this demo are not persisted/);
  assert.match(body, /\+133\.7%/);
  assert.match(body, /6 days · 21–26 Oct 2026/); // zero-conversion run, from the evidence
  assert.match(body, /£910\.80/);
  assert.match(body, /-55\.4%/);
  for (const pattern of FORBIDDEN) assert.doesNotMatch(body, pattern);
});

test('export writes both PNGs at exactly 1920 x 1080, and only if the layouts fit', { skip }, async () => {
  const out = mkdtempSync(path.join(tmpdir(), 'gp-export-'));
  try {
    const results = await exportShowcase(out);
    assert.deepEqual(results.map((r) => path.basename(r.file)), SHOTS.map((s) => s.file));
    for (const result of results) {
      assert.equal(result.fits, 'true');
      assert.equal(result.viewport, '1920x1080');
      assert.deepEqual(pngSize(readFileSync(result.file)), { width: 1920, height: 1080 });
    }
    assert.equal(readdirSync(out).length, 2);
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
});
