import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { test } from 'node:test';

test('debugger attachment never activates a tab or focuses a window, including repeated attachment', async () => {
  const source = await readFile(new URL('../extension/service-worker.js', import.meta.url), 'utf8');
  const start = source.indexOf('async function attachDebugger(tabId)');
  const end = source.indexOf('function handleNativeNotification', start);
  const calls = [];
  const context = { attachedTabs: new Set(), waitForSessionState: async () => {}, saveAttachedTabs: async () => {},
    withTimeout: async p => p, DEFAULT_TIMEOUT_MS: 1000, CDP_VERSION: '1.3',
    chrome: { debugger: { attach: async target => calls.push(target.tabId) },
      tabs: { update: () => assert.fail('Unexpected activation') }, windows: { update: () => assert.fail('Unexpected focus') } } };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  await context.attachDebugger(7);
  await context.attachDebugger(7);
  assert.deepEqual(calls, [7]);
});
