'use strict';
// Offline regression checks for the real frontend. No packages, network or user data.
// Run: node web/test_frontend.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'static', 'app.js'), 'utf8');
const chartSource = fs.readFileSync(path.join(__dirname, 'static', 'research-charts.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, 'static', 'index.html'), 'utf8');
const clone = value => structuredClone(value);
const decode = value => value.replace(/&(?:amp|lt|gt|quot|#39);/g, token => ({
  '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'"
}[token]));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};

// A deliberately small DOM: only the browser operations the frontend uses.
// Attribute values are decoded without interpreting escaped markup as elements.
class Element {
  constructor(tagName, document) {
    this.tagName = tagName.toLowerCase();
    this.ownerDocument = document;
    this.attributes = new Map();
    // Browser dataset writes also create attributes. Delegated closest() must
    // therefore see data-* attributes set at runtime on ancestor elements.
    this.dataset = new Proxy({}, {
      set: (target, name, value) => {
        target[name] = String(value);
        this.attributes.set('data-' + String(name).replace(/[A-Z]/g, letter => '-' + letter.toLowerCase()), String(value));
        return true;
      },
      deleteProperty: (target, name) => {
        delete target[name];
        this.attributes.delete('data-' + String(name).replace(/[A-Z]/g, letter => '-' + letter.toLowerCase()));
        return true;
      }
    });
    this.style = {};
    this.children = [];
    this.parentElement = null;
    this.listeners = {};
    this._text = '';
    this._html = '';
    this._className = '';
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.open = false;
    this.files = [];
    this.classList = {
      contains: name => this.className.split(/\s+/).includes(name),
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/), ...names])].filter(Boolean).join(' '); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter(x => !names.includes(x)).join(' '); },
      toggle: (name, force) => {
        const enabled = force === undefined ? !this.classList.contains(name) : force;
        if (enabled) this.classList.add(name); else this.classList.remove(name);
        return enabled;
      }
    };
  }
  get id() { return this.getAttribute('id') || ''; }
  get className() { return this._className; }
  set className(value) { this._className = String(value); this.attributes.set('class', this._className); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.clearChildren(); this._html = ''; }
  get innerHTML() { return this._html; }
  set innerHTML(value) {
    this._html = String(value);
    this._text = '';
    this.clearChildren();
    parseMarkup(this._html, this);
  }
  get scrollHeight() { return this.children.length; }
  contains(element) { for(let node=element;node;node=node.parentElement)if(node===this)return true;return false; }
  get isConnected() {
    let element = this;
    while (element.parentElement) element = element.parentElement;
    return element === this.ownerDocument.root;
  }
  setAttribute(name, value) {
    value = String(value);
    this.attributes.set(name, value);
    if (name === 'class') this._className = value;
    if (name === 'value') this.value = value;
    if (name === 'checked') this.checked = true;
    if (name === 'hidden') this.hidden = true;
    if (name === 'disabled') this.disabled = true;
    if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, x) => x.toUpperCase())] = value;
  }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  clearChildren() { for (const child of this.children) child.parentElement = null; this.children = []; }
  append(...elements) { for (const element of elements) { element.remove(); element.parentElement = this; this.children.push(element); } }
  appendChild(element) { this.append(element); return element; }
  insertBefore(element, reference) {
    if (reference === null) { this.append(element); return element; }
    if (reference.parentElement !== this) throw new Error('Reference is not a child');
    element.remove(); element.parentElement = this;
    this.children.splice(this.children.indexOf(reference),0,element); return element;
  }
  prepend(element) { element.remove(); element.parentElement = this; this.children.unshift(element); }
  replaceChildren(...elements) { this.clearChildren(); this.append(...elements); }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(x => x !== this);
    this.parentElement = null;
  }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  querySelectorAll(selector) {
    const matches = [];
    const visit = element => { for (const child of element.children) { if (matchesSelector(child, selector)) matches.push(child); visit(child); } };
    visit(this);
    return matches;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    let element = this;
    while (element) { if (matchesSelector(element, selector)) return element; element = element.parentElement; }
    return null;
  }
  get elements() {
    return {namedItem: name => this.querySelectorAll('input').concat(this.querySelectorAll('textarea'), this.querySelectorAll('select')).find(x => x.getAttribute('name') === name) || null};
  }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() { this.ownerDocument.activeElement = this; }
}

