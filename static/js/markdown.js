// static/js/markdown.js

/**
 * Markdown rendering and content processing utilities
 */

import uiModule from './ui.js';
import { splitTableRow } from './markdown/tableRow.js';
import { replaceEmojiShortcodes, hasEmojiShortcode } from './emojiShortcodes.js';

var escapeHtml = uiModule.esc;

// Sentinel standing in for a literal <br> between extraction and restore.
// Not the ___ALLOWED_HTML_n___ shape on purpose — the table converter refuses
// any block containing that prefix, and <br> has to survive inside a cell.
// Control characters: no markdown meaning (emphasis/heading passes ignore
// them), nothing for the HTML escape to touch, and unlike a word sentinel
// they cannot collide with real prose.
const LINE_BREAK_TOKEN = 'BRTOKEN';
const CDOT_COMMAND_RE = /\\(?:cdot|cdotp)\b/g;
const CDOT_COMMAND_ONLY_RE = /^\\(?:cdot|cdotp)\b$/;

// Unicode vulgar fraction → LaTeX \frac mapping. Models often emit ½, ⅓
// etc. instead of \frac{}{} in prose. Converted to KaTeX inline math.
const UNICODE_FRACTIONS = {
  '\u00BC': '\\frac{1}{4}', '\u00BD': '\\frac{1}{2}', '\u00BE': '\\frac{3}{4}',
  '\u2153': '\\frac{1}{3}', '\u2154': '\\frac{2}{3}',
  '\u2155': '\\frac{1}{5}', '\u2156': '\\frac{2}{5}', '\u2157': '\\frac{3}{5}', '\u2158': '\\frac{4}{5}',
  '\u2159': '\\frac{1}{6}', '\u215A': '\\frac{5}{6}',
  '\u215B': '\\frac{1}{8}', '\u215C': '\\frac{3}{8}', '\u215D': '\\frac{5}{8}', '\u215E': '\\frac{7}{8}',
  '\u2150': '\\frac{1}{7}', '\u2151': '\\frac{1}{9}', '\u2152': '\\frac{1}{10}',
};
const FRACTION_CHARS_RE = new RegExp('[' + Object.keys(UNICODE_FRACTIONS).join('') + ']', 'g');

function safeLinkUrl(rawUrl) {
  const url = String(rawUrl || '').trim();
  if (url.startsWith('#')) {
    return /^#[A-Za-z0-9_-]*$/.test(url) ? url : '';
  }
  try {
    const parsed = new URL(url, window.location.origin);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return parsed.href;
    }
  } catch (_) {
    return '';
  }
  return '';
}

function linkHtml(text, url) {
  const safeUrl = safeLinkUrl(url);
  const safeText = escapeHtml(text);
  if (!safeUrl) return safeText;
  if (safeUrl.startsWith('#')) {
    return `<a href="${safeUrl}" class="chat-link">${safeText}</a>`;
  }
  return `<a href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${safeText}</a>`;
}

function imageHtml(alt, url, title) {
  const safeUrl = safeLinkUrl(url);
  if (!safeUrl || safeUrl.startsWith('#')) return escapeHtml(alt || '');
  const safeAlt = escapeHtml(alt || '');
  const safeTitle = title ? ` title="${escapeHtml(title)}"` : '';
  return `<img src="${escapeHtml(safeUrl)}" alt="${safeAlt}"${safeTitle} loading="lazy" decoding="async">`;
}

function generatedImageLinkHtml(text, url, title) {
  const safeUrl = safeLinkUrl(url);
  const safeText = escapeHtml(text || 'Generated image');
  if (!safeUrl) return safeText;
  try {
    const parsed = new URL(safeUrl, window.location.origin);
    if (parsed.origin !== window.location.origin || !parsed.pathname.startsWith('/api/generated-image/')) {
      return imageHtml(text, url, title);
    }
  } catch (_) {
    return safeText;
  }
  return `<a href="${escapeHtml(safeUrl)}" class="generated-image-markdown-link" data-image-url="${escapeHtml(safeUrl)}" data-image-prompt="${safeText}">${safeText}</a>`;
}

export function normalizeHighlightLanguage(lang) {
  const raw = String(lang || '').trim().toLowerCase();
  if (!raw) return '';
  const aliases = {
    py: 'python',
    js: 'javascript',
    ts: 'typescript',
    yml: 'yaml',
    sh: 'bash',
    shell: 'bash',
  };
  const candidate = aliases[raw] || raw;
  if (window.hljs && typeof window.hljs.getLanguage === 'function') {
    return window.hljs.getLanguage(candidate) ? candidate : '';
  }
  return candidate;
}

