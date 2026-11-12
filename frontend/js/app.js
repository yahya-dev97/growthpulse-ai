/* GrowthPulse AI dashboard.

   Data flows through one adapter (`loadDashboardData`), today a synthetic fixture built
   by frontend/tools/build_dashboard_data.py from the real analytics pipeline. A future
   API adapter returns the same shape from the Phase C backend's read-only views.
   Everything user-visible is inserted with textContent — never innerHTML. */

import { el, lineChart, barChart, sparkline, legend, hideTooltip } from './charts.js';

const DATA_URL = 'data/dashboard_data.json';

async function loadDashboardData() {
  const response = await fetch(DATA_URL);
  if (!response.ok) throw new Error(`fixture not found (${response.status})`);
  return response.json();
}

/* ---------- formatting ---------- */

const CURRENCY = { GBP: '£', EUR: '€', USD: '$' };
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const num = (value) => (value === null || value === undefined ? NaN : parseFloat(value));
const int = new Intl.NumberFormat('en-GB');
const money0 = (currency) => (value) => `${CURRENCY[currency] ?? ''}${int.format(Math.round(value))}`;
const money2 = (currency) => (value) =>
  `${CURRENCY[currency] ?? ''}${value.toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const pct = (value) => `${(value * 100).toFixed(2)}%`;
const times = (value) => `${value.toFixed(2)}×`;
const count = (value) => int.format(Math.round(value));

function day(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return { y, m, d };
}
function fmtPeriod(period) {
  const a = day(period.start);
  const b = day(period.end);
  if (a.m === b.m && a.y === b.y) return `${a.d}–${b.d} ${MONTHS[a.m - 1]} ${a.y}`;
  return `${a.d} ${MONTHS[a.m - 1]} – ${b.d} ${MONTHS[b.m - 1]} ${b.y}`;
}
function fmtAsOf(iso) {
  const [date, time] = iso.split('T');
  const { y, m, d } = day(date);
  return `${d} ${MONTHS[m - 1]} ${y}, ${time.slice(0, 5)} UTC`;
}

/* Which direction is good, per metric. Spend is neutral. */
const UP_IS_GOOD = new Set([
  'clicks', 'impressions', 'ctr', 'platform_attributed_conversions',
  'platform_attributed_conversion_value', 'platform_attributed_conversion_rate',
  'platform_attributed_roas',
]);
const UP_IS_BAD = new Set(['cpc', 'cost_per_platform_attributed_conversion']);

function metricFormat(metric, currency) {
  if (metric.includes('roas')) return times;
  if (metric.includes('rate') || metric === 'ctr') return pct;
  if (metric.includes('cost_per') || metric === 'cpc') return money2(currency);
  if (metric === 'spend' || metric.includes('value')) return money0(currency);
  return count;
}

function deltaBadge(metric, change, label = 'vs prev 7 days') {
  if (!change || change.percent_change === null || change.percent_change === undefined) return null;
  const value = num(change.percent_change);
  const up = value >= 0;
  let tone = '';
  if (UP_IS_GOOD.has(metric)) tone = up ? 'up-good' : 'down-bad';
  else if (UP_IS_BAD.has(metric)) tone = up ? 'up-bad' : 'down-good';
  const badge = el('span', `delta ${tone}`);
  badge.append(
    el('span', '', `${up ? '▲' : '▼'} ${Math.abs(value).toFixed(1)}%`),
    el('span', 'vs', ` ${label}`)
  );
  return badge;
}

/* ---------- icons & small components ---------- */

function icon(path, size = 12) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', size);
  svg.setAttribute('height', size);
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2.4');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', path);
  svg.append(p);
  return svg;
}
const ICONS = {
  alert: 'M12 3L2.5 20h19L12 3zm0 7v4m0 3.4v.1',
  dot: 'M12 5a7 7 0 100 14 7 7 0 000-14zm0 4.6v.1m0 3v2.8',
  info: 'M12 3a9 9 0 100 18 9 9 0 000-18zm0 5v.1m0 3.4V16',
  up: 'M4 17l6-7 4 4 6-8m0 0h-5m5 0v5',
};

function severityBadge(severity, direction) {
  if (direction === 'positive') {
    const badge = el('span', 'badge positive');
    badge.append(icon(ICONS.up), document.createTextNode('Positive'));
    badge.title = 'A favourable change that cleared the noise guard';
    return badge;
  }
  const map = { high: ['sev-high', ICONS.alert, 'High'], medium: ['sev-medium', ICONS.dot, 'Medium'], info: ['sev-info', ICONS.info, 'Info'] };
  const [cls, glyph, label] = map[severity] ?? map.info;
  const badge = el('span', `badge ${cls}`);
  badge.append(icon(glyph), document.createTextNode(label));
  return badge;
}

function platformBadge(platformLabel) {
  return el('span', 'badge plat', platformLabel);
}

function card(title, subtitle) {
  const box = el('section', 'card');
  if (title) box.append(el('h3', '', title));
  if (subtitle) box.append(el('p', 'sub', subtitle));
  return box;
}

function dataTable(head, rows, options = {}) {
  const table = el('table', 'data');
  const thead = el('thead');
  const headRow = el('tr');
  for (const cell of head) headRow.append(el('th', '', cell));
  thead.append(headRow);
  const tbody = el('tbody');
  for (const cells of rows) {
    const tr = el('tr');
    cells.forEach((cell, index) => {
      const td = el('td');
      if (cell instanceof Node) td.append(cell);
      else td.textContent = cell;
      if (options.mono?.includes(index)) td.classList.add('mono');
      tr.append(td);
    });
    tbody.append(tr);
  }
  table.append(thead, tbody);
  const wrap = el('div', 'table-wrap');
  wrap.append(table);
  return wrap;
}

/* ---------- state ---------- */

const state = {
  data: null,
  clientKey: null,
  accountId: 'all',
  tab: 'overview',
  severity: 'all',
  workflow: {}, // clientKey -> {draftId: 'draft'|'proposed'|'approved'|'rejected'} (demo only)
};

const client = () => state.data.clients.find((item) => item.key === state.clientKey);
const scopeAccounts = () => {
  const all = client().accounts;
  return state.accountId === 'all' ? all : all.filter((a) => a.external_account_id === state.accountId);
};

/* ---------- overview ---------- */

function dailyFor(accounts) {
  const ids = new Set(accounts.map((account) => account.external_account_id));
  const daily = client().daily;
  return {
    delivery: daily.delivery.filter((row) => ids.has(row.account_id)),
    conversions: daily.conversions.filter((row) => ids.has(row.account_id)),
    bases: daily.bases,
  };
}

function seriesByKey(rows, dates, key, value) {
  const buckets = new Map();
  for (const row of rows) {
    const id = key(row);
    if (!buckets.has(id)) buckets.set(id, new Map());
    const bucket = buckets.get(id);
    bucket.set(row.date, (bucket.get(row.date) ?? 0) + num(value(row)));
  }
  return [...buckets.entries()].map(([id, bucket]) => ({
    id,
    values: dates.map((date) => bucket.get(date) ?? 0),
  }));
}

const SERIES_COLORS = ['var(--s1)', 'var(--s2)', 'var(--s3)', 'var(--s4)', 'var(--s5)'];

function chartCard(title, subtitle, build) {
  const box = card(title, subtitle);
  const head = box.querySelector('h3');
  const headRow = el('div', 'chart-head');
  box.insertBefore(headRow, head);
  headRow.append(head);
  const body = el('div');
  box.append(body);
  build(body, headRow);
  return box;
}

function statTile(label, value, { delta, spark, hero, subrow } = {}) {
  const tile = el('div', 'card tile' + (hero ? ' hero' : ''));
  tile.append(el('div', 'label', label));
  tile.append(el('div', 'value', value));
  if (delta) tile.append(delta);
  if (spark) {
    const holder = el('span', 'spark');
    holder.append(spark);
    tile.append(holder);
  }
  if (subrow) tile.append(subrow);
  return tile;
}

function portfolioDeltas(currency) {
  const comparison = client().comparisons[0];
  return comparison?.portfolios.find((item) => item.currency === currency) ?? null;
}

function renderPortfolioGroup(container, portfolio, groupAccounts, multiCurrency) {
  const currency = portfolio.currency;
  const scoped = state.accountId !== 'all';
  const breakdownAccount = scoped
    ? client().breakdown.accounts.find((item) => item.account_id === state.accountId)
    : null;
  const kpis = breakdownAccount ? breakdownAccount.kpis : portfolio;
  const deltas = scoped ? null : portfolioDeltas(currency);
  const daily = dailyFor(groupAccounts);
  const dates = [...new Set(daily.delivery.map((row) => row.date))].sort();

  if (multiCurrency) {
    const head = el('div', 'section-head');
    head.append(el('h2', '', `${currency} portfolio`));
    head.append(el('span', 'meta',
      `${groupAccounts.length} account${groupAccounts.length === 1 ? '' : 's'} · amounts in ${currency}; portfolios are never summed across currencies`));
    container.append(head);
  }

  const spendDaily = seriesByKey(daily.delivery, dates, () => 'spend', (row) => row.spend)[0]?.values ?? [];
  const clicksDaily = seriesByKey(daily.delivery, dates, () => 'clicks', (row) => row.clicks)[0]?.values ?? [];

  const tiles = el('div', 'grid tiles');
  const delivery = kpis.delivery;
  tiles.append(statTile('Spend', money0(currency)(num(delivery.spend)), {
    hero: !multiCurrency,
    delta: deltas ? deltaBadge('spend', deltas.delivery.spend) : scopedNote(),
    spark: sparkline(spendDaily),
  }));
  tiles.append(statTile('Clicks', count(num(delivery.clicks)), {
    delta: deltas ? deltaBadge('clicks', deltas.delivery.clicks) : null,
    spark: sparkline(clicksDaily),
  }));
  tiles.append(statTile('Click-through rate', pct(num(delivery.ctr)), {
    delta: deltas ? deltaBadge('ctr', deltas.delivery.ctr) : null,
  }));
  tiles.append(statTile('Cost per click', money2(currency)(num(delivery.cpc)), {
    delta: deltas ? deltaBadge('cpc', deltas.delivery.cpc) : null,
  }));
  container.append(tiles);

  const basisTiles = el('div', 'grid tiles');
  for (const basis of kpis.conversions_by_basis) {
    const basisId = basis.basis.basis_id;
    const label = daily.bases[basisId]?.label ?? basis.basis.event_name;
    const basisDeltas = deltas?.bases[basisId];
    const subrow = el('div', 'subrow');
    const mini = (k, v) => {
      const box = el('span', 'mini');
      box.append(el('span', 'k', k), el('span', 'v', v));
      return box;
    };
    subrow.append(
      mini('Cost per conversion', money2(currency)(num(basis.cost_per_platform_attributed_conversion))),
      mini('ROAS', times(num(basis.platform_attributed_roas))),
      mini('Conv. rate', pct(num(basis.platform_attributed_conversion_rate)))
    );
    basisTiles.append(statTile(`${label} — platform-attributed`, count(num(basis.platform_attributed_conversions)), {
      delta: basisDeltas ? deltaBadge('platform_attributed_conversions', basisDeltas.platform_attributed_conversions) : null,
      subrow,
    }));
  }
  container.append(basisTiles);

  const charts = el('div', 'grid charts');
  charts.append(chartCard('Daily spend', `By account · ${currency}`, (body, head) => {
    const series = seriesByKey(daily.delivery, dates, (row) => row.account_id, (row) => row.spend)
      .map((item, index) => ({
        ...item,
        label: groupAccounts.find((a) => a.external_account_id === item.id)?.platform_label ?? item.id,
        color: SERIES_COLORS[index % SERIES_COLORS.length],
      }));
    if (series.length > 1) head.append(legend(series, 'line'));
    lineChart(body, { dates, series, format: money0(currency), tableLabel: `Daily spend (${currency})` });
  }));
  charts.append(chartCard('Daily conversions', 'By conversion basis — bases are never summed together', (body, head) => {
    const series = seriesByKey(daily.conversions, dates, (row) => row.basis_id, (row) => row.conversions)
      .map((item, index) => ({
        ...item,
        label: daily.bases[item.id]?.label ?? item.id,
        color: SERIES_COLORS[index % SERIES_COLORS.length],
      }));
    if (series.length > 1) head.append(legend(series, 'line'));
    lineChart(body, { dates, series, format: (value) => value.toFixed(1), tableLabel: 'Daily platform-attributed conversions' });
  }));
  container.append(charts);

  const campaignRows = client().campaigns
    .filter((item) => groupAccounts.some((a) => a.external_account_id === item.account_id))
    .map((item) => ({
      label: item.campaign_name.replace('[SYNTHETIC] ', ''),
      value: num(item.kpis.delivery.spend),
      campaign: item,
    }))
    .sort((a, b) => b.value - a.value);
  const shown = campaignRows.slice(0, 8);
  const rest = campaignRows.slice(8);
  if (rest.length) {
    shown.push({
      label: `Other (${rest.length} campaigns)`,
      value: rest.reduce((sum, row) => sum + row.value, 0),
      campaign: null,
    });
  }
  container.append(chartCard('Campaign spend', `Full period · ${currency}`, (body) => {
    barChart(body, {
      rows: shown,
      format: money0(currency),
      tooltipRows: (row) => row.campaign
        ? [
            { name: 'Spend', color: 'var(--s1)', value: money0(currency)(num(row.campaign.kpis.delivery.spend)) },
            { name: 'Clicks', color: 'var(--axis)', value: count(num(row.campaign.kpis.delivery.clicks)) },
            { name: 'CPC', color: 'var(--axis)', value: money2(currency)(num(row.campaign.kpis.delivery.cpc)) },
            { name: 'CTR', color: 'var(--axis)', value: pct(num(row.campaign.kpis.delivery.ctr)) },
          ]
        : [{ name: 'Combined spend', color: 'var(--s1)', value: money0(currency)(row.value) }],
    });
  }));
}

function scopedNote() {
  const note = el('span', 'delta');
  note.append(el('span', 'vs', 'period deltas shown at client scope'));
  return note;
}

function renderOverview(main) {
  const data = client();
  const accounts = scopeAccounts();
  const currencies = [...new Set(accounts.map((account) => account.currency))];
  const portfolios = data.portfolios.filter((portfolio) => currencies.includes(portfolio.currency));
  const multiCurrency = data.portfolios.length > 1;

  for (const portfolio of portfolios) {
    const groupAccounts = accounts.filter((account) => account.currency === portfolio.currency);
    renderPortfolioGroup(main, portfolio, groupAccounts, multiCurrency);
  }

  const bottom = el('div', 'grid split');
  const breakdownCard = card('Account breakdown', 'Full period, each account in its own currency');
  breakdownCard.append(dataTable(
    ['Account', 'Platform', 'Currency', 'Spend', 'Clicks', 'CTR', 'CPC', 'Campaigns'],
    data.breakdown.accounts.map((account) => {
      const meta = data.accounts.find((item) => item.external_account_id === account.account_id);
      const currency = account.currency;
      return [
        meta?.display_name ?? account.account_id,
        platformBadge(meta?.platform_label ?? account.platform),
        currency,
        money0(currency)(num(account.kpis.delivery.spend)),
        count(num(account.kpis.delivery.clicks)),
        pct(num(account.kpis.delivery.ctr)),
        money2(currency)(num(account.kpis.delivery.cpc)),
        String(account.campaign_count),
      ];
    })
  ));
  const compat = data.breakdown.compatibility;
  const flags = el('div', 'compat');
  const flag = (condition, label) => {
    if (!condition) return;
    const chip = el('span', 'badge neutral');
    chip.append(icon(ICONS.info), document.createTextNode(label));
    flags.append(chip);
  };
  flag(compat.mixed_currencies, 'Mixed currencies — never summed');
  flag(compat.mixed_timezones, 'Mixed timezones');
  flag(compat.multiple_conversion_bases, 'Multiple conversion bases — compared only within a basis');
  flag(compat.mixed_attribution_settings, 'Mixed attribution settings');
  if (flags.children.length) breakdownCard.append(flags);

  const findingsCard = card('Latest findings', 'Strongest first — full list under AI Insights');
  const top = [...filteredFindings('all')].slice(0, 3);
  if (!top.length) findingsCard.append(el('div', 'empty', 'No findings in scope.'));
  for (const finding of top) findingsCard.append(findingRow(finding, { compact: true }));
  const goButton = el('button', '', 'Open AI Insights & Experiments →');
  goButton.style.marginTop = '12px';
  goButton.addEventListener('click', () => setTab('insights'));
  const linkRow = el('div', 'filter-row');
  linkRow.style.marginTop = '12px';
  linkRow.append(goButton);
  findingsCard.append(linkRow);

  bottom.append(breakdownCard, findingsCard);
  main.append(bottom);
}

/* ---------- insights & experiments ---------- */

const SEVERITY_ORDER = { high: 0, medium: 1, info: 2 };

function filteredFindings(severity) {
  const ids = new Set(scopeAccounts().map((account) => account.external_account_id));
  return client().findings
    .filter((finding) => ids.has(finding.account_id))
    .filter((finding) => severity === 'all' || finding.severity === severity)
    .sort((a, b) =>
      SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]
      || Math.abs(num(b.percent_change) || 0) - Math.abs(num(a.percent_change) || 0));
}

function findingRow(finding, { compact = false } = {}) {
  const box = el('div', 'finding');
  const top = el('div', 'frow');
  top.append(severityBadge(finding.severity, finding.direction));
  top.append(platformBadge(finding.platform === 'google_ads' ? 'Google Ads' : 'Meta Ads'));
  top.append(el('span', 'fmetric', finding.metric_label));
  const change = num(finding.percent_change);
  if (!Number.isNaN(change)) {
    top.append(el('span', `fchange ${finding.direction}`, `${change >= 0 ? '+' : ''}${change.toFixed(1)}%`));
  }
  box.append(top);

  const format = metricFormat(finding.metric, finding.currency);
  const baseline = num(finding.baseline_value);
  const current = num(finding.current_value);
  const values = el('div', 'fvals',
    `${finding.campaign_name.replace('[SYNTHETIC] ', '')} · ${format(baseline)} → ${format(current)}`
    + ` · ${fmtPeriod(finding.baseline_period)} vs ${fmtPeriod(finding.current_period)}`);
  box.append(values);
  if (!compact) {
    box.append(el('p', 'fobs', finding.observation));
    box.append(el('div', 'ffoot',
      `Noise guard: ${finding.noise_guard_outcome.replaceAll('_', ' ')} · ${finding.comparison_purpose === 'recent_change' ? 'recent change' : 'sustained change'} comparison · platform-attributed, not incrementality`));
  }
  return box;
}

function workflowState() {
  if (!state.workflow[state.clientKey]) {
    state.workflow[state.clientKey] = Object.fromEntries(
      client().experiments.drafts.map((draft) => [draft.draft_id, 'draft'])
    );
  }
  return state.workflow[state.clientKey];
}

function experimentCard(draft, status, actions) {
  const box = el('div', 'exp-card');
  box.append(el('div', 'et', draft.title));
  box.append(el('p', 'eh', draft.hypothesis));
  const row = el('div', 'erow');
  row.append(severityBadge(draft.severity, draft.direction));
  row.append(el('span', 'emetric', `Primary metric: ${draft.primary_metric.replaceAll('_', ' ')}`));
  box.append(row);
  const foot = el('div', 'erow');
  foot.style.marginTop = '6px';
  const guard = el('span', 'badge neutral');
  guard.append(icon(ICONS.info), document.createTextNode('Requires human approval — never auto-applied'));
  foot.append(guard);
  box.append(foot);
  if (actions.length) {
    const bar = el('div', 'exp-actions');
    for (const [label, next, primary] of actions) {
      const button = el('button', primary ? 'primary' : '', label);
      button.addEventListener('click', () => {
        workflowState()[draft.draft_id] = next;
        render();
      });
      bar.append(button);
    }
    box.append(bar);
  }
  return box;
}

/* AI insights: a pre-generated, validated static artifact or the deterministic fallback.
   The browser never calls the Anthropic API; the provenance line says which source this is. */

function citationChips(citations) {
  const row = el('div', 'ai-cites');
  for (const c of citations) {
    const chip = el('span', 'ai-cite', `${c.metric_label} · ${c.campaign_name.replace('[SYNTHETIC] ', '')}`);
    chip.title = `Finding ${c.finding_id}`;
    row.append(chip);
  }
  return row;
}

function aiStatement(item) {
  const box = el('div', 'ai-item');
  box.append(el('p', '', item.statement), citationChips(item.citations));
  return box;
}

function fieldList(pairs) {
  const list = el('dl', 'ai-fields');
  for (const [key, value] of pairs) list.append(el('dt', '', key), el('dd', '', value));
  return list;
}

const EXPERIMENT_TYPES = {
  diagnostic_investigation: 'Diagnostic investigation',
  measurement_repair: 'Measurement repair',
  monitoring: 'Monitoring',
  randomized_experiment: 'Randomized experiment',
};
const METRIC_LABELS = {
  spend: 'Spend', impressions: 'Impressions', clicks: 'Clicks', ctr: 'CTR', cpc: 'CPC',
  platform_attributed_conversions: 'Platform-attributed conversions',
  platform_attributed_conversion_rate: 'Platform-attributed conversion rate',
  cost_per_platform_attributed_conversion: 'Cost per platform-attributed conversion',
  platform_attributed_conversion_value: 'Platform-attributed conversion value',
  platform_attributed_roas: 'Platform-attributed ROAS',
};

function referenceText(ref) {
  return `${metricFormat(ref.metric, ref.currency)(num(ref.value))} · ${fmtPeriod(ref.period)}`;
}

function feasibilityBlock(f) {
  const box = el('div', 'ai-feas');
  box.append(el('div', 'ai-feas-head', 'Feasibility · GrowthPulse assessment from the evidence, not AI output'));
  box.append(el('p', '', f.summary));
  const extra = [...f.caveats, ...f.assumptions];
  if (f.volume?.clicks !== undefined && f.volume.clicks !== null) {
    extra.unshift(`Volume in the current ${f.volume.window_days}-day window: ${count(num(f.volume.clicks))} clicks, ${f.volume.platform_attributed_conversions} platform-attributed conversions.`);
  }
  if (extra.length) {
    const details = el('details', 'ai-feas-more');
    details.append(el('summary', '', 'Volume, assumptions and caveats'));
    const list = el('ul', 'limit-list');
    for (const line of extra) list.append(el('li', '', line));
    details.append(list);
    box.append(details);
  }
  return box;
}

function aiExperiment(exp) {
  const box = el('div', 'ai-item');
  box.append(el('div', 'ai-title', exp.title));
  const tags = el('div', 'erow');
  tags.append(el('span', `badge ${exp.legacy ? 'draft' : 'neutral'}`,
    exp.legacy ? 'Legacy proposal · type not stated' : EXPERIMENT_TYPES[exp.experiment_type]));
  if (exp.feasibility.interpretation_blocked_until_measurement_repair) {
    const blocked = el('span', 'badge blocked');
    blocked.append(icon(ICONS.alert), document.createTextNode('Blocked until tracking repair is verified'));
    tags.append(blocked);
  }
  tags.style.marginBottom = '6px';
  box.append(tags);
  if (exp.legacy) box.append(el('p', 'ai-legacy', exp.legacy_note));
  box.append(el('p', '', exp.problem_statement));
  box.append(el('p', '', `Hypothesis (untested): ${exp.hypothesis}`));
  box.append(el('p', '', `Proposed: ${exp.proposed_experiment}`));
  const a = exp.attribution;
  const windows = [a.click_window_days && `${a.click_window_days}-day click`, a.view_window_days && `${a.view_window_days}-day view`]
    .filter(Boolean).join(', ');
  const refs = exp.reference_values;
  const notStated = 'Not stated (legacy proposal)';
  const rows = [
    ['Target metric', exp.target_metric_label],
    ['Current baseline', referenceText(refs.current)],
  ];
  if (refs.comparison) rows.push([refs.comparison.label, `${referenceText(refs.comparison)} · ${refs.comparison.note}`]);
  rows.push(
    ['Attribution', `${a.event_name} · ${a.attribution_model.replaceAll('_', ' ')}${windows ? ` · ${windows}` : ''}`],
    ['Measurement', exp.measurement_method],
    ['Success criteria', exp.success_criteria ?? notStated],
    ['Guardrails', exp.guardrail_metrics.length ? exp.guardrail_metrics.map((m) => METRIC_LABELS[m] ?? m).join(', ') : notStated],
    ['Duration', `${exp.duration_days} days`],
    ['Risks', exp.risks.join(' ')],
    ['Assumptions', exp.assumptions.join(' ')],
  );
  box.append(fieldList(rows));
  box.append(feasibilityBlock(exp.feasibility));
  box.append(citationChips(exp.citations));
  const foot = el('div', 'erow');
  foot.style.marginTop = '8px';
  const guard = el('span', 'badge neutral');
  guard.append(icon(ICONS.info), document.createTextNode('Proposal · requires human approval · not executed'));
  foot.append(guard);
  box.append(foot);
  return box;
}

function renderAiInsights(main) {
  const ai = client().ai_insights;
  const meta = state.data.meta.ai;
  const p = ai.provenance;
  const panel = card(
    p.is_ai_output ? 'AI-generated advisory insights' : 'Advisory insights (deterministic fallback)',
    'Facts, untested hypotheses and unexecuted experiment proposals, each citing the deterministic findings it rests on'
  );
  panel.classList.add('ai-panel');

  const prov = el('div', 'ai-prov');
  const source = el('span', `badge ${p.is_ai_output ? 'ai-source' : 'ai-fallback'}`);
  source.append(icon(ICONS.info), document.createTextNode(p.source_label));
  prov.append(source);
  const checks = p.validation_checks.length;
  prov.append(el('span', 'badge neutral', p.validated
    ? `Validated · ${checks} evidence checks passed · response schema ${p.response_schema_version}`
    : `Fallback · passes the same ${checks} checks`));
  if (p.generated_at) prov.append(el('span', 'mono', `generated ${fmtAsOf(p.generated_at)} · ${p.model}`));
  prov.append(el('span', 'mono', `evidence ${p.package_id.slice(0, 12)}… · report ${p.report_id.slice(0, 8)}…`));
  panel.append(prov);

  const banner = el('div', 'demo-banner');
  banner.append(icon(ICONS.info, 15));
  const text = el('div');
  text.append(el('strong', '', 'Static, not live inference. '));
  const sampleNote = p.is_ai_output ? ' This is a previously generated sample on synthetic data.' : '';
  text.append(document.createTextNode(`${p.static_note}${sampleNote}${p.fallback_reason ? ` Showing the fallback because ${p.fallback_reason}.` : ''}`));
  banner.append(text);
  panel.append(banner);

  const cols = el('div', 'ai-cols');
  const column = (title, note, items, render) => {
    const col = el('div', 'ai-col');
    col.append(el('h4', '', title), el('div', 'kind-note', note));
    if (!items.length) col.append(el('div', 'note', 'None for this report.'));
    for (const item of items) col.append(render(item));
    return col;
  };
  cols.append(
    column('Observed facts', p.is_ai_output ? 'AI-written, each checked against the cited evidence. No causes.' : 'Templated from the evidence. No causes.', ai.observed_facts, aiStatement),
    column('Hypotheses', 'Possible explanations, untested.', ai.hypotheses, aiStatement),
    column('Experiment proposals', 'Advisory only, never executed. Nothing is applied to any account.', ai.experiments, aiExperiment),
  );
  panel.append(cols);

  const limits = el('details', 'table-view');
  limits.append(el('summary', '', 'Attribution limits and caveats'));
  const list = el('ul', 'limit-list');
  for (const line of [...ai.limitations.attribution, ...ai.limitations.model_noted]) list.append(el('li', '', line));
  limits.append(list);
  limits.style.marginTop = '10px';
  panel.append(limits);

  const spend = el('div', 'ai-spend');
  const item = (label, value) => {
    const span = el('span');
    span.append(document.createTextNode(`${label} `), el('strong', '', value));
    return span;
  };
  spend.append(item('Cost controls:', `development spend capped at $${meta.budget_usd}`));
  spend.append(p.estimated_cost_usd
    ? item('This sample:', `about $${num(p.estimated_cost_usd).toFixed(2)} to generate (estimate)`)
    : item('This content:', 'no API call (deterministic)'));
  spend.append(el('span', '', 'No live advertising connection · approvals are not saved · nothing is executed'));
  panel.append(spend);
  main.append(panel);
}

function renderInsights(main) {
  renderAiInsights(main);
  const grid = el('div', 'grid split');

  const findingsCard = card('Analytical findings', 'Deterministic rule-based detections from the synthetic reporting period, after the noise guard');
  const filters = el('div', 'filter-row');
  const counts = { all: filteredFindings('all').length };
  for (const severity of ['high', 'medium', 'info']) counts[severity] = filteredFindings(severity).length;
  for (const [key, label] of [['all', 'All'], ['high', 'High'], ['medium', 'Medium'], ['info', 'Info']]) {
    const button = el('button', '', `${label} (${counts[key]})`);
    button.setAttribute('aria-pressed', String(state.severity === key));
    button.addEventListener('click', () => { state.severity = key; render(); });
    filters.append(button);
  }
  filters.style.marginBottom = '14px';
  findingsCard.append(filters);

  const findings = filteredFindings(state.severity);
  if (!findings.length) findingsCard.append(el('div', 'empty', 'No findings match this filter.'));
  for (const finding of findings) findingsCard.append(findingRow(finding));

  const suppressed = client().suppressed_findings;
  if (suppressed.length) {
    const details = el('details', 'table-view');
    details.append(el('summary', '', `${suppressed.length} findings suppressed by the noise guard`));
    const wrap = el('div', 'table-wrap');
    wrap.append(dataTable(
      ['Metric', 'Campaign', 'Pre-guard severity', 'Guard outcome', 'Comparison'],
      suppressed.map((item) => [
        item.metric_label,
        item.campaign_name.replace('[SYNTHETIC] ', ''),
        item.pre_guard_severity,
        item.noise_guard_outcome,
        item.comparison_label.replaceAll('_', ' '),
      ])
    ));
    details.append(wrap);
    details.style.marginTop = '16px';
    findingsCard.append(details);
  }

  const workflowCard = card('Experiment workflow');
  const banner = el('div', 'demo-banner');
  banner.append(icon(ICONS.info, 15));
  const bannerText = el('div');
  bannerText.append(el('strong', '', 'Demonstration only. '));
  bannerText.append(document.createTextNode(client().experiments.note));
  banner.append(bannerText);
  workflowCard.append(banner);

  const flow = workflowState();
  const byStatus = (status) => client().experiments.drafts.filter((draft) => flow[draft.draft_id] === status);
  const columns = [
    ['draft', 'Drafts from findings', (draft) => [['Promote to proposal', 'proposed', true]]],
    ['proposed', 'Proposed', () => [['Approve', 'approved', true], ['Reject', 'rejected', false]]],
    ['approved', 'Approved — awaiting human application', () => []],
  ];
  for (const [status, title, actions] of columns) {
    const column = el('div', 'wf-col');
    const head = el('h4');
    head.append(document.createTextNode(title), el('span', 'count', String(byStatus(status).length)));
    column.append(head);
    const items = byStatus(status);
    if (!items.length) column.append(el('div', 'note', status === 'draft' ? 'All drafts have moved on.' : 'Nothing here yet.'));
    for (const draft of items) column.append(experimentCard(draft, status, actions(draft)));
    workflowCard.append(column);
  }
  const rejected = byStatus('rejected').length;
  const footer = el('div', 'note',
    `${rejected ? `${rejected} rejected. ` : ''}Workflow state is local to this page and resets on reload.`);
  const reset = el('button', '', 'Reset workflow');
  reset.className = '';
  reset.style.cssText = 'margin-left:8px;border:0;background:none;color:var(--accent);cursor:pointer;font-size:12px;padding:0';
  reset.addEventListener('click', () => { delete state.workflow[state.clientKey]; render(); });
  footer.append(reset);
  workflowCard.append(footer);

  grid.append(findingsCard, workflowCard);
  main.append(grid);
}

/* ---------- accounts & sync ---------- */

function renderAccounts(main) {
  const data = client();
  const sync = data.sync;

  const banner = el('div', 'demo-banner');
  banner.append(icon(ICONS.info, 15));
  const text = el('div');
  text.append(el('strong', '', 'Illustrative. '));
  text.append(document.createTextNode(sync.note));
  banner.append(text);
  main.append(banner);

  const grid = el('div', 'grid wide');
  const accountsCard = card('Advertising accounts', 'One client per account for life — accounts are never reassigned');
  accountsCard.append(dataTable(
    ['Account', 'Platform', 'External ID', 'Currency', 'Timezone', 'Status'],
    data.accounts.map((account) => {
      const status = el('span', 'status-chip');
      const dot = el('span', 'dot');
      dot.style.background = 'var(--good)';
      status.append(dot, document.createTextNode('Active'));
      return [
        account.display_name,
        platformBadge(account.platform_label),
        account.external_account_id,
        account.currency,
        account.timezone,
        status,
      ];
    }),
    { mono: [2] }
  ));
  grid.append(accountsCard);

  const syncCard = card('Sync status', 'Per-account watermark, schedule and failure state');
  syncCard.append(dataTable(
    ['Account', 'Frequency', 'Backfill start', 'Synced through', 'Consecutive failures', 'Sync status'],
    sync.accounts.map((account) => {
      const status = el('span', 'status-chip');
      const dot = el('span', 'dot');
      dot.style.background = account.sync_status === 'idle' ? 'var(--good)' : 'var(--warning)';
      status.append(dot, document.createTextNode(account.sync_status));
      return [
        account.display_name,
        account.frequency,
        fmtPeriod({ start: account.backfill_start, end: account.backfill_start }),
        fmtPeriod({ start: account.synced_through, end: account.synced_through }),
        String(account.consecutive_failures),
        status,
      ];
    })
  ));
  grid.append(syncCard);
  main.append(grid);
}

/* ---------- reports ---------- */

function copyButton(value) {
  const button = el('button', 'copy-btn', '⧉');
  button.title = 'Copy full ID';
  button.setAttribute('aria-label', 'Copy full ID');
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(value);
      button.textContent = '✓';
      setTimeout(() => { button.textContent = '⧉'; }, 900);
    } catch {
      /* clipboard unavailable — the full ID is in the title attribute */
    }
  });
  return button;
}

function idCell(id) {
  const cell = el('span', 'mono', `${id.slice(0, 12)}…`);
  cell.title = id;
  const holder = el('span');
  holder.append(cell, copyButton(id));
  return holder;
}

function renderReports(main) {
  const data = client();
  const grid = el('div', 'grid wide');

  const history = card('Report history', 'Reports are append-only; the ID is the SHA-256 of the report body');
  history.append(dataTable(
    ['Report ID', 'Data as of', 'Period', 'Records', 'Findings', 'Suppressed', 'Breakdown', 'Schema'],
    data.history.map((item) => [
      idCell(item.report_id),
      fmtAsOf(item.data_as_of),
      fmtPeriod(item.period),
      int.format(item.record_count),
      String(item.findings_active),
      String(item.findings_suppressed),
      idCell(item.breakdown_id),
      item.schema_version,
    ])
  ));
  grid.append(history);

  const split = el('div', 'grid charts');
  const quality = card('Data quality', 'Validation summary for the loaded reporting period');
  const summary = data.report.data_quality;
  quality.append(dataTable(
    ['Total rows', 'Valid records', 'Quarantined', 'Warnings'],
    [[int.format(summary.total_rows), int.format(summary.valid_records), int.format(summary.quarantined_rows), int.format(summary.warnings)]]
  ));
  const warnings = Object.entries(summary.warnings_by_code ?? {});
  if (warnings.length) {
    const list = el('ul', 'limit-list');
    for (const [code, value] of warnings) list.append(el('li', '', `${code.replaceAll('_', ' ')}: ${value} (planted in the synthetic dataset)`));
    quality.append(list);
  }
  split.append(quality);

  const limits = card('Stated limitations', 'Carried on every report the pipeline produces');
  const list = el('ul', 'limit-list');
  for (const limitation of data.report.limitations) list.append(el('li', '', limitation));
  limits.append(list);
  split.append(limits);
  grid.append(split);
  main.append(grid);
}

/* ---------- shell ---------- */

const TABS = [
  ['overview', 'Performance Overview'],
  ['insights', 'AI Insights & Experiments'],
  ['accounts', 'Accounts & Sync'],
  ['reports', 'Reports'],
];

function setTab(tab) {
  state.tab = tab;
  if (location.hash !== `#${tab}`) history.replaceState(null, '', `#${tab}`);
  render();
}

