// A deliberately small markdown subset, built node by node.
//
// Every node here is created with document.createElement and filled with
// textContent. There is no innerHTML in this module and there must never be:
// the input is language-model output that was itself shaped by whatever the
// model read out of a tool result.
//
// LINKS AND IMAGES ARE NOT SUPPORTED, on purpose. `[text](url)` renders as its
// literal characters. Model prose is influenced by data the model read, so an
// assistant-authored clickable URL is an injection surface this panel does not
// open. A `file` block is no exception: it is model-emittable too, so its card
// links only same-origin URLs and shows anything else as text (see blocks.js).
//
// Raw HTML is literal text for the same reason.

const MAX_INPUT = 100000;
// A pathological single line (a minified blob, a base64 payload) is emitted as
// plain text: the inline scanner is linear per line, and nothing readable is
// lost by not looking for **bold** inside four thousand characters.
const MAX_INLINE_LINE = 4000;
const HEADING_RE = /^(#{1,6})\s+(.*)$/;
const BULLET_RE = /^(\s*)([-*])\s+(.*)$/;
const ORDERED_RE = /^(\s*)(\d{1,3})[.)]\s+(.*)$/;
const RULE_RE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const QUOTE_RE = /^\s*>\s?(.*)$/;
const FENCE_RE = /^\s*```(.*)$/;

function inline(text, parent) {
  if (text.length > MAX_INLINE_LINE) { parent.append(document.createTextNode(text)); return; }
  // Code spans first: nothing inside a code span is emphasis.
  const parts = text.split('`');
  parts.forEach((part, index) => {
    if (index % 2 === 1) {
      const code = document.createElement('code');
      code.textContent = part;
      parent.append(code);
      return;
    }
    emphasis(part, parent);
  });
}

function emphasis(text, parent) {
  const pattern = /(\*\*|__)(.+?)\1|(\*|_)([^*_]+?)\3/;
  let rest = text;
  for (let guard = 0; guard < 500; guard += 1) {
    const match = pattern.exec(rest);
    if (!match) break;
    if (match.index > 0) parent.append(document.createTextNode(rest.slice(0, match.index)));
    const strong = Boolean(match[1]);
    const node = document.createElement(strong ? 'strong' : 'em');
    node.textContent = strong ? match[2] : match[4];
    parent.append(node);
    rest = rest.slice(match.index + match[0].length);
  }
  if (rest) parent.append(document.createTextNode(rest));
}

function flushParagraph(lines, fragment) {
  if (!lines.length) return;
  const paragraph = document.createElement('p');
  lines.forEach((line, index) => {
    if (index) paragraph.append(document.createElement('br'));
    inline(line, paragraph);
  });
  fragment.append(paragraph);
  lines.length = 0;
}

function listItem(text) {
  const item = document.createElement('li');
  inline(text, item);
  return item;
}

export function renderMarkdown(text) {
  const fragment = document.createDocumentFragment();
  let source = typeof text === 'string' ? text : String(text ?? '');
  let truncated = false;
  if (source.length > MAX_INPUT) { source = source.slice(0, MAX_INPUT); truncated = true; }

  const lines = source.split('\n');
  const paragraph = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    const fence = FENCE_RE.exec(line);
    if (fence) {
      flushParagraph(paragraph, fragment);
      const body = [];
      index += 1;
      while (index < lines.length && !FENCE_RE.test(lines[index])) { body.push(lines[index]); index += 1; }
      index += 1;
      const pre = document.createElement('pre');
      const code = document.createElement('code');
      code.textContent = body.join('\n');
      pre.append(code);
      fragment.append(pre);
      continue;
    }
    if (!line.trim()) { flushParagraph(paragraph, fragment); index += 1; continue; }
    if (RULE_RE.test(line)) {
      flushParagraph(paragraph, fragment);
      fragment.append(document.createElement('hr'));
      index += 1;
      continue;
    }
    const heading = HEADING_RE.exec(line);
    if (heading) {
      flushParagraph(paragraph, fragment);
      // Clamped to h4-h6 so a panel heading never outranks the page <h1>.
      const node = document.createElement(`h${Math.min(6, heading[1].length + 3)}`);
      inline(heading[2], node);
      fragment.append(node);
      index += 1;
      continue;
    }
    const quote = QUOTE_RE.exec(line);
    if (quote) {
      flushParagraph(paragraph, fragment);
      const block = document.createElement('blockquote');
      const body = [quote[1]];
      index += 1;
      while (index < lines.length && QUOTE_RE.test(lines[index])) {
        body.push(QUOTE_RE.exec(lines[index])[1]);
        index += 1;
      }
      body.forEach((entry, position) => {
        if (position) block.append(document.createElement('br'));
        inline(entry, block);
      });
      fragment.append(block);
      continue;
    }
    const bullet = BULLET_RE.exec(line);
    const ordered = ORDERED_RE.exec(line);
    if (bullet || ordered) {
      flushParagraph(paragraph, fragment);
      const root = document.createElement(bullet ? 'ul' : 'ol');
      let nested = null;
      while (index < lines.length) {
        const current = BULLET_RE.exec(lines[index]) || ORDERED_RE.exec(lines[index]);
        if (!current) break;
        // Two levels only: deeper indentation folds into the second level
        // rather than growing an arbitrarily deep tree from model output.
        const depth = current[1].length >= 2 ? 1 : 0;
        if (depth && !nested) {
          nested = document.createElement(BULLET_RE.test(lines[index]) ? 'ul' : 'ol');
          (root.lastElementChild || root).append(nested);
        }
        if (!depth) nested = null;
        (depth && nested ? nested : root).append(listItem(current[3]));
        index += 1;
      }
      fragment.append(root);
      continue;
    }
    paragraph.push(line);
    index += 1;
  }
  flushParagraph(paragraph, fragment);

  if (truncated) {
    const note = document.createElement('p');
    note.className = 'assistant-truncated';
    note.textContent = 'This response was truncated for display.';
    fragment.append(note);
  }
  return fragment;
}
