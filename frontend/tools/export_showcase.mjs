// Export the two portfolio screenshots at exactly 1920 x 1080.
//
//   node frontend/tools/export_showcase.mjs [output-dir]
//
// Default output: portfolio/screenshots/. Needs Python 3 (static server) and Google Chrome
// (set CHROME_PATH if it lives elsewhere). Uses the Chrome DevTools Protocol to pin the
// viewport to 1920 x 1080 and capture that exact region, then refuses to save a layout
// that reports overflow, and checks each PNG's real dimensions. No npm packages needed.

import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUTPUT = path.resolve(process.argv[2] ?? path.join(FRONTEND, '..', 'portfolio', 'screenshots'));
const WIDTH = 1920;
const HEIGHT = 1080;
export const SHOTS = [
  { layout: 'performance', file: 'growthpulse_performance_intelligence_1920x1080.png' },
  { layout: 'findings', file: 'growthpulse_analytical_findings_experiments_1920x1080.png' },
];
const CHROME = [
  process.env.CHROME_PATH,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].find((candidate) => candidate && existsSync(candidate));

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function pngSize(buffer) {
  const signature = '89504e470d0a1a0a';
  if (buffer.subarray(0, 8).toString('hex') !== signature) throw new Error('not a PNG');
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
}

async function waitFor(check, what, tries = 100) {
  for (let i = 0; i < tries; i += 1) {
    try {
      const value = await check();
      if (value) return value;
    } catch { /* not ready yet */ }
    await sleep(100);
  }
  throw new Error(`timed out waiting for ${what}`);
}

class Cdp {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.id = 0;
    this.pending = new Map();
    this.ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(message.error.message));
        else resolve(message.result);
      }
    };
  }
  open() {
    return new Promise((resolve, reject) => {
      this.ws.onopen = resolve;
      this.ws.onerror = reject;
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }
  async evaluate(expression) {
    const { result } = await this.send('Runtime.evaluate', { expression, returnByValue: true });
    return result.value;
  }
  close() { this.ws.close(); }
}

export async function exportShowcase(outputDir = OUTPUT) {
  if (!CHROME) throw new Error('Google Chrome not found; set CHROME_PATH');
  const port = 8950 + Math.floor(Math.random() * 40);
  const debugPort = 9300 + Math.floor(Math.random() * 40);
  const profile = mkdtempSync(path.join(tmpdir(), 'gp-showcase-'));
  const server = spawn('python3', ['-m', 'http.server', String(port), '--bind', '127.0.0.1'], {
    cwd: FRONTEND, stdio: 'ignore',
  });
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
    '--no-default-browser-check', `--user-data-dir=${profile}`,
    `--remote-debugging-port=${debugPort}`, `--window-size=${WIDTH},${HEIGHT}`, 'about:blank',
  ], { stdio: 'ignore' });
  const results = [];
  try {
    await waitFor(async () => (await fetch(`http://127.0.0.1:${port}/showcase.html`)).ok, 'static server');
    const target = await waitFor(async () => {
      const list = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
      return list.find((item) => item.type === 'page');
    }, 'Chrome');
    const cdp = new Cdp(target.webSocketDebuggerUrl);
    await cdp.open();
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false,
    });
    await cdp.send('Emulation.setEmulatedMedia', {
      features: [{ name: 'prefers-color-scheme', value: 'dark' }],
    });
    mkdirSync(outputDir, { recursive: true });
    for (const shot of SHOTS) {
      // A fresh query string per layout forces a full load, not just a hash change.
      await cdp.send('Page.navigate', { url: `http://127.0.0.1:${port}/showcase.html?shot=${shot.layout}#${shot.layout}` });
      await waitFor(async () => (await cdp.evaluate(
        `document.documentElement.dataset.ready === 'true' && document.documentElement.dataset.layout === '${shot.layout}'`
      )), `${shot.layout} layout`);
      await sleep(300); // let web fonts and the last paint settle
      const state = await cdp.evaluate(`({ ...document.documentElement.dataset, vw: innerWidth, vh: innerHeight })`);
      if (state.fits !== 'true') {
        throw new Error(`${shot.layout}: content does not fit the canvas (${state.overflowing} overflowing elements)`);
      }
      const { data } = await cdp.send('Page.captureScreenshot', {
        format: 'png', clip: { x: 0, y: 0, width: WIDTH, height: HEIGHT, scale: 1 },
      });
      const file = path.join(outputDir, shot.file);
      writeFileSync(file, Buffer.from(data, 'base64'));
      const size = pngSize(readFileSync(file));
      if (size.width !== WIDTH || size.height !== HEIGHT) {
        throw new Error(`${shot.file}: ${size.width}x${size.height}, expected ${WIDTH}x${HEIGHT}`);
      }
      results.push({ file, ...size, viewport: `${state.vw}x${state.vh}`, fits: state.fits });
    }
    cdp.close();
  } finally {
    chrome.kill();
    server.kill();
    await sleep(200);
    rmSync(profile, { recursive: true, force: true });
  }
  return results;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  exportShowcase().then((results) => {
    for (const r of results) console.log(`${r.width}x${r.height}  viewport ${r.viewport}  fits=${r.fits}  ${r.file}`);
  }).catch((error) => {
    console.error(`Export failed: ${error.message}`);
    process.exit(1);
  });
}