function tabFromHash() {
  const key = location.hash.slice(1);
  return TABS.some(([tab]) => tab === key) ? key : 'overview';
}

function renderChrome() {
  const data = client();
  document.getElementById('period-chip').textContent =
    `${fmtPeriod(data.report.period)} · data as of ${fmtAsOf(data.report.data_as_of)}`;

  const clientSelect = document.getElementById('client-select');
  clientSelect.replaceChildren();
  for (const item of state.data.clients) {
    const option = el('option', '', item.name);
    option.value = item.key;
    clientSelect.append(option);
  }
  clientSelect.value = state.clientKey;

  const tabs = document.getElementById('tabs');
  tabs.replaceChildren();
  for (const [key, label] of TABS) {
    const button = el('button', '', label);
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-selected', String(state.tab === key));
    button.addEventListener('click', () => setTab(key));
    tabs.append(button);
  }

  const accountSelect = document.getElementById('account-select');
  accountSelect.replaceChildren();
  const all = el('option', '', `All accounts (${data.accounts.length})`);
  all.value = 'all';
  accountSelect.append(all);
  for (const account of data.accounts) {
    const option = el('option', '', `${account.display_name} — ${account.currency}`);
    option.value = account.external_account_id;
    accountSelect.append(option);
  }
  accountSelect.value = state.accountId;

  const scopeNote = document.getElementById('scope-note');
  if (state.accountId === 'all') {
    const currencies = [...new Set(data.accounts.map((account) => account.currency))];
    scopeNote.textContent = `${data.accounts.length} accounts · ${currencies.join(', ')}`;
  } else {
    const account = data.accounts.find((item) => item.external_account_id === state.accountId);
    scopeNote.textContent = `${account.platform_label} · ${account.currency} · ${account.timezone}`;
  }
  document.getElementById('scopebar').style.display = ['overview', 'insights'].includes(state.tab) ? 'flex' : 'none';
}

