/* Portfolio showcase: two fixed 1920 x 1080 layouts built from the same pipeline-generated
   data as the dashboard (data/dashboard_data.json). Nothing here computes new figures: it
   selects and formats values the analytics pipeline already produced.

     showcase.html#performance  — performance intelligence
     showcase.html#findings     — analytical findings -> experiment drafts

   After rendering, it records whether everything fits the canvas in data attributes that
   the export script and the tests check. */

import { el, lineChart, sparkline, legend } from './charts.js';

const CLIENT_KEY = 'examplescan-uk'; // the client whose report is the committed sample
const CURRENCY = { GBP: '£', EUR: '€', USD: '$' };
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const SERIES = ['var(--s1)', 'var(--s2)', 'var(--s3)'];
const PLATFORM = { google_ads: 'Google Ads', meta_ads: 'Meta Ads' };

const num = (value) => parseFloat(value);
const int = new Intl.NumberFormat('en-GB');
const money0 = (c) => (v) => `${CURRENCY[c]}${int.format(Math.round(v))}`;
const money2 = (c) => (v) => `${CURRENCY[c]}${v.toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const pct = (v) => `${(v * 100).toFixed(2)}%`;
const times = (v) => `${v.toFixed(2)}×`;
const count = (v) => int.format(Math.round(v));
const signed = (v) => `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`;
const clean = (name) => name.replace('[SYNTHETIC] ', '');

function fmtDay(iso) {
  const [, m, d] = iso.split('-').map(Number);
  return `${d} ${MONTHS[m - 1]}`;
}
function fmtPeriod(period) {
  const [y, m1, d1] = period.start.split('-').map(Number);
  const [, m2, d2] = period.end.split('-').map(Number);
  return m1 === m2 ? `${d1}–${d2} ${MONTHS[m1 - 1]} ${y}` : `${fmtDay(period.start)} – ${fmtDay(period.end)} ${y}`;
}
function metricFormat(metric, currency) {
  if (metric.includes('roas')) return times;
  if (metric.includes('rate') || metric === 'ctr') return pct;
  if (metric.includes('cost_per') || metric === 'cpc') return money2(currency);
  if (metric === 'spend' || metric.includes('value')) return money0(currency);
  return count;
}

const UP_GOOD = new Set(['clicks', 'ctr', 'platform_attributed_conversions']);
const UP_BAD = new Set(['cpc']);
function delta(metric, change) {
  if (!change || change.percent_change == null) return null;
  const value = num(change.percent_change);
  const up = value >= 0;
  let tone = '';
  if (UP_GOOD.has(metric)) tone = up ? 'up-good' : 'down-bad';
  else if (UP_BAD.has(metric)) tone = up ? 'up-bad' : 'down-good';
  const node = el('span', `delta ${tone}`);
  node.append(el('span', '', `${up ? '▲' : '▼'} ${Math.abs(value).toFixed(1)}%`), el('span', 'vs', ' vs prior 7 days'));
  return node;
}

function icon(d, size = 13) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  for (const [k, v] of Object.entries({ viewBox: '0 0 24 24', width: size, height: size, fill: 'none', stroke: 'currentColor', 'stroke-width': '2.4', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) svg.setAttribute(k, v);
  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', d);
  svg.append(path);
  return svg;
}
const ALERT = 'M12 3L2.5 20h19L12 3zm0 7v4m0 3.4v.1';
const INFO = 'M12 3a9 9 0 100 18 9 9 0 000-18zm0 5v.1m0 3.4V16';
const LOCK = 'M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 017 0v3';

function badge(cls, text, glyph) {
  const node = el('span', `badge ${cls}`);
  if (glyph) node.append(icon(glyph, 12));
  node.append(document.createTextNode(text));
  return node;
}
const severityBadge = (severity) =>
  severity === 'high' ? badge('sev-high', 'High', ALERT) : badge('sev-medium', 'Medium', INFO);

function card(title, subtitle) {
  const box = el('section', 'card');
  if (title) box.append(el('h3', '', title));
  if (subtitle) box.append(el('p', 'sub', subtitle));
  return box;
}

/* ---------- shared header & footer ---------- */

function header(data, client, layoutTitle, layoutSub) {
  const head = el('header', 'sc-head');
  const brand = el('div', 'sc-brand');
  const logo = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  logo.setAttribute('viewBox', '0 0 32 32');
  logo.setAttribute('width', '30');
  logo.setAttribute('height', '30');
  const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  for (const [k, v] of Object.entries({ width: 32, height: 32, rx: 8, fill: 'var(--accent)' })) rect.setAttribute(k, v);
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  for (const [k, v] of Object.entries({ d: 'M7 21l5-6 4 3 6-9', fill: 'none', stroke: '#fff', 'stroke-width': 2.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })) line.setAttribute(k, v);
  logo.append(rect, line);
  brand.append(logo, el('span', '', 'GrowthPulse AI'), el('span', 'tag', 'Synthetic demo'));
  const meta = el('div', 'sc-meta');
  const pill = (label, value, mono) => {
    const node = el('span', 'sc-pill');
    node.append(document.createTextNode(label));
    node.append(el(mono ? 'span' : 'strong', mono ? 'mono' : '', value));
    return node;
  };
  meta.append(
    pill('Client ', client.name),
    pill('Period ', fmtPeriod(client.report.period)),
    pill('Report ', `${client.report.report_id.slice(0, 12)}…`, true)
  );
  head.append(brand, el('div', 'spacer'), meta);

  const title = el('div', 'sc-title');
  const text = el('div');
  text.append(el('h1', '', layoutTitle), el('p', '', layoutSub));
  title.append(text);
  return [head, title];
}

function footer(parts) {
  const foot = el('footer', 'sc-foot');
  parts.forEach((part, index) => {
    if (index) foot.append(el('span', 'sep', '·'));
    foot.append(typeof part === 'string' ? el('span', '', part) : part);
  });
  return foot;
}

/* How a finding is stated: headline change, one-line values and evidence cells. Every value
   comes from the report; the zero-conversion rule is stated by its own evidence (the run
   of days with spend and no conversions), not by a percentage. */
function describe(finding) {
  const ev = finding.evidence ?? {};
  const money = money0(finding.currency);
  const money2c = money2(finding.currency);
  const pair = (b, c, f) => (b === undefined ? 'n/a' : `${f(num(b))} → ${f(num(c))}`);
  const periods = `${fmtPeriod(finding.baseline_period)} vs ${fmtPeriod(finding.current_period)}`;
  if (finding.rule_id === 'spend_with_zero_conversions') {
    const run = fmtPeriod({ start: ev.zero_run_start, end: ev.zero_run_end });
    return {
      change: `${ev.zero_run_days} days`,
      values: `${money2c(num(ev.zero_run_spend))} spent with 0 conversions`,
      when: run,
      cells: [
        ['Zero-conversion run', `${ev.zero_run_days} days · ${run}`],
        ['Spend in run', money2c(num(ev.zero_run_spend))],
        ['Conversions, window', pair(ev.baseline_conversions, ev.current_conversions, (v) => v.toFixed(0))],
        ['Rule', `≥${ev.threshold_days} days · ≥${money(num(ev.threshold_spend))}`],
      ],
      reference: `${ev.zero_run_days} days · ${money2c(num(ev.zero_run_spend))} unconverted`,
    };
  }
  const fmt = metricFormat(finding.metric, finding.currency);
  return {
    change: signed(num(finding.percent_change)),
    values: `${fmt(num(finding.baseline_value))} → ${fmt(num(finding.current_value))}`,
    when: periods,
    cells: [
      ['Spend', pair(ev.baseline_spend, ev.current_spend, money)],
      ['Clicks', pair(ev.baseline_clicks, ev.current_clicks, count)],
      ['Conversions', pair(ev.baseline_conversions, ev.current_conversions, (v) => v.toFixed(1))],
      ['Rule · guard', `±${ev.threshold_percent}% · ${finding.noise_guard_outcome.replaceAll('_', ' ')}`],
    ],
    reference: `baseline ${fmt(num(finding.baseline_value))} · now ${fmt(num(finding.current_value))}`,
  };
}

/* ---------- finding selection (shared by both layouts) ---------- */

/* The strongest high-severity adverse findings that have an experiment draft, ranked by
   size of change, at most one per detection rule so each leads to a different kind of
   experiment. Deterministic: ties break on finding ID. */
export function pickFindings(client, limit = 3) {
  const drafts = new Map(client.experiments.drafts.map((d) => [d.derived_from_finding, d]));
  const ranked = client.findings
    .filter((f) => f.severity === 'high' && f.direction === 'adverse' && drafts.has(f.finding_id))
    .sort((a, b) => Math.abs(num(b.percent_change)) - Math.abs(num(a.percent_change)) || a.finding_id.localeCompare(b.finding_id));
  const picked = [];
  const rules = new Set();
  for (const finding of ranked) {
    if (rules.has(finding.rule_id)) continue;
    picked.push({ finding, draft: drafts.get(finding.finding_id) });
    rules.add(finding.rule_id);
    if (picked.length === limit) break;
  }
  return picked;
}

/* ---------- layout 1: performance intelligence ---------- */

function series(rows, dates, keyOf, valueOf) {
  const buckets = new Map();
  for (const row of rows) {
    const key = keyOf(row);
    if (!buckets.has(key)) buckets.set(key, new Map());
    buckets.get(key).set(row.date, (buckets.get(key).get(row.date) ?? 0) + num(valueOf(row)));
  }
  return [...buckets.entries()].map(([id, bucket]) => ({ id, values: dates.map((d) => bucket.get(d) ?? 0) }));
}

function chartCard(title, subtitle, render) {
  const box = el('section', 'card');
  const head = el('div', 'chart-head');
  head.append(el('h3', '', title));
  box.append(head, el('p', 'sub', subtitle));
  const body = el('div');
  body.style.flex = '1';
  body.style.minHeight = '0';
  box.append(body);
  box._render = () => render(body, head);
  return box;
}

function tile(label, value, extra = []) {
  const node = el('div', 'card tile');
  node.append(el('div', 'label', label), el('div', 'value', value));
  for (const item of extra) if (item) node.append(item);
  return node;
}

function renderPerformance(canvas, data, client) {
  const portfolio = client.portfolios[0];
  const currency = portfolio.currency;
  const deltas = client.comparisons[0].portfolios.find((p) => p.currency === currency);
  const bases = client.daily.bases;
  const dates = [...new Set(client.daily.delivery.map((r) => r.date))].sort();

  canvas.append(...header(data, client, 'Performance intelligence',
    `Cross-platform delivery and platform-attributed conversions across ${client.accounts.length} ad accounts — Google Ads and Meta Ads, ${currency}`));

  const body = el('div', 'sc-body');
  const tiles = el('div', 'sc-tiles');
  const spendDaily = series(client.daily.delivery, dates, () => 's', (r) => r.spend)[0].values;
  const clickDaily = series(client.daily.delivery, dates, () => 'c', (r) => r.clicks)[0].values;
  const d = portfolio.delivery;
  const sparkHolder = (values) => { const s = el('span', 'spark'); s.append(sparkline(values, { width: 84, height: 24 })); return s; };
  tiles.append(
    tile('Spend', money0(currency)(num(d.spend)), [delta('spend', deltas.delivery.spend), sparkHolder(spendDaily)]),
    tile('Clicks', count(num(d.clicks)), [delta('clicks', deltas.delivery.clicks), sparkHolder(clickDaily)]),
    tile('Click-through rate', pct(num(d.ctr)), [delta('ctr', deltas.delivery.ctr)]),
    tile('Cost per click', money2(currency)(num(d.cpc)), [delta('cpc', deltas.delivery.cpc)]),
  );
  // One tile per conversion basis: platform, event and attribution are never mixed.
  const orderedBases = [...portfolio.conversions_by_basis].sort((a, b) =>
    a.basis.platform.localeCompare(b.basis.platform) || a.basis.stage.localeCompare(b.basis.stage));
  for (const basis of orderedBases) {
    const b = basis.basis;
    const sub = el('div', 'subrow');
    const mini = (k, v) => { const m = el('span', 'mini'); m.append(el('span', 'k', k), el('span', 'v', v)); return m; };
    sub.append(
      mini('Cost / conv.', money2(currency)(num(basis.cost_per_platform_attributed_conversion))),
      mini('ROAS', times(num(basis.platform_attributed_roas))),
      mini('Conv. rate', pct(num(basis.platform_attributed_conversion_rate)))
    );
    const attribution = b.attribution_model === 'data_driven'
      ? `data-driven · ${b.click_window_days}d click`
      : `${b.click_window_days}d click + ${b.view_window_days}d view`;
    tiles.append(tile(bases[b.basis_id].label, count(num(basis.platform_attributed_conversions)),
      [el('div', 'basis-note', `Platform-attributed · ${attribution}`), sub]));
  }
  body.append(tiles);

  const main = el('div', 'sc-main');
  const spendCard = chartCard('Daily spend', `By ad account · ${currency}`, (box, head) => {
    const s = series(client.daily.delivery, dates, (r) => r.account_id, (r) => r.spend).map((item, i) => ({
      ...item, color: SERIES[i], label: PLATFORM[client.accounts.find((a) => a.external_account_id === item.id).platform],
    }));
    head.append(legend(s, 'line'));
    lineChart(box, { dates, series: s, format: money0(currency), height: Math.round((box.clientHeight * 640) / box.clientWidth), tableLabel: 'Daily spend' });
  });
  const convCard = chartCard('Daily platform-attributed conversions', 'One line per conversion basis — never summed across bases', (box, head) => {
    const s = series(client.daily.conversions, dates, (r) => r.basis_id, (r) => r.conversions)
      .sort((a, b) => bases[a.id].label.localeCompare(bases[b.id].label))
      .map((item, i) => ({ ...item, color: SERIES[i], label: bases[item.id].label }));
    head.append(legend(s, 'line'));
    lineChart(box, { dates, series: s, format: (v) => v.toFixed(1), height: Math.round((box.clientHeight * 640) / box.clientWidth), tableLabel: 'Daily conversions' });
  });

  const findingsCard = card('Priority findings', 'Strongest high-severity change per detection rule, after the noise guard');
  for (const { finding } of pickFindings(client)) {
    const item = el('div', 'sc-finding');
    const row = el('div', 'row');
    row.append(severityBadge(finding.severity), badge('plat', PLATFORM[finding.platform]));
    const said = describe(finding);
    row.append(el('span', 'change', said.change));
    item.append(row,
      el('div', 'metric', finding.metric_label),
      el('div', 'ctx', clean(finding.campaign_name)),
      el('div', 'vals', `${said.values} · ${said.when}`));
    findingsCard.append(item);
  }
  findingsCard.classList.add('span-rows');

  const campaignCard = card('Campaign performance', `Top campaigns by spend · full period · each campaign's own conversion basis`);
  campaignCard.classList.add('span-cols');
  const campaigns = [...client.campaigns].sort((a, b) => num(b.kpis.delivery.spend) - num(a.kpis.delivery.spend)).slice(0, 6);
  const maxSpend = num(campaigns[0].kpis.delivery.spend);
  const table = el('table', 'camp');
  const headRow = el('tr');
  for (const h of ['Campaign', 'Spend', 'Clicks', 'CTR', 'CPC', 'Conversions', 'Cost / conv.', 'ROAS']) headRow.append(el('th', '', h));
  const thead = el('thead');
  thead.append(headRow);
  const tbody = el('tbody');
  for (const c of campaigns) {
    const k = c.kpis;
    const conv = k.conversions_by_basis[0];
    const tr = el('tr');
    const nameCell = el('td');
    nameCell.append(el('span', 'name', clean(c.campaign_name)), el('span', 'platform', PLATFORM[c.platform]));
    const barCell = el('td', 'barcell');
    const track = el('div', 'bartrack');
    const bar = el('div', 'bar');
    bar.style.width = `${(num(k.delivery.spend) / maxSpend) * 70}%`;
    track.append(bar, el('span', 'barval', money0(currency)(num(k.delivery.spend))));
    barCell.append(track);
    tr.append(nameCell, barCell,
      el('td', '', count(num(k.delivery.clicks))),
      el('td', '', pct(num(k.delivery.ctr))),
      el('td', '', money2(currency)(num(k.delivery.cpc))),
      el('td', '', count(num(conv.platform_attributed_conversions))),
      el('td', '', money2(currency)(num(conv.cost_per_platform_attributed_conversion))),
      el('td', '', times(num(conv.platform_attributed_roas))));
    tbody.append(tr);
  }
  table.append(thead, tbody);
  campaignCard.append(table);

  main.append(spendCard, convCard, findingsCard, campaignCard);
  body.append(main);
  canvas.append(body);
  canvas.append(footer([
    el('strong', '', 'Synthetic data'),
    'Fictional client and accounts · every figure computed by the analytics pipeline',
    'Conversions are platform-attributed, not bookings or true CAC',
    `${client.report.record_count} records`,
  ]));
  // Charts size themselves to the space the grid gave them.
  spendCard._render();
  convCard._render();
}

