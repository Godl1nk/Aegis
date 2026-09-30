import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

test('image failure restores the model picker and permits a successful retry', async () => {
  class Element {
    constructor() { this.children = []; this.handlers = {}; this.value = ''; }
    appendChild(child) { this.children.push(child); child.parent = this; return child; }
    setAttribute() {}
    addEventListener(name, handler) { this.handlers[name] = handler; }
    get isConnected() { return this === box || !!this.parent?.isConnected; }
    querySelectorAll(selector) {
      return this.children.flatMap(child => [
        ...(child.className?.split(' ').includes(selector.slice(1)) ? [child] : []),
        ...child.querySelectorAll(selector),
      ]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    replaceWith(next) {
      const parent = this.parent;
      parent.children[parent.children.indexOf(this)] = next;
      next.parent = parent;
      this.parent = null;
    }
    remove() {
      if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this);
      this.parent = null;
    }
  }
  const box = new Element();
  const requests = [];
  const source = readFileSync(new URL('../static/js/chatRenderer.js', import.meta.url), 'utf8');
  const start = source.indexOf('export function renderImageChoiceCard(');
  const end = source.indexOf('export function renderAskUserCard(', start);
  const context = vm.createContext({
    document: { getElementById: () => box, createElement: () => new Element() },
    window: { sessionModule: { getCurrentSessionId: () => 'test-session' }, dispatchEvent() {} },
    uiModule: {}, TextDecoder, CustomEvent: class {},
    buildImageBubble: () => { const image = new Element(); image.className = 'image-result'; return image; },
    fetch: async (_url, options) => {
      requests.push(JSON.parse(options.body));
      const data = requests.length === 1 ? { error: 'Backend returned 404' } : { image_url: '/api/generated-image/test.png' };
      let done = false;
      return { ok: true, body: { getReader: () => ({ read: async () => {
        if (done) return { done: true };
        done = true;
        return { done: false, value: new TextEncoder().encode(`data: ${JSON.stringify(data)}\n\n`) };
      } }) } };
    },
  });
  vm.runInContext(source.slice(start, end).replace('export function', 'function'), context);
  const card = context.renderImageChoiceCard({ prompt: 'apple', options: [{ spec: 'Qwen-Image-2.1' }] });
  const select = card.querySelector('.image-choice-select');
  select.value = 'Qwen-Image-2.1';
  const go = card.querySelector('.image-choice-go');
  await go.handlers.click();
  assert.equal(card.isConnected, true);
  assert.equal(go.disabled, false);
  assert.equal(card.querySelector('.image-choice-error').hidden, false);
  assert.match(card.querySelector('.image-choice-error').textContent, /Backend returned 404/);
  await go.handlers.click();
  assert.equal(requests.length, 2);
  assert.equal(requests[1].model, 'Qwen-Image-2.1');
  assert.equal(box.querySelectorAll('.image-result').length, 1);
  assert.equal(card.isConnected, false);
});
