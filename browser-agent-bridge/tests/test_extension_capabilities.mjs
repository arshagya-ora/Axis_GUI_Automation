import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

test('extension handshake advertises the control verification contract', async () => {
  const source = await readFile(new URL('../extension/service-worker.js', import.meta.url), 'utf8');
  const definition = source.match(/async function extensionInfo\(\) \{[\s\S]*?\n\}/)?.[0];
  assert.ok(definition);
  const context = vm.createContext({
    chrome: { runtime: { getManifest: () => ({ name: 'Bridge', version: '1.0' }), id: 'fixture' } },
    nativeStatus: { connected: true },
    rpcRouter: { 'extension.info': () => {}, 'permission.check': () => {} }
  });
  const result = await vm.runInContext(definition + '; extensionInfo()', context);
  assert.equal(result.capabilities.valuePropertyEvidence, true);
  assert.equal(result.capabilities.attributeParameter, true);
  assert.equal(result.tools.includes('extension.info'), true);
  assert.equal(result.tools.includes('permission.check'), false);
});
