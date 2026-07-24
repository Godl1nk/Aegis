// journey.js — Journey tab in the Memory modal.
//
// Paints the server-rendered learning timeline (services/memory/
// learning_graph_render.py, ported from Hermes): an animated play-through
// of date rows with proportional skill/memory bars colored by the day's
// dominant category, a cumulative trajectory sparkline, legend, and a
// per-slice bucket tree with memory bodies.

import uiModule from './ui.js';

const API = window.location.origin;

function esc(s) { return uiModule.esc(String(s ?? '')); }

let _data = null;
let _animTimer = null;

// Style keys from the renderer → CSS classes (colors in style.css).
const STYLE_CLASS = {
  bg: 'j-bg',
  skill: 'j-skill',
  memory: 'j-memory',
  label: 'j-label',
  dim: 'j-dim',
};

function _runHtml(run) {
  const [text, style, alpha, hex] = run;
  const cls = STYLE_CLASS[style] || 'j-label';
  const a = typeof alpha === 'number' ? alpha : 1.0;
  const color = hex ? `color:${esc(hex)};` : '';
  return `<span class="${cls}" style="opacity:${a.toFixed(3)};${color}">${esc(text)}</span>`;
}

function _paintFrame(gridEl, frame) {
  const rows = (frame.grid || []).map(row =>
    `<div class="journey-row">${(row || []).map(_runHtml).join('') || '&nbsp;'}</div>`
  );
  let labels = '';
  if (frame.labels && frame.labels.length) {
    labels = frame.labels.map(l =>
      `<div class="journey-row journey-label-row">` +
      `<span class="j-label" style="opacity:0.95">${esc(l.key)} </span>` +
      `<span class="${STYLE_CLASS[l.style] || 'j-label'}" style="opacity:${(l.alpha ?? 0.9)}">${esc(l.glyph)} ${esc(l.label)}</span>` +
      `<span class="j-dim" style="opacity:0.6">  ${esc(l.meta)}</span></div>`
    ).join('');
  }
  const date = (frame.date && frame.date !== 'unknown')
    ? `<div class="journey-row j-dim" style="opacity:0.6">${esc(frame.date)}</div>` : '';
  gridEl.innerHTML = rows.join('') + labels + date;
}

function _animate() {
  const gridEl = document.getElementById('journey-grid');
  if (!gridEl || !_data || !_data.frames || !_data.frames.length) return;
  if (_animTimer) clearInterval(_animTimer);
  let i = 0;
  const frames = _data.frames;
  _animTimer = setInterval(() => {
    _paintFrame(gridEl, frames[i]);
    i++;
    if (i >= frames.length) { clearInterval(_animTimer); _animTimer = null; }
  }, 35);
}

function _renderLegend() {
  const el = document.getElementById('journey-legend');
  if (!el || !_data) return;
  const parts = [];
  for (const item of _data.legend || []) {
    parts.push(`<span class="${STYLE_CLASS[item.style] || 'j-label'} journey-chip">${esc(item.glyph)} ${esc(item.label)}</span>`);
  }
  for (const cat of _data.categories || []) {
    const color = cat.color ? `style="color:${esc(cat.color)}"` : '';
    parts.push(`<span class="journey-chip" ${color}>${esc(cat.glyph)} ${esc(cat.label)}</span>`);
  }
  const axis = _data.axis || {};
  if (axis.start || axis.end) {
    parts.push(`<span class="journey-chip j-dim">${esc(axis.start || '')} → ${esc(axis.end || '')}</span>`);
  }
  el.innerHTML = parts.join('');
}

function _renderSummary() {
  const el = document.getElementById('journey-summary');
  if (!el || !_data) return;
  el.innerHTML = (_data.summary || []).map(line => `<div>${esc(line)}</div>`).join('');
}

function _renderBuckets() {
  const el = document.getElementById('journey-buckets');
  if (!el || !_data) return;
  const buckets = (_data.buckets || []).filter(b => b.total > 0);
  if (!buckets.length) { el.innerHTML = ''; return; }
  el.innerHTML = buckets.map((b, i) => {
    const color = b.color ? `style="color:${esc(b.color)}"` : '';
    const nodes = (b.nodes || []).map(n => {
      const cls = STYLE_CLASS[n.style] || 'j-label';
      const body = n.body ? `<div class="journey-node-body">${esc(n.body)}</div>` : '';
      return `<div class="journey-node"><span class="${cls}">${esc(n.glyph)}</span> ` +
        `<span class="journey-node-label" title="${esc(n.fullLabel)}">${esc(n.fullLabel)}</span>` +
        `<span class="j-dim journey-node-meta">${esc(n.meta)}</span>${body}</div>`;
    }).join('');
    return `<div class="journey-bucket" data-bucket="${i}">` +
      `<div class="journey-bucket-head">` +
      `<span class="journey-bucket-caret">▸</span>` +
      `<span class="journey-bucket-date" ${color} title="${esc(b.date)}">${esc(b.label)}</span>` +
      `<span class="j-dim">${b.skills} skill${b.skills === 1 ? '' : 's'}${b.memories ? ` + ${b.memories} memor${b.memories === 1 ? 'y' : 'ies'}` : ''}</span>` +
      `</div><div class="journey-bucket-nodes hidden">${nodes}</div></div>`;
  }).join('');
  el.querySelectorAll('.journey-bucket-head').forEach(head => {
    head.addEventListener('click', () => {
      const nodes = head.parentElement.querySelector('.journey-bucket-nodes');
      const caret = head.querySelector('.journey-bucket-caret');
      const open = nodes.classList.toggle('hidden');
      if (caret) caret.textContent = open ? '▸' : '▾';
    });
  });
}

export async function loadJourney(force = false) {
  const gridEl = document.getElementById('journey-grid');
  if (!gridEl) return;
  if (_data && !force) { _animate(); return; }
  gridEl.innerHTML = '<div class="j-dim journey-row" style="opacity:0.6">loading journey…</div>';
  try {
    const charW = 7.25; // 12px monospace advance
    const cols = Math.max(44, Math.min(160, Math.floor((gridEl.clientWidth || 640) / charW)));
    const res = await fetch(`${API}/api/learning/journey?cols=${cols}&rows=18&frames=48`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _data = await res.json();
  } catch (e) {
    gridEl.innerHTML = `<div class="j-dim journey-row">journey unavailable (${esc(e.message)})</div>`;
    return;
  }
  _renderSummary();
  _renderLegend();
  _renderBuckets();
  _animate();
}

document.addEventListener('DOMContentLoaded', () => {
  const replay = document.getElementById('journey-replay-btn');
  if (replay) replay.addEventListener('click', () => loadJourney(true));
});

export default { loadJourney };
