/* Hand-rolled SVG charts: 2px lines, hairline solid gridlines, crosshair + one tooltip
   listing every series, keyboard focus, and a table view twin for every chart.
   All labels are inserted with textContent — never innerHTML. */

const SVG = 'http://www.w3.org/2000/svg';

export function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function niceMax(value) {
  if (value <= 0) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) {
    if (step * power >= value) return step * power;
  }
  return 10 * power;
}

function shortDate(iso) {
  const [, month, day] = iso.split('-').map(Number);
  const names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return `${day} ${names[month - 1]}`;
}

const tooltip = () => document.getElementById('viz-tooltip');

function showTooltip(clientX, clientY, title, rows) {
  const tip = tooltip();
  tip.replaceChildren();
  tip.append(el('div', 't-title', title));
  for (const row of rows) {
    const line = el('div', 't-row');
    const key = el('span', 't-key');
    key.style.borderTopColor = row.color;
    const name = el('span', 't-name', row.name);
    const value = el('span', 't-val', row.value);
    line.append(key, name, value);
    tip.append(line);
  }
  tip.hidden = false;
  const rect = tip.getBoundingClientRect();
  const x = Math.min(clientX + 14, window.innerWidth - rect.width - 10);
  const y = Math.min(clientY + 14, window.innerHeight - rect.height - 10);
  tip.style.left = `${Math.max(10, x)}px`;
  tip.style.top = `${Math.max(10, y)}px`;
}

export function hideTooltip() {
  tooltip().hidden = true;
}

export function legend(series, shape = 'line') {
  const box = el('div', 'legend');
  for (const item of series) {
    const key = el('span', 'key');
    const swatch = el('span', shape === 'line' ? 'swatch-line' : 'swatch-rect');
    if (shape === 'line') swatch.style.borderTopColor = item.color;
    else swatch.style.background = item.color;
    key.append(swatch, document.createTextNode(item.label));
    box.append(key);
  }
  return box;
}

function tableView(summaryText, head, rows) {
  const details = el('details', 'table-view');
  details.append(el('summary', '', summaryText));
  const wrap = el('div', 'table-wrap');
  const table = el('table', 'data');
  const thead = el('thead');
  const headRow = el('tr');
  for (const cell of head) headRow.append(el('th', '', cell));
  thead.append(headRow);
  const tbody = el('tbody');
  for (const row of rows) {
    const tr = el('tr');
    for (const cell of row) tr.append(el('td', '', cell));
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrap.append(table);
  details.append(wrap);
  return details;
}

/* Multi-series line chart. series: [{label, color, values: number[]}] over shared dates. */
export function lineChart(container, { dates, series, format, height = 215, tableLabel }) {
  container.replaceChildren();
  const chart = el('div', 'chart');
  chart.tabIndex = 0;
  chart.setAttribute('role', 'img');
  chart.setAttribute(
    'aria-label',
    `${tableLabel}: ${series.map((item) => item.label).join(', ')}, ${dates.length} days. Data table below.`
  );

  const width = 640;
  const margin = { top: 10, right: 14, bottom: 26, left: 52 };
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const root = svg('svg', { viewBox: `0 0 ${width} ${height}` });

  const maxValue = niceMax(Math.max(...series.flatMap((item) => item.values)));
  const x = (index) => margin.left + (plotW * index) / Math.max(1, dates.length - 1);
  const y = (value) => margin.top + plotH - (plotH * value) / maxValue;

  const ticks = 4;
  for (let i = 0; i <= ticks; i += 1) {
    const value = (maxValue * i) / ticks;
    const line = svg('line', {
      x1: margin.left, x2: width - margin.right, y1: y(value), y2: y(value),
      class: i === 0 ? 'baseline' : 'gridline',
    });
    root.append(line);
    const label = svg('text', { x: margin.left - 8, y: y(value) + 3.5, 'text-anchor': 'end', class: 'axis-text' });
    label.textContent = value >= 10000 ? `${Math.round(value / 1000)}K` : String(Math.round(value));
    root.append(label);
  }
  const xStep = Math.max(1, Math.round(dates.length / 6));
  for (let i = 0; i < dates.length; i += xStep) {
    const label = svg('text', { x: x(i), y: height - 7, 'text-anchor': 'middle', class: 'axis-text' });
    label.textContent = shortDate(dates[i]);
    root.append(label);
  }

  for (const item of series) {
    const points = item.values.map((value, index) => `${x(index)},${y(value)}`).join(' ');
    if (series.length === 1) {
      const area = svg('polygon', {
        points: `${margin.left},${y(0)} ${points} ${x(dates.length - 1)},${y(0)}`,
        fill: item.color, opacity: '0.09',
      });
      root.append(area);
    }
    root.append(svg('polyline', {
      points, fill: 'none', stroke: item.color, 'stroke-width': 2,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    }));
  }

  const crosshair = svg('line', { y1: margin.top, y2: margin.top + plotH, class: 'crosshair', visibility: 'hidden' });
  root.append(crosshair);
  const dots = series.map((item) => {
    const ring = svg('circle', { r: 6, fill: item.color, stroke: 'var(--surface)', 'stroke-width': 2, visibility: 'hidden' });
    root.append(ring);
    return ring;
  });

  let activeIndex = -1;
  const present = (index, clientX, clientY) => {
    activeIndex = index;
    const px = x(index);
    crosshair.setAttribute('x1', px);
    crosshair.setAttribute('x2', px);
    crosshair.setAttribute('visibility', 'visible');
    series.forEach((item, s) => {
      dots[s].setAttribute('cx', px);
      dots[s].setAttribute('cy', y(item.values[index]));
      dots[s].setAttribute('visibility', 'visible');
    });
    showTooltip(clientX, clientY, shortDate(dates[index]) + ' 2026',
      series.map((item) => ({ name: item.label, color: item.color, value: format(item.values[index]) })));
  };
  const clear = () => {
    activeIndex = -1;
    crosshair.setAttribute('visibility', 'hidden');
    for (const dot of dots) dot.setAttribute('visibility', 'hidden');
    hideTooltip();
  };

  root.addEventListener('pointermove', (event) => {
    const rect = root.getBoundingClientRect();
    const relative = ((event.clientX - rect.left) / rect.width) * width;
    const index = Math.round(((relative - margin.left) / plotW) * (dates.length - 1));
    if (index < 0 || index >= dates.length) return clear();
    present(index, event.clientX, event.clientY);
  });
  root.addEventListener('pointerleave', clear);
  chart.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const next = Math.min(dates.length - 1, Math.max(0,
      (activeIndex < 0 ? dates.length - 1 : activeIndex) + (event.key === 'ArrowRight' ? 1 : -1)));
    const rect = root.getBoundingClientRect();
    present(next, rect.left + (rect.width * next) / dates.length, rect.top + 40);
  });
  chart.addEventListener('blur', clear);

  chart.append(root);
  container.append(chart);
  container.append(tableView(
    'View data as table',
    ['Date', ...series.map((item) => item.label)],
    dates.map((date, index) => [shortDate(date), ...series.map((item) => format(item.values[index]))])
  ));
}