function matchesSimple(element, selector) {
  const checked = selector.endsWith(':checked');
  if (checked) selector = selector.slice(0, -8);
  if (checked && !element.checked) return false;
  if (selector.startsWith('#')) return element.id === selector.slice(1);
  if (selector.startsWith('.')) return element.classList.contains(selector.slice(1));
  const attribute = /^\[([^=\]]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(selector);
  if (attribute) return element.attributes.has(attribute[1]) && (attribute[2] === undefined || element.getAttribute(attribute[1]) === attribute[2]);
  return element.tagName === selector.toLowerCase();
}
function matchesSelector(element, selector) {
  const parts = selector.trim().split(/\s+/);
  if (!matchesSimple(element, parts.pop())) return false;
  for (const part of parts.reverse()) {
    element = element.parentElement;
    while (element && !matchesSimple(element, part)) element = element.parentElement;
    if (!element) return false;
  }
  return true;
}
function parseMarkup(markup, root) {
  const voidTags = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);
  const stack = [root];
  const tags = /<!--[^]*?-->|<![^>]*>|<\/?([a-zA-Z][\w:-]*)([^<>]*?)>/g;
  let match, previous = 0;
  while ((match = tags.exec(markup))) {
    stack.at(-1)._text += decode(markup.slice(previous, match.index));
    previous = tags.lastIndex;
    if (!match[1]) continue;
    const tag = match[1].toLowerCase();
    if (match[0].startsWith('</')) {
      for (let index = stack.length - 1; index > 0; --index) {
        if (stack[index].tagName === tag) { stack.length = index; break; }
      }
      continue;
    }
    const element = new Element(tag, root.ownerDocument);
    const attributes = /([^\s=/'">]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s'"=<>`]+)))?/g;
    let attribute;
    while ((attribute = attributes.exec(match[2]))) element.setAttribute(attribute[1], decode(attribute[2] ?? attribute[3] ?? attribute[4] ?? ''));
    stack.at(-1).append(element);
    if (!voidTags.has(tag) && !match[0].endsWith('/>')) stack.push(element);
  }
  stack.at(-1)._text += decode(markup.slice(previous));
  for (const element of root.querySelectorAll('textarea')) element.value = element.textContent;
  for (const element of root.querySelectorAll('select')) {
    const options = element.querySelectorAll('option');
    const selected = options.find(x => x.attributes.has('selected')) || options[0];
    element.value = selected?.getAttribute('value') ?? selected?.textContent ?? '';
  }
}
class Document {
  constructor(markup) {
    this.listeners = {};
    this.root = new Element('document', this);
    this.root.innerHTML = markup;
    this.body = this.root.querySelector('body');
  }
  querySelector(selector) { return this.root.querySelector(selector); }
  querySelectorAll(selector) { return this.root.querySelectorAll(selector); }
  createElement(tag) { return new Element(tag, this); }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  async dispatch(type, target, extra = {}) {
    const event = {type, target, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }, ...extra};
    const chain = [];
    for (let element = target; element; element = element.parentElement) chain.push(element);
    for (const element of chain) {
      event.currentTarget = element;
      if (typeof element['on' + type] === 'function') await element['on' + type](event);
      for (const listener of element.listeners[type] || []) await listener(event);
      if (event.stopped) return;
    }
    event.currentTarget = this;
    for (const listener of this.listeners[type] || []) await listener(event);
  }
}
class OfflineFormData {
  constructor(form) {
    this.values = [];
    if (form) for (const element of form.querySelectorAll('input').concat(form.querySelectorAll('textarea'), form.querySelectorAll('select'))) {
      const name = element.getAttribute('name'), type = element.getAttribute('type');
      if (!name || element.disabled || (type === 'checkbox' && !element.checked)) continue;
      this.values.push([name, type === 'file' ? element.files[0] || '' : element.value]);
    }
  }
  append(name, value) { this.values.push([name, value]); }
  [Symbol.iterator]() { return this.values[Symbol.iterator](); }
}

function fixtureState() {
  return {
    active_industry: 'alpha', as_of: '2026-10-18', write_locked: false,
    industries: [
      {id: 'alpha', name: '甲地区', evidence_count: 7, review_count: 2, enabled: false, update_state: {}, scope: {specialty: 'forestry', primary_region: '甲地区', topics: ['政策']}},
      {id: 'national', name: '全国基础', evidence_count: 5, review_count: 1, enabled: false, update_state: {}, scope: {specialty: 'forestry', library_role: 'national-foundation', topics: ['政策']}},
      {id: 'beta', name: '乙地区', evidence_count: 11, review_count: 3, enabled: false, update_state: {}, scope: {primary_region: '乙地区', topics: ['项目']}}
    ],
    automation: {status: 'PAUSED', schedule: '每天09:00'},
    agent: {display_name: '合成测试模型', model: 'offline-fixture', provider_id: 'offline', protocol: 'chat_completions', base_url: 'http://127.0.0.1:9999/v1', request_limit: 4, timeout_seconds: 120, key_configured: false},
    network: {provider: 'ddgs', backend: 'bing'}, knowledge: {save_conversations: false},
    material_kinds: {policy: '政策'}, material_states: {unknown: '待核验'}
  };
}
function material(changes = {}) {
  return {id: 'material-a', industry_id: 'alpha', title: '合成资料', kind: 'policy', kind_label: '政策', region: '甲地区', status: 'unknown', status_label: '待核验', model_allowed: false, has_attachment: false, published_date: null, updated_at: '2026-10-17', ...changes};
}
function evidence(changes = {}) {
  return {id: 'evidence-a', industry_id: 'alpha', title: '合成证据', category: '政策', region: '甲地区', status: 'reported', publisher: '合成来源', summary: '仅用于离线测试', checked_date: '2026-10-18', published_date: null, event_date: null, freshness: 'current', freshness_label: '未到复核期限', tags: [], url: 'https://example.org/source', ...changes};
}

async function harness(evaluatedSource = source) {
  const document = new Document(html), calls = [], streams = [], errors = [], timers = new Map();
  const state = fixtureState();
  const h = {document, calls, streams, errors, timers, state, handler: null};
  const fallback = request => {
    if (request.path === '/api/state') return clone(state);
    if (request.path.startsWith('/api/sources/')) return {mode: 'preferred', sources: []};
    if (['/api/materials', '/api/evidence', '/api/reports', '/api/memories', '/api/conversations', '/api/crawls'].includes(request.path)) return [];
    throw new Error('Unmocked request: ' + request.method + ' ' + request.url);
  };
  class OfflineEventSource {
    constructor(url) { this.url = url; this.listeners = {}; this.closed = false; streams.push(this); }
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
    close() { this.closed = true; }
    async emit(event) { for (const listener of this.listeners.update || []) await listener({data: JSON.stringify(event)}); }
  }
  const context = vm.createContext({
    document, location: {origin: 'http://127.0.0.1:8765'}, URL, URLSearchParams,
    FormData: OfflineFormData, EventSource: OfflineEventSource, structuredClone,
    crypto: {randomUUID: () => '00000000-0000-4000-8000-000000000000'},
    console: {error: error => errors.push(error), log() {}},
    setTimeout: callback => { const id = timers.size + 1; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, init = {}) => {
      const parsed = new URL(url, 'http://127.0.0.1:8765');
      const request = {url, path: parsed.pathname, query: parsed.searchParams, method: init.method || 'GET', init, body: typeof init.body === 'string' ? JSON.parse(init.body) : init.body};
      calls.push(request);
      const value = await (h.handler ? h.handler(request, fallback) : fallback(request));
      return {ok: true, status: 200, json: async () => clone(value)};
    }
  });
  vm.runInContext(chartSource + '\n' + evaluatedSource + '\n;globalThis.__frontend = {S, render, renderSources, sourceDialog, materialDialog, sendAgent, refresh, esc, safeURL, markdown};', context, {filename: 'app.js'});
  h.app = context.__frontend;
  // Initial refresh is the production boot path; wait for its finite microtasks.
  for (let index = 0; index < 20; ++index) await Promise.resolve();
  assert.equal(errors.length, 0, 'frontend boot must not report an error');
  assert.equal(h.app.S.view, 'research', 'boot opens the research conversation');
  assert.equal(document.querySelector('#agent-panel').parentElement.id, 'research-agent-slot');
  return h;
}

const tests = [];
function test(name, run) { tests.push({name, run}); }
async function clickResearchTool(h, tool) {
  if (!h.app.S.toolsOpen) await openResearchTools(h);
  const button = h.document.querySelectorAll('[data-research-tool]').find(node => node.dataset.researchTool === tool);
  assert.ok(button, 'missing research tool: ' + tool);
  return h.document.dispatch('click', button);
}
async function openResearchTools(h) {
  if (!h.app.S.toolsOpen) await h.document.dispatch('click', h.document.querySelector('#research-tools-toggle'));
}
async function closeResearchTools(h) {
  if (h.app.S.toolsOpen) await h.document.dispatch('click', h.document.querySelector('#close-research-tools'));
}
function assertNoMarkupInjection(container) {
  assert.equal(container.querySelector('img'), null, 'untrusted text must not produce an image element');
  assert.equal(container.querySelector('script'), null, 'untrusted text must not produce a script element');
  const visit = element => {
    for (const name of element.attributes.keys()) assert.ok(!/^on/i.test(name), 'untrusted text must not add an event attribute: ' + name);
    element.children.forEach(visit);
  };
  visit(container);
}

test('DOM moves detach the old parent and replaced children become disconnected', async () => {
  const document = new Document('<body><div id="one"><div id="moving"></div></div><div id="two"></div></body>');
  const one = document.querySelector('#one'), two = document.querySelector('#two'), moving = document.querySelector('#moving');
  two.append(moving);
  assert.equal(one.children.length, 0);
  assert.equal(two.children[0], moving);
  assert.equal(document.querySelectorAll('#moving').length, 1);
  two.innerHTML = '<span>replacement</span>';
  assert.equal(moving.parentElement, null);
  assert.equal(moving.isConnected, false);
});

test('three primary destinations and all startup bindings exist once', async () => {
  const h = await harness();
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size, ids.length, 'static HTML must not contain duplicate IDs');
  const bound = [...source.matchAll(/^\$\('#([^']+)'\)\.(?:addEventListener|onclick)/gm)].map(match => match[1]);
  for (const id of new Set([...bound, 'content', 'server-date', 'model-name', 'dialog', 'dialog-title', 'dialog-body', 'toast', 'research-mode', 'source-changes', 'chat-scope', 'agent-panel', 'agent-parking'])) {
    assert.ok(ids.includes(id), 'missing static initialization element: ' + id);
  }
  const primary = h.document.querySelector('.topbar').querySelectorAll('[data-workspace]').map(node => node.dataset.workspace);
  assert.deepEqual(primary.sort(), ['library', 'research', 'settings']);
  assert.equal(h.app.S.view, 'research');
  assert.equal(h.document.querySelector('#research-mode').checked, false);
  assert.equal(h.document.querySelector('#source-changes').checked, false);
  assert.ok(h.calls.every(call => call.url.startsWith('/api/')));
});

test('default research keeps tools closed without requesting hidden tools', async () => {
  const h = await harness();
  assert.equal(h.app.S.toolsOpen, false);
  const toggle = h.document.querySelector('#research-tools-toggle');
  assert.equal(toggle.getAttribute('aria-controls'), 'research-tools-drawer');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(h.document.querySelector('#research-tools-drawer').hidden, true);
  assert.equal(h.document.querySelector('.research-workspace').classList.contains('tools-open'), false);
  assert.equal(h.calls.filter(call => ['/api/crawls', '/api/reports', '/api/network/search'].includes(call.path)).length, 0);
  assert.equal(h.document.querySelector('#web-search-form'), null);
});

test('drawer toggle preserves search draft and does not interrupt the live Agent', async () => {
  const h = await harness();
  h.handler = (request, fallback) => request.path === '/api/agent/jobs' ? {id: 'drawer-agent', conversation: 'drawer-conversation'} : fallback(request);
  const panel = h.document.querySelector('#agent-panel'), input = h.document.querySelector('#chat-input');
  input.value = '收起工具期间继续研究';
  await h.app.sendAgent();
  input.value = '尚未发送的助手草稿';
  await openResearchTools(h);
  const form = h.document.querySelector('#web-search-form');
  form.elements.namedItem('query').value = '未提交的检索词';
  const toggle = h.document.querySelector('#research-tools-toggle');
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(h.document.querySelector('#research-tools-drawer').hidden, false);
  assert.equal(h.document.querySelector('.research-workspace').classList.contains('tools-open'), true);
  await closeResearchTools(h);
  assert.equal(h.app.S.toolsOpen, false);
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(h.document.activeElement, toggle);
  await h.streams[0].emit({type: 'tool', title: '工具收起时继续检索', detail: '仅合成事件'});
  await h.streams[0].emit({type: 'answer', text: '工具抽屉不影响研究回答'});
  assert.equal(h.streams[0].closed, false);
  assert.equal(h.app.S.job, 'drawer-agent');
  await openResearchTools(h);
  assert.equal(h.document.querySelector('#web-search-form'), form, 'same-scope reopening reuses unsubmitted form DOM');
  assert.equal(form.elements.namedItem('query').value, '未提交的检索词');
  assert.equal(h.document.querySelector('#agent-panel'), panel);
  assert.equal(h.document.querySelector('#chat-input'), input);
  assert.equal(input.value, '尚未发送的助手草稿');
  assert.match(h.document.querySelector('#chat-log').textContent, /工具抽屉不影响研究回答/);
  assert.equal(h.document.querySelectorAll('#agent-panel').length, 1);
  assert.equal(h.streams.length, 1);
  await h.streams[0].emit({type: 'done', archived_ids: []});
  assert.equal(h.errors.length, 0);
});

test('Escape preserves crawl draft, respects modal dialogs, and stops/resumes polling', async () => {
  const h = await harness();
  h.handler = (request, fallback) => request.path === '/api/crawls' ? [{id: 'running-fixture', status: 'running', items: [], counts: {}, started_at: '2026-10-18', attempted: 0, max_pages: 10, max_depth: 1, remaining: 0}] : fallback(request);
  await clickResearchTool(h, 'crawl');
  const form = h.document.querySelector('#crawl-form');
  form.elements.namedItem('urls').value = 'https://example.org/unsubmitted';
  form.elements.namedItem('max_pages').value = '17';
  form.elements.namedItem('max_depth').value = '2';
  assert.ok(h.timers.size > 0, 'running crawl starts a polling timer');
  const modal = h.document.querySelector('#dialog');
  modal.showModal();
  await h.document.dispatch('keydown', h.document.body, {key: 'Escape'});
  assert.equal(h.app.S.toolsOpen, true, 'modal Escape must not close the underlying drawer');
  modal.close();
  await h.document.dispatch('keydown', form.elements.namedItem('urls'), {key: 'Escape'});
  assert.equal(h.app.S.toolsOpen, false);
  assert.equal(h.document.activeElement, h.document.querySelector('#research-tools-toggle'));
  assert.equal(h.timers.size, 0, 'closing drawer removes the crawl polling timer');
  await openResearchTools(h);
  assert.equal(h.document.querySelector('#crawl-form'), form);
  assert.equal(form.elements.namedItem('urls').value, 'https://example.org/unsubmitted');
  assert.equal(form.elements.namedItem('max_pages').value, '17');
  assert.equal(form.elements.namedItem('max_depth').value, '2');
  assert.ok(h.timers.size > 0, 'reopening a running crawl resumes polling');
  assert.equal(h.errors.length, 0);
});

test('search completing while closed updates only hidden tools and is visible on reopen', async () => {
  const h = await harness(), pending = deferred();
  h.handler = (request, fallback) => request.path === '/api/network/search' ? pending.promise : fallback(request);
  await openResearchTools(h);
  const panel = h.document.querySelector('#agent-panel'), log = h.document.querySelector('#chat-log');
  const input = h.document.querySelector('#chat-input');
  input.value = '后台搜索期间的助手草稿';
  const form = h.document.querySelector('#web-search-form');
  form.elements.namedItem('query').value = 'FIRST';
  const search = h.document.dispatch('submit', form);
  await closeResearchTools(h);
  pending.resolve({query: 'FIRST', backend: 'offline-fixture', items: [{title: 'CLOSED-SEARCH-RESULT', snippet: '合成线索', url: 'https://example.org/closed'}]});
  await search;
  assert.equal(h.app.S.toolsOpen, false);
  assert.equal(h.document.querySelector('#research-tools-drawer').hidden, true);
  assert.equal(h.document.querySelector('#research-tools-toggle').getAttribute('aria-expanded'), 'false');
  assert.match(h.document.querySelector('#research-tool-content').textContent, /CLOSED-SEARCH-RESULT/);
  const completedForm = h.document.querySelector('#web-search-form');
  await openResearchTools(h);
  assert.equal(h.document.querySelector('#web-search-form'), completedForm);
  assert.match(h.document.querySelector('#research-tool-content').textContent, /CLOSED-SEARCH-RESULT/);
  assert.equal(h.calls.filter(call => call.path === '/api/network/search').length, 1);
  assert.equal(h.document.querySelector('#agent-panel'), panel);
  assert.equal(h.document.querySelector('#chat-log'), log);
  assert.equal(input.value, '后台搜索期间的助手草稿');
  assert.equal(h.errors.length, 0);
});

test('output arriving while closed does not reopen drawer or reload on same-context reopen', async () => {
  const h = await harness(), pending = deferred();
  h.handler = (request, fallback) => request.path === '/api/reports' ? pending.promise : fallback(request);
  h.app.S.researchTool = 'outputs';
  await h.app.render();
  const opening = openResearchTools(h);
  await closeResearchTools(h);
  pending.resolve([{title: 'CLOSED-OUTPUT', path: 'reports/fixture.md'}]);
  await opening;
  assert.equal(h.app.S.toolsOpen, false);
  assert.equal(h.document.querySelector('#research-tools-drawer').hidden, true);
  assert.match(h.document.querySelector('#research-tool-content').textContent, /CLOSED-OUTPUT/);
  const before = h.document.querySelector('#research-tool-content').innerHTML;
  await openResearchTools(h);
  assert.equal(h.document.querySelector('#research-tool-content').innerHTML, before);
  assert.equal(h.calls.filter(call => call.path === '/api/reports').length, 1);
});

test('changing industry invalidates closed drawer context instead of reusing old crawl form', async () => {
  const h = await harness();
  await clickResearchTool(h, 'crawl');
  const oldForm = h.document.querySelector('#crawl-form');
  oldForm.elements.namedItem('urls').value = 'https://example.org/alpha-only';
  await closeResearchTools(h);
  const selector = h.document.querySelector('#industry-select');
  selector.value = 'beta';
  await h.document.dispatch('change', selector);
  const callsBefore = h.calls.filter(call => call.path === '/api/crawls').length;
  await openResearchTools(h);
  const form = h.document.querySelector('#crawl-form');
  assert.notEqual(form, oldForm);
  assert.equal(oldForm.isConnected, false);
  assert.equal(form.elements.namedItem('urls').value, '');
  const crawlCalls = h.calls.filter(call => call.path === '/api/crawls');
  assert.equal(crawlCalls.length, callsBefore + 1);
  assert.equal(crawlCalls.at(-1).query.get('industry'), 'beta');
  assert.equal(h.errors.length, 0);
});

test('materials show returned local/shared entries and escape titles/IDs', async () => {
  const h = await harness();
  h.app.S.view = 'materials';
  const payload = '\"><img src=x onerror=alert(1)> &';
  h.handler = (request, fallback) => request.path === '/api/materials' ? [material({id: payload, title: payload, has_attachment: true}), material({id: 'm2', industry_id: 'national'})] : fallback(request);
  await h.app.render();
  const content = h.document.querySelector('#content');
  const buttons = content.querySelectorAll('[data-action]').filter(node => node.dataset.action === 'view-material');
  assert.equal(buttons.length, 2, 'one detail action per API material');
  assert.equal(buttons[0].dataset.id, payload);
  assert.equal(buttons[0].dataset.industry, 'alpha');
  assert.equal(buttons[1].dataset.industry, 'national');
  assert.ok(content.innerHTML.includes(h.app.esc(payload)));
  assertNoMarkupInjection(content);
  const count = h.document.querySelector('#nav-count');
  if (count) assert.equal(String(count.textContent), '12', 'shared national evidence is included once');
  assert.equal(h.calls.at(-1).query.get('industry'), 'alpha');
});

test('empty materials show an empty state and retain import action', async () => {
  const h = await harness();
  h.app.S.view = 'materials';
  await h.app.render();
  const content = h.document.querySelector('#content');
  assert.ok(content.querySelector('.empty'), 'an empty API result has a visible empty state');
  assert.equal(content.querySelectorAll('[data-action]').filter(node => node.dataset.action === 'view-material').length, 0);
  assert.ok(content.querySelectorAll('[data-action]').some(node => node.dataset.action === 'add-material'));
});

async function assertDelegatedMaterialClick(h) {
  h.app.S.view = 'materials';
  const record = material({id: 'click-detail', title: '点击后应打开的合成资料', content: '合成原文，不访问真实资料。'});
  h.handler = (request, fallback) => {
    if (request.path === '/api/materials') return [record];
    if (request.path === '/api/materials/alpha/click-detail') return record;
    return fallback(request);
  };
  await h.app.render();
  const button = h.document.querySelector('#content').querySelectorAll('[data-action]').find(node => node.dataset.action === 'view-material');
  const target = h.document.createElement('span');
  target.textContent = '合成嵌套点击目标';
  button.append(target);
  assert.equal(target.closest('[data-action]'), button);
  assert.equal(target.closest('body'), h.document.body);
  const navigationAncestor = target.closest('[data-view]');
  await h.document.dispatch('click', target);
  assert.equal(h.calls.filter(call => call.path === '/api/materials/alpha/click-detail').length, 1, 'delegated material action must request its detail exactly once');
  assert.equal(h.document.querySelector('#dialog').open, true);
  assert.equal(h.document.querySelector('#dialog-title').textContent, record.title);
  assert.match(h.document.querySelector('#dialog-body').textContent, /合成原文/);
  assert.equal(h.errors.length, 0);
  return navigationAncestor;
}

test('nested material action is not intercepted by navigation ancestors', async () => {
  assert.equal(await assertDelegatedMaterialClick(await harness()), null);
});

test('mutation control detects body data-view intercepting delegated actions', async () => {
  const legacySource = source.replace(/async function render\(\)\s*\{/, 'async function render(){\n document.body.dataset.view=S.view;');
  assert.notEqual(legacySource, source);
  const h = await harness(legacySource);
  assert.equal(h.document.body.getAttribute('data-view'), 'research');
  await assert.rejects(() => assertDelegatedMaterialClick(h), /delegated material action must request its detail exactly once/);
  assert.equal(h.document.querySelector('#dialog').open, false);
});

function figure(kind='timeline') {
  const source={kind:'evidence',id:'test-contract',industry_id:'alpha',title:'合成来源',url:'https://example.org/original',checked_date:'2026-10-18',excerpt:'2025年3月4日公开披露，仅为合成测试。'};
  const point={label:'第一阶段',date:'2025-03-04',date_kind:'event',value:null,status:'reported',summary:'阶段说明',quote:'',sources:[source]};
  if(kind==='timeline')return {kind,title:'问题发展时间线',description:'当前依据支持的阶段',items:[point,{...point,label:'待补日期',date:null,date_kind:'unknown',status:'uncertain'}],coverage_note:'仅依据当前引用，不代表完整覆盖'};
  return {kind,title:'年度面积变化',metric:'面积',unit:'公顷',geography:'测试地区',scope:'示范林',basis:'年度实际完成',nature:'actual',items:[{...point,label:'2023',date:'2023',value:0},{...point,label:'2024',date:'2024',value:null},{...point,label:'2025',date:'2025',value:12.5}]};
}

test('library has only materials and evidence; old timeline view returns to research', async () => {
  const h=await harness();h.app.S.view='materials';await h.app.render();
  const tabs=h.document.querySelector('.library-switch').querySelectorAll('button');
  assert.deepEqual(tabs.map(b=>b.textContent),['资料','知识证据']);
  assert.equal(h.document.querySelector('[data-view="timeline"]'),null);
  h.app.S.view='timeline';await h.app.render();
  assert.equal(h.app.S.view,'research');
  assert.equal(h.document.querySelector('#agent-panel').parentElement.id,'research-agent-slot');
});

test('sourced timeline streams once while parked, survives navigation and opens references', async () => {
  const h=await harness();h.handler=(request,fallback)=>request.path==='/api/agent/jobs'?{id:'figure-job',conversation:'figure-conversation'}:fallback(request);
  h.document.querySelector('#chat-input').value='画出这个问题的时间线';await h.app.sendAgent();
  h.app.S.view='materials';await h.app.render();
  const payload=figure();payload.title='\"><img src=x onerror=alert(1)>';payload.items[0].label='<script>坏标签</script>';
  await h.streams[0].emit({type:'chart',seq:4,chart:payload});
  await h.streams[0].emit({type:'chart',seq:4,chart:payload});
  await h.streams[0].emit({type:'answer',seq:5,text:'图中列出了已知与未知日期。'});
  await h.streams[0].emit({type:'done',seq:6,archived_ids:[]});
  assert.equal(h.document.querySelectorAll('.research-chart').length,1);
  h.app.S.view='research';await h.app.render();
  const card=h.document.querySelector('.research-chart');
  assert.equal(card.querySelectorAll('.chart-node').length,1,'unknown dates are not placed on the axis');
  assert.match(card.textContent,/日期未知节点/);
  assert.match(card.textContent,/事件日期/);
  assertNoMarkupInjection(card);
  await h.document.dispatch('click',card.querySelector('.chart-source-button'));
  assert.equal(h.document.querySelector('#dialog-title').textContent,payload.items[0].label);
  assert.match(h.document.querySelector('#dialog-body').textContent,/仅为合成测试/);
  assert.equal(h.document.querySelector('#dialog-body').querySelector('a').getAttribute('href'),'https://example.org/original');
  assert.equal(h.document.querySelector('#send-agent').disabled,false);
  assert.equal(h.errors.length,0);
});

test('trend graph preserves missing gaps and true zero; invalid numbers are not drawn', async () => {
  const h=await harness();h.handler=(request,fallback)=>request.path==='/api/agent/jobs'?{id:'trend-job',conversation:'trend-conversation'}:fallback(request);
  h.document.querySelector('#chat-input').value='画变化趋势';await h.app.sendAgent();
  await h.streams[0].emit({type:'chart',seq:1,chart:figure('line')});
  const graph=h.document.querySelector('.research-chart');
  assert.equal(graph.querySelectorAll('.chart-node').length,2);
  const path=graph.querySelector('.chart-series').getAttribute('d');
  assert.equal((path.match(/M/g)||[]).length,2,'a missing middle value must break the line');
  assert.ok(!path.includes(' L'),'do not connect across the missing value');
  assert.match(graph.textContent,/公顷/);assert.match(graph.textContent,/缺失/);
  assert.ok(!/NaN|Infinity/.test(graph.innerHTML));
  const invalid=figure('bar');invalid.items[0].value=NaN;
  await h.streams[0].emit({type:'chart',seq:2,chart:invalid});
  assert.equal(h.document.querySelectorAll('.research-chart').length,1);
  assert.match(h.document.querySelector('.research-figures').textContent,/数据不完整/);
  assert.equal(h.errors.length,0);
});

test('opt-in history renders charts and source details without executing markup', async () => {
  const h=await harness();
  const chart=figure();chart.items[0].sources[0].url='javascript:alert(1)';
  h.handler=(request,fallback)=>request.path==='/api/conversations'?[{id:'saved',turns:[{at:'2026-10-19',prompt:'历史问题',answer:'历史回答',charts:[chart]}]}]:fallback(request);
  h.app.S.view='settings';await h.app.render();
  await h.document.dispatch('click',h.document.querySelector('[data-action="view-conversation"]'));
  assert.equal(h.document.querySelector('#dialog-body').querySelectorAll('.research-chart').length,1);
  const point=h.document.querySelector('#dialog-body').querySelector('.chart-source-button');
  await h.document.dispatch('click',point);
  assert.equal(h.document.querySelector('#dialog-body').querySelector('a'),null,'unsafe source protocols must not become links');
  assertNoMarkupInjection(h.document.querySelector('#dialog-body'));
});

test('newer material query wins when old query completes last', async () => {
  const h = await harness(), pending = deferred();
  h.app.S.view = 'materials';
  h.handler = (request, fallback) => request.path === '/api/materials' ? (request.query.get('q') === 'FIRST' ? pending.promise : [material({title: 'SECOND-CURRENT'})]) : fallback(request);
  h.app.S.q = 'FIRST';
  const oldRender = h.app.render();
  h.app.S.q = 'SECOND';
  await h.app.render();
  pending.resolve([material({title: 'FIRST-STALE'})]);
  await oldRender;
  const content = h.document.querySelector('#content');
  assert.match(content.textContent, /SECOND-CURRENT/);
  assert.ok(!content.textContent.includes('FIRST-STALE'));
  assert.equal(h.document.querySelector('#library-search').elements.namedItem('q').value, 'SECOND');
});

test('late material response cannot show the previous industry', async () => {
  const h = await harness(), pending = deferred();
  h.app.S.view = 'materials';
  h.handler = (request, fallback) => request.path === '/api/materials' ? (request.query.get('industry') === 'alpha' ? pending.promise : [material({title: 'BETA-CURRENT', industry_id: 'beta'})]) : fallback(request);
  const oldRender = h.app.render();
  const selector = h.document.querySelector('#industry-select');
  selector.value = 'beta';
  await h.document.dispatch('change', selector);
  pending.resolve([material({title: 'ALPHA-STALE'})]);
  await oldRender;
  const content = h.document.querySelector('#content');
  assert.match(content.textContent, /BETA-CURRENT/);
  assert.ok(!content.textContent.includes('ALPHA-STALE'));
  assert.equal(h.app.S.industry, 'beta');
});

test('review is an evidence filter with review_only parameter, not a page', async () => {
  const h = await harness();
  h.app.S.view = 'evidence';
  await h.app.render();
  const checkbox = h.document.querySelector('#review-only');
  assert.ok(checkbox, 'review-only checkbox belongs to evidence');
  checkbox.checked = true;
  await h.document.dispatch('change', checkbox);
  assert.equal(h.app.S.reviewOnly, true);
  assert.equal(h.app.S.view, 'evidence');
  assert.equal(h.calls.filter(call => call.path === '/api/evidence').at(-1).query.get('review_only'), 'true');
  const updated = h.document.querySelector('#review-only');
  updated.checked = false;
  await h.document.dispatch('change', updated);
  assert.equal(h.calls.filter(call => call.path === '/api/evidence').at(-1).query.get('review_only'), 'false');
  assert.ok(!h.document.querySelectorAll('[data-view]').some(node => node.dataset.view === 'review'));
});

test('settings source responses stay in their group and active industry', async () => {
  const h = await harness(), pending = deferred();
  const beta = {mode: 'only', sources: [{id: 'beta-source', name: '乙来源', url: 'https://example.org/beta'}]};
  h.handler = (request, fallback) => request.path.startsWith('/api/sources/') ? (request.path.endsWith('/alpha') ? pending.promise : clone(beta)) : fallback(request);
  h.app.S.view = 'settings';
  const oldRender = h.app.render();
  const selector = h.document.querySelector('#industry-select');
  selector.value = 'beta';
  await h.document.dispatch('change', selector);
  pending.resolve({mode: 'preferred', sources: [{id: 'alpha-source', name: 'ALPHA-STALE', url: 'https://example.org/alpha'}]});
  await oldRender;
  assert.equal(h.app.S.sourceIndustry, 'beta');
  assert.equal(h.app.S.sources.sources[0].id, 'beta-source');
  assert.match(h.document.querySelector('#settings-sources').textContent, /乙来源/);
  assert.ok(!h.document.querySelector('#content').textContent.includes('ALPHA-STALE'));
  assert.ok(h.document.querySelector('#settings-memory'));
  assert.ok(h.document.querySelector('#settings-archives'));
});

test('Agent pending POST is single-flight and old industry conversation is not restored', async () => {
  const h = await harness(), pending = deferred();
  let started = 0;
  h.handler = (request, fallback) => {
    if (request.path === '/api/agent/jobs') return ++started === 1 ? pending.promise : {id: 'job-beta', conversation: 'conversation-beta'};
    return fallback(request);
  };
  h.app.S.view = 'research';
  await h.app.render();
  h.app.S.conversation = 'conversation-alpha-before';
  h.document.querySelector('#chat-input').value = '甲档案问题';
  const first = h.app.sendAgent();
  h.document.querySelector('#chat-input').value = '重复点击';
  await h.app.sendAgent();
  assert.equal(started, 1);
  assert.equal(h.app.S.sending, true);
  assert.equal(h.document.querySelector('#send-agent').disabled, true);
  const selector = h.document.querySelector('#industry-select');
  selector.value = 'beta';
  await h.document.dispatch('change', selector);
  pending.resolve({id: 'job-alpha', conversation: 'conversation-alpha-late'});
  await first;
  assert.equal(h.app.S.conversation, null);
  assert.equal(h.app.S.sending, false);
  assert.equal(h.streams[0].url, '/api/agent/jobs/job-alpha/events');
  await h.streams[0].emit({type: 'done', archived_ids: []});
  assert.equal(h.streams[0].closed, true);
  assert.equal(h.app.S.job, null);
  h.document.querySelector('#chat-input').value = '乙档案问题';
  await h.app.sendAgent();
  const posts = h.calls.filter(call => call.path === '/api/agent/jobs');
  assert.equal(posts.length, 2);
  assert.equal(posts[0].body.industry, 'alpha');
  assert.equal(posts[0].body.conversation, 'conversation-alpha-before');
  assert.equal(posts[1].body.industry, 'beta');
  assert.equal(posts[1].body.conversation, null);
  assert.equal(posts[1].body.research, false);
  assert.equal(posts[1].body.allow_source_changes, false);
  assert.equal(posts[1].init.headers['X-KB-Request'], '1');
});

test('one persistent Agent survives page changes, streamed events and refresh on completion', async () => {
  const h = await harness();
  h.handler = (request, fallback) => request.path === '/api/agent/jobs' ? {id: 'job-persistent', conversation: 'conversation-persistent'} : fallback(request);
  h.app.S.view = 'research';
  await h.app.render();
  const panel = h.document.querySelector('#agent-panel'), log = h.document.querySelector('#chat-log'), input = h.document.querySelector('#chat-input');
  assert.equal(panel.parentElement.id, 'research-agent-slot');
  input.value = '保留的合成问题';
  await h.app.sendAgent();
  input.value = '尚未发送的草稿';
  h.app.S.view = 'materials';
  await h.app.render();
  assert.equal(panel.parentElement.id, 'agent-parking');
  assert.equal(panel.isConnected, true);
  await h.streams[0].emit({type: 'tool', title: '本机检索', detail: '合成工具事件'});
  await h.streams[0].emit({type: 'answer', text: '只出现一次的合成回答'});
  await h.streams[0].emit({type: 'done', archived_ids: ['fixture-only-id']});
  assert.equal(h.app.S.view, 'materials', 'completion refresh must not navigate away');
  h.app.S.view = 'research';
  h.app.S.researchTool = 'outputs';
  await h.app.render();
  assert.equal(h.document.querySelector('#agent-panel'), panel);
  assert.equal(h.document.querySelector('#chat-log'), log);
  assert.equal(h.document.querySelector('#chat-input'), input);
  assert.equal(input.value, '尚未发送的草稿');
  assert.match(log.textContent, /保留的合成问题/);
  assert.match(log.textContent, /合成工具事件/);
  assert.equal(log.textContent.split('只出现一次的合成回答').length - 1, 1);
  assert.equal(h.streams.length, 1);
  assert.equal(h.document.querySelectorAll('#agent-panel').length, 1);
  assert.equal(h.document.querySelectorAll('#chat-log').length, 1);
  assert.equal(h.document.querySelector('#send-agent').disabled, false);
  assert.equal(h.document.querySelector('#stop-agent').hidden, true);
  assert.equal(h.errors.length, 0);
  await h.document.dispatch('click', h.document.querySelector('#new-chat'));
  assert.equal(h.app.S.conversation, null);
  assert.ok(!log.textContent.includes('保留的合成问题'));
});

test('late research output cannot overwrite a newly selected search tool', async () => {
  const h = await harness(), pending = deferred();
  h.handler = (request, fallback) => request.path === '/api/reports' ? pending.promise : fallback(request);
  h.app.S.view = 'research';
  h.app.S.researchTool = 'search';
  await h.app.render();
  await openResearchTools(h);
  const mainRender = h.app.S.renderSeq;
  const oldRender = clickResearchTool(h, 'outputs');
  await clickResearchTool(h, 'search');
  const expected = h.document.querySelector('#research-tool-content').innerHTML;
  pending.resolve([{title: 'STALE-REPORT', path: 'reports/fixture.md'}]);
  await oldRender;
  assert.equal(h.app.S.renderSeq, mainRender, 'tool switching must test local tickets without a full-page render');
  assert.equal(h.document.querySelector('#research-tool-content').innerHTML, expected);
  assert.ok(h.document.querySelector('#web-search-form'));
});

test('late web search does not replace a newer query after a tool round trip', async () => {
  const h = await harness(), pending = deferred();
  const results = title => ({query: title, backend: 'offline-fixture', items: [{title, snippet: '合成线索', url: 'https://example.org/source'}]});
  h.handler = (request, fallback) => {
    if (request.path === '/api/network/search') return request.body.query === 'FIRST' ? pending.promise : results('LATEST-SEARCH');
    return fallback(request);
  };
  h.app.S.view = 'research';
  h.app.S.researchTool = 'search';
  await h.app.render();
  await openResearchTools(h);
  const oldForm = h.document.querySelector('#web-search-form');
  oldForm.elements.namedItem('query').value = 'FIRST';
  const oldSearch = h.document.dispatch('submit', oldForm);
  const mainRender = h.app.S.renderSeq;
  await clickResearchTool(h, 'outputs');
  await clickResearchTool(h, 'search');
  const newForm = h.document.querySelector('#web-search-form');
  assert.notEqual(newForm, oldForm);
  assert.equal(oldForm.isConnected, false);
  newForm.elements.namedItem('query').value = 'SECOND';
  await h.document.dispatch('submit', newForm);
  pending.resolve(results('STALE-SEARCH'));
  await oldSearch;
  assert.equal(h.app.S.renderSeq, mainRender, 'search race is resolved by tool/request tickets, not page tickets');
  const content = h.document.querySelector('#research-tool-content');
  assert.match(content.textContent, /LATEST-SEARCH/);
  assert.ok(!content.textContent.includes('STALE-SEARCH'));
  assert.equal(h.errors.length, 0);
});

test('source dialog preserves opening industry and catalog after settings navigation', async () => {
  const h = await harness();
  const alpha = {mode: 'preferred', sources: [
    {id: 'alpha-source', name: '甲来源', url: 'https://example.org/alpha', type: 'primary', priority: 1, categories: ['政策'], enabled: false, notes: ''},
    {id: 'alpha-preserved', name: '甲保留来源', url: 'https://example.org/preserved', priority: 2, enabled: true}
  ]};
  const beta = {mode: 'only', sources: [{id: 'beta-source', name: '乙来源', url: 'https://example.org/beta', priority: 1, enabled: true}]};
  h.app.S.sources = clone(alpha);
  h.app.S.sourceIndustry = 'alpha';
  h.app.sourceDialog('alpha-source');
  const form = h.document.querySelector('#source-form');
  assert.equal(form.dataset.industry, 'alpha');
  assert.notEqual(form.sourceCatalog, h.app.S.sources);
  form.elements.namedItem('name').value = '甲来源已编辑';
  h.app.S.industry = 'beta';
  h.app.S.view = 'settings';
  h.app.S.sourceIndustry = 'beta';
  h.app.S.sources = clone(beta);
  h.handler = (request, fallback) => {
    if (request.method === 'PUT' && request.path === '/api/sources/alpha') return request.body;
    if (request.path === '/api/sources/beta') return beta;
    return fallback(request);
  };
  await h.document.dispatch('submit', form);
  assert.equal(h.errors.length, 0);
  const writes = h.calls.filter(call => call.method === 'PUT');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].path, '/api/sources/alpha');
  assert.equal(writes[0].body.mode, 'preferred');
  assert.deepEqual(writes[0].body.sources.map(item => item.id), ['alpha-source', 'alpha-preserved']);
  assert.equal(writes[0].body.sources[0].name, '甲来源已编辑');
  assert.equal(writes[0].body.sources[0].enabled, false);
  assert.equal(h.app.S.sources.sources[0].id, 'beta-source');
});