/* ---------- layout 2: findings -> experiments ---------- */

function evidenceCell(label, value) {
  const cell = el('div', 'ev-cell');
  cell.append(el('div', 'k', label), el('div', 'v', value));
  return cell;
}

function findingCard(finding) {
  const box = el('section', 'card');
  const said = describe(finding);
  const top = el('div', 'ev-top');
  top.append(severityBadge(finding.severity), badge('plat', PLATFORM[finding.platform]),
    el('span', 'ev-change adverse', said.change));
  const head = el('div');
  head.append(top, el('div', 'ev-metric', finding.metric_label), el('div', 'ev-camp', clean(finding.campaign_name)));
  const periods = el('div', 'ev-periods');
  periods.append(el('strong', '', said.values), document.createTextNode(`   ${said.when}`));
  head.append(periods);
  const grid = el('div', 'ev-grid');
  for (const [label, value] of said.cells) grid.append(evidenceCell(label, value));
  box.append(head, grid);
  return box;
}

function draftCard(draft, finding) {
  const box = el('section', 'card');
  const top = el('div', 'ex-top');
  top.append(badge('draft', 'Draft experiment'), badge('approval ex-status', 'Requires human approval', LOCK));
  const head = el('div');
  head.append(top, el('div', 'ex-title', draft.title.split(' — ')[0]), el('p', 'ex-hyp', draft.hypothesis));
  const meta = el('div', 'ex-meta');
  meta.append(
    evidenceCell('Success metric', `${finding.metric_label.replace(' (proxy, not true CAC)', '')}`),
    evidenceCell('Reference values', describe(finding).reference),
  );
  box.append(head, meta);
  return box;
}