/* Ranked horizontal bars, one series (single hue), value at the tip. */
export function barChart(container, { rows, format, tooltipRows }) {
  container.replaceChildren();
  const box = el('div', 'hbars');
  const max = Math.max(...rows.map((row) => row.value), 1);
  for (const row of rows) {
    const hit = el('div', 'row-hit');
    hit.tabIndex = 0;
    const label = el('span', 'blabel', row.label);
    label.title = row.label;
    const track = el('div', 'track');
    const bar = el('div', 'bar');
    bar.style.width = `${Math.max(1.2, (row.value / max) * 100)}%`;
    track.append(bar, el('span', 'bval', format(row.value)));
    const show = (clientX, clientY) =>
      showTooltip(clientX, clientY, row.label, tooltipRows(row));
    hit.addEventListener('pointermove', (event) => show(event.clientX, event.clientY));
    hit.addEventListener('pointerleave', hideTooltip);
    hit.addEventListener('focus', () => {
      const rect = bar.getBoundingClientRect();
      show(rect.right, rect.top);
    });
    hit.addEventListener('blur', hideTooltip);
    hit.append(label, track);
    box.append(hit);
  }
  container.append(box);
}

/* 30-point sparkline: history in the dim tone, the last 7 days in the accent. */
export function sparkline(values, { width = 92, height = 26, accentDays = 7 } = {}) {
  const root = svg('svg', { viewBox: `0 0 ${width} ${height}`, width, height, 'aria-hidden': 'true' });
  const max = Math.max(...values, 1e-9);
  const min = Math.min(...values, 0);
  const x = (index) => 1 + ((width - 2) * index) / Math.max(1, values.length - 1);
  const y = (value) => height - 2 - ((height - 4) * (value - min)) / Math.max(1e-9, max - min);
  const split = Math.max(0, values.length - accentDays);
  const path = (from, to, cls, color) => {
    const points = [];
    for (let i = from; i <= to; i += 1) points.push(`${x(i)},${y(values[i])}`);
    root.append(svg('polyline', {
      points: points.join(' '), fill: 'none', stroke: color, 'stroke-width': 2,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round', class: cls,
    }));
  };
  if (split > 0) path(0, split, 'dim', 'var(--spark-dim)');
  path(split, values.length - 1, 'accent', 'var(--s1)');
  return root;
}