test('research report ignores hidden library query and preserves Agent draft/tree', async () => {
  const h = await harness();
  const reportPath = 'reports/fixture-only-generated.md';
  h.handler = (request, fallback) => {
    if (request.path === '/api/reports' && request.method === 'POST') return {path: reportPath};
    if (request.path === '/api/document') {
      assert.equal(request.query.get('path'), reportPath);
      return {path: reportPath, text: '合成研究简报正文，未读取真实资料。'};
    }
    return fallback(request);
  };
  h.app.S.q = '某关键词';
  h.app.S.industry = 'beta';
  h.app.S.view = 'research';
  h.app.S.researchTool = 'outputs';
  await h.app.render();
  await openResearchTools(h);
  const panel = h.document.querySelector('#agent-panel');
  const log = h.document.querySelector('#chat-log');
  const input = h.document.querySelector('#chat-input');
  input.value = '生成简报时保留的未发送草稿';
  const button = h.document.querySelector('#research-tool-content').querySelectorAll('[data-action]').find(node => node.dataset.action === 'report');
  assert.ok(button, 'report generation belongs to research outputs');
  await h.document.dispatch('click', button);
  const writes = h.calls.filter(call => call.path === '/api/reports' && call.method === 'POST');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].body.industry, 'beta');
  assert.equal(writes[0].body.query, '', 'the unseen library query must not silently filter research output');
  assert.equal(h.app.S.q, '某关键词', 'generating a report does not erase the library search');
  assert.equal(h.document.querySelector('#dialog').open, true);
  assert.match(h.document.querySelector('#dialog-body').textContent, /合成研究简报正文/);
  assert.equal(h.document.querySelector('#agent-panel'), panel);
  assert.equal(h.document.querySelector('#chat-log'), log);
  assert.equal(h.document.querySelector('#chat-input'), input);
  assert.equal(panel.parentElement.id, 'research-agent-slot');
  assert.equal(input.value, '生成简报时保留的未发送草稿');
  assert.equal(h.document.querySelectorAll('#agent-panel').length, 1);
  assert.equal(h.errors.length, 0);
});