function link() {
  const holder = el('div', 'flow-link');
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  for (const [k, v] of Object.entries({ viewBox: '0 0 76 40', width: 76, height: 40, 'aria-hidden': 'true' })) svg.setAttribute(k, v);
  const path = document.createElementNS(NS, 'path');
  for (const [k, v] of Object.entries({ d: 'M10 20h50m-10-9l10 9-10 9', fill: 'none', stroke: 'var(--accent)', 'stroke-width': 2.5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })) path.setAttribute(k, v);
  svg.append(path);
  holder.append(svg);
  return holder;
}

function renderFindings(canvas, data, client) {
  canvas.append(...header(data, client, 'From analytical finding to experiment design',
    'The strongest high-severity change for each of three detection rules, the evidence behind it, and the experiment drafted from it'));
  const body = el('div', 'sc-body');
  const heads = el('div', 'flow-head');
  heads.append(el('div', '', 'Evidence — what changed'), el('div', ''), el('div', '', 'Action — proposed experiment'));
  const flow = el('div', 'flow');
  for (const { finding, draft } of pickFindings(client)) {
    const row = el('div', 'flow-row');
    row.append(findingCard(finding), link(), draftCard(draft, finding));
    flow.append(row);
  }
  const disclosure = el('div', 'disclosure');
  disclosure.append(icon(INFO, 18));
  const text = el('div');
  text.append(el('strong', '', 'Deterministic experiment templates — not AI-generated. '),
    document.createTextNode('Each draft is a fixed template keyed to its finding\'s rule. Every experiment requires human approval and is applied manually; GrowthPulse never changes an ad account. Approvals in this demo are not persisted.'));
  disclosure.append(text);
  body.append(heads, flow, disclosure);
  canvas.append(body);
  canvas.append(footer([
    el('strong', '', 'Synthetic data'),
    'Fictional client and accounts · findings computed by the analytics pipeline',
    'Threshold rules with a noise guard, not significance tests · changes do not establish causes',
  ]));
}

