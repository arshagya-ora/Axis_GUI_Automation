// C2: locator.locator() parent scoping (`within`). Runs the REAL shipped in-page
// resolver func against a fake DOM via an injected scripting.executeScript (the
// pattern from test_frames_resolver.mjs / test_locator_resolver.mjs), so the
// asserts exercise the actual findLocatorMatches/normalizeLocatorParams logic.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

async function importLocatorModule() {
  const source = await readFile(new URL('../extension/sw/locator.js', import.meta.url), 'utf8');
  const dataUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
  return import(dataUrl);
}

async function loadDomA11ySource() {
  return readFile(new URL('../extension/content/dom-a11y.js', import.meta.url), 'utf8');
}

const RECT = { x: 0, y: 0, width: 10, height: 10 };

function makeEl({ tag, text = '', attrs = {}, children = [] }) {
  return {
    tagName: tag.toUpperCase(),
    id: attrs.id || '',
    shadowRoot: undefined,
    hidden: false, disabled: false, readOnly: false, isContentEditable: false, multiple: false,
    innerText: text, textContent: text, children,
    getAttribute(name) { return name in attrs ? attrs[name] : null; },
    hasAttribute(name) { return name in attrs; },
    closest() { return null; },
    getBoundingClientRect() { return RECT; },
    querySelector() { return null; },
    // shallow (direct children) — sufficient because every `within` hop in these
    // tests queries a direct parent->child relationship.
    querySelectorAll(sel) {
      if (sel === '*') return children;
      const tokens = sel.split(',').map(s => s.trim().toLowerCase());
      return children.filter(c => tokens.includes(c.tagName.toLowerCase()));
    }
  };
}

function buildDom() {
  const saveA = makeEl({ tag: 'button', text: 'Save' });
  const delA = makeEl({ tag: 'button', text: 'Delete' });
  const saveB = makeEl({ tag: 'button', text: 'Save' });
  const sectionA = makeEl({ tag: 'section', attrs: { 'data-test': 'a' }, children: [saveA, delA] });
  const sectionB = makeEl({ tag: 'section', attrs: { 'data-test': 'b' }, children: [saveB] });
  const region = makeEl({ tag: 'div', attrs: { 'data-region': 'x' }, children: [sectionA] });
  const topSave = makeEl({ tag: 'button', text: 'Save' });
  const all = [region, sectionA, sectionB, topSave, saveA, delA, saveB];
  const doc = {
    querySelector() { return null; },
    getElementById() { return null; },
    querySelectorAll(sel) {
      if (sel === '*') return all;
      const tokens = sel.split(',').map(s => s.trim().toLowerCase());
      return all.filter(el => tokens.includes(el.tagName.toLowerCase()));
    }
  };
  return { doc, els: { saveA, delA, saveB, sectionA, sectionB, region, topSave } };
}

let createLocatorHandlers;
let domA11ySource;
async function run(doc, params, method = 'locatorCount') {
  createLocatorHandlers ||= (await importLocatorModule()).createLocatorHandlers;
  domA11ySource ||= await loadDomA11ySource();
  const chromeApi = {
    scripting: {
      async executeScript({ func, args }) {
        const prev = { d: globalThis.document, g: globalThis.getComputedStyle, w: globalThis.window, m: globalThis.MutationObserver };
        globalThis.document = doc;
        globalThis.getComputedStyle = () => ({ visibility: 'visible', display: 'block', opacity: '1' });
        globalThis.window = { innerWidth: 1024, innerHeight: 768 };
        globalThis.MutationObserver = class { observe() {} disconnect() {} };
        try { return [{ result: await func(...args) }]; }
        finally { globalThis.document = prev.d; globalThis.getComputedStyle = prev.g; globalThis.window = prev.w; globalThis.MutationObserver = prev.m; }
      }
    }
  };
  const h = createLocatorHandlers({
    assertTabId: id => id,
    assertTabAllowed: async () => {},
    assertString: () => {},
    recordAction: async () => {},
    resolveFrameTarget: async tabId => ({ target: { tabId }, frame: { url: 'about:test' } }),
    chromeApi,
    domA11ySource
  });
  return h[method]({ tabId: 1, timeoutMs: 30, intervalMs: 5, ...params });
}

