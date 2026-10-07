import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const source = await readFile(new URL('../extension/sw/page.js', import.meta.url), 'utf8');
const { createPageHandlers } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function fixture(error, persistent = false) {
  let reads = 0, installs = 0;
  const handlers = createPageHandlers({
    assertTabId: id => id, assertTabAllowed: async () => {},
    ensureContentScripts: async () => { installs++; },
    chromeApi: {
      tabs: {sendMessage: async () => {
        reads++;
        if (persistent || reads === 1) throw new Error(error);
        return {ok: true, tree: {nodes: [], iframes: [], url: 'http://10.75.181.51/', title: 'Login'}};
      }},
      webNavigation: {getAllFrames: async () => []},
    },
  });
  return {handlers, counts: () => ({reads, installs})};
}

test('a replaced content-script receiver is repaired before retrying a page read', async () => {
  const f = fixture('Could not establish connection. Receiving end does not exist.');
  const result = await f.handlers.pageAccessibilityTree({tabId: 1});
  assert.equal(result.title, 'Login');
  assert.deepEqual(f.counts(), {reads: 2, installs: 2});
});

test('missing receiver recovery is bounded and permission failures are not retried', async () => {
  for (const [error, reads] of [['Receiving end does not exist.', 2], ['Access denied', 1]]) {
    const f = fixture(error, true);
    await assert.rejects(f.handlers.pageAccessibilityTree({tabId: 1}), new RegExp(error));
    assert.equal(f.counts().reads, reads);
  }
});