test('material file save is single-flight and commits metadata with the file', async () => {
  const h = await harness(), pending = deferred();
  h.app.materialDialog();
  const form = h.document.querySelector('#material-form');
  form.elements.namedItem('title').value = '合成批复';
  form.elements.namedItem('file').files = [{name:'fixture.docx',size:400}];
  h.handler = (request, fallback) => request.path === '/api/materials/alpha/import' ? pending.promise : fallback(request);
  const first = h.document.dispatch('submit', form);
  for(let i=0;i<5;i++)await Promise.resolve();
  assert.equal(form.querySelector('[type="submit"]').disabled,true);
  await h.document.dispatch('submit',form);
  const writes=h.calls.filter(c=>c.method==='POST');
  assert.equal(writes.length,1,'double submit must not create another upload or metadata record');
  assert.equal(writes[0].path,'/api/materials/alpha/import');
  const metadata=JSON.parse(writes[0].body.values.find(([key])=>key==='metadata')[1]);
  assert.equal(metadata.title,'合成批复');
  assert.equal(metadata.model_allowed,false);
  assert.equal(metadata.id,undefined);
  assert.equal(form.dataset.id,'','no record ID before the combined request succeeds');
  pending.resolve({id:'imported-once'});await first;
  assert.equal(form.dataset.id,'imported-once');
  assert.equal(form.dataset.saving,'false');
  assert.equal(h.document.querySelector('#dialog').open,false);
  assert.equal(h.errors.length,0);
});

