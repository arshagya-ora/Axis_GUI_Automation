"""Real Chromium DOM regressions for native inputs; no external site or user profile."""
from pathlib import Path
import json
import pytest

playwright = pytest.importorskip('playwright.sync_api')
EXTENSION = Path(__file__).resolve().parents[1] / 'extension'


@pytest.fixture
def browser_page():
    with playwright.sync_playwright() as p:
        chrome = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
        options = {'executable_path': str(chrome)} if chrome.exists() else {}
        browser = p.chromium.launch(headless=True, **options)
        page = browser.new_page()
        page.set_content('''<form aria-label="Registration"><label for="dob">Date of Birth</label>
            <input id="dob" type="date"><label for="password">Password</label>
            <input id="password" type="password"></form>''')
        page.evaluate('''() => {
            window.listeners = [];
            window.chrome = {runtime:{onMessage:{addListener: listener => listeners.push(listener)}}};
            window.sendContent = message => new Promise((resolve,reject) => {
                for (const listener of listeners) if (listener(message,{},resolve)) return;
                reject(new Error('Unhandled content message'));
            });
            window.events = [];
            for (const kind of ['input','change']) dob.addEventListener(kind, () => events.push(kind));
        }''')
        page.add_script_tag(content=(EXTENSION/'content/dom-a11y.js').read_text(encoding='utf-8'))
        page.add_script_tag(content=(EXTENSION/'content/accessibility-tree.js').read_text(encoding='utf-8'))
        source = (EXTENSION/'sw/locator.js').read_text(encoding='utf-8').replace('export function ', 'function ')
        page.add_script_tag(content=source)
        page.evaluate('''() => {
            window.handlers = createLocatorHandlers({
                assertTabId: id => id, assertTabAllowed: async () => {},
                assertString: (value,name) => { if (typeof value !== 'string') throw new Error(name); },
                recordAction: async () => {}, attachDebugger: async () => {},
                cdp: async () => { throw new Error('Native date fills must not insert ordinary text.'); },
                defaultTimeoutMs: 100,
                resolveFrameTarget: async () => ({target:{tabId:1},frameId:0}),
                chromeApi: {
                    tabs:{sendMessage: (id,message) => sendContent(message)},
                    scripting:{executeScript: async ({func,args}) => [{result:await func(...args)}]}
                }
            });
            window.refreshRefs = async () => {
                const response = await sendContent({type:'GET_ACCESSIBILITY_TREE',snapshotId:'fixture',maxNodes:1000});
                window.dateRef = response.tree.nodes.find(node => node.name === 'Date of Birth').ref;
            };
        }''')
        page.evaluate('refreshRefs()')
        yield page
        browser.close()


@pytest.mark.parametrize('path', ['locator', 'ref'])
def test_native_date_fills_replaces_and_clears(browser_page, path):
    page = browser_page
    for value in ['1995-06-15','2000-02-29','']:
        result = page.evaluate('''async ({path,value}) => {
            const params = {tabId:1,text:value,timeoutMs:100,stable:false};
            return path === 'ref' ? handlers.locatorFillRef({...params,ref:dateRef})
                : handlers.locatorFill({...params,selector:'#dob'});
        }''', {'path':path,'value':value})
        assert result['ok']
        assert page.locator('#dob').input_value() == value
    assert page.evaluate('events') == ['input','change'] * 3


@pytest.mark.parametrize('path', ['locator', 'ref'])
def test_invalid_date_does_not_clear_existing_value(browser_page, path):
    page = browser_page
    page.locator('#dob').fill('1995-06-15')
    for value in ['06/15/1995','2025-02-30']:
        result = page.evaluate('''async ({path,value}) => {
            try {
                const params = {tabId:1,text:value,timeoutMs:100,stable:false};
                await (path === 'ref' ? handlers.locatorFillRef({...params,ref:dateRef})
                    : handlers.locatorFill({...params,selector:'#dob'}));
            } catch (error) { return error.code; }
        }''', {'path':path,'value':value})
        assert result == 'INVALID_ARGUMENT'
        assert page.locator('#dob').input_value() == '1995-06-15'


@pytest.mark.parametrize('path', ['locator', 'ref'])
def test_date_reversion_is_reported_as_failure(browser_page, path):
    page = browser_page
    page.evaluate("dob.addEventListener('change', () => { dob.value = ''; })")
    result = page.evaluate('''async path => {
        try {
            const params = {tabId:1,text:'1995-06-15',timeoutMs:100,stable:false};
            await (path === 'ref' ? handlers.locatorFillRef({...params,ref:dateRef})
                : handlers.locatorFill({...params,selector:'#dob'}));
        } catch (error) { return error.code; }
    }''', path)
    assert result == 'INPUT_VALUE_NOT_RETAINED'


def test_hidden_password_is_verified_without_revealing_it(browser_page):
    page = browser_page
    page.locator('#password').fill('fixture-secret')
    result = page.evaluate('''async () => {
        return handlers.expectLocatorToHaveValue({tabId:1,selector:'#password',expected:'fixture-secret',timeoutMs:100});
    }''')
    assert result['ok']
    assert 'fixture-secret' not in json.dumps(result)
    assert page.locator('#password').get_attribute('type') == 'password'
    failure = page.evaluate('''async () => {
        try { await handlers.expectLocatorToHaveValue({tabId:1,selector:'#password',expected:'wrong-secret',timeoutMs:10}); }
        catch (error) { return {code:error.code,diagnostic:error.diagnostic,message:error.message}; }
    }''')
    assert failure['code'] == 'LOCATOR_EXPECT_TIMEOUT'
    assert 'fixture-secret' not in json.dumps(failure) and 'wrong-secret' not in json.dumps(failure)