async function count(doc, params) {
  return (await run(doc, params)).count;
}

test('native selection rejects custom controls before readiness or interaction', async () => {
  const { doc, els } = buildDom();
  const interactions = [];
  els.saveB.getAttribute = name => ({ role: 'combobox', 'aria-label': 'Feature Status' }[name] ?? null);
  els.saveB.focus = () => interactions.push('focus');
  els.saveB.scrollIntoView = () => interactions.push('scroll');
  els.saveB.dispatchEvent = event => interactions.push(event.type);
  const locator = { role: 'combobox', name: 'Feature Status', exact: true };
  for (const force of [false, true]) {
    await assert.rejects(run(doc, { locator, value: 'Enabled', force }, 'locatorSelectOption'),
      error => error.code === 'INVALID_ARGUMENT' && /not a <select>/.test(error.message));
  }
  assert.deepEqual(interactions, []);
});

test('native selection still selects and dispatches events', async () => {
  const { doc, els } = buildDom();
  const select = els.saveB;
  select.tagName = 'SELECT';
  select.options = [
    { value: 'disabled', label: 'Disabled', index: 0, selected: true },
    { value: 'enabled', label: 'Enabled', index: 1, selected: false }
  ];
  const interactions = [];
  select.focus = () => interactions.push('focus');
  select.scrollIntoView = () => {};
  select.dispatchEvent = event => interactions.push(event.type);
  const result = await run(doc, { selector: 'select', value: 'enabled', stable: false }, 'locatorSelectOption');
  assert.equal(result.ok, true);
  assert.equal(select.options[0].selected, false);
  assert.equal(select.options[1].selected, true);
  assert.deepEqual(interactions, ['focus', 'input', 'change']);
});

test('native selection preserves strict ambiguity before checking control type', async () => {
  const { doc } = buildDom();
  await assert.rejects(run(doc, { selector: 'button', value: 'Enabled', strict: true }, 'locatorSelectOption'),
    error => error.code === 'LOCATOR_STRICT_MODE_VIOLATION');
});

test('attribute reads preserve the named target for new and legacy callers', async () => {
  const { doc, els } = buildDom();
  els.saveB.getAttribute = name => ({ id: 'save-second', 'aria-label': 'Save', value: 'stored' }[name] ?? null);
  const locator = { role: 'button', name: 'Save', exact: true,
    within: { selector: 'section', hasAttribute: { name: 'data-test', value: 'b' } } };
  for (const [attribute, expected] of [['value', 'stored'], ['id', 'save-second'], ['title', null]]) {
    for (const params of [{ locator, attribute }, { locator, name: attribute }]) {
      const result = await run(doc, params, 'locatorGetAttribute');
      assert.equal(result.value, expected);
      assert.equal(result.element.accessibleName, 'Save');
    }
  }
});

test('a read-only textbox without a value property requires text validation', async () => {
  const { doc, els } = buildDom();
  els.saveB.innerText = els.saveB.textContent = '10';
  els.saveB.getAttribute = name => ({ role: 'textbox', 'aria-label': 'Default Load' }[name] ?? null);
  const locator = { role: 'textbox', name: 'Default Load', exact: true };
  for (const expected of ['10', '']) {
    await assert.rejects(run(doc, { locator, expected }, 'expectLocatorToHaveValue'),
      error => error.code === 'UNSUPPORTED_ELEMENT_PROPERTY');
  }
  const textCheck = await run(doc, { locator, expected: '10' }, 'expectLocatorToHaveText');
  assert.equal(textCheck.ok, true);
  assert.equal(textCheck.count, 1);
  assert.equal(textCheck.element.hasValue, false);
  const observed = await run(doc, { locator }, 'locatorTextContent');
  assert.equal(observed.count, 1);
  assert.equal(observed.element.hasValue, false);
  els.saveB.value = '';
  assert.equal((await run(doc, { locator, expected: '10' }, 'expectLocatorToHaveText')).element.hasValue, true);
  assert.equal((await run(doc, { locator, expected: '' }, 'expectLocatorToHaveValue')).ok, true);
});

