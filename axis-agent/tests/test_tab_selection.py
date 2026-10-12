import pytest

from test_axis_agent import FakeBridge, Script, browse, make
from browser_tools import BrowserBridgeError


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class TabBridge(FakeBridge):
    def rpc(self, method, params=None, **kwargs):
        result = super().rpc(method, params, **kwargs)
        if method == 'page.accessibilityTree':
            tab = next((t for t in self.extra_tabs if t['id'] == params['tabId']), None)
            if tab:
                result.update(url=tab['url'], title=tab['title'], snapshot='[f0:settings] button "Settings"')
        return result


def stopped():
    return {'outcome': {'status': 'failed', 'reason': 'Inspection finished.', 'final_answer': 'Inspection finished.'}}


@pytest.mark.anyio
async def test_named_application_with_blank_title_reaches_the_navigator():
    bridge = TabBridge()
    bridge.title = ''
    bridge.url = 'http://10.75.181.51/index.html'
    script = Script([], [stopped()])
    orchestrator, _, _ = make(script, bridge=bridge, direct=True)
    result = await orchestrator.start_task('From CNCC-Core GUI, click NF Screening.')
    assert result.status == 'failed'
    assert script.navigator_calls == 1
    assert 'page.accessibilityTree' in bridge.methods
    assert 'http://10.75.181.51/index.html' in script.navigator_prompts[0]


@pytest.mark.anyio
async def test_navigator_can_inspect_and_select_a_relevant_background_tab():
    bridge = TabBridge()
    bridge.extra_tabs = [{'id': 42, 'title': 'Inventory', 'url': 'https://inventory.test/', 'active': False}]
    bridge.focused_id = 41
    script = Script([], [{
        'calls': [
            ('browser_observe', {'tab': 'tab_2'}),
            ('browser_tabs', {'command': {'operation': 'activate', 'tab': 'tab_2'}}),
        ],
        **stopped(),
    }])
    orchestrator, _, _ = make(script, bridge=bridge, direct=True)
    result = await orchestrator.start_task('From Inventory GUI, click Settings.')
    assert result.status == 'failed'
    assert 'Inventory' in script.navigator_prompts[0]
    observed = [params['tabId'] for method, params in bridge.calls if method == 'page.accessibilityTree']
    assert observed == [41, 42]
    assert orchestrator.tab == 'tab_2'
    assert orchestrator.state.authorized_origins['tab_2'] == ['https://inventory.test:443']


def test_focus_uses_the_last_focused_window_and_keeps_other_tabs_live():
    bridge = TabBridge()
    bridge.extra_tabs = [
        {'id': 42, 'title': 'First window', 'url': 'https://first.test/', 'active': True},
        {'id': 43, 'title': 'Second window', 'url': 'https://second.test/', 'active': True},
    ]
    bridge.focused_id = 43
    orchestrator, _, config = make(Script([]), bridge=bridge)
    orchestrator.state = config.new_memory('Inspect the current tab')
    assert orchestrator._ensure_tab() == 'tab_3'
    assert all(not tab.closed for tab in orchestrator.browser.tabs.values())
    assert bridge.calls[-1] == ('tabs.list', {'query': {'active': True, 'lastFocusedWindow': True}})


@pytest.mark.anyio
@pytest.mark.parametrize('reply', [
    'Current open tab',
    'I have opened the tab use that only',
    'what you want from me? I have opened the tab in my desktop as Inventory use that only',
])
async def test_current_tab_reply_rebinds_without_navigating_to_the_old_url(reply):
    bridge = TabBridge()
    script = Script([browse('Inspect the requested page')], [stopped()])
    orchestrator, _, config = make(script, bridge=bridge, direct=True)
    orchestrator.state = config.new_memory('From Inventory GUI, click Settings at https://www.google.com/.')
    assert orchestrator._ensure_tab() == 'tab_1'
    bridge.extra_tabs = [{'id': 42, 'title': '', 'url': 'https://inventory.test/', 'active': True}]
    bridge.focused_id = 42
    result = await orchestrator.continue_task(reply)
    assert result.status == 'failed'
    assert orchestrator.tab == 'tab_2'
    assert 'page.navigate' not in bridge.methods
    assert 'https://inventory.test/' in script.navigator_prompts[0]


def test_explicit_ip_matches_an_existing_origin_before_the_focused_tab():
    bridge = TabBridge()
    bridge.url = 'http://10.75.181.51/index.html'
    bridge.title = ''
    bridge.extra_tabs = [{'id': 42, 'title': 'Other', 'url': 'https://other.test/', 'active': True}]
    orchestrator, _, config = make(Script([]), bridge=bridge)
    orchestrator.state = config.new_memory('IP: 10.75.181.51:80\nFrom Console GUI, inspect Settings.')
    assert orchestrator._ensure_tab() == 'tab_1'


@pytest.mark.anyio
async def test_closed_explicit_tab_does_not_fall_back_to_another_tab():
    orchestrator, bridge, _ = make(Script([]), direct=True)
    result = await orchestrator.start_task('Use tab_99 only and inspect Settings.')
    assert result.status == 'needs_user'
    assert 'closed' in result.reason
    assert 'page.accessibilityTree' not in bridge.methods


@pytest.mark.anyio
async def test_unreadable_current_tab_does_not_create_a_replacement():
    bridge = TabBridge()
    bridge.url = 'chrome://settings/'
    orchestrator, _, _ = make(Script([]), bridge=bridge, direct=True)
    result = await orchestrator.start_task('Use the current tab only and inspect Settings.')
    assert result.status == 'needs_user'
    assert 'tabs.create' not in bridge.methods


@pytest.mark.anyio
async def test_duplicate_application_titles_are_inspected_instead_of_rejected():
    bridge = TabBridge()
    bridge.title = 'Inventory'
    bridge.extra_tabs = [{'id': 42, 'title': 'Inventory', 'url': 'https://inventory.test/', 'active': True}]
    script = Script([], [stopped()])
    orchestrator, _, _ = make(script, bridge=bridge, direct=True)
    result = await orchestrator.start_task('From Inventory GUI, inspect Settings.')
    assert result.status == 'failed'
    assert script.navigator_calls == 1
    assert len(orchestrator.state.last_browser_state.tabs) == 2


@pytest.mark.anyio
async def test_focus_lookup_failure_reports_the_bridge_error():
    bridge = TabBridge()
    original_rpc = bridge.rpc
    def rpc(method, params=None, **kwargs):
        if method == 'tabs.list' and (params or {}).get('query', {}).get('lastFocusedWindow'):
            raise BrowserBridgeError('Focus lookup failed.')
        return original_rpc(method, params, **kwargs)
    bridge.rpc = rpc
    orchestrator, _, _ = make(Script([]), bridge=bridge, direct=True)
    result = await orchestrator.start_task('Inspect the current tab.')
    assert result.status == 'failed'
    assert 'Focus lookup failed.' in result.reason


@pytest.mark.anyio
async def test_address_followup_navigates_to_the_new_address():
    bridge = TabBridge()
    orchestrator, _, config = make(Script([], [stopped()]), bridge=bridge, direct=True)
    orchestrator.state = config.new_memory('Inspect https://old.test/')
    orchestrator._ensure_tab()
    result = await orchestrator.continue_task('Use https://new.test/ instead.')
    assert result.status == 'failed'
    navigations = [params['url'] for method, params in bridge.calls if method == 'page.navigate']
    assert navigations == ['https://new.test/']