function replaceOutsideInlineCode(text, pattern, replacer) {
  return String(text || '').split(/(`[^`]*`)/g).map(part => {
    if (part.startsWith('`') && part.endsWith('`')) return part;
    return part.replace(pattern, replacer);
  }).join('');
}

const KATEX_RENDER_OPTIONS = {
  throwOnError: false,
  strict: false,
  macros: {
    "\\cdotp": "\\cdot",
  },
};

function renderKatexToString(math, options = {}) {
  return katex.renderToString(String(math || '').trim(), {
    ...KATEX_RENDER_OPTIONS,
    ...options,
  });
}

function repairKatexCommandErrors(html) {
  return String(html || '').replace(
    /<span\b([^>]*\bclass=(["'])[^"']*\bkatex-error\b[^"']*\2[^>]*)>\\(?:cdot|cdotp)<\/span>/gi,
    '&centerdot;'
  );
}

function _isModelEndpointUrl(rawUrl) {
  try {
    const parsed = new URL(String(rawUrl || ''), window.location.origin);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
    const path = parsed.pathname.replace(/\/+$/, '');
    return path === '/v1';
  } catch (_) {
    return false;
  }
}

/**
 * Sanitize the raw-HTML fragments that mdToHtml deliberately preserves from
 * the source text — <details> blocks (collapsible agent output) and <a> tags
 * (emitted by the markdown link pass). Those fragments are later restored
 * verbatim into innerHTML, so without scrubbing them a model — or any content
 * routed through here — could smuggle in an `<img onerror=...>`, an
 * `<a href="javascript:...">`, an `onmouseover=` handler, etc. and execute
 * script in the authenticated page (DOM XSS).
 *
 * Parsing into a <template> is inert: assigning to template.innerHTML neither
 * fetches resources nor runs scripts, so we can walk the resulting tree,
 * drop script-capable elements, and strip event-handler attributes and
 * dangerous URL schemes before the (now safe) fragment is handed back.
 */
const _ALLOWED_HTML_BAD_TAGS = new Set([
  'SCRIPT', 'IFRAME', 'OBJECT', 'EMBED', 'LINK', 'META',
  'STYLE', 'BASE', 'FORM', 'NOSCRIPT', 'TEMPLATE',
  // Foreign-content roots. SVG/MathML have their own parser rules and are a
  // classic mutation-XSS vehicle — e.g. an SVG-namespaced <script>, whose
  // `tagName` is the lower-case 'script' and would slip a name check that
  // assumed HTML's upper-casing. They aren't needed in the <details>/<a>
  // fragments we preserve, so drop the whole subtree.
  'SVG', 'MATH',
]);
const _ALLOWED_HTML_URL_ATTRS = new Set([
  'href', 'src', 'srcset', 'xlink:href', 'action', 'formaction', 'background', 'poster',
]);

function _compactUrlSchemeValue(value) {
  return String(value || '').replace(/[\u0000-\u0020\u007f-\u009f]+/g, '').toLowerCase();
}

function _isDangerousUrl(value) {
  return /^(javascript|vbscript|data):/.test(_compactUrlSchemeValue(value));
}

function _isDangerousSrcset(value) {
  return String(value || '').split(',').some(candidate => _isDangerousUrl(candidate));
}

function _cleanAllowedHtmlOnce(htmlString) {
  const tpl = document.createElement('template');
  tpl.innerHTML = htmlString;
  for (const el of Array.from(tpl.content.querySelectorAll('*'))) {
    // Upper-case the tag for comparison: HTML tagNames are upper-case, but
    // SVG/MathML elements preserve their original (lower/camel) case, so a
    // raw `Set.has(el.tagName)` would miss e.g. a namespaced <script>.
    if (_ALLOWED_HTML_BAD_TAGS.has(el.tagName.toUpperCase())) {
      el.remove();
      continue;
    }
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      // Drop every inline event handler (onerror, onclick, onmouseover, ...)
      // and srcdoc (a frame-less script vector).
      if (name.startsWith('on') || name === 'srcdoc') {
        el.removeAttribute(attr.name);
        continue;
      }
      if (name === 'style') {
        const value = _compactUrlSchemeValue(attr.value);
        if (/javascript:|vbscript:|data:|expression\(/.test(value)) {
          el.removeAttribute(attr.name);
        }
        continue;
      }
      // Neutralize javascript:/vbscript:/data: in URL-bearing attributes.
      // Strip control/space chars first so e.g. "java\tscript:" can't slip by.
      if (_ALLOWED_HTML_URL_ATTRS.has(name)) {
        if (name === 'srcset' ? _isDangerousSrcset(attr.value) : _isDangerousUrl(attr.value)) {
          el.removeAttribute(attr.name);
        }
      }
    }
  }
  return tpl.innerHTML;
}

export function sanitizeAllowedHtml(html) {
  const raw = String(html == null ? '' : html);
  // Non-browser context (e.g. a future SSR/Node import): fail closed by
  // escaping rather than trusting the markup.
  if (typeof document === 'undefined') return escapeHtml(raw);

  // Sanitize to a fixpoint. Re-parsing the serialized output can mutate the
  // tree (the basis of mutation-XSS), so re-clean until it stops changing.
  let out = raw;
  for (let i = 0; i < 4; i++) {
    const next = _cleanAllowedHtmlOnce(out);
    if (next === out) break;
    out = next;
  }
  return out;
}

/**
 * Check if text has unclosed think tag
 */
export function hasUnclosedThinkTag(text) {
  text = normalizeThinkingMarkup(text || '');
  const openCount =
    (text.match(/<(?:think(?:ing)?|thought)(?:\s+[^>]*)?>/gi) || []).length
    + (text.match(/<\|channel>thought/gi) || []).length;
  const closeCount =
    (text.match(/<\/(?:think(?:ing)?|thought)\s*>/gi) || []).length
    + (text.match(/<channel\|>/gi) || []).length;
  return openCount > closeCount;
}

// llama-swap prepends a model-load banner to the response stream as ORDINARY
// content, e.g.
//   ─────────
//   llama-swap loading model: GRM2.6-27B
//   Compressing optimism into FP16 ......
//   Done! (7.26s)
//   ─────────
// It is infrastructure noise, never part of the reply.
const LLAMA_SWAP_BANNER_RE =
  /^\s*(?:[-—–_─━=*]{3,}[ \t]*\r?\n)?[ \t]*llama-swap\s+loading\s+model:[\s\S]*?Done!\s*\([^)]*\)(?:\s*[-—–_─━=*]{3,}[ \t]*)?\s*/i;

export function startsWithReasoningPrefix(text) {
  let candidate = String(text || '').trimStart();
  const bannerMatch = LLAMA_SWAP_BANNER_RE.exec(candidate);
  if (bannerMatch) candidate = candidate.slice(bannerMatch[0].length).trimStart();
  return /^(?:thinking(?:\s+process)?\s*:|the user |user wants|we need |i need |i should |i will |i'll |i am going |let me (?:think|look|see|check|read|review|analyze|parse|figure|draft|write)|they are |the question |i can )/i.test(candidate);
}

export function normalizeThinkingMarkup(text) {
  if (!text) return text;
  let normalized = text;
  // MiniMax M-series can emit namespaced reasoning tags like
  // <mm:think>...</mm:think>. Normalize them into the shared thinking parser.
  normalized = normalized.replace(/<mm:think(\s+[^>]*)?>/gi, (_m, attrs = '') => `<think${attrs || ''}>`);
  normalized = normalized.replace(/<\/mm:think>/gi, '</think>');
  normalized = normalized.replace(/<thought(\s+[^>]*)?>/gi, (_m, attrs = '') => `<think${attrs || ''}>`);
  normalized = normalized.replace(/<\/thought>/gi, '</think>');
  normalized = normalized.replace(/<\|channel>thought\s*\n?([\s\S]*?)<channel\|>\s*/gi, (_m, content = '') => {
    const thought = String(content || '').trim();
    return thought ? `<think>${thought}</think>\n` : '';
  });
  normalized = normalized.replace(/<\|channel>response\s*\n?([\s\S]*?)<channel\|>/gi, (_m, content = '') => content || '');
  normalized = normalized.replace(/<\|channel>response\s*\n?/gi, '');
  normalized = normalized.replace(/<channel\|>/gi, '');
  return normalized;
}

function normalizePlainThinking(text) {
  if (!text) return text;
  text = normalizeThinkingMarkup(text);
  if (/<think/i.test(text)) return text;

  const trimmed = text.trimStart();

  // The llama-swap load banner arrives BEFORE the model's own output, so
  // `trimmed` started with the banner instead of a reasoning phrase — the
  // prefix test below failed and untagged reasoning leaked into the chat body
  // (visible as the loading text + "The user wants…" rendered as the reply).
  // Split the banner off, detect reasoning on the real text, and fold the
  // banner into the thinking block so it stays collapsed either way.
  const bannerMatch = LLAMA_SWAP_BANNER_RE.exec(trimmed);
  if (bannerMatch) {
    const banner = bannerMatch[0].trim();
    const rest = trimmed.slice(bannerMatch[0].length);
    const normalizedRest = normalizePlainThinking(rest).trimStart();
    const inner = /^<think>([\s\S]*?)<\/think>/i.exec(normalizedRest);
    if (inner) {
      const after = normalizedRest.slice(inner[0].length);
      return `<think>${banner}\n\n${inner[1]}</think>${after}`;
    }
    // No reasoning followed — still collapse the banner out of the reply.
    return `<think>${banner}</think>\n${normalizedRest}`;
  }

  if (!startsWithReasoningPrefix(trimmed)) return text;

  const replyStarts = [
    'Hey', 'Hi ', 'Hi!', 'Hello', 'Sure', 'Yes', 'No ', 'No,', 'Yo', 'OK',
    'Here', 'Absolutely', 'Of course', 'Great', 'Alright', 'Thanks', 'Welcome',
    'Good ', "I'm happy", "I'd be"
  ];
  const prefixRegex = /^(thinking(?:\s+process)?\s*:)\s*/i;
  const escapedReplyStarts = replyStarts.map((value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const boundaryRegex = new RegExp(
    `^([\\s\\S]*?)(\\n\\n(?=${escapedReplyStarts.join('|')}))[\\s\\S]*$`,
    'i'
  );
  const boundaryMatch = boundaryRegex.exec(trimmed);

  if (boundaryMatch) {
    const thinkBlock = boundaryMatch[1].replace(prefixRegex, '').trim();
    const reply = trimmed.slice(boundaryMatch[1].length).trimStart();
    if (thinkBlock && reply) return `<think>${thinkBlock}</think>\n\n${reply}`;
  }

  const lines = trimmed.split('\n');
  for (let index = 1; index < lines.length; index += 1) {
    const line = lines[index].trim();
    if (!line) continue;
    if (replyStarts.some((prefix) => line.startsWith(prefix))) {
      const thinkBlock = lines.slice(0, index).join('\n').replace(prefixRegex, '').trim();
      const reply = lines.slice(index).join('\n').trim();
      if (thinkBlock && reply) return `<think>${thinkBlock}</think>\n${reply}`;
    }
  }

  const withoutPrefix = trimmed.replace(prefixRegex, '');
  for (const prefix of replyStarts) {
    const rx = new RegExp(`[.!?]\\s*(${prefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`);
    const match = rx.exec(withoutPrefix);
    if (match && match.index > 20) {
      const thinkBlock = withoutPrefix.slice(0, match.index + 1).trim();
      const reply = withoutPrefix.slice(match.index + 1).trim();
      if (thinkBlock && reply) return `<think>${thinkBlock}</think>\n${reply}`;
    }
  }

  if (/^\s*(?:thinking(?:\s+process)?\s*:|the user |user wants|we need |let me (?:think|look|see|check|read|review|analyze|parse|figure|draft|write)|i need to |i should |i will |i'll |i am going )/i.test(trimmed)) {
    const thinkBlock = withoutPrefix.trim();
    if (thinkBlock) return `<think>${thinkBlock}</think>`;
  }

  return text;
}

/**
 * Extract all complete thinking blocks and remaining content
 */
export function extractThinkingBlocks(text) {
  // Handle malformed patterns: <think></think>\n...actual thinking...\n</think>
  // Some models emit an empty <think></think> then put thinking text outside,
  // closed by a second orphaned </think>.
  let normalized = normalizePlainThinking(text);
  // Collapse <think>short</think>...real thinking...</think> into one block
  // Models sometimes emit a trivial first block then continue thinking outside tags
  normalized = normalized.replace(/<think(?:ing)?(?:\s+[^>]*)?>.{0,30}<\/think(?:ing)?>\s*([\s\S]*?)<\/think(?:ing)?>/gi, (m, content) => {
    return '<think>' + content.trim() + '</think>';
  });

  // Merge consecutive <think> blocks (some models split thinking across multiple tags)
  normalized = normalized.replace(/<\/think(?:ing)?>\s*<think(?:ing)?(?:\s+[^>]*)?>/gi, '\n\n');

  // Extract thinking time attribute if present
  const timeMatch = normalized.match(/<think(?:ing)?\s+time="([\d.]+)"/i);
  const thinkingTime = timeMatch ? timeMatch[1] : null;
  // Strip time attribute for content extraction
  normalized = normalized.replace(/<think(?:ing)?\s+time="[\d.]+"/gi, '<think');

  const thinkRegex = /<think(?:ing)?(?:\s+[^>]*)?>([\s\S]*?)<\/think(?:ing)?>/gi;
  const thinkingBlocks = [];
  let match;

  // Extract all complete thinking blocks
  while ((match = thinkRegex.exec(normalized)) !== null) {
    const content = match[1].trim();
    if (content) thinkingBlocks.push(content);
  }

  // Remove all complete <think>/<thinking> blocks
  let cleanContent = normalized.replace(thinkRegex, '');

  // If there's an unclosed tag, decide between two cases:
  // (a) Stray opener at the very start with no real reply before it — typical
  //     of quantized models (MiniMax-AWQ) that emit a literal `<think>` token
  //     at the start of every reply without ever closing it. Strip just the
  //     opener and keep the body as the reply, otherwise the bubble looks
  //     blank on reload (the body was being treated as collapsed thinking).
  // (b) Cut-off mid-generation — there's already real reply text before the
  //     opener. Drop from the tag onward as before (it's truncated thinking).
  if (hasUnclosedThinkTag(normalized)) {
    const gemmaThoughtStart = cleanContent.search(/<\|channel>thought/i);
    if (gemmaThoughtStart >= 0) {
      const leakedThought = cleanContent
        .slice(gemmaThoughtStart)
        .replace(/^<\|channel>thought\s*\n?/i, '')
        .trim();
      if (gemmaThoughtStart === 0 && leakedThought) thinkingBlocks.push(leakedThought);
      cleanContent = cleanContent.slice(0, gemmaThoughtStart);
    } else {
      const strayOpener = cleanContent.match(/^\s*<think(?:ing)?(?:\s+[^>]*)?>([\s\S]*)$/i);
      if (strayOpener) {
        cleanContent = strayOpener[1];
      } else {
        cleanContent = cleanContent.replace(/<think(?:ing)?(?:\s+[^>]*)?>[\s\S]*$/gi, '');
      }
    }
  }

  // Handle orphaned </think> with no opening tag — text before it is leaked thinking
  const orphanMatch = cleanContent.match(/^([\s\S]+?)<\/think(?:ing)?>/i);
  if (orphanMatch && orphanMatch[1].trim()) {
    thinkingBlocks.push(orphanMatch[1].trim());
    cleanContent = cleanContent.slice(orphanMatch[0].length);
  }

  // Strip any remaining orphaned closing tags
  cleanContent = cleanContent.replace(/<\/think(?:ing)?>/gi, '');

  // Merge all thinking blocks into one — no reason to show multiple dropdowns
  const mergedBlocks = thinkingBlocks.length > 1
    ? [thinkingBlocks.join('\n\n')]
    : thinkingBlocks;

  return {
    thinkingBlocks: mergedBlocks,
    content: cleanContent.trim(),
    thinkingTime,
  };
}

/**
 * Create a collapsible thinking section
 */
function createThinkingSection(thinkingContent, index = 0, thinkingTime = null) {
  const id = `thinking-${Date.now()}-${index}`;
  const timeHtml = thinkingTime ? `<span style="font-size:11px;opacity:0.4;font-variant-numeric:tabular-nums;">${thinkingTime}s</span>` : '';
  return `
    <div class="thinking-section">
      <div class="thinking-header" data-thinking-id="${id}">
        <div class="thinking-header-left">
          <span>View thinking process</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          ${timeHtml}
          <span class="thinking-toggle" id="${id}-toggle"></span>
        </div>
      </div>
      <div class="thinking-content" id="${id}">
        <div class="thinking-content-inner">
          ${mdToHtml(thinkingContent)}
        </div>
      </div>
    </div>
  `;
}

function createTaskCompletedMarker() {
  return `
    <div class="task-completed-marker" role="status" aria-label="Task completed">
      <span class="task-completed-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
      </span>
      <span>Task completed</span>
    </div>
  `;
}

/**
 * Process text and render with thinking sections
 */
// ── Emoji → monochrome SVG (OpenMoji-black via same-origin /api/emoji proxy) ──
// Replace colorful system/Twemoji emoji with single-color line icons tinted to
// the surrounding text color (project rule: never colorful emoji). Operates on
// rendered HTML: only touches text outside tags and skips <code>/<pre>.
const _EMOJI_RE = /\p{Extended_Pictographic}/u;
const _emojiSeg = (typeof Intl !== 'undefined' && Intl.Segmenter)
  ? new Intl.Segmenter(undefined, { granularity: 'grapheme' }) : null;

function _emojiCodepoints(emoji) {
  // Twemoji filename rule: strip U+FE0F unless the sequence has a ZWJ (U+200D).
  const s = emoji.indexOf('‍') >= 0 ? emoji : emoji.replace(/️/g, '');
  const cps = [];
  for (const ch of s) { const c = ch.codePointAt(0); if (c) cps.push(c.toString(16)); }
  return cps.join('-');
}
function _emojiImg(emoji) {
  const code = _emojiCodepoints(emoji);
  if (!code) return emoji;
  // Monochrome line icon: the OpenMoji black SVG is used as a CSS mask filled
  // with the surrounding text color (currentColor), so emoji render as a single
  // theme-tinted line glyph — never colorful (project rule). If the proxy can't
  // supply the glyph it returns a transparent SVG, so the mask shows nothing.
  return `<span class="emoji" role="img" aria-label="${emoji}" style="--em:url('/api/emoji/${code}.svg')"></span>`;
}
function _svgifyText(text) {
  if (!_emojiSeg) return text;
  let out = '';
  for (const { segment } of _emojiSeg.segment(text)) {
    out += _EMOJI_RE.test(segment) ? _emojiImg(segment) : segment;
  }
  return out;
}
/** When "Text-only Emojis" is on, keep Unicode in HTML so deEmojify() can strip them. */
function _useSvgEmoji() {
  return typeof document === 'undefined' || !document.body?.classList.contains('text-emojis');
}

// `opts.shortcodes` (default true) controls the issue-#345 `:name:` → emoji
// expansion. Chat passes it through as true; document/email body renderers pass
// false so author-typed `:shortcode:` text stays literal (see mdToHtml callers).
// The Unicode-emoji → monochrome-SVG pass always runs regardless, so a real 😀
// in a document still renders as the themed line icon as it always has.
export function svgifyEmoji(html, opts) {
  if (!html) return html;
  const useSvgEmoji = _useSvgEmoji();
  const allowShortcodes = useSvgEmoji && (!opts || opts.shortcodes !== false);
  // Two reasons to walk the HTML: real Unicode emoji to turn into SVG icons,
  // or `:shortcode:` text the model emitted instead of an emoji (issue #345).
  const hasUnicode = useSvgEmoji && _EMOJI_RE.test(html);
  const hasShortcode = allowShortcodes && hasEmojiShortcode(html);
  if (!hasUnicode && !hasShortcode) return html;
  const parts = html.split(/(<[^>]*>)/);   // odd indices = tags
  let codeDepth = 0;
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) {
      const t = parts[i].toLowerCase();
      if (/^<(pre|code)[\s>]/.test(t)) codeDepth++;
      else if (/^<\/(pre|code)\s*>/.test(t)) codeDepth = Math.max(0, codeDepth - 1);
      continue;
    }
    if (codeDepth !== 0) continue;
    let seg = parts[i];
    // Expand shortcodes to Unicode first, then both they and any pre-existing
    // Unicode emoji get rendered as the same monochrome line icons below.
    if (hasShortcode) seg = replaceEmojiShortcodes(seg);
    if (useSvgEmoji && _EMOJI_RE.test(seg)) seg = _svgifyText(seg);
    parts[i] = seg;
  }
  return parts.join('');
}
/**
 * Generic collapsible section that reuses the thinking-dropdown styling and its
 * delegated toggle (any `.thinking-header[data-thinking-id]`). The label drives
 * the "View <label>" / "Hide <label>" text via data-label. Used e.g. for the
 * vision-model image description on a user's photo message.
 */
export function createCollapsible(contentMarkdown, label = 'details') {
  const id = `collapse-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  const safeLabel = escapeHtml(label);
  return `
    <div class="thinking-section">
      <div class="thinking-header" data-thinking-id="${id}">
        <div class="thinking-header-left"><span data-label="${safeLabel}">View ${safeLabel}</span></div>
        <div style="display:flex;align-items:center;gap:6px;"><span class="thinking-toggle" id="${id}-toggle"></span></div>
      </div>
      <div class="thinking-content" id="${id}"><div class="thinking-content-inner">${mdToHtml(contentMarkdown)}</div></div>
    </div>`;
}

export function processWithThinking(text) {
  const { thinkingBlocks, content, thinkingTime } = extractThinkingBlocks(text);

  let html = '';
  let visibleContent = content || '';
  const doneOnly = /^\s*\[DONE\]\s*$/i.test(visibleContent);
  const hadTrailingDone = !doneOnly && /(?:^|\n)\s*\[DONE\]\s*$/i.test(visibleContent);

  // Add thinking sections (collapsed by default)
  thinkingBlocks.forEach((block, index) => {
    html += createThinkingSection(block, index, thinkingTime);
  });

  // Add the actual content
  if (doneOnly) {
    html += createTaskCompletedMarker();
  } else {
    if (hadTrailingDone) visibleContent = visibleContent.replace(/\n?\s*\[DONE\]\s*$/i, '').trimEnd();
    if (visibleContent) html += mdToHtml(visibleContent);
    if (hadTrailingDone) html += createTaskCompletedMarker();
  }

  return svgifyEmoji(html);
}

/**
 * Convert markdown to HTML
 */
export function mdToHtml(src, opts) {
  const allowedHtmlBlocks = [];
  const codeBlocks = [];
  const inlineCodeBlocks = [];
  const mermaidBlocks = [];
  let s = (src ?? '');

  // Extract fenced code blocks before any markdown/HTML preservation passes.
  // Otherwise placeholders from the allowed-HTML sanitizer (e.g.
  // ___ALLOWED_HTML_0___) can leak into quoted HTML/JS samples, because the
  // placeholder gets captured as literal code content and never restored inside
  // the final <pre><code> block.
  s = s.replace(/```([a-zA-Z0-9_-]+)?\r?\n([\s\S]*?)```/g, (_, lang, code) => {
    const cleaned = code
      .replace(/\r\n/g, '\n')
      .replace(/[ \t]+$/gm, '')
      .replace(/^\s*\n+/, '')
      .replace(/\n+\s*$/g, '');

    // Mermaid diagrams: render as diagram instead of code block
    if (lang && lang.toLowerCase() === 'mermaid') {
      const mermaidId = 'mermaid-' + Date.now() + '-' + mermaidBlocks.length;
      const raw = cleaned.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
      const placeholder = `___MERMAID_BLOCK_${mermaidBlocks.length}___`;
      mermaidBlocks.push(`<div class="mermaid-container"><pre class="mermaid" id="${mermaidId}">${escapeHtml(raw)}</pre></div>`);
      return placeholder;
    }

    const escaped = cleaned.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
    const placeholder = `___CODE_BLOCK_${codeBlocks.length}___`;

    const langClass = lang ? ` class="language-${lang}"` : '';
    const runnableLangs = ['python','py','javascript','js','html','bash','sh','shell','zsh'];
    const runBtn = (lang && runnableLangs.includes(lang.toLowerCase()))
      ? `<button type="button" class="run-code" data-code="${escapeHtml(escaped)}" data-lang="${lang}" title="Run code"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg></button>`
      : '';
    const editBtn = `<button type="button" class="edit-code" title="Edit"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button>`;
    codeBlocks.push(`<pre><code${langClass} data-lang="${lang || ''}">${escapeHtml(escaped)}</code>${runBtn}${editBtn}<button type="button" class="copy-code" data-code="${escapeHtml(escaped)}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button></pre>`);

    return placeholder;
  });

  // Extract inline code spans before the link/autolink/HTML passes, mirroring
  // the fenced-block handling above. A URL inside `inline code` (e.g.
  // `irm http://127.0.0.1:3000/x`) is preceded by a space, so the bare-URL
  // autolink matches it, wraps it in an <a> tag, and swaps that for an
  // ___ALLOWED_HTML_ placeholder — corrupting the command. The old inline-code
  // pass ran after those passes, too late to protect it.
  s = s.replace(/`([^`]+?)`/g, (match, code) => {
    if (code.startsWith('___CODE_BLOCK_') || code.startsWith('___MERMAID_BLOCK_')) return match;
    const placeholder = `___INLINE_CODE_${inlineCodeBlocks.length}___`;
    inlineCodeBlocks.push(`<code>${escapeHtml(code)}</code>`);
    return placeholder;
  });

  // Repair common ways the agent mangles the entity-anchor convention
  // (`[Name](#kind-<id>)`). Models reliably get the single-link case
  // right but slip into other formats when listing many in a table.
  // These regexes upgrade the broken forms to proper markdown links so
  // the standard `[text](url)` handler below picks them up.
  const ANCHOR_KIND = '(?:session|document|note|image|email|event|task|skill|research)';
  // Case A: `[Name] [#kind-id]` — agent put the URL in brackets, often
  // in a table cell next to the label. Pair them.
  s = s.replace(
    new RegExp(`\\[([^\\]\\n]+?)\\]\\s*\\[#(${ANCHOR_KIND}-[A-Za-z0-9_-]+)\\]`, 'g'),
    '[$1](#$2)',
  );
  // Case B: bare `[#kind-id]` with no preceding label — give it a
  // generic "→ open" link text so it still renders as a button.
  s = s.replace(
    new RegExp(`\\[#(${ANCHOR_KIND}-[A-Za-z0-9_-]+)\\]`, 'g'),
    '[→ open](#$1)',
  );
  // Case C: bare `#kind-id` in plain text — only when it's word-
  // boundary delimited and NOT already inside a markdown link or
  // anchor syntax. Use a lookbehind for `](` or `[` to skip those.
  s = s.replace(
    new RegExp(`(^|[^\\[(])#(${ANCHOR_KIND}-[A-Za-z0-9_-]+)\\b`, 'g'),
    '$1[#$2](#$2)',
  );
  // Legacy search_chats output used bare session hashes (`#<uuid>`). Upgrade
  // those too so old answers and model summaries remain clickable.
  s = s.replace(
    /(^|[^\[(])#([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/gi,
    '$1[#session-$2](#session-$2)',
  );

  // Generated image markdown should open the same in-app preview as image
  // bubbles, not navigate to the raw file in a new tab. Normal images should
  // render as standard image elements.
  s = s.replace(/!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)/g, (match, alt, url, title) => {
    return generatedImageLinkHtml(alt, url, title);
  });

  // Convert markdown links [text](url) to clickable links
  // Internal #hash links navigate in-page; external links open in new tab
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, text, url) => {
    return linkHtml(text, url);
  });

  // Autolink bare URLs (http/https). Skips URLs already inside <a> tags
  // (placed by markdown link replacement above) and URLs in backticks.
  s = s.replace(
    /(^|[\s(<])(https?:\/\/[^\s<>"'`\]]+[^\s<>"'`\].,;:!?])/g,
    (match, prefix, url) => `${prefix}${linkHtml(url, url)}`
  );

  // Autolink scheme-less domains the model often emits as plain text
  // (e.g. "techcrunch.com/ai", "perplexity.ai", "www.wired.com"). The TLD
  // allowlist keeps it from matching file names / versions ("package.json",
  // "node.js", "v1.2.3"); the required start/[\s(<] prefix means domains
  // already inside an http link (preceded by "//") or an email ("@") are
  // skipped. Require the TLD to end at a real domain boundary so dotted code
  // identifiers like `sklearn.metrics` do not link `sklearn.me` and leave
  // placeholder fragments in the remaining text.
  s = s.replace(
    /(^|[\s(<])((?:www\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|ai|co|dev|app|gov|edu|news|info|tech|xyz|me)(?=$|[\/\s<>"'`\]).,;:!?])(?:\/[^\s<>"'`\])]*)?)/gi,
    (match, prefix, domain) => {
      const trail = (domain.match(/[.,;:!?)]+$/) || [''])[0];
      const core = trail ? domain.slice(0, -trail.length) : domain;
      return `${prefix}${linkHtml(core, 'https://' + core)}${trail}`;
    }
  );

  // Extract <details>...</details> blocks and replace with placeholders
  // Default to open so agent output is visible
  s = s.replace(/<details>([\s\S]*?)<\/details>/gi, (match) => {
    const placeholder = `___ALLOWED_HTML_${allowedHtmlBlocks.length}___`;
    allowedHtmlBlocks.push(sanitizeAllowedHtml(match.replace(/<details>/i, '<details open>')));
    return placeholder;
  });

  // ALSO preserve <a>/<img> tags the same way (they're now in the HTML from
  // markdown conversion)
  s = s.replace(/<(?:a\s+[^>]*>.*?<\/a|img\s+[^>]*?)>/gi, (match) => {
    const placeholder = `___ALLOWED_HTML_${allowedHtmlBlocks.length}___`;
    allowedHtmlBlocks.push(sanitizeAllowedHtml(match));
    return placeholder;
  });

  // <br> — a line break inside a table cell. GFM tables can't hold block-level
  // markdown, so <br> is the ONLY way to break a line in a cell, and models use
  // it constantly for multi-item cells. Escaping it printed a literal "<br>"
  // through the middle of the text.
  //
  // It gets its OWN token rather than joining allowedHtmlBlocks: the table
  // converter bails on any block containing ___ALLOWED_HTML_ (block-level
  // markup like <details> would wreck row splitting), so reusing that prefix
  // stopped the surrounding table from rendering at all — worse than the
  // literal tag. This token is inline and safe inside a cell.
  //
  // Deliberately matched with no attributes: `<br onload=x>` does not match and
  // still gets escaped, and the emitted tag is a fresh literal rather than the
  // model's text, so nothing from the model reaches the DOM.
  s = s.replace(/<br\s*\/?>/gi, () => LINE_BREAK_TOKEN);

  // Now escape everything else
  s = s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  s = s.replace(/\n{3,}/g, '\n\n');

  // KaTeX math rendering (after code blocks are extracted, so math in code is safe)
  const mathBlocks = [];
  if (window.katex) {
    // Display math: \[ ... \]  — GPT-style delimiter (gpt-5.x, Claude, etc.).
    // Handle before $$/$ so all common delimiters render.
    s = s.replace(/\\\[([\s\S]*?)\\\]/g, (match, math) => {
      try {
        const raw = math.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
        const placeholder = `___MATH_BLOCK_${mathBlocks.length}___`;
        mathBlocks.push(renderKatexToString(raw, { displayMode: true }));
        return placeholder;
      } catch (e) { return match; }
    });
    // Inline math: \( ... \)  — GPT-style inline delimiter. Single-line only
    // ([^\n]) so a stray escaped paren in prose can't swallow across lines.
    s = s.replace(/\\\(([^\n]*?)\\\)/g, (match, math) => {
      try {
        const raw = math.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
        const placeholder = `___MATH_BLOCK_${mathBlocks.length}___`;
        mathBlocks.push(renderKatexToString(raw, { displayMode: false }));
        return placeholder;
      } catch (e) { return match; }
    });
    // Display math: $$...$$
    s = s.replace(/\$\$([\s\S]*?)\$\$/g, (match, math) => {
      try {
        const raw = math.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
        const placeholder = `___MATH_BLOCK_${mathBlocks.length}___`;
        mathBlocks.push(renderKatexToString(raw, { displayMode: true }));
        return placeholder;
      } catch (e) { return match; }
    });
    // Inline math: $...$  (not preceded/followed by $ or digit, not spanning multiple lines)
    // Two unrelated currency mentions on one line ("~$96.35 ... ($110...") were
    // being paired as a $...$ math span, swallowing the whole sentence between
    // them (markdown syntax and all) into KaTeX — which ignores markdown and
    // collapses whitespace between plain-text tokens, so it rendered as
    // "96.35**asofJuly13—recoveringfromtheMaycrash(**110" in italic math font.
    // Real inline math is short and symbolic; three-plus consecutive English
    // words inside the span means it's prose, not math — leave it as literal
    // text (the dollar signs render as-is, which is what the user wrote).
    const PROSE_NOT_MATH_RE = /[A-Za-z]{2,}(?:\s+[A-Za-z]{2,}){2,}/;
    // The 3-word rule misses SHORT swallowed spans. Two more currency tells,
    // both observed live ("$110.18** ✅ (already crossed $110)" and "$95 to $110"):
    //   1. Markdown emphasis (**, __) can never appear in KaTeX, so its presence
    //      means a currency $ got mis-paired and ate real prose.
    //   2. The span opens with a money amount whose next token is a WORD, not a
    //      math operator — "$95 to$" pairs "95 to". Genuine math after a number
    //      carries an operator ("$2 + 2 = 4$") or no space ("$3x$"), so those are
    //      left untouched.
    const HAS_MD_EMPHASIS_RE = /\*\*|__/;
    const CURRENCY_THEN_WORD_RE = /^\s*\d[\d,]*(?:\.\d+)?(?:\s+[A-Za-z(]|\s*$)/;
    s = s.replace(/(?<!\$)\$(?!\$)([^\$\n]+?)\$(?!\$)/g, (match, math) => {
      if (PROSE_NOT_MATH_RE.test(math)) return match;
      if (HAS_MD_EMPHASIS_RE.test(math)) return match;
      if (CURRENCY_THEN_WORD_RE.test(math)) return match;
      try {
        const raw = math.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
        const placeholder = `___MATH_BLOCK_${mathBlocks.length}___`;
        mathBlocks.push(renderKatexToString(raw, { displayMode: false }));
        return placeholder;
      } catch (e) { return match; }
    });
    // ── Bare LaTeX outside math delimiters ──────────────────────────────────────────
    // Models sometimes emit LaTeX in prose without $...$ wrappers. Catch the
    // unambiguous patterns here so they still render through KaTeX.

    // \begin{env}...\end{env} → display math (align, cases, matrix, gather...)
    s = s.replace(/\\begin\{(\w+\*?)\}([\s\S]*?)\\end\{\1\}/g, (match) => {
      try {
        const raw = match.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
        const placeholder = `___MATH_BLOCK_${mathBlocks.length}___`;
        mathBlocks.push(renderKatexToString(raw, { displayMode: true }));
        return placeholder;
      } catch (e) { return match; }
    });

    // Bare two-arg commands: \frac{...}{...}, \binom{...}{...}, etc.
    // Brace-content regex handles up to 2 levels of nesting.
    const _B = '(?:[^{}]|[{](?:[^{}]|[{][^{}]*[}])*[}])*';
    const _twoArgRe = new RegExp(
      '\\\\(?:frac|dfrac|tfrac|cfrac|binom|dbinom|tbinom|overset|underset)' +
      '[{](' + _B + ')[}][{](' + _B + ')[}]', 'g'
    );
    s = s.replace(_twoArgRe, (match) => {
      try {
        const raw = match.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
        const placeholder = `___MATH_BLOCK_${mathBlocks.length}___`;
        mathBlocks.push(renderKatexToString(raw, { displayMode: false }));
        return placeholder;
      } catch (e) { return match; }
    });

    // Bare one-arg commands: \sqrt{...}, \vec{...}, \mathbb{...}, etc.
    // Also handles optional arg: \sqrt[3]{8}
    const _oneArgRe = new RegExp(
      '\\\\(?:sqrt|boxed|vec|hat|bar|dot|ddot|tilde|widetilde|widehat' +
      '|overline|underline|overbrace|underbrace|overrightarrow|overleftarrow' +
      '|mathbf|mathit|mathrm|mathbb|mathcal|mathsf|mathfrak|mathscr|boldsymbol' +
      '|cancel|bcancel|xcancel|operatorname)' +
      '(?:\\[[^\\]]*\\])?' +
      '[{](' + _B + ')[}]', 'g'
    );
    s = s.replace(_oneArgRe, (match) => {
      try {
        const raw = match.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
        const placeholder = `___MATH_BLOCK_${mathBlocks.length}___`;
        mathBlocks.push(renderKatexToString(raw, { displayMode: false }));
        return placeholder;
      } catch (e) { return match; }
    });

    // Unicode vulgar fractions → KaTeX inline math (½ → \frac{1}{2} etc.)
    s = replaceOutsideInlineCode(s, FRACTION_CHARS_RE, (ch) => {
      try {
        const placeholder = `___MATH_BLOCK_${mathBlocks.length}___`;
        mathBlocks.push(renderKatexToString(UNICODE_FRACTIONS[ch], { displayMode: false }));
        return placeholder;
      } catch (e) { return ch; }
    });
  }
  s = replaceOutsideInlineCode(s, CDOT_COMMAND_RE, '&centerdot;');

  // Handle pipe tables
  s = s.replace(/(?:^|\n)([^\n]*\|[^\n]*\|[^\n]*)(?:\n([^\n]*\|[^\n]*\|[^\n]*))*/g, (table) => {
    if (table.includes('___CODE_BLOCK_') || table.includes('___ALLOWED_HTML_')) return table;

    const rows = table.trim().split('\n');
    if (rows.length < 2) return table;

    let html = '<table style="border-collapse: collapse; width: 100%; margin: 10px 0;">';

    rows.forEach((row, idx) => {
      if (idx === 1 && /^[\s|:\-]+$/.test(row)) {
        html += '<tbody>';
        return;
      }
      const cells = splitTableRow(row);
      if (cells.length === 0) return;

      html += '<tr>';

      cells.forEach(cell => {
        const tag = idx === 0 ? 'th' : 'td';
        html += `<${tag} style="padding: 8px; text-align: left; border-bottom: 1px solid var(--border);">${cell.trim()}</${tag}>`;
      });

      html += '</tr>';
    });

    html += '</tbody></table>';
    return html;
  });

  // Horizontal rules (must come before bold/italic to avoid * conflicts)
  s = s.replace(/^(?:---|\*\*\*|___)\s*$/gm, '<hr>');

  // Bold, italic, strikethrough. Keep emphasis on one line and require the
  // authored marker to touch its content. A stray `*` must not pair with a
  // marker several paragraphs later and italicize the entire section.
  s = s
    .replace(/(?<!\*)\*\*(?![\s*])([^*\n]*?\S)\*\*(?!\*)/g, '<strong>$1</strong>')
    .replace(/(?<!\*)\*(?![\s*])([^*\n]*?\S)\*(?!\*)/g, '<em>$1</em>');
  s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');

  // Headers
  s = s.replace(/^###### (.*)$/gm, '<h6>$1</h6>')
       .replace(/^##### (.*)$/gm, '<h5>$1</h5>')
       .replace(/^#### (.*)$/gm, '<h4>$1</h4>')
       .replace(/^### (.*)$/gm, '<h3>$1</h3>')
       .replace(/^## (.*)$/gm, '<h2>$1</h2>')
       .replace(/^# (.*)$/gm, '<h1>$1</h1>');

  // Ordered lists (1. 2. 3. etc.). Up to 3 leading spaces is still the same
  // list level per CommonMark — models routinely indent continuation items.
  // Keep the authored number: a list that gets interrupted (by a nested
  // bullet block, a paragraph, …) resumes at the right value via `start`
  // instead of silently restarting at 1.
  s = s.replace(/^ {0,3}(\d+)\. (.*)$/gm, '<oli data-n="$1">$2</oli>');
  // NB: blank-line-separated items are deliberately NOT merged into one <ol>.
  // Merging would make a cut's safety depend on text that hasn't streamed in
  // yet ("- a\n\n-" looks like a safe boundary and gets frozen; one character
  // later "- " turns it into a list item that should have merged), breaking
  // the streaming segmenter's freeze invariant. Carrying the authored number
  // on each list instead is purely local, so it stays stream-safe.
  s = s.replace(/(?:^|\n)(<oli\b[\s\S]*?)(?=\n(?!<oli\b)|$)/g, m => {
    const first = (m.match(/<oli data-n="(\d+)"/) || [])[1];
    const startAttr = first && first !== '1' ? ` start="${first}"` : '';
    return `<ol${startAttr}>${m.trim()
      .replace(/<oli data-n="\d+">/g, '<li>')
      .replace(/<\/oli>/g, '</li>')}</ol>`;
  });

  // GitHub-style task lists (- [ ] / - [x]) → checkbox items. Must run before
  // the generic unordered-list rule so the "- " prefix isn't consumed first.
  // Emits <uli> (with a class) so the unordered-list wrapper below treats it
  // as a list item. Used by plan mode: plan + progress render as a checklist.
  s = s.replace(/^ {0,3}(?:- |\* )\[([ xX])\] (.*)$/gm, (_m, mark, text) => {
    const done = mark.toLowerCase() === 'x';
    return `<uli class="task-item${done ? ' task-done' : ''}"><span class="task-check" aria-hidden="true"></span><span class="task-text">${text}</span></uli>`;
  });

  // Unordered lists. <uli> may carry attributes (task-item class), so the
  // wrapper preserves them when converting <uli ...> → <li ...>.
  // Same ≤3-space allowance: an indented "- " sub-bullet was falling through
  // to the paragraph rule and rendering as literal "- text" (verified live).
  s = s.replace(/^ {0,3}(?:- |\* )(.*)$/gm, '<uli>$1</uli>');
  s = s.replace(/(^|\n)((?:<uli\b[^>]*>[^\n]*<\/uli>(?:\n|$))+)/g, (_, prefix, block) =>
    `${prefix}<ul>${block.trim().replace(/<uli\b([^>]*)>/g, '<li$1>').replace(/<\/uli>/g, '</li>')}</ul>`);

  // Blockquotes
  s = s.replace(/^&gt; (.*)$/gm, '<bq>$1</bq>');
  s = s.replace(/(?:^|\n)(<bq>[\s\S]*?)(?=\n(?!<bq>)|$)/g, m =>
    `<blockquote>${m.trim().replace(/<\/?bq>/g, (t) => t === '<bq>' ? '<p>' : '</p>')}</blockquote>`);

  // Paragraphs - but NOT for code block placeholders or allowed HTML
  // <ol/<ul (no closing bracket) so tags carrying attributes — <ol start="4">,
  // <oli data-n="2"> — are still recognized as list markup, not paragraphs.
  s = s.replace(/^(?!<h\d|<ul|<ol|<li|<\/li>|<pre>|<blockquote>|<bq>|<hr>|___CODE_BLOCK_|___ALLOWED_HTML_|___MATH_BLOCK_|___MERMAID_BLOCK_)([^\n]+)$/gm, '<p>$1</p>');

  // Line breaks within paragraphs
  s = s.replace(/<p>([\s\S]*?)<\/p>/g, (match, content) => {
    if (content.includes('___CODE_BLOCK_') || content.includes('___ALLOWED_HTML_') || content.includes('___MATH_BLOCK_') || content.includes('___MERMAID_BLOCK_')) return match;
    const withLineBreaks = content.replace(/\n{2,}/g, '</p><p>').replace(/\n/g, '<br>');
    return `<p>${withLineBreaks}</p>`;
  });

  // Remove empty paragraphs
  s = s.replace(/<p><\/p>/g, '');

  // CRITICAL: Restore allowed HTML blocks first
  allowedHtmlBlocks.forEach((block, index) => {
    s = s.replace(`___ALLOWED_HTML_${index}___`, block);
  });

  // Restore <br>. All occurrences are identical, so one global pass.
  if (s.includes(LINE_BREAK_TOKEN)) {
    s = s.split(LINE_BREAK_TOKEN).join('<br>');
  }

  // Restore math blocks
  mathBlocks.forEach((block, index) => {
    s = s.replace(`___MATH_BLOCK_${index}___`, block);
  });

  // Restore mermaid diagram blocks
  mermaidBlocks.forEach((block, index) => {
    s = s.replace(`___MERMAID_BLOCK_${index}___`, block);
  });

  // CRITICAL: Restore code blocks at the end
  codeBlocks.forEach((block, index) => {
    s = s.replace(`___CODE_BLOCK_${index}___`, block);
  });

  // Restore inline code spans last, so placeholders carried inside restored
  // <a>/allowed-HTML blocks are resolved too. The function replacer keeps the
  // escaped code literal — e.g. a shell snippet like `echo $1` is not treated
  // as a regex back-reference.
  inlineCodeBlocks.forEach((block, index) => {
    s = s.replace(`___INLINE_CODE_${index}___`, () => block);
  });

  const processed = repairKatexCommandErrors(s);
  return _useSvgEmoji() ? svgifyEmoji(processed, opts) : processed;
}

/**
 * Reduce excessive whitespace outside of code blocks
 */
export function squashOutsideCode(s) {
  if (!s) return "";
  const parts = String(s).split(/```/);
  for (let i = 0; i < parts.length; i += 2) {
    parts[i] = parts[i]
      .replace(/\r\n/g, '\n')
      .replace(/[ \t]+\n/g, '\n')
      .replace(/\n{3,}/g, '\n\n');
  }
  return parts.join('```');
}

/**
 * Render content that may be text or array of content blocks
 */
export function renderContent(content) {
  if (Array.isArray(content)) {
    const texts = [];
    for (const blk of content) {
      if (blk.type === 'text') texts.push(blk.text);
      else if (blk.type === 'image_url') texts.push('[image]');
    }
    return texts.join('\n');
  }
  return content;
}

/**
 * Initialize any unprocessed Mermaid diagrams in a container (or whole document)
 */
export function renderMermaid(container) {
  const target = container || document;
  // Check for work BEFORE touching the library: mermaid is 3.5 MB and most
  // sessions never contain a diagram, so the common path must cost nothing.
  if (target.querySelectorAll('pre.mermaid:not([data-processed])').length === 0) return;
  loadMermaid().then(() => {
    initMermaid();
    // Re-query: the load is async, so the set may have changed meanwhile.
    const nodes = target.querySelectorAll('pre.mermaid:not([data-processed])');
    if (nodes.length === 0) return;
    try {
      window.mermaid.run({ nodes });
    } catch (e) {
      console.warn('Mermaid render error:', e);
    }
  }).catch(e => console.warn('Mermaid load failed:', e));
}

const markdownModule = {
  escapeHtml,
  mdToHtml,
  sanitizeAllowedHtml,
  squashOutsideCode,
  renderContent,
  processWithThinking,
  createCollapsible,
  hasUnclosedThinkTag,
  extractThinkingBlocks,
  normalizeThinkingMarkup,
  startsWithReasoningPrefix,
  renderMermaid,
  repairCdotCommandsInDom
};

export default markdownModule;

// Mermaid is fetched ON DEMAND (see renderMermaid). It used to ship as an
// eager <script async> in index.html, but at 3.5 MB its parse/compile alone
// measured 4546 ms with transferSize 0 (i.e. pure main-thread work, already
// cached) and DOMContentLoaded landed at 4693 ms — mermaid effectively WAS
// the startup cost, and on a phone that is far worse. Nothing renders a
// diagram until a `pre.mermaid` block actually exists, so the vast majority
// of sessions should never pay for it at all.
const MERMAID_SRC = 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js';
let _mermaidLoader = null;

function loadMermaid() {
  if (window.mermaid) return Promise.resolve(window.mermaid);
  if (_mermaidLoader) return _mermaidLoader;
  _mermaidLoader = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = MERMAID_SRC;   // cdn.jsdelivr.net is already allowed by script-src
    s.async = true;
    s.addEventListener('load', () => resolve(window.mermaid), { once: true });
    s.addEventListener('error', () => {
      _mermaidLoader = null;  // let a later diagram retry
      reject(new Error('mermaid failed to load'));
    }, { once: true });
    document.head.appendChild(s);
  });
  return _mermaidLoader;
}

function initMermaid() {
  if (!window.mermaid || window.__odysseusMermaidReady) return;
  window.mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'loose' });
  window.__odysseusMermaidReady = true;
}
// Kept for any caller that still pokes it; safe no-op until the lib is loaded.
window.odysseusInitMermaid = initMermaid;

function _repairCdotTextNode(node) {
  if (!node || node.nodeType !== 3) return;
  const value = node.nodeValue || '';
  CDOT_COMMAND_RE.lastIndex = 0;
  if (!CDOT_COMMAND_RE.test(value)) return;
  CDOT_COMMAND_RE.lastIndex = 0;
  const parent = node.parentElement;
  if (parent?.closest?.('pre, code, textarea, input')) return;
  const repaired = value.replace(CDOT_COMMAND_RE, '\u00b7');
  const katexError = parent?.classList?.contains?.('katex-error') ? parent : parent?.closest?.('.katex-error');
  if (katexError && CDOT_COMMAND_ONLY_RE.test(value.trim()) && document.createTextNode && katexError.replaceWith) {
    katexError.replaceWith(document.createTextNode(repaired));
    return;
  }
  node.nodeValue = repaired;
}

export function repairCdotCommandsInDom(root = document.body) {
  if (!root || typeof document === 'undefined') return;
  if (root.nodeType === 3) {
    _repairCdotTextNode(root);
    return;
  }
  if (!root.querySelectorAll || !document.createTreeWalker) return;
  const showText = typeof NodeFilter !== 'undefined' ? NodeFilter.SHOW_TEXT : 4;
  const walker = document.createTreeWalker(root, showText);
  while (walker.nextNode()) _repairCdotTextNode(walker.currentNode);
}

(function _watchCdotCommands() {
  if (typeof document === 'undefined' || typeof window === 'undefined' || window._cdotCommandWatcherWired) return;
  window._cdotCommandWatcherWired = true;
  const start = () => {
    const root = document.getElementById?.('chat-history') || document.body;
    if (!root) return;
    repairCdotCommandsInDom(root);
    if (typeof MutationObserver === 'undefined') return;
    new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.type === 'characterData') {
          repairCdotCommandsInDom(mutation.target);
          continue;
        }
        for (const node of mutation.addedNodes) repairCdotCommandsInDom(node);
      }
    }).observe(root, { childList: true, subtree: true, characterData: true });
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();

// Persist which thinking sections were expanded across page refreshes.
// IDs are render-generated (Date.now-based) so we key by a stable hash of
// the inner text content instead — same content reproduces the same hash on
// reload. LocalStorage holds a Set of expanded hashes; we observe the chat
// history and re-expand matching sections as they're inserted.
const THINK_EXPANDED_KEY = 'odysseus-thinking-expanded';
function _loadExpandedSet() {
  try { return new Set(JSON.parse(localStorage.getItem(THINK_EXPANDED_KEY) || '[]')); }
  catch { return new Set(); }
}
function _saveExpandedSet(set) {
  try {
    const arr = [...set];
    // Bound storage growth — keep the most recent 200 entries.
    if (arr.length > 200) arr.splice(0, arr.length - 200);
    localStorage.setItem(THINK_EXPANDED_KEY, JSON.stringify(arr));
  } catch {}
}
function _hashThinkingContent(el) {
  if (!el) return '';
  const text = (el.textContent || '').trim();
  if (!text) return '';
  let h = 0;
  for (let i = 0; i < text.length; i++) {
    h = (h * 31 + text.charCodeAt(i)) | 0;
  }
  return String(h);
}
function _setThinkingExpanded(content, toggle, header, expanded) {
  if (!content || !toggle) return;
  content.classList.toggle('expanded', expanded);
  toggle.classList.toggle('expanded', expanded);
  const label_el = header?.querySelector('.thinking-header-left span');
  if (label_el) {
    const label = label_el.dataset.label || 'thinking process';
    label_el.textContent = expanded ? `Hide ${label}` : `View ${label}`;
  }
}

// Delegated click handler for thinking toggle (CSP-safe, no inline onclick)
document.addEventListener('click', function(e) {
  const header = e.target.closest('.thinking-header[data-thinking-id]');
  if (!header) return;
  const id = header.dataset.thinkingId;
  const content = document.getElementById(id);
  const toggle = document.getElementById(id + '-toggle');
  if (!content || !toggle) return;

  const willExpand = !content.classList.contains('expanded');
  _setThinkingExpanded(content, toggle, header, willExpand);

  // Persist by content hash so the choice survives a refresh.
  const hash = _hashThinkingContent(content);
  if (!hash) return;
  const set = _loadExpandedSet();
  if (willExpand) set.add(hash);
  else set.delete(hash);
  _saveExpandedSet(set);
});

// Watch the chat history; whenever a thinking section appears, expand it if
// its hash matches one the user previously expanded.
(function _watchThinking() {
  if (window._thinkingWatcherWired) return;
  window._thinkingWatcherWired = true;
  const _apply = (root) => {
    if (!root || !root.querySelectorAll) return;
    const sections = root.matches?.('.thinking-section')
      ? [root]
      : [...root.querySelectorAll('.thinking-section')];
    if (!sections.length) return;
    const set = _loadExpandedSet();
    if (!set.size) return;
    for (const sec of sections) {
      const content = sec.querySelector('.thinking-content');
      if (!content) continue;
      if (content.classList.contains('expanded')) continue;
      const hash = _hashThinkingContent(content);
      if (!hash || !set.has(hash)) continue;
      const header = sec.querySelector('.thinking-header[data-thinking-id]');
      const id = header?.dataset.thinkingId;
      const toggle = id ? document.getElementById(id + '-toggle') : null;
      _setThinkingExpanded(content, toggle, header, true);
    }
  };
  const start = () => {
    const root = document.body;
    if (!root) return;
    _apply(root);
    new MutationObserver((mutations) => {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (node.nodeType === 1) _apply(node);
        }
      }
    }).observe(root, { childList: true, subtree: true });
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();

function _endpointNameFromUrl(url) {
  try {
    const parsed = new URL(url, window.location.origin);
    return parsed.host || parsed.hostname || 'Model endpoint';
  } catch (_) {
    return 'Model endpoint';
  }
}

function _appendEndpointAddButtons(root) {
  if (!root || !root.querySelectorAll) return;
  const anchors = root.matches?.('a[href]')
    ? [root]
    : [...root.querySelectorAll('a[href]')];
  for (const anchor of anchors) {
    if (anchor.dataset.endpointAddChecked === '1') continue;
    anchor.dataset.endpointAddChecked = '1';
    const href = anchor.getAttribute('href') || '';
    if (!_isModelEndpointUrl(href)) continue;
    if (anchor.nextElementSibling?.classList?.contains('model-endpoint-add-btn')) continue;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'model-endpoint-add-btn';
    btn.dataset.endpointUrl = new URL(href, window.location.origin).href.replace(/\/+$/, '');
    btn.title = 'Add this OpenAI-compatible endpoint to the model picker';
    btn.innerHTML = '<span aria-hidden="true">+</span><span>Add to model picker</span>';
    anchor.insertAdjacentElement('afterend', btn);
  }
}

async function _registerEndpointFromButton(btn) {
  const baseUrl = String(btn?.dataset?.endpointUrl || '').trim();
  if (!baseUrl || !_isModelEndpointUrl(baseUrl)) return;
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span aria-hidden="true">...</span><span>Adding</span>';
  try {
    const existingRes = await fetch('/api/model-endpoints', { credentials: 'same-origin' });
    if (existingRes.ok) {
      const endpoints = await existingRes.json();
      const existing = Array.isArray(endpoints)
        ? endpoints.find((ep) => String(ep.base_url || '').replace(/\/+$/, '') === baseUrl)
        : null;
      if (existing) {
        btn.classList.add('added');
        btn.innerHTML = '<span aria-hidden="true">✓</span><span>Already added</span>';
        window.dispatchEvent(new CustomEvent('ge:model-endpoints-updated', { detail: { baseUrl } }));
        if (window.modelsModule?.refreshModels) window.modelsModule.refreshModels(true);
        if (window.sessionModule?.updateModelPicker) window.sessionModule.updateModelPicker();
        uiModule.showToast?.(`Already in model picker: ${existing.name || _endpointNameFromUrl(baseUrl)}`);
        return;
      }
    }

    const parsed = new URL(baseUrl, window.location.origin);
    const fd = new FormData();
    fd.append('base_url', baseUrl);
    fd.append('name', _endpointNameFromUrl(baseUrl));
    fd.append('model_type', 'llm');
    fd.append('endpoint_kind', 'auto');
    fd.append('skip_probe', 'true');
    if (/^(localhost|127\.0\.0\.1|0\.0\.0\.0)$/i.test(parsed.hostname)) {
      fd.append('container_local', 'true');
    }
    const res = await fetch('/api/model-endpoints', {
      method: 'POST',
      credentials: 'same-origin',
      body: fd,
    });
    if (!res.ok) {
      const body = await res.text().catch(() => '');
      throw new Error(`HTTP ${res.status}${body ? ': ' + body.slice(0, 160) : ''}`);
    }
    btn.classList.add('added');
    btn.innerHTML = '<span aria-hidden="true">✓</span><span>Added</span>';
    window.dispatchEvent(new CustomEvent('ge:model-endpoints-updated', { detail: { baseUrl } }));
    if (window.modelsModule?.refreshModels) await window.modelsModule.refreshModels(true);
    if (window.sessionModule?.updateModelPicker) window.sessionModule.updateModelPicker();
    uiModule.showToast?.(`Model endpoint added: ${_endpointNameFromUrl(baseUrl)}`);
  } catch (err) {
    btn.disabled = false;
    btn.innerHTML = original;
    uiModule.showError?.(`Add endpoint failed: ${err.message || err}`);
  }
}

(function _watchModelEndpointLinks() {
  if (window._modelEndpointLinkWatcherWired) return;
  window._modelEndpointLinkWatcherWired = true;

  document.addEventListener('click', (e) => {
    const btn = e.target.closest?.('.model-endpoint-add-btn');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    _registerEndpointFromButton(btn);
  });

  const start = () => {
    const root = document.body;
    if (!root) return;
    _appendEndpointAddButtons(root);
    new MutationObserver((mutations) => {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (node.nodeType === 1) _appendEndpointAddButtons(node);
        }
      }
    }).observe(root, { childList: true, subtree: true });
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