function render() {
  hideTooltip();
  renderChrome();
  const main = document.getElementById('app');
  main.replaceChildren();
  if (state.tab === 'overview') renderOverview(main);
  else if (state.tab === 'insights') renderInsights(main);
  else if (state.tab === 'accounts') renderAccounts(main);
  else renderReports(main);
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem('gp-theme'); } catch { /* private mode */ }
  if (saved === 'light' || saved === 'dark') document.documentElement.dataset.theme = saved;
  document.getElementById('theme-toggle').addEventListener('click', () => {
    const current = document.documentElement.dataset.theme
      ?? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('gp-theme', next); } catch { /* private mode */ }
  });
}

async function main() {
  initTheme();
  try {
    state.data = await loadDashboardData();
  } catch (error) {
    const app = document.getElementById('app');
    app.replaceChildren(el('div', 'empty',
      'Could not load data/dashboard_data.json. Serve this folder over HTTP — see frontend/README.md.'));
    return;
  }
  // Deep links: ?client=<key>&account=<external id>#<tab>. Unknown values are ignored.
  const params = new URLSearchParams(location.search);
  const wanted = state.data.clients.find((item) => item.key === params.get('client'));
  state.clientKey = (wanted ?? state.data.clients[0]).key;
  if (client().accounts.some((item) => item.external_account_id === params.get('account'))) {
    state.accountId = params.get('account');
  }
  state.tab = tabFromHash();
  window.addEventListener('hashchange', () => setTab(tabFromHash()));
  document.getElementById('client-select').addEventListener('change', (event) => {
    state.clientKey = event.target.value;
    state.accountId = 'all';
    state.severity = 'all';
    render();
  });
  document.getElementById('account-select').addEventListener('change', (event) => {
    state.accountId = event.target.value;
    render();
  });
  render();
  document.documentElement.dataset.ready = 'true';
}

main();