test('failed material upload preserves the draft and can be retried once', async () => {
  const h=await harness();h.app.materialDialog({id:'existing-v1',industry_id:'alpha',title:'旧版本'});
  const form=h.document.querySelector('#material-form');
  form.elements.namedItem('title').value='待保存修订';
  form.elements.namedItem('file').files=[{name:'bad.docx',size:20}];
  let failed=true;
  h.handler=(request,fallback)=>{
    if(request.path!=='/api/materials/alpha/import')return fallback(request);
    if(failed)throw new Error('DOCX结构无法读取');
    return {id:'existing-v1'};
  };
  await h.document.dispatch('submit',form);
  assert.equal(h.document.querySelector('#dialog').open,true);
  assert.equal(form.elements.namedItem('title').value,'待保存修订');
  assert.equal(form.dataset.id,'existing-v1');
  assert.equal(form.querySelector('[type="submit"]').disabled,false);
  assert.match(form.querySelector('.form-error').textContent,/DOCX/);
  assert.equal(h.calls.filter(c=>c.path==='/api/materials/alpha').length,0,'no metadata save before attachment validation');
  failed=false;await h.document.dispatch('submit',form);
  assert.equal(h.calls.filter(c=>c.path==='/api/materials/alpha/import').length,2);
  assert.equal(h.document.querySelector('#dialog').open,false);
});

