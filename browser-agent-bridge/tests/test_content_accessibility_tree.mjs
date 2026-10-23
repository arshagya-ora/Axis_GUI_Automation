import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { test } from 'node:test';

async function importLocatorModule() {
  const source = await readFile(new URL('../extension/sw/locator.js', import.meta.url), 'utf8');
  const dataUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
  return import(dataUrl);
}

class FakeElement {
  constructor(tag, { attrs = {}, text = '', value = '', children = [] } = {}) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.id = attrs.id || '';
    this._attrs = attrs;
    this.innerText = text;
    this.textContent = text;
    this.value = value;
    this.children = children;
    this.parentElement = null;
    this.ownerDocument = null;
    for (const child of children) child.parentElement = this;
  }

  get firstChild() { return this.children[0] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get nextSibling() { return nextSibling(this); }
  get nextElementSibling() { return nextSibling(this); }
  getBoundingClientRect() { return { x: 0, y: 0, left: 0, top: 0, width: 10, height: 10 }; }
  getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; }
  hasAttribute(name) { return name in this._attrs; }
  contains(node) { return this === node || allDescendants(this).includes(node); }
  querySelector(selector) { return findDescendant(this, el => matchesSelectorList(el, selector)); }
  querySelectorAll(selector) { return allDescendants(this).filter(el => matchesSelectorList(el, selector)); }
  matches(selector) { return matchesSelectorList(this, selector); }
  closest(selector) {
    let cur = this;
    while (cur) {
      if (matchesSelectorList(cur, selector)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }
}

class FakeInput extends FakeElement {}
class FakeTextArea extends FakeElement {}
class FakeSelect extends FakeElement {}
class FakeAnchor extends FakeElement {}

function nextSibling(el) {
  const siblings = el.parentElement?.children || [];
  const index = siblings.indexOf(el);
  return index >= 0 ? siblings[index + 1] || null : null;
}

function allDescendants(root) {
  const out = [];
  for (const child of root.children || []) {
    out.push(child);
    out.push(...allDescendants(child));
  }
  return out;
}

function findDescendant(root, predicate) {
  return allDescendants(root).find(predicate) || null;
}

function matchesSelectorList(el, selector) {
  return selector.split(',').some(part => matchesSelector(el, part.trim()));
}

function matchesSelector(el, selector) {
  const tag = el.tagName.toLowerCase();
  if (selector === '*') return true;
  if (selector === 'label') return tag === 'label';
  if (selector === ':scope > caption') return tag === 'caption' && el.parentElement?.tagName?.toLowerCase() === 'table';
  if (selector.startsWith('#')) return el.id === selector.slice(1);
  const attrMatch = selector.match(/^([a-z]*)?\[([^=\]]+)(?:="([^"]*)")?\]$/i);
  if (attrMatch) {
    const [, wantedTag, attr, value] = attrMatch;
    if (wantedTag && tag !== wantedTag.toLowerCase()) return false;
    if (!el.hasAttribute(attr)) return false;
    return value === undefined || el.getAttribute(attr) === value;
  }
  return tag === selector.toLowerCase();
}

function assignDocument(el, doc) {
  el.ownerDocument = doc;
  for (const child of el.children || []) assignDocument(child, doc);
}

function makeDocument(bodyChildren) {
  const body = new FakeElement('body', { children: bodyChildren });
  const all = [body, ...allDescendants(body)];
  const doc = {
    body,
    documentElement: body,
    getElementById(id) { return all.find(el => el.id === id) || null; },
    querySelector(selector) { return all.find(el => matchesSelectorList(el, selector)) || null; },
    querySelectorAll(selector) { return all.filter(el => matchesSelectorList(el, selector)); }
  };
  assignDocument(body, doc);
  return doc;
}

async function runAccessibilitySession(document, message = {}) {
  const a11ySource = await readFile(new URL('../extension/content/dom-a11y.js', import.meta.url), 'utf8');
  const treeSource = await readFile(new URL('../extension/content/accessibility-tree.js', import.meta.url), 'utf8');
  let listener = null;
  const context = {
    chrome: { runtime: { onMessage: { addListener(fn) { listener = fn; } } } },
    document,
    location: { href: 'https://example.test/' },
    window: {
      CSS: { escape: value => String(value).replace(/["\\]/g, '\\$&') },
      getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' })
    },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }),
    Node: { ELEMENT_NODE: 1, TEXT_NODE: 3 },
    Event: class { constructor(type) { this.type = type; } },
    HTMLAnchorElement: FakeAnchor,
    HTMLInputElement: FakeInput,
    HTMLTextAreaElement: FakeTextArea,
    HTMLSelectElement: FakeSelect,
    globalThis: null
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(a11ySource, context);
  vm.runInContext(treeSource, context);
  let response = null;
  listener({ type: 'GET_ACCESSIBILITY_TREE', maxNodes: 100, snapshotId: 'snap_test', frameId: 0, ...message }, {}, value => { response = value; });
  assert.equal(response.ok, true, response.error);
  return {
    context,
    tree: response.tree,
    send(message) {
      let sent = null;
      listener(message, {}, value => { sent = value; });
      return sent;
    }
  };
}

async function runAccessibilityTree(document) {
  return (await runAccessibilitySession(document)).tree;
}

test('field observations preserve original values, row identity and display-only values', async () => {
  const input = new FakeInput('input', { attrs: { 'aria-label': 'Limit' }, value: '0' });
  const display = new FakeElement('div', { attrs: { role: 'textbox', 'aria-label': 'Saved Limit' }, text: '10' });
  delete display.value;
  const password = new FakeInput('input', { attrs: { type: 'password', 'aria-label': 'Password' }, value: 'secret' });
  const row = new FakeElement('tr', { text: '310 14', children: [new FakeElement('td', { text: '310' }), new FakeElement('td', { text: '14' })] });
  const doc = makeDocument([input, display, password, new FakeElement('table', { children: [row] })]);
  const tree = await runAccessibilityTree(doc);
  assert.equal(tree.fields.find(field => field.name === 'Limit').value, '0');
  assert.equal(tree.fields.find(field => field.name === 'Saved Limit').display, '10');
  assert.equal(tree.nodes.find(node => node.name === 'Saved Limit').hasValue, false);
  assert.ok(tree.fields.some(field => field.role === 'row' && field.name.includes('310')));
  assert.ok(!JSON.stringify(tree.fields).includes('secret'));
  input.value = '150';
  const updated = await runAccessibilityTree(doc);
  assert.equal(updated.fields.find(field => field.name === 'Limit').value, '150');
  assert.equal(tree.fields.find(field => field.name === 'Limit').value, '0');
});

test('ref actionability accepts its associated label and rejects unrelated overlays', async () => {
  const input = new FakeInput('input', { attrs: { id: 'commit', type: 'checkbox' } });
  const label = new FakeElement('label', { attrs: { for: 'commit' }, text: 'Commit' });
  label.control = input;
  input.labels = [label];
  const overlay = new FakeElement('div', { text: 'Overlay' });
  const doc = makeDocument([input, label, overlay]);
  doc.elementFromPoint = () => label;
  const session = await runAccessibilitySession(doc);
  const ref = session.tree.nodes.find(node => node.role === 'checkbox').ref;
  const target = () => session.send({ type: 'GET_ACCESSIBILITY_REF_TARGET', ref, snapshotId: 'snap_test' }).target;
  assert.equal(target().actionability.actionable, true);
  doc.elementFromPoint = () => overlay;
  assert.equal(target().actionability.actionable, false);
  const link = new FakeAnchor('a', { attrs: { href: '/help' }, text: 'Help' });
  link.parentElement = label;
  doc.elementFromPoint = () => link;
  assert.equal(target().actionability.actionable, false);
});

async function locatorCount(document, params) {
  const { createLocatorHandlers } = await importLocatorModule();
  const domA11ySource = await readFile(new URL('../extension/content/dom-a11y.js', import.meta.url), 'utf8');
  const chromeApi = {
    scripting: {
      async executeScript({ func, args }) {
        const prev = { d: globalThis.document, g: globalThis.getComputedStyle, w: globalThis.window };
        globalThis.document = document;
        globalThis.getComputedStyle = () => ({ visibility: 'visible', display: 'block', opacity: '1', pointerEvents: 'auto' });
        globalThis.window = { innerWidth: 1024, innerHeight: 768, CSS: { escape: value => String(value).replace(/["\\]/g, '\\$&') } };
        try {
          return [{ result: func(...args) }];
        } finally {
          globalThis.document = prev.d;
          globalThis.getComputedStyle = prev.g;
          globalThis.window = prev.w;
        }
      }
    }
  };
  const handlers = createLocatorHandlers({
    assertTabId: id => id,
    assertTabAllowed: async () => {},
    assertString: () => {},
    recordAction: async () => {},
    resolveFrameTarget: async tabId => ({ target: { tabId }, frame: { url: 'about:test' } }),
    chromeApi,
    domA11ySource
  });
  return (await handlers.locatorCount({ tabId: 1, ...params })).count;
}

test('content accessibility tree uses locator-aligned accessible names and implicit roles', async () => {
  const title = new FakeElement('h2', { attrs: { id: 'title' }, text: 'Billing' });
  const suffix = new FakeElement('span', { attrs: { id: 'suffix' }, text: 'Save' });
  const labelledButton = new FakeElement('button', { attrs: { 'aria-labelledby': 'title suffix' }, text: 'Ignored text' });
  const emailLabel = new FakeElement('label', { attrs: { for: 'email' }, text: 'Email address' });
  const email = new FakeInput('input', { attrs: { id: 'email', type: 'text', placeholder: 'Fallback' } });
  const wrapped = new FakeInput('input', { attrs: { type: 'search' } });
  const wrappingLabel = new FakeElement('label', { text: 'Search query', children: [wrapped] });
  const submit = new FakeInput('input', { attrs: { type: 'submit' } });
  const checkbox = new FakeInput('input', { attrs: { type: 'checkbox', 'aria-label': 'Accept terms' } });
  const logo = new FakeElement('img', { attrs: { alt: 'Company logo' } });
  const document = makeDocument([title, suffix, labelledButton, emailLabel, email, wrappingLabel, submit, checkbox, logo]);

  const tree = await runAccessibilityTree(document);
  const byName = new Map(tree.nodes.map(node => [node.name, node]));
  const nodeDump = JSON.stringify(tree.nodes);

  for (const name of ['Billing Save', 'Email address', 'Search query', 'Submit', 'Accept terms', 'Company logo']) {
    assert.ok(byName.has(name), `${name} missing from ${nodeDump}`);
  }
  assert.equal(byName.get('Billing Save').role, 'button');
  assert.equal(byName.get('Email address').role, 'textbox');
  assert.equal(byName.get('Search query').role, 'searchbox');
  assert.equal(byName.get('Submit').role, 'button');
  assert.equal(byName.get('Accept terms').role, 'checkbox');
  assert.equal(byName.get('Company logo').role, 'img');
  assert.equal(await locatorCount(document, { selector: 'img' }), 1);
  assert.equal(await locatorCount(document, { selector: 'img', name: 'Company logo' }), 1);

  for (const [name, role] of [
    ['Billing Save', 'button'],
    ['Email address', 'textbox'],
    ['Search query', 'searchbox'],
    ['Submit', 'button'],
    ['Accept terms', 'checkbox']
  ]) {
    assert.equal(await locatorCount(document, { role, name }), 1, `${role} ${name}`);
  }
});

test('content accessibility refs can be resolved after a snapshot', async () => {
  const button = new FakeElement('button', { text: 'Save' });
  const document = makeDocument([button]);

  const session = await runAccessibilitySession(document);
  const node = session.tree.nodes.find(item => item.name === 'Save');
  assert.ok(node?.ref, JSON.stringify(session.tree.nodes));
  assert.equal(node.snapshotId, 'snap_test');
  assert.equal(node.frameId, 0);

  const response = session.send({ type: 'GET_ACCESSIBILITY_REF_TARGET', ref: node.ref, snapshotId: node.snapshotId });
  assert.equal(response.ok, true, response.error);
  assert.equal(response.target.ref, node.ref);
  assert.equal(response.target.snapshotId, 'snap_test');
  assert.equal(response.target.element.accessibleName, 'Save');
  assert.equal(response.target.actionability.actionable, true);
});

test('content accessibility refs reject stale snapshot ids', async () => {
  const button = new FakeElement('button', { text: 'Save' });
  const document = makeDocument([button]);

  const session = await runAccessibilitySession(document);
  const node = session.tree.nodes.find(item => item.name === 'Save');
  const response = session.send({ type: 'GET_ACCESSIBILITY_REF_TARGET', ref: node.ref, snapshotId: 'old_snapshot' });
  assert.equal(response.ok, false);
  assert.match(response.error, /Stale accessibility ref snapshot/);
});

test('ref actionability uses the shared atom (fieldset[disabled] ancestor => not enabled)', async () => {
  // The button itself has no `disabled`, but a disabled fieldset ancestor makes
  // it inactive — only the shared dom-a11y isEnabled (closest('fieldset[disabled]'))
  // catches this; the previous inline check did not.
  const button = new FakeElement('button', { text: 'Save' });
  const fieldset = new FakeElement('fieldset', { attrs: { disabled: '' }, children: [button] });
  const document = makeDocument([fieldset]);

  const session = await runAccessibilitySession(document);
  const node = session.tree.nodes.find(item => item.name === 'Save');
  assert.ok(node?.ref);
  const response = session.send({ type: 'GET_ACCESSIBILITY_REF_TARGET', ref: node.ref, snapshotId: node.snapshotId });
  assert.equal(response.ok, true, response.error);
  assert.equal(response.target.actionability.enabled, false);
  assert.equal(response.target.actionability.actionable, false);
  assert.ok(response.target.actionability.reasons.includes('disabled'));
});

test('FOCUS_ACCESSIBILITY_REF focuses the element and selects its content', async () => {
  const input = new FakeInput('input', { attrs: { type: 'text' }, value: 'old text' });
  let focused = false;
  let selectedRange = null;
  input.focus = () => { focused = true; };
  input.setSelectionRange = (start, end) => { selectedRange = [start, end]; };

  const document = makeDocument([input]);
  const session = await runAccessibilitySession(document);
  const node = session.tree.nodes.find(n => n.ref);
  assert.ok(node?.ref);

  const res = session.send({ type: 'FOCUS_ACCESSIBILITY_REF', ref: node.ref, snapshotId: node.snapshotId, select: true });
  assert.equal(res.ok, true, res.error);
  assert.equal(focused, true);
  assert.deepEqual(selectedRange, [0, 'old text'.length]); // whole value selected
});

test('SELECT_ACCESSIBILITY_REF_OPTIONS selects a <select> option and fires change', async () => {
  const select = new FakeSelect('select', {});
  select.multiple = false;
  select.options = [
    { value: 'us', label: 'United States', textContent: 'United States', index: 0, selected: true },
    { value: 'ca', label: 'Canada', textContent: 'Canada', index: 1, selected: false }
  ];
  const dispatched = [];
  select.dispatchEvent = (event) => { dispatched.push(event.type); };
  select.focus = () => {};

  const document = makeDocument([select]);
  const session = await runAccessibilitySession(document);
  const node = session.tree.nodes.find(n => n.ref);
  assert.ok(node?.ref);

  const res = session.send({ type: 'SELECT_ACCESSIBILITY_REF_OPTIONS', ref: node.ref, snapshotId: node.snapshotId, values: [{ value: 'ca' }] });
  assert.equal(res.ok, true, res.error);
  // res.selected is a vm-realm array; compare fields rather than deepEqual.
  assert.equal(res.selected.length, 1);
  assert.equal(res.selected[0].value, 'ca');
  assert.equal(res.selected[0].label, 'Canada');
  assert.equal(res.selected[0].index, 1);
  assert.equal(select.options[0].selected, false);
  assert.equal(select.options[1].selected, true);
  assert.ok(dispatched.includes('input') && dispatched.includes('change'));
});

test('SELECT_ACCESSIBILITY_REF_OPTIONS matches options by label', async () => {
  const select = new FakeSelect('select', {});
  select.multiple = false;
  select.options = [
    { value: 'us', label: 'United States', textContent: 'United States', index: 0, selected: false },
    { value: 'ca', label: 'Canada', textContent: 'Canada', index: 1, selected: false }
  ];
  select.dispatchEvent = () => {};
  select.focus = () => {};
  const document = makeDocument([select]);
  const session = await runAccessibilitySession(document);
  const node = session.tree.nodes.find(n => n.ref);
  const res = session.send({ type: 'SELECT_ACCESSIBILITY_REF_OPTIONS', ref: node.ref, snapshotId: node.snapshotId, values: [{ label: 'Canada' }] });
  assert.equal(res.ok, true, res.error);
  assert.equal(select.options[1].selected, true);
});

test('SELECT_ACCESSIBILITY_REF_OPTIONS rejects a non-select ref', async () => {
  const button = new FakeElement('div', { attrs: { role: 'combobox', 'aria-label': 'Feature Status' }, text: 'DISABLED' });
  const interactions = [];
  button.focus = () => interactions.push('focus');
  button.dispatchEvent = event => interactions.push(event.type);
  const document = makeDocument([button]);
  const session = await runAccessibilitySession(document);
  const node = session.tree.nodes.find(n => n.ref);
  const res = session.send({ type: 'SELECT_ACCESSIBILITY_REF_OPTIONS', ref: node.ref, snapshotId: node.snapshotId, values: [{ value: 'x' }] });
  assert.equal(res.ok, false);
  assert.equal(res.code, 'INVALID_ARGUMENT');
  assert.match(res.error, /not a <select>/);
  const { createLocatorHandlers } = await importLocatorModule();
  const handlers = createLocatorHandlers({
    assertTabId: id => id, assertTabAllowed: async () => {}, assertString: () => {},
    ensureContentScripts: async () => {},
    resolveFrameTarget: async tabId => ({ target: { tabId }, frame: {} }),
    recordAction: async () => assert.fail('Rejected selection must not be recorded as an action'),
    chromeApi: { tabs: { sendMessage: async (_, message) => session.send(message) } }
  });
  const worker = await readFile(new URL('../extension/service-worker.js', import.meta.url), 'utf8');
  const errorData = vm.runInNewContext(worker.slice(worker.indexOf('function errorData(error)'),
    worker.indexOf('async function assertOptionalPermissions')) + '\nerrorData;');
  await assert.rejects(handlers.locatorSelectOptionRef({ tabId: 1, ref: node.ref,
    snapshotId: node.snapshotId, value: 'Enabled' }), error => {
    assert.equal(error.message, res.error);
    assert.equal(JSON.parse(JSON.stringify(errorData(error))).code, 'INVALID_ARGUMENT');
    return true;
  });
  assert.deepEqual(interactions, []);
  assert.equal(button.textContent, 'DISABLED');
});

test('containers and open shadow roots expose nested controls once', async () => {
  const save = new FakeElement('button', { text: 'Save' });
  const aside = new FakeElement('aside', { attrs: { 'aria-label': 'Sidebar' }, children: [save] });
  const tab = new FakeElement('button', { attrs: { role: 'tab' }, text: 'Settings' });
  const tablist = new FakeElement('div', { attrs: { role: 'tablist' }, children: [tab] });
  const shadowButton = new FakeElement('button', { text: 'Shadow Save' });
  const host = new FakeElement('custom-widget');
  host.shadowRoot = new FakeElement('div', { children: [shadowButton] });
  const doc = makeDocument([aside, tablist, host]);
  assignDocument(host.shadowRoot, doc);
  const tree = await runAccessibilityTree(doc);
  for (const name of ['Save', 'Settings', 'Shadow Save'])
    assert.equal(tree.nodes.filter(node => node.name === name).length, 1, name);
  assert.match(tree.nodes.find(node => node.name === 'Save').context, /Sidebar/);
});

test('snapshot state, hidden filtering and password masking are consistent', async () => {
  const checked = new FakeInput('input', { attrs: { type: 'checkbox', 'aria-label': 'Enabled' }, value: 'on' });
  checked.checked = true;
  const disabled = new FakeElement('button', { text: 'Unavailable', attrs: { 'aria-disabled': 'true' } });
  const hidden = new FakeElement('button', { text: 'Hidden', attrs: { 'aria-hidden': 'true' } });
  const password = new FakeInput('input', { attrs: { type: 'password' }, value: 'dummy-secret' });
  const tree = await runAccessibilityTree(makeDocument([checked, disabled, hidden, password]));
  assert.equal(tree.nodes.find(node => node.name === 'Enabled').checked, true);
  assert.equal(tree.nodes.find(node => node.name === 'Unavailable').disabled, true);
  assert.equal(tree.nodes.some(node => node.name === 'Hidden'), false);
  assert.equal(JSON.stringify(tree).includes('dummy-secret'), false);
});

test('duplicate labels retain their dialog context', async () => {
  const dialogs = ['Profile', 'Billing'].map(name => new FakeElement('dialog', {
    attrs: { 'aria-label': name }, children: [new FakeElement('button', { text: 'Save' })]
  }));
  const tree = await runAccessibilityTree(makeDocument(dialogs));
  const saves = tree.nodes.filter(node => node.name === 'Save');
  assert.equal(saves.length, 2);
  assert.deepEqual(Array.from(saves, node => node.context), ['dialog Profile', 'dialog Billing']);
});

test('actionable controls survive a long prose prefix and node budget', async () => {
  const prose = Array.from({ length: 200 }, (_, i) => new FakeElement('p', { text: 'Paragraph ' + i }));
  const doc = makeDocument([...prose, new FakeElement('button', { text: 'Continue' })]);
  const { tree } = await runAccessibilitySession(doc, { maxNodes: 10 });
  assert.equal(tree.nodes.length, 10);
  assert.equal(tree.nodes[0].name, 'Continue');
  assert.equal(tree.truncated, true);
});

test('same-origin iframe contents are not attributed to the parent frame', async () => {
  const frame = new FakeElement('iframe');
  frame.contentDocument = makeDocument([new FakeElement('button', { text: 'Frame Save' })]);
  const tree = await runAccessibilityTree(makeDocument([frame]));
  assert.equal(tree.nodes.some(node => node.name === 'Frame Save'), false);
  assert.equal(tree.iframes.length, 1);
});

test('shadow controls receive deep hit tests; empty hit tests refuse clicks', async () => {
  const button = new FakeElement('button', { text: 'Shadow action' });
  const host = new FakeElement('custom-widget');
  host.shadowRoot = new FakeElement('div', { children: [button] });
  host.shadowRoot.elementFromPoint = () => button;
  const doc = makeDocument([host]);
  assignDocument(host.shadowRoot, doc);
  doc.elementFromPoint = () => host;
  const session = await runAccessibilitySession(doc);
  const node = session.tree.nodes.find(node => node.name === 'Shadow action');
  assert.equal(session.send({ type: 'GET_ACCESSIBILITY_REF_TARGET', ref: node.ref }).target.actionability.actionable, true);
  doc.elementFromPoint = () => null;
  assert.equal(session.send({ type: 'GET_ACCESSIBILITY_REF_TARGET', ref: node.ref }).target.actionability.actionable, false);
});


test('table observations number data rows excluding headers and retain edit-control row context', async () => {
  const header = new FakeElement('tr', { children: [new FakeElement('th', { text: 'Operation' })] });
  const rows = Array.from({ length: 17 }, (_, i) => new FakeElement('tr', { text: 'Operation ' + (i + 1), children: [
    new FakeElement('td', { text: 'Operation ' + (i + 1) }),
    new FakeElement('td', { children: [new FakeElement('button', { text: 'Edit' })] }),
  ] }));
  const table = new FakeElement('table', { children: [header, ...rows] });
  const tree = await runAccessibilityTree(makeDocument([table]));
  const dataRows = tree.nodes.filter(node => node.role === 'row' && !node.rowHeader);
  assert.equal(dataRows.length, 17);
  assert.equal(dataRows[16].rowIndex, 17);
  assert.equal(dataRows[16].rowOrder, 'rendered');
  const edit = tree.nodes.find(node => node.role === 'button' && node.rowIndex === 17);
  assert.ok(edit);
  assert.match(edit.context, /row 17 Operation 17/);
});


test('data rows with row header cells retain rendered ordinal and aria position', async () => {
  const header = new FakeElement('tr', { children: [new FakeElement('th', { text: 'Name' }), new FakeElement('th', { text: 'Actions' })] });
  const edit = new FakeElement('button', { text: 'Edit' });
  const row = new FakeElement('tr', { attrs: { 'aria-rowindex': '42' }, children: [
    new FakeElement('th', { attrs: { scope: 'row' }, text: 'Operation 41' }), new FakeElement('td', { children: [edit] })] });
  const tree = await runAccessibilityTree(makeDocument([new FakeElement('table', { children: [header, row] })]));
  const target = tree.nodes.find(n => n.name === 'Edit');
  assert.equal(target.rowIndex, 1);
  assert.equal(target.ariaRowIndex, 42);
  assert.equal(target.rowOrder, 'rendered');
  assert.equal(target.rowHeader, false);
});

test('manual target identity changes invalidate a ref instead of clicking its replacement meaning', async () => {
  const button = new FakeElement('button', { text: 'Save' });
  const session = await runAccessibilitySession(makeDocument([button]));
  const node = session.tree.nodes.find(n => n.name === 'Save');
  button.innerText = button.textContent = 'Delete';
  const result = session.send({ type: 'GET_ACCESSIBILITY_REF_TARGET', ref: node.ref, snapshotId: node.snapshotId });
  assert.equal(result.ok, false);
  assert.match(result.error, /Stale accessibility ref identity/);
});

test('reference hit testing chooses an exposed point when the center is covered', async () => {
  const button = new FakeElement('button', { text: 'Save' });
  const overlay = new FakeElement('div', { text: 'Overlay' });
  button.getBoundingClientRect = () => ({ x: 0, y: 0, width: 100, height: 100 });
  const doc = makeDocument([button, overlay]);
  doc.elementFromPoint = (x, y) => x > 40 && x < 60 && y > 40 && y < 60 ? overlay : button;
  const session = await runAccessibilitySession(doc);
  const node = session.tree.nodes.find(n => n.name === 'Save');
  const result = session.send({ type: 'GET_ACCESSIBILITY_REF_TARGET', ref: node.ref, snapshotId: node.snapshotId });
  assert.equal(result.target.actionability.actionable, true);
  assert.equal(result.target.element.clickPoint.x, 20);
});


test('scoped observations use the real resolver and retain roles, state, context and refs in the isolated world', async () => {
  const save = new FakeElement('button', { text: 'Save', attrs: { 'aria-disabled': 'true' } });
  const previous = new FakeElement('button', { text: 'Previous' });
  const menu = new FakeElement('div', { attrs: { role: 'group', 'aria-label': 'Options' }, children: [previous, save] });
  const outside = new FakeElement('button', { text: 'Outside' });
  const document = makeDocument([menu, outside]);
  const session = await runAccessibilitySession(document);
  const { createLocatorHandlers } = await importLocatorModule();
  const { renderCompactRefTree } = await import('../extension/sw/page.js');
  const worlds = [];
  const handlers = createLocatorHandlers({
    assertTabId: id => id, assertTabAllowed: async () => {}, ensureContentScripts: async () => {},
    resolveFrameTarget: async tabId => ({ target: { tabId, frameIds: [7] }, frameId: 7 }),
    renderRefTree: renderCompactRefTree,
    chromeApi: { tabs: {get: async () => ({url: 'https://top.example/', title: 'Top page'})}, scripting: { async executeScript({func, args, world}) {
      worlds.push(world);
      session.context.options = args[0];
      return [{ result: await vm.runInContext('(' + func.toString() + ')(options)', session.context) }];
    } } },
  });
  for (const format of ['compact', 'aria']) {
    const result = await handlers.locatorSnapshot({ tabId: 1, locator: { role: 'group', name: 'Options', exact: true }, format });
    assert.equal(result.url, 'https://top.example/');
    assert.match(result.snapshot, /group "Options"/);
    assert.match(result.snapshot, /button "Previous"/);
    assert.match(result.snapshot, /disabled=true/);
    assert.match(result.snapshot, /context="group Options"/);
    assert.doesNotMatch(result.snapshot, /Outside/);
    assert.match(result.snapshot, /\[f7:ref_/);
    if (format === 'aria') assert.match(result.snapshot, /- button/);
    const ref = result.snapshot.match(/\[f7:(ref_\d+)\].*button "Previous"/)[1];
    assert.equal(session.send({type: 'GET_ACCESSIBILITY_REF_TARGET', ref, snapshotId: result.snapshotId}).ok, true);
  }
  assert.deepEqual(worlds, ['ISOLATED', 'ISOLATED']);
  await assert.rejects(handlers.locatorSnapshot({tabId: 1, locator: {role: 'link', name: 'Missing'}}), error => error.code === 'TARGET_NOT_FOUND');
  await assert.rejects(handlers.locatorSnapshot({tabId: 1, locator: {role: 'button'}}), error => error.code === 'TARGET_AMBIGUOUS' && error.data.count === 3);
});


test('password assertions compare the live value without exposing it or toggling visibility', async () => {
  const input = new FakeInput('input', {attrs: {id: 'password', type: 'password'}, value: 'fixture-secret'});
  const session = await runAccessibilitySession(makeDocument([input]));
  Object.assign(session.context, {setTimeout, clearTimeout, setInterval, clearInterval,
    MutationObserver: class { observe() {} disconnect() {} }});
  const {createLocatorHandlers} = await importLocatorModule();
  const handlers = createLocatorHandlers({
    assertTabId: id => id, assertTabAllowed: async () => {},
    resolveFrameTarget: async tabId => ({target: {tabId}}),
    chromeApi: {scripting: {async executeScript({func,args}) {
      session.context.options = args[0];
      return [{result: await vm.runInContext('(' + func.toString() + ')(options)',session.context)}];
    }}}
  });
  const params = {tabId: 1, selector: '#password', expected: 'fixture-secret', timeoutMs: 10, intervalMs: 1};
  const result = await handlers.expectLocatorToHaveValue(params);
  assert.equal(result.ok,true);
  assert.equal(input.getAttribute('type'),'password');
  assert.ok(!JSON.stringify(result).includes('fixture-secret'));
  await assert.rejects(handlers.expectLocatorToHaveValue({...params,expected:'wrong-secret'}),error => {
    const output = JSON.stringify({message:error.message,diagnostic:error.diagnostic});
    assert.ok(!output.includes('fixture-secret') && !output.includes('wrong-secret'));
    return error.code === 'LOCATOR_EXPECT_TIMEOUT';
  });
});