test('an ambiguous assertion can be corrected by keeping the label and narrowing its section', async () => {
  const { doc } = buildDom();
  const params = { selector: 'button', hasText: 'Save', expected: 'Save' };
  await assert.rejects(run(doc, params, 'expectLocatorToHaveText'),
    error => error.code === 'LOCATOR_STRICT_MODE_VIOLATION' && error.diagnostic.count === 3);
  const result = await run(doc, { ...params,
    within: { selector: 'section', hasAttribute: { name: 'data-test', value: 'b' } }
  }, 'expectLocatorToHaveText');
  assert.equal(result.actual, 'Save');
});

test('a custom control validates its value child without weakening text matching', async () => {
  const { doc, els } = buildDom();
  els.saveB.innerText = els.saveB.textContent = 'ENABLED';
  els.sectionB.innerText = els.sectionB.textContent = 'Feature Status ENABLED';
  const scope = { selector: 'section', hasAttribute: { name: 'data-test', value: 'b' } };
  await assert.rejects(run(doc, { ...scope, expected: 'ENABLED' }, 'expectLocatorToHaveText'),
    error => error.code === 'LOCATOR_EXPECT_TIMEOUT' && error.diagnostic.actual === 'Feature Status ENABLED');
  assert.equal((await run(doc, { selector: 'button', within: scope, expected: 'ENABLED' },
    'expectLocatorToHaveText')).ok, true);
});

test('an assertion re-resolves a value child after a rerender', async () => {
  const { doc, els } = buildDom();
  const replacement = makeEl({ tag: 'button', text: 'Saved' });
  const timer = setTimeout(() => els.sectionB.children.splice(0, 1, replacement), 5);
  try {
    const result = await run(doc, { selector: 'button', expected: 'Saved', timeoutMs: 100,
      within: { selector: 'section', hasAttribute: { name: 'data-test', value: 'b' } }
    }, 'expectLocatorToHaveText');
    assert.equal(result.actual, 'Saved');
  } finally { clearTimeout(timer); }
});

test('baseline (no within) matches every Save button page-wide', async () => {
  const { doc } = buildDom();
  assert.equal(await count(doc, { selector: 'button', hasText: 'Save' }), 3);
});

test('within narrows to Saves inside any matching parent', async () => {
  const { doc } = buildDom();
  // sections A and B each have a Save; the top-level Save is excluded.
  assert.equal(await count(doc, { selector: 'button', hasText: 'Save', within: { selector: 'section' } }), 2);
});

test('within with a discriminating parent filter scopes to one subtree', async () => {
  const { doc } = buildDom();
  assert.equal(await count(doc, {
    selector: 'button', hasText: 'Save',
    within: { selector: 'section', hasAttribute: { name: 'data-test', value: 'a' } }
  }), 1);
});

test('within yields nothing when no parent matches', async () => {
  const { doc } = buildDom();
  assert.equal(await count(doc, {
    selector: 'button', hasText: 'Save',
    within: { selector: 'section', hasAttribute: { name: 'data-test', value: 'z' } }
  }), 0);
});

test('child filters still apply within the scope', async () => {
  const { doc } = buildDom();
  // Delete exists only in section A; scoping to section B finds none.
  assert.equal(await count(doc, { selector: 'button', hasText: 'Delete', within: { selector: 'section' } }), 1);
  assert.equal(await count(doc, {
    selector: 'button', hasText: 'Delete',
    within: { selector: 'section', hasAttribute: { name: 'data-test', value: 'b' } }
  }), 0);
});

test('nested within (region > section > button) chains scopes', async () => {
  const { doc } = buildDom();
  // region x contains only section A (which has the Save). Two-level parent chain.
  assert.equal(await count(doc, {
    selector: 'button', hasText: 'Save',
    within: { selector: 'section', within: { selector: 'div' } }
  }), 1);
});

test('within accepts the nested locator form too', async () => {
  const { doc } = buildDom();
  assert.equal(await count(doc, {
    locator: { selector: 'button', hasText: 'Save', within: { selector: 'section' } }
  }), 2);
});

test('within parent must carry its own matcher (else rejected)', async () => {
  const { doc } = buildDom();
  await assert.rejects(
    () => count(doc, { selector: 'button', within: { hasText: '' } }),
    /locator requires one of/
  );
});