test('indexed original is read-only while summaries and notes remain editable', async () => {
  const h=await harness();
  h.app.materialDialog({id:'indexed',industry_id:'alpha',title:'原文',content:'附件原文',extraction:{text_index:'text/test/index.json'}});
  let form=h.document.querySelector('#material-form');
  assert.notEqual(form.elements.namedItem('content').getAttribute('readonly'),null);
  assert.equal(form.elements.namedItem('summary').getAttribute('readonly'),null);
  assert.equal(form.elements.namedItem('notes').getAttribute('readonly'),null);
  assert.match(form.textContent,/重新上传附件/);
  h.app.materialDialog({title:'手写资料',content:'手写正文'});
  form=h.document.querySelector('#material-form');
  assert.equal(form.elements.namedItem('content').getAttribute('readonly'),null);
});

test('URL and answer markup reject active content while retaining safe citations', async () => {
  const h = await harness();
  assert.equal(h.app.safeURL('javascript:alert(1)'), '#');
  assert.equal(h.app.safeURL('data:text/html,<script>alert(1)</script>'), '#');
  const output = h.app.markdown('<script>alert(1)</script> [来源](https://example.org/a?q=1&b=2)');
  const container = h.document.createElement('div');
  container.innerHTML = output;
  assertNoMarkupInjection(container);
  assert.equal(container.querySelector('a').getAttribute('href'), 'https://example.org/a?q=1&b=2');
  assert.equal(container.querySelector('a').getAttribute('rel'), 'noopener noreferrer');
});

(async () => {
  let failed = 0;
  for (const {name, run} of tests) {
    try { await run(); process.stdout.write('PASS ' + name + '\n'); }
    catch (error) { ++failed; process.stderr.write('FAIL ' + name + '\n' + error.stack + '\n'); }
  }
  process.stdout.write((tests.length - failed) + '/' + tests.length + ' frontend regression checks passed (offline synthetic data only).\n');
  if (failed) process.exitCode = 1;
})();
