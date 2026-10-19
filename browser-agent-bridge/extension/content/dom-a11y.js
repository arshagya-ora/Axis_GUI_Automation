(() => {
  if (globalThis.__browserAgentBridgeDomA11y?.fillNativeDate) return;

  function accessibleName(el, root = el?.getRootNode?.() || document) {
    if (isAriaHidden(el)) return '';
    return accessibleNameInternal(el, root, new Set()).trim().replace(/\s+/g, ' ').slice(0, 500);
  }

  function accessibleNameInternal(el, root, visited) {
    if (!el || visited.has(el)) return '';
    visited.add(el);
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const value = labelledBy.split(/\s+/)
        .map(id => getElementByIdDeep(root, id))
        .filter(Boolean)
        .map(label => accessibleNameInternal(label, root, visited) || visibleText(label))
        .join(' ')
        .trim();
      if (value) return value;
    }
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (el.id) {
      const label = querySelectorDeep(root, `label[for="${cssEscape(el.id)}"]`);
      if (label) return visibleText(label);
    }
    const wrappingLabel = el.closest?.('label');
    if (wrappingLabel) return visibleText(wrappingLabel);
    if (tag === 'img' || tag === 'area') return el.getAttribute('alt') || '';
    if (tag === 'input' && ['button', 'submit', 'reset'].includes(type)) {
      return el.value || el.getAttribute('value') || defaultInputButtonName(type);
    }
    if (tag === 'button') return visibleText(el);
    if (tag === 'fieldset') {
      const legend = Array.from(el.children || []).find(child => child.tagName?.toLowerCase() === 'legend');
      if (legend) return visibleText(legend);
    }
    if (tag === 'table') {
      const caption = querySelectorDeep(el, ':scope > caption');
      if (caption) return visibleText(caption);
    }
    return [
      el.getAttribute('title'),
      el.getAttribute('placeholder'),
      visibleText(el),
      'value' in el && type !== 'password' ? String(el.value) : ''
    ].filter(Boolean).join(' ').trim();
  }

  function implicitRole(el, root = document) {
    const explicit = firstExplicitRole(el);
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'area' && el.hasAttribute('href')) return 'link';
    if (tag === 'button' || ['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
    if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'textarea' || (tag === 'input' && ['email', 'password', 'tel', 'text', 'url', ''].includes(type))) return 'textbox';
    if (tag === 'input' && type === 'search') return 'searchbox';
    if (tag === 'input' && type === 'checkbox') return 'checkbox';
    if (tag === 'input' && type === 'radio') return 'radio';
    if (tag === 'input' && type === 'range') return 'slider';
    if (tag === 'input' && type === 'number') return 'spinbutton';
    if (tag === 'option') return 'option';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'img') return 'img';
    if (tag === 'article') return 'article';
    if (tag === 'aside') return 'complementary';
    if (tag === 'body') return 'document';
    if (tag === 'form' && accessibleName(el, root)) return 'form';
    if (tag === 'main') return 'main';
    if (tag === 'nav') return 'navigation';
    if (tag === 'section' && accessibleName(el, root)) return 'region';
    if (tag === 'ul' || tag === 'ol') return 'list';
    if (tag === 'li') return 'listitem';
    if (tag === 'table') return 'table';
    if (tag === 'th') return 'columnheader';
    if (tag === 'td') return 'cell';
    if (tag === 'tr') return 'row';
    if (tag === 'summary') return 'button';
    if (tag === 'details') return 'group';
    if (tag === 'dialog') return 'dialog';
    if (tag === 'progress') return 'progressbar';
    if (tag === 'meter') return 'meter';
    return '';
  }

  function firstExplicitRole(el) {
    const role = el.getAttribute('role');
    if (!role) return '';
    const token = role.trim().split(/\s+/).find(Boolean);
    if (!token || token === 'none' || token === 'presentation') return '';
    return token.toLowerCase();
  }

  function visibleText(el) {
    if (isAriaHidden(el)) return '';
    return (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
  }

  function isAriaHidden(el) {
    return Boolean(el.closest?.('[aria-hidden="true"]'));
  }

  function defaultInputButtonName(type) {
    if (type === 'submit') return 'Submit';
    if (type === 'reset') return 'Reset';
    return '';
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(value);
    return String(value).replace(/["\\]/g, '\\$&');
  }

  function querySelectorDeep(root, selector) {
    return querySelectorAllDeep(root, selector)[0] || null;
  }

  function querySelectorAllDeep(root, selector) {
    const results = [];
    const visited = new Set();
    visitRoot(root);
    return results;

    function visitRoot(currentRoot) {
      if (!currentRoot || visited.has(currentRoot)) return;
      visited.add(currentRoot);
      if (typeof currentRoot.querySelectorAll === 'function') {
        results.push(...currentRoot.querySelectorAll(selector));
        for (const element of currentRoot.querySelectorAll('*')) {
          if (element.shadowRoot) visitRoot(element.shadowRoot);
        }
      }
    }
  }

  function getElementByIdDeep(root, id) {
    return querySelectorDeep(root, `#${cssEscape(id)}`);
  }

  function isVisible(element) {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 &&
      rect.height > 0 &&
      style.visibility !== 'hidden' &&
      style.display !== 'none' &&
      style.opacity !== '0' &&
      !element.hidden &&
      !element.closest('[hidden]') &&
      !isAriaHidden(element);
  }

  // Shared by semantic locators and snapshot refs. Prefer an exposed point in
  // a real client rect; account for scroll-container clipping before hit tests.
  function clickablePoint(element) {
    const doc = element.ownerDocument || document;
    const view = doc.defaultView || window;
    let fallback = null;
    for (const candidate of [element, ...Array.from(element.labels || []).filter(label => label.control === element && isVisible(label))]) {
      const boxes = candidate.getClientRects ? Array.from(candidate.getClientRects()) : [candidate.getBoundingClientRect()];
      for (const box of boxes) {
        let left = Math.max(0, box.x), top = Math.max(0, box.y);
        let right = Math.min(view.innerWidth ?? Infinity, box.x + box.width);
        let bottom = Math.min(view.innerHeight ?? Infinity, box.y + box.height);
        const visited = new Set();
        for (let parent = candidate.parentElement || candidate.getRootNode?.().host;
             parent && !visited.has(parent); parent = parent.parentElement || parent.getRootNode?.().host) {
          visited.add(parent);
          const style = getComputedStyle(parent), rect = parent.getBoundingClientRect();
          if (/(auto|scroll|hidden|clip)/.test(style.overflowX || style.overflow || '')) {
            left = Math.max(left, rect.x); right = Math.min(right, rect.x + rect.width);
          }
          if (/(auto|scroll|hidden|clip)/.test(style.overflowY || style.overflow || '')) {
            top = Math.max(top, rect.y); bottom = Math.min(bottom, rect.y + rect.height);
          }
        }
        if (right <= left || bottom <= top) continue;
        for (const [fx, fy] of [[.5,.5],[.2,.2],[.8,.2],[.2,.8],[.8,.8]]) {
          const point = { x: left + (right-left)*fx, y: top + (bottom-top)*fy };
          fallback ||= point;
          if (receivesEvents(element, point)) return point;
        }
      }
    }
    return fallback;
  }

  function receivesEvents(element, point) {
    const doc = element.ownerDocument || document;
    if (!point) return false;
    if (typeof doc.elementFromPoint !== 'function') return true;
    let hit = doc.elementFromPoint(point.x, point.y);
    const visited = new Set();
    while (hit?.shadowRoot?.elementFromPoint && !visited.has(hit)) {
      visited.add(hit);
      const inner = hit.shadowRoot.elementFromPoint(point.x, point.y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    if (hit && (hit === element || element.contains?.(hit))) return true;
    const label = hit?.closest?.('label');
    const interactive = hit?.closest?.('a[href],button,input,select,textarea');
    return Boolean(label?.control === element && !interactive);
  }

  function isHiddenForRole(element) {
    return !isVisible(element) || isAriaHidden(element);
  }

  function isEnabled(element) {
    return !Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true' || element.closest('fieldset[disabled]'));
  }

  function isTextInputElement(element) {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute('type') || '').toLowerCase();
    if (tag === 'textarea') return true;
    if (tag !== 'input') return false;
    return !['button', 'checkbox', 'color', 'file', 'hidden', 'image', 'radio', 'range', 'reset', 'submit'].includes(type);
  }

  function isEditable(element) {
    if (!isEnabled(element)) return false;
    if (element.isContentEditable) return true;
    const tag = element.tagName.toLowerCase();
    if (tag === 'select') return true;
    if (!isTextInputElement(element)) return false;
    if (element.readOnly) return false;
    return true;
  }

  function matchesDisabled(element, expected) {
    return expected ? !isEnabled(element) : isEnabled(element);
  }

  function headingLevel(element) {
    const ariaLevel = Number.parseInt(element.getAttribute('aria-level') || '', 10);
    if (Number.isInteger(ariaLevel)) return ariaLevel;
    const tag = element.tagName.toLowerCase();
    return /^h[1-6]$/.test(tag) ? Number.parseInt(tag.slice(1), 10) : null;
  }

  function ariaBooleanState(element, state) {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute('type') || '').toLowerCase();
    if (state === 'checked' && tag === 'input' && ['checkbox', 'radio'].includes(type)) return element.checked;
    if (state === 'selected' && tag === 'option') return element.selected;
    if (state === 'expanded' && tag === 'details') return element.open;
    const value = element.getAttribute(`aria-${state}`);
    if (value === 'true') return true;
    if (value === 'false') return false;
    return null;
  }

  function matchesText(value, expected, locator) {
    if (locator.regex === true) {
      try {
        return new RegExp(String(expected), locator.caseSensitive ? '' : 'i').test(String(value));
      } catch {
        return false;
      }
    }
    const source = locator.caseSensitive ? String(value) : String(value).toLowerCase();
    const needle = locator.caseSensitive ? String(expected) : String(expected).toLowerCase();
    return locator.exact ? source.trim() === needle.trim() : source.includes(needle);
  }

  async function fillNativeDate(element, value) {
    const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };
    if (element.tagName?.toLowerCase() !== 'input' || element.type !== 'date')
      fail('INVALID_ARGUMENT', 'Native date filling requires an input of type date.');
    if (!isEditable(element)) fail('TARGET_DISABLED', 'The date input is not editable.');
    // Validate without changing the live field; browsers sanitize invalid dates.
    const probe = element.ownerDocument.createElement('input');
    probe.type = 'date';
    probe.value = value;
    if (typeof value !== 'string' || probe.value !== value)
      fail('INVALID_ARGUMENT', 'Use a valid ISO date (YYYY-MM-DD), or an empty string to clear it.');
    const view = element.ownerDocument.defaultView;
    const setter = Object.getOwnPropertyDescriptor(view.HTMLInputElement.prototype, 'value').set;
    element.focus({ preventScroll: true });
    setter.call(element, value);
    element.dispatchEvent(new view.Event('input', { bubbles: true, composed: true }));
    element.dispatchEvent(new view.Event('change', { bubbles: true }));
    await new Promise(resolve => setTimeout(resolve, 0));
    if (!element.isConnected || element.value !== value)
      fail('INPUT_VALUE_NOT_RETAINED', 'The date input did not retain the requested value; inspect its current state.');
  }

  globalThis.__browserAgentBridgeDomA11y = {
    fillNativeDate,
    accessibleName,
    implicitRole,
    firstExplicitRole,
    visibleText,
    isAriaHidden,
    cssEscape,
    querySelectorDeep,
    querySelectorAllDeep,
    getElementByIdDeep,
    isVisible,
    clickablePoint,
    receivesEvents,
    isHiddenForRole,
    isEnabled,
    isEditable,
    isTextInputElement,
    matchesDisabled,
    headingLevel,
    ariaBooleanState,
    matchesText
  };
})();