/* ---------- boot ---------- */

function measure(canvas) {
  const overflowing = [...canvas.querySelectorAll('.card, .sc-finding, .ev-cell .v, .ex-hyp')]
    .filter((node) => node.scrollHeight > node.clientHeight + 1 || node.scrollWidth > node.clientWidth + 1);
  const root = document.documentElement;
  root.dataset.canvas = `${canvas.offsetWidth}x${canvas.offsetHeight}`;
  root.dataset.fits = String(
    root.scrollWidth <= 1920 && root.scrollHeight <= 1080
      && canvas.scrollHeight <= canvas.clientHeight && overflowing.length === 0);
  root.dataset.overflowing = String(overflowing.length);
  root.dataset.scroll = `page ${root.scrollWidth}x${root.scrollHeight} canvas ${canvas.scrollWidth}x${canvas.scrollHeight}`;
  root.dataset.overflowList = overflowing.map((node) => node.className).join(',');
}

async function main() {
  const canvas = document.getElementById('canvas');
  const data = await (await fetch('data/dashboard_data.json')).json();
  const client = data.clients.find((item) => item.key === CLIENT_KEY);
  const layout = location.hash === '#findings' ? 'findings' : 'performance';
  document.documentElement.dataset.layout = layout;
  if (layout === 'findings') renderFindings(canvas, data, client);
  else renderPerformance(canvas, data, client);
  await document.fonts.ready;
  measure(canvas);
  document.documentElement.dataset.ready = 'true';
}

main();
