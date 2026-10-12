(() => {
  if (globalThis.__localBrowserAgentAccessibilityTreeLoaded) return;
  globalThis.__localBrowserAgentAccessibilityTreeLoaded = true;
  const refState = {
    snapshotId: '',
    frameId: 0,
    refs: new Map()
  };

  function getActiveElementDeep() {
    let active = document.activeElement;
    while (active && active.shadowRoot && active.shadowRoot.activeElement) {
      active = active.shadowRoot.activeElement;
    }
    return active;
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === 'PING_AGENT_BRIDGE_CONTENT') {
      sendResponse({ ok: true });
      return true;
    }
    if (message?.type === 'GET_ACCESSIBILITY_REF_TARGET') {
      try {
        sendResponse({ ok: true, target: resolveRefTarget(message) });
      } catch (error) {
        sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error) });
      }
      return true;
    }
    if (message?.type === 'FOCUS_ACCESSIBILITY_REF') {
      try {
        sendResponse({ ok: true, target: focusRefTarget(message) });
      } catch (error) {
        sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error) });
      }
      return true;
    }
    if (message?.type === 'SELECT_ACCESSIBILITY_REF_OPTIONS') {
      try {
        sendResponse({ ok: true, ...selectRefOptions(message) });
      } catch (error) {
        sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error),
          ...(typeof error?.code === 'string' ? { code: error.code } : {}) });
      }
      return true;
    }
    if (message?.type !== 'GET_ACCESSIBILITY_TREE') return false;
    try {
      sendResponse({
        ok: true,
        tree: buildTree(
          message.maxNodes || 1000,
          message.offsetX || 0,
          message.offsetY || 0,
          message.snapshotId || '',
          Number.isInteger(message.frameId) ? message.frameId : 0
        )
      });
    } catch (error) {
      sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error) });
    }
    return true;
  });

  globalThis.__browserAgentBridgeBuildTree = buildTree;

  function buildTree(maxNodes, initialOffsetX = 0, initialOffsetY = 0, snapshotId = '', frameId = 0, scope = null) {
    const controls = [], prose = [], iframes = [];
    const visited = new WeakSet();
    const rowCounters = new WeakMap(), rowMetadata = new WeakMap();
    const a11y = globalThis.__browserAgentBridgeDomA11y;
    const containers = new Set(['document', 'article', 'complementary', 'main', 'navigation', 'region', 'form', 'list', 'listitem', 'table', 'row', 'cell', 'columnheader', 'rowheader', 'group', 'dialog', 'tablist', 'menu', 'menubar', 'tree', 'treeitem', 'grid', 'radiogroup', 'toolbar']);
    const contextRoles = new Set(['dialog', 'form', 'row', 'main', 'navigation', 'region', 'complementary', 'tablist', 'group', 'menu', 'menubar', 'tree']);
    const leaves = new Set(['button', 'input', 'textarea', 'select', 'option', 'img']);
    let index = 1, scanned = 0;
    refState.snapshotId = snapshotId || `snapshot_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
    refState.frameId = frameId;
    refState.refs = new Map();

    function children(node) {
      if (node.tagName?.toLowerCase() === 'slot' && node.assignedNodes) {
        const assigned = node.assignedNodes({ flatten: true });
        if (assigned.length) return assigned;
      }
      const parent = node.shadowRoot || node;
      const result = [];
      for (let child = parent.firstChild; child; child = child.nextSibling) result.push(child);
      return result;
    }
    function emit(el, data, context, actionable) {
      const rect = el.getBoundingClientRect();
      const ref = `ref_${index++}`;
      rememberRef(ref, el, frameId === 0 ? initialOffsetX : 0, frameId === 0 ? initialOffsetY : 0);
      const node = { ref, snapshotId: refState.snapshotId, frameId, tag: el.tagName.toLowerCase(), ...data,
        depth: context.length, context: context.slice(-2).join(' > ') || undefined,
        ...rowMetadata.get(el.closest?.('tr,[role="row"]')),
        focused: getActiveElementDeep() === el || undefined,
        bounds: { x: Math.round(rect.x + initialOffsetX), y: Math.round(rect.y + initialOffsetY), width: Math.round(rect.width), height: Math.round(rect.height) } };
      (actionable ? controls : prose).push(node);
    }
    function traverse(node, context = []) {
      if (!node || visited.has(node) || scanned >= 50000) return;
      visited.add(node); scanned++;
      if (node.nodeType === Node.TEXT_NODE) {
        const text = (node.textContent || '').replace(/\s+/g, ' ').trim();
        if (text && node.parentElement && a11y.isVisible(node.parentElement)) emit(node.parentElement, { text: text.slice(0, 500), textTruncated: text.length > 500 }, context, false);
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const el = node, tag = el.tagName.toLowerCase();
      const style = (el.ownerDocument?.defaultView || window).getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0' || el.hidden || a11y.isAriaHidden(el)) return;
      if (tag === 'iframe' || tag === 'frame') {
        if (a11y.isVisible(el)) {
          const rect = el.getBoundingClientRect();
          iframes.push({ src: el.src || '', id: el.id || '', name: el.name || '', x: rect.x, y: rect.y, width: rect.width, height: rect.height });
        }
        return; // Frame documents are collected once under their actual frame ID.
      }
      const role = a11y.implicitRole(el);
      if (role === 'row') {
        const table = el.closest?.('table,[role="table"],[role="grid"]') || document.body;
        const cells = Array.from(el.children || []).filter(cell => ['th', 'td'].includes(cell.tagName?.toLowerCase()) || ['cell', 'gridcell', 'columnheader', 'rowheader'].includes(cell.getAttribute('role')));
        const header = cells.length > 0 && cells.every(cell => cell.getAttribute('role') === 'columnheader' ||
          (cell.tagName?.toLowerCase() === 'th' && cell.getAttribute('scope') !== 'row' && cell.getAttribute('role') !== 'rowheader'));
        const rowIndex = header ? undefined : (rowCounters.get(table) || 0) + 1;
        if (!header) rowCounters.set(table, rowIndex);
        rowMetadata.set(el, { rowIndex, rowHeader: header, rowOrder: 'rendered',
          ariaRowIndex: Number(el.getAttribute('aria-rowindex')) || undefined });
      }
      const interactive = Boolean((role && !containers.has(role)) || el.matches('a[href],button,input,textarea,select,[tabindex],[contenteditable="true"]'));
      const name = (interactive || contextRoles.has(role)) ? a11y.accessibleName(el, el.getRootNode?.() || el.ownerDocument || document) : '';
      if ((interactive || (scope && contextRoles.has(role))) && a11y.isVisible(el)) {
        const states = { disabled: !a11y.isEnabled(el), readonly: el.readOnly === true || el.getAttribute('aria-readonly') === 'true' };
        for (const state of ['checked', 'selected', 'expanded']) {
          const value = a11y.ariaBooleanState(el, state);
          if (value !== null && value !== undefined) states[state] = value;
        }
        emit(el, { role: role || undefined, name, type: el.getAttribute('type') || undefined,
          hasValue: 'value' in el, value: valueOf(el),
          displayValue: ['textbox', 'combobox', 'spinbutton', 'row'].includes(role) && !('value' in el) ? a11y.visibleText(el).slice(0, 500) : undefined,
          href: el instanceof HTMLAnchorElement ? el.href : undefined, ...states }, context, interactive);
      }
      if (role === 'row' && !interactive && !scope && a11y.isVisible(el)) emit(el, { role, name }, context, false);
      const row = rowMetadata.get(el);
      const nextContext = contextRoles.has(role) ? [...context, `${role}${row?.rowIndex ? ' ' + row.rowIndex : ''}${name ? ' ' + name.slice(0, 120) : ''}`] : context;
      if (leaves.has(tag) || (tag === 'a' && interactive)) return;
      const composed = children(el);
      if (!composed.length && !interactive && a11y.isVisible(el)) {
        const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
        if (text) emit(el, { text: text.slice(0, 500) }, nextContext, false);
      } else for (const child of composed) traverse(child, nextContext);
    }
    traverse(scope || document.body || document.documentElement);
    controls.sort((a, b) => Number(Boolean(b.focused)) - Number(Boolean(a.focused)) || Number(Boolean(b.context?.includes('dialog'))) - Number(Boolean(a.context?.includes('dialog'))));
    const all = [...controls, ...prose], nodes = all.slice(0, maxNodes);
    const retained = new Set(nodes.map(node => node.ref));
    for (const ref of refState.refs.keys()) if (!retained.has(ref)) refState.refs.delete(ref);
    const fieldNodes = nodes.filter(node => ['textbox', 'combobox', 'spinbutton', 'checkbox', 'switch', 'radio', 'row'].includes(node.role) && node.type !== 'password');
    const fields = fieldNodes.slice(0, 128).map(node => ({ frame: frameId, role: node.role, name: node.name,
        context: node.context, value: node.value, display: node.displayValue,
        checked: node.checked, selected: node.selected, readonly: node.readonly }));
    return { url: location.href, title: document.title, snapshotId: refState.snapshotId, frameId, nodes, fields,
      fieldsTruncated: fieldNodes.length > fields.length, iframes, truncated: all.length > maxNodes || scanned >= 50000 || nodes.some(node => node.textTruncated || (typeof node.value === 'string' && node.value.length > 500)) };
  }

  function targetIdentity(element) {
    const a11y = globalThis.__browserAgentBridgeDomA11y;
    const container = element.closest?.('tr,[role="row"],dialog,[role="dialog"],form');
    return JSON.stringify([a11y.implicitRole(element), a11y.accessibleName(element),
      element.getAttribute('type'), element.getAttribute('href'),
      container ? a11y.accessibleName(container) : null]);
  }

  function rememberRef(ref, element, offsetX = 0, offsetY = 0) {
    refState.refs.set(ref, { element, offsetX, offsetY, identity: targetIdentity(element) });
  }

  function lookupRefEntry(message) {
    const ref = typeof message.ref === 'string' ? message.ref : '';
    if (!ref) throw new Error('ref is required');
    if (message.snapshotId && message.snapshotId !== refState.snapshotId) {
      throw new Error(`Stale accessibility ref snapshot: ${message.snapshotId}`);
    }
    const entry = refState.refs.get(ref);
    if (!entry?.element || entry.element.isConnected === false) throw new Error(`Accessibility ref not found or stale: ${ref}`);
    if (entry.identity !== targetIdentity(entry.element)) throw new Error(`Stale accessibility ref identity: ${ref}; observe the changed page`);
    return { ref, entry };
  }

  function resolveRefTarget(message) {
    const { ref, entry } = lookupRefEntry(message);
    if (message.scrollIntoView && typeof entry.element.scrollIntoView === 'function') entry.element.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
    return summarizeRefTarget(ref, entry);
  }

  // Focus the ref's element directly (no synthetic click, so it does not
  // activate buttons/links), optionally selecting its content so a following
  // real text input replaces it. Works for input/textarea/contentEditable.
  function focusRefTarget(message) {
    const { ref, entry } = lookupRefEntry(message);
    const element = entry.element;
    if (typeof element.focus === 'function') {
      try { element.focus({ preventScroll: true }); } catch { element.focus(); }
    }
    if (message.select) selectAllContent(element);
    return summarizeRefTarget(ref, entry);
  }

  function selectAllContent(element) {
    const tag = element.tagName ? element.tagName.toLowerCase() : '';
    if (tag === 'input' || tag === 'textarea') {
      if (typeof element.setSelectionRange === 'function') {
        try { element.setSelectionRange(0, String(element.value || '').length); return; } catch { /* fall through */ }
      }
      if (typeof element.select === 'function') element.select();
      return;
    }
    if (element.isContentEditable) {
      const doc = element.ownerDocument || document;
      const selection = doc.getSelection && doc.getSelection();
      if (selection && typeof doc.createRange === 'function') {
        const range = doc.createRange();
        range.selectNodeContents(element);
        selection.removeAllRanges();
        selection.addRange(range);
      }
    }
  }

  // Select option(s) on a <select> resolved by ref. Mirrors the locator
  // resolver's selectOption: match each requested option by index/value/label,
  // set selection (respecting `multiple`), and fire input/change.
  function selectRefOptions(message) {
    const { ref, entry } = lookupRefEntry(message);
    const element = entry.element;
    if (!element.tagName || element.tagName.toLowerCase() !== 'select') {
      const error = new Error('Accessibility ref is not a <select>. Inspect the custom control, open it, and click an observed option.');
      error.code = 'INVALID_ARGUMENT';
      throw error;
    }
    const requested = Array.isArray(message.values) ? message.values : [];
    if (requested.length === 0) throw new Error('no options requested');
    const multiple = element.multiple === true;
    const optionsToSelect = [];
    for (const request of requested) {
      const option = findRefOption(element, request);
      if (!option) throw new Error(`Option not found: ${JSON.stringify(request)}`);
      optionsToSelect.push(option);
      if (!multiple) break;
    }
    for (const option of element.options) option.selected = false;
    const selected = [];
    for (const option of optionsToSelect) {
      option.selected = true;
      selected.push({ value: option.value, label: option.label || option.textContent || '', index: option.index });
    }
    if (typeof element.focus === 'function') {
      try { element.focus({ preventScroll: true }); } catch { element.focus(); }
    }
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
    return { target: summarizeRefTarget(ref, entry), selected };
  }

  function findRefOption(select, request) {
    const options = Array.from(select.options);
    if (Number.isInteger(request.index)) return options[request.index] || null;
    if (typeof request.value === 'string') return options.find(option => option.value === request.value) || null;
    if (typeof request.label === 'string') {
      const a11y = globalThis.__browserAgentBridgeDomA11y;
      return options.find(option => a11y.matchesText(option.label || option.textContent || '', request.label, {
        exact: request.exact === true,
        caseSensitive: request.caseSensitive === true
      })) || null;
    }
    return null;
  }

  function summarizeRefTarget(ref, entry) {
    const { element, offsetX = 0, offsetY = 0 } = entry;
    const rect = element.getBoundingClientRect();
    const localClickPoint = globalThis.__browserAgentBridgeDomA11y.clickablePoint(element);
    const clickPoint = localClickPoint ? { x: localClickPoint.x + offsetX, y: localClickPoint.y + offsetY } : null;
    // Reuse the shared dom-a11y atom so ref actionability matches locator.click
    // (covers element.hidden / [hidden] ancestor / aria-hidden / fieldset[disabled]).
    const a11y = globalThis.__browserAgentBridgeDomA11y;
    const visible = a11y.isVisible(element) || Array.from(element.labels || []).some(label => label.control === element && a11y.isVisible(label));
    const enabled = globalThis.__browserAgentBridgeDomA11y.isEnabled(element);
    const receivesEvents = a11y.receivesEvents(element, localClickPoint);
    const reasons = [];
    if (!visible) reasons.push('not visible');
    if (!enabled) reasons.push('disabled');
    const pointerEvents = getComputedStyle(element).pointerEvents !== 'none' || receivesEvents;
    if (!pointerEvents) reasons.push('pointer-events none');
    if (!clickPoint) reasons.push('no clickable point');
    if (!receivesEvents) reasons.push('covered by another element');
    return {
      ref,
      snapshotId: refState.snapshotId,
      frameId: refState.frameId,
      element: {
        ref,
        tagName: element.tagName.toLowerCase(),
        href: (element.closest?.('a[href]') || element).href || '',
        download: (element.closest?.('a[href]') || element).hasAttribute('download'),
        id: element.id || '',
        role: element.getAttribute('role') || globalThis.__browserAgentBridgeDomA11y.implicitRole(element) || '',
        accessibleName: globalThis.__browserAgentBridgeDomA11y.accessibleName(element),
        text: (element.innerText || element.textContent || '').trim().slice(0, 500),
        visible,
        disabled: !enabled,
        rect: { x: rect.x + offsetX, y: rect.y + offsetY, width: rect.width, height: rect.height },
        clickPoint
      },
      actionability: {
        actionable: visible && enabled && pointerEvents && Boolean(clickPoint) && receivesEvents,
        visible,
        enabled,
        receivesEvents,
        reasons
      }
    };
  }

  function valueOf(el) {
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) {
      if (el instanceof HTMLInputElement && (el.getAttribute('type') || '').toLowerCase() === 'password') return undefined;
      return el.value;
    }
    return undefined;
  }
})();
