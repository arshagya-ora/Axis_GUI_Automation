import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_core import ValidationError
from pydantic_ai.tools import Tool


AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

import browser_tools as bt  # noqa: E402


LEGACY_SHA256 = "cb3f8cbcf7dc8da89f596189fd2df88459d6dc9ab88f0daa2389253802e63f1d"

TOOL_FUNCTIONS = (
    bt.browser_tabs,
    bt.browser_observe,
    bt.browser_act,
    bt.browser_navigate,
    bt.browser_wait,
    bt.browser_assert,
    bt.browser_capture_evidence,
    bt.browser_diagnose,
    bt.browser_downloads,
)


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.saved = []

    def rpc(self, method, params=None, **_):
        self.calls.append((method, params or {}))
        response = self.responses.get(method, {})
        return response() if callable(response) else response

    def save_data_url(self, data_url, filename=None, directory=None):
        self.saved.append((data_url, filename, directory))
        return {"path": f"artifacts/{filename or 'artifact'}", "bytes": len(data_url)}


def context(runtime):
    return SimpleNamespace(deps=runtime)


def runtime_with_tab(url="https://example.test/"):
    bridge = FakeBridge()
    runtime = bt.BrowserRuntime(bridge)
    alias = runtime._register({"id": 41, "url": url, "title": "Fixture", "active": True})
    return bridge, runtime, alias


def test_url_assertion_uses_exact_url_matching_and_returns_expected_value():
    bridge, runtime, tab = runtime_with_tab()
    expected = "https://example.test/receipt?order=42"
    bridge.responses["page.waitForURL"] = {"ok": True, "url": expected}
    result = bt.browser_assert(context(runtime), tab, bt.URLAssertion(assertion="url", expected=expected, timeout_ms=1000))
    assert result["ok"] and result["data"]["passed"]
    assert result["data"]["expected"] == expected
    assert result["data"]["match"] == "exact"
    method, params = bridge.calls[-1]
    assert method == "page.waitForURL"
    assert params == {"tabId": 41, "timeoutMs": 1000, "url": expected}


def test_url_assertion_timeout_is_a_failed_assertion_instead_of_success_or_transport_error():
    bridge, runtime, tab = runtime_with_tab()
    expected = "https://example.test/receipt"

    def timeout():
        raise bt.BrowserBridgeError("Timed out waiting for exact URL", data={"code": "PAGE_WAIT_FOR_URL_TIMEOUT"})

    bridge.responses["page.waitForURL"] = timeout
    result = bt.browser_assert(context(runtime), tab, bt.URLAssertion(assertion="url", expected=expected))
    assert result["ok"] is True
    assert result["data"]["passed"] is False
    assert result["data"]["expected"] == expected
    assert result["data"]["detail"]["code"] == "PAGE_WAIT_FOR_URL_TIMEOUT"


def test_text_assertion_keeps_actual_value_and_ambiguity_is_not_a_value_mismatch():
    bridge, runtime, tab = runtime_with_tab()
    command = bt.TextAssertion(assertion='text', locator=bt.Locator(role='combobox', name='Status'), expected='ENABLED')

    def mismatch():
        raise bt.BrowserBridgeError('Text did not match', data={'code': 'LOCATOR_EXPECT_TIMEOUT',
            'diagnostic': {'actual': 'Status DISABLED', 'count': 1}})

    bridge.responses['expect.locator.toHaveText'] = mismatch
    result = bt.browser_assert(context(runtime), tab, command)
    assert result['ok'] and not result['data']['passed']
    assert result['data']['actual'] == 'Status DISABLED'
    assert result['data']['count'] == 1
    assert result['data']['match'] == 'exact'

    def ambiguous():
        raise bt.BrowserBridgeError('Multiple controls match', data={'code': 'LOCATOR_STRICT_MODE_VIOLATION',
            'diagnostic': {'count': 3}})

    bridge.responses['expect.locator.toHaveText'] = ambiguous
    result = bt.browser_assert(context(runtime), tab, command)
    assert not result['ok']
    assert result['error']['code'] == 'LOCATOR_STRICT_MODE_VIOLATION'
    assert 'passed' not in (result.get('data') or {})


def test_attribute_extraction_keeps_attribute_separate_from_locator_name():
    bridge, runtime, tab = runtime_with_tab()
    locator = bt.Locator(role='textbox', name='Default Load', exact=True)
    bridge.responses['locator.getAttribute'] = {'value': '10'}
    result = bt.browser_observe(context(runtime), tab, locator=locator, extract='attribute', attribute='value')
    assert result['ok']
    params = bridge.calls[-1][1]
    assert params['locator']['name'] == 'Default Load'
    assert params['attribute'] == 'value'
    assert 'name' not in params


def test_field_observations_exclude_sensitive_fields_and_remain_bounded():
    bridge, runtime, tab = runtime_with_tab()
    bridge.responses['page.accessibilityTree'] = {'snapshot': '', 'fields': [
        {'name': 'Password', 'value': 'hidden'}, {'name': 'Limit', 'value': '0'},
        *[{'name': str(i), 'value': 'x' * 500} for i in range(40)],
    ]}
    result = bt.browser_observe(context(runtime), tab)
    assert result['ok']
    assert 'hidden' not in json.dumps(result['data']['fields'])
    assert result['data']['fields'][0]['value'] == '0'
    assert len(json.dumps(result['data']['fields'])) < 8200
    assert not result['data']['fields_complete']
    assert 'Earlier snapshot refs have been replaced' in result['data']['ref_notice']


def test_all_nine_functions_are_independently_selectable():
    names = [function.__name__ for function in TOOL_FUNCTIONS]
    assert names == [
        "browser_tabs", "browser_observe", "browser_act", "browser_navigate",
        "browser_wait", "browser_assert", "browser_capture_evidence", "browser_diagnose",
        "browser_downloads",
    ]
    selected = [bt.browser_tabs, bt.browser_observe, bt.browser_navigate]
    assert [Tool(function, takes_ctx=True).name for function in selected] == [
        "browser_tabs", "browser_observe", "browser_navigate",
    ]


def test_pydantic_generates_bounded_schemas_without_internal_identifiers():
    forbidden = {
        "browserSessionId", "observationId", "snapshotId", "frameId", "tabId",
        "windowId", "groupId", "sessionId", "bridgeMethod", "method",
    }
    schemas = []
    for function in TOOL_FUNCTIONS:
        schema = Tool(function, takes_ctx=True).tool_def.parameters_json_schema
        schemas.append(schema)
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        serialized = json.dumps(schema)
        assert not any(f'"{name}"' in serialized for name in forbidden)
        assert "page.executeJavaScript" not in serialized
        assert "tabs.list" not in serialized
    assert "oneOf" in json.dumps(Tool(bt.browser_act, takes_ctx=True).tool_def.parameters_json_schema)
    # The six hot-path schemas stay compact. Specialist evidence, diagnostics,
    # and downloads are progressively disclosed and do not burden normal runs.
    assert len(json.dumps(schemas[:6])) < 25_000
    assert len(json.dumps(schemas)) < 32_000


def test_pydantic_rejects_an_incomplete_action_before_the_function_runs():
    bridge, runtime, alias = runtime_with_tab()
    tool = Tool(bt.browser_act, takes_ctx=True)
    with pytest.raises(ValidationError):
        tool.function_schema.validator.validate_python({
            "tab": alias,
            "command": {
                "action": "fill",
                "target": {"kind": "locator", "locator": {"label": "Search"}},
            },
        })
    assert bridge.calls == []


def test_stateful_validation_rejects_bad_hover_before_rpc():
    bridge, runtime, alias = runtime_with_tab()
    result = bt.browser_act(context(runtime), alias, bt.Hover(
        action="hover",
        target=bt.LocatorTarget(kind="locator", locator=bt.Locator(role="button", name="Menu")),
    ))
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert bridge.calls == []


def test_observation_hides_bridge_refs_and_navigation_makes_them_stale():
    bridge, runtime, alias = runtime_with_tab()
    bridge.responses["page.accessibilityTree"] = {
        "snapshotId": "raw-snapshot-9",
        "snapshot": '[f7:bridge-node-3] textbox "Search"',
        "url": "https://example.test/",
        "title": "Fixture",
    }
    observed = bt.browser_observe(context(runtime), alias)
    assert observed["ok"] is True
    assert observed["data"]["snapshot"] == '[ref_1] textbox "Search"'
    assert "raw-snapshot" not in json.dumps(observed)
    assert "bridge-node" not in json.dumps(observed)

    bridge.responses["page.reload"] = {"tab": {"url": "https://example.test/"}}
    navigated = bt.browser_navigate(context(runtime), alias, bt.History(operation="reload"))
    assert navigated["ok"] is True
    calls_before_stale_action = len(bridge.calls)

    stale = bt.browser_act(context(runtime), alias, bt.Click(
        action="click", target=bt.RefTarget(kind="ref", ref="ref_1"),
    ))
    assert stale["ok"] is False
    assert stale["error"]["code"] == "STALE_REFERENCE"
    assert len(bridge.calls) == calls_before_stale_action


def test_current_ref_resolves_only_inside_runtime():
    bridge, runtime, alias = runtime_with_tab()
    bridge.responses["page.accessibilityTree"] = {
        "snapshotId": "snap-secret",
        "snapshot": '[f2:node-secret] button "Go"',
    }
    bt.browser_observe(context(runtime), alias)
    bt.browser_act(context(runtime), alias, bt.Click(
        action="click", target=bt.RefTarget(kind="ref", ref="ref_1"),
    ))
    method, params = bridge.calls[-1]
    assert method == "locator.clickRef"
    assert params["tabId"] == 41
    assert params["frameId"] == 2
    assert params["snapshotId"] == "snap-secret"
    assert params["ref"] == "node-secret"


def test_firewall_blocks_urls_and_methods_before_rpc():
    bridge, runtime, alias = runtime_with_tab()
    denied = bt.browser_navigate(
        context(runtime), alias, bt.Open(operation="open", url="javascript:alert(1)"),
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "FIREWALL_DENIED"
    assert bridge.calls == []

    with pytest.raises(bt.ToolFault, match="not allowed"):
        runtime._rpc("page.executeJavaScript", {"tabId": 41}, tab=alias)
    assert bridge.calls == []


def test_diagnostic_output_is_redacted_and_bounded():
    bridge, runtime, alias = runtime_with_tab()
    bridge.responses["console.read"] = {
        "events": [
            {"message": "x" * (bt.MAX_TEXT + 50), "authorization": "Bearer secret"},
            *({"message": f"event-{i}"} for i in range(249)),
        ]
    }
    result = bt.browser_diagnose(
        context(runtime), alias, bt.BufferedDiagnostic(diagnostic="console", limit=500),
    )
    events = result["data"]["events"]
    assert result["ok"] is True
    assert events[0]["authorization"] == "<redacted>"
    assert len(events[0]["message"]) < bt.MAX_TEXT + 100
    assert len(events) == bt.MAX_ITEMS
    assert result["data"]["truncated"] is True


def test_sensitive_diagnostics_and_unsafe_artifacts_never_reach_bridge():
    bridge, runtime, alias = runtime_with_tab()
    blocked = bt.browser_diagnose(
        context(runtime), alias,
        bt.ResponseBodyDiagnostic(diagnostic="response_body", request="request_1"),
    )
    assert blocked["error"]["code"] == "SENSITIVE_OPERATION_BLOCKED"

    unsafe = bt.browser_capture_evidence(
        context(runtime), alias,
        bt.PageCapture(capture="page_screenshot", filename="../escape.png"),
    )
    assert unsafe["error"]["code"] == "INVALID_ARGUMENT"
    assert bridge.calls == []
    assert bridge.saved == []


def test_locator_actions_use_visible_strict_stable_bridge_defaults():
    bridge, runtime, alias = runtime_with_tab()
    command = bt.Fill(
        action="fill",
        target=bt.LocatorTarget(
            kind="locator",
            locator=bt.Locator(
                role="textbox", name="Message",
                within=bt.Locator(selector="main", has_text="Chat"),
                frame_selector="iframe.app",
            ),
        ),
        value="hello",
    )
    result = bt.browser_act(context(runtime), alias, command)
    assert result["ok"] is True
    method, params = bridge.calls[-1]
    assert method == "locator.fill"
    assert params["locator"]["visible"] is True
    assert params["locator"]["within"]["hasText"] == "Chat"
    assert params["frameSelector"] == "iframe.app"
    assert params["strict"] is True
    assert params["stable"] is True
    assert params["timeoutMs"] == bt.DEFAULT_ACTION_TIMEOUT_MS


def test_structured_bridge_error_code_and_diagnostic_are_preserved():
    bridge, runtime, alias = runtime_with_tab()

    def fail():
        raise bt.BrowserBridgeError(
            "not actionable",
            {"code": "LOCATOR_ACTIONABILITY_TIMEOUT", "diagnostic": {"visibleCount": 0}},
        )

    bridge.responses["locator.click"] = fail
    result = bt.browser_act(
        context(runtime), alias,
        bt.Click(action="click", target=bt.LocatorTarget(
            kind="locator", locator=bt.Locator(role="button", name="Save"),
        )),
    )
    assert result["error"]["code"] == "LOCATOR_ACTIONABILITY_TIMEOUT"
    assert result["error"]["retryable"] is True
    assert result["error"]["detail"]["diagnostic"]["visibleCount"] == 0


def test_bridge_exception_messages_do_not_expose_raw_identifiers():
    bridge, runtime, alias = runtime_with_tab()

    def fail():
        raise bt.BrowserBridgeError(
            "FrameId 88 failed in tabId 41", {"code": "TERMINAL_BRIDGE_ERROR"},
        )

    bridge.responses["locator.click"] = fail
    result = bt.browser_act(
        context(runtime), alias,
        bt.Click(action="click", target=bt.LocatorTarget(
            kind="locator", locator=bt.Locator(role="button", name="Save"),
        )),
    )
    assert "88" not in result["error"]["message"]
    assert "41" not in result["error"]["message"]
    assert "frame identifier" in result["error"]["message"].lower()


def test_observe_supports_locator_inspection_and_frames_without_raw_ids():
    bridge, runtime, alias = runtime_with_tab()
    bridge.responses["locator.count"] = {"count": 2, "visibleCount": 1}
    counted = bt.browser_observe(
        context(runtime), alias, locator=bt.Locator(role="button", name="Save"), extract="count",
    )
    assert counted["data"]["result"] == {"count": 2, "visibleCount": 1}

    bridge.responses["page.frames"] = {
        "frames": [{"frameId": 7, "parentFrameId": 0, "url": "https://frame.example/"}],
    }
    frames = bt.browser_observe(context(runtime), alias, mode="frames")
    assert frames["data"]["frames"] == [{"url": "https://frame.example/"}]
    assert "frameId" not in json.dumps(frames)


def test_locator_text_observe_without_extract_defaults_to_all_inner_text():
    bridge, runtime, alias = runtime_with_tab()
    bridge.responses["locator.allInnerTexts"] = {"texts": ["First", "Second"]}
    result = bt.browser_observe(
        context(runtime), alias, mode="text", locator=bt.Locator(role="list", name="Results"),
    )
    method, params = bridge.calls[-1]
    assert result["ok"] is True
    assert result["data"]["extract"] == "all_inner_text"
    assert method == "locator.allInnerTexts"
    assert params["locator"]["role"] == "list"


def test_popup_wait_registers_a_safe_tab_alias():
    bridge, runtime, alias = runtime_with_tab()
    bridge.responses["page.waitForPopup"] = {
        "tab": {"id": 99, "url": "https://login.example/callback", "title": "Signed in"},
        "elapsedMs": 12,
    }
    result = bt.browser_wait(
        context(runtime), alias,
        bt.EventWait(condition="popup", url_contains="callback"),
    )
    assert result["data"]["openedTab"] == "tab_2"
    assert result["data"]["result"]["tab"]["tab"] == "tab_2"
    assert "99" not in json.dumps(result)


def test_network_wait_maps_typed_filters_and_aliases_request_ids():
    bridge, runtime, alias = runtime_with_tab()
    bridge.responses["page.waitForResponse"] = {
        "response": {"requestId": "raw-request", "url": "https://api.example/items", "status": 201},
    }
    result = bt.browser_wait(
        context(runtime), alias,
        bt.ResponseWait(
            condition="response", url_contains="/items", status=201,
            http_method="post", mime_type="application/json",
        ),
    )
    method, params = bridge.calls[-1]
    assert method == "page.waitForResponse"
    assert params["urlContains"] == "/items"
    assert params["method"] == "post"
    assert params["mimeType"] == "application/json"
    assert result["data"]["result"]["response"]["request"] == "request_1"
    assert "raw-request" not in json.dumps(result)


def test_nested_network_diagnostics_preserve_failed_response_and_body_alias():
    bridge, runtime, alias = runtime_with_tab()
    bridge.responses["network.read"] = {"events": [
        {"method": "Network.requestWillBeSent", "params": {"requestId": "raw-id",
         "request": {"url": "https://example.test/unavailable", "method": "GET", "headers": {"Authorization": "secret-value"}}}},
        {"method": "Network.responseReceived", "params": {"requestId": "raw-id",
         "response": {"url": "https://example.test/unavailable", "status": 503, "mimeType": "text/plain"}}},
        {"method": "Network.responseReceivedExtraInfo", "params": {"requestId": "raw-id", "statusCode": 503}},
        {"method": "Network.loadingFailed", "params": {"requestId": "raw-id", "errorText": "net::ERR_FAILED"}},
    ]}
    result = bt.browser_diagnose(context(runtime), alias, bt.BufferedDiagnostic(diagnostic="network", limit=100))
    assert result["ok"]
    event, = result["data"]["events"]
    assert event["status"] == 503 and event["method"] == "GET"
    assert event["errorText"] == "net::ERR_FAILED"
    assert runtime.request(event["request"]) == "raw-id"
    assert "secret-value" not in json.dumps(result) and "raw-id" not in json.dumps(result)


def test_network_request_alias_storage_is_bounded():
    _, runtime, _ = runtime_with_tab()
    runtime.public_requests([{"requestId": str(i)} for i in range(bt.MAX_ITEMS + 10)])
    assert len(runtime.requests) == bt.MAX_ITEMS


def test_download_tool_hides_download_ids():
    bridge, runtime, _ = runtime_with_tab()
    bridge.responses["downloads.list"] = {
        "items": [{"id": 12, "filename": "report.csv", "state": "complete", "url": "https://x.test/file"}],
    }
    result = bt.browser_downloads(context(runtime), bt.ListDownloads(operation="list"))
    assert result["data"]["items"][0]["filename"] == "report.csv"
    assert "\"id\"" not in json.dumps(result)


def test_capability_negotiation_prevents_unadvertised_rpc_calls():
    bridge, runtime, alias = runtime_with_tab()
    bridge.responses["extension.info"] = {"tools": ["tabs.list"]}
    result = bt.browser_act(
        context(runtime), alias,
        bt.Click(action="click", target=bt.LocatorTarget(
            kind="locator", locator=bt.Locator(role="button", name="Save"),
        )),
    )
    assert result["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert "locator.click" not in [method for method, _ in bridge.calls]


def test_site_pattern_preflight_is_attached_to_observation():
    bridge, runtime, alias = runtime_with_tab("https://app.example.test/path")
    bridge.responses["extension.info"] = {"tools": ["page.accessibilityTree", "native.sitePatterns"]}
    bridge.responses["native.sitePatterns"] = {
        "patterns": [{
            "domain": "example.test", "summary": "Known app shell",
            "content": "Use the visible composer and wait for Save.",
        }],
    }
    bridge.responses["page.accessibilityTree"] = {"snapshotId": "s", "snapshot": "", "url": "https://app.example.test/path"}
    result = bt.browser_observe(context(runtime), alias)
    assert result["data"]["sitePattern"]["domain"] == "example.test"
    assert "visible composer" in result["data"]["sitePattern"]["notes"]


def test_debug_telemetry_is_bounded_redacted_and_uses_safe_aliases():
    bridge, runtime, alias = runtime_with_tab()
    bridge.responses["extension.info"] = {
        "tools": ["recording.start", "recording.stop", "trace.start", "trace.stop", "native.sitePatterns"],
    }
    bridge.responses["recording.start"] = {"recording": {"id": "raw-recording"}}
    bridge.responses["trace.start"] = {"trace": {"id": "raw-trace"}}
    runtime.start_debug_telemetry(alias)
    assert runtime.debug_recording == "recording_1"
    assert runtime.debug_trace == "trace_1"
    start_params = {method: params for method, params in bridge.calls}
    assert start_params["recording.start"]["includeText"] is False
    assert start_params["recording.start"]["captureScreenshots"] is False
    assert start_params["recording.start"]["maxActions"] == 250
    assert start_params["trace.start"]["includeText"] is False
    stopped = runtime.stop_debug_telemetry()
    assert stopped == {"recording": "recording_1", "trace": "trace_1"}


def test_new_module_is_substantially_smaller_and_legacy_file_is_unchanged():
    legacy = AGENT_DIR / "browser_agent_tools.py"
    new = AGENT_DIR / "browser_tools.py"
    # Freeze committed source, independent of Windows checkout line endings.
    assert hashlib.sha256(legacy.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == LEGACY_SHA256
    # The optional visual specialist expands capability without growing the
    # six ordinary tool schemas (bounded separately above).
    assert len(new.read_text(encoding="utf-8").splitlines()) < len(legacy.read_text(encoding="utf-8").splitlines())


@pytest.mark.parametrize('mode', ['compact', 'aria'])
def test_scoped_observation_keeps_roles_states_and_actionable_refs(mode):
    bridge, runtime, tab = runtime_with_tab()
    bridge.responses['page.accessibilityTree'] = {
        'snapshot': '[f7:ref_3] button "Save" [disabled=true] context="dialog Preferences"',
        'snapshotId': 'scoped-1', 'url': 'https://example.test/'}
    result = bt.browser_observe(context(runtime), tab, mode=mode,
                                locator=bt.Locator(role='dialog', name='Preferences'), max_nodes=80)
    assert result['ok']
    assert '[disabled=true]' in result['data']['snapshot']
    assert '[ref_1]' in result['data']['snapshot']
    assert runtime.observations[tab].refs['ref_1'] == (7, 'ref_3')
    method, params = bridge.calls[-1]
    assert method == 'page.accessibilityTree'
    assert params['format'] == mode and params['maxNodes'] == 80
    assert params['locator']['role'] == 'dialog'


def test_reading_another_format_does_not_discard_document_refs():
    bridge, runtime, tab = runtime_with_tab()
    runtime.observe_refs(tab, '[f0:ref_1] button "Save"', 'snap-1')
    previous = runtime.observations[tab]
    bridge.responses['page.ariaSnapshot'] = {'snapshot': '- button "Save"'}
    runtime.observe(tab, 'aria')
    assert runtime.observations[tab] is previous
    runtime.invalidate(tab)
    assert tab not in runtime.observations


@pytest.mark.parametrize('capabilities, warning', [
    ({'valuePropertyEvidence': True, 'attributeParameter': True, 'nativeDateFill': True, 'privateValueAssertions': True}, False),
    ({'valuePropertyEvidence': True, 'attributeParameter': True}, True),
    ({'valuePropertyEvidence': True}, True),
    ({}, True),
])
def test_bridge_handshake_reports_control_protocol_compatibility(capabilities, warning):
    bridge, runtime, tab = runtime_with_tab()
    bridge.responses['extension.info'] = {'version': '1.0', 'capabilities': capabilities}
    bridge.responses['page.accessibilityTree'] = {'snapshotId': 's', 'snapshot': '', 'url': 'https://example.test'}
    result = bt.browser_observe(context(runtime), tab)
    assert result['ok']
    assert bool(result['data'].get('runtimeWarning')) == warning
    assert runtime.bridge_capabilities == frozenset(k for k, v in capabilities.items() if v is True)


def test_legacy_attribute_protocol_failure_is_actionable_and_not_retried():
    bridge, runtime, tab = runtime_with_tab()

    def legacy():
        raise bt.BrowserBridgeError('name must be a non-empty string')

    bridge.responses['locator.getAttribute'] = legacy
    result = bt.browser_observe(context(runtime), tab, locator=bt.Locator(role='textbox', name='Status'),
                                extract='attribute', attribute='title')
    assert result['error']['code'] == 'BRIDGE_PROTOCOL_MISMATCH'
    assert 'Reload or upgrade' in result['error']['message']
    assert len([method for method, _ in bridge.calls if method == 'locator.getAttribute']) == 1


def test_text_assertion_retains_property_metadata_without_changing_expected_result():
    bridge, runtime, tab = runtime_with_tab()
    bridge.responses['expect.locator.toHaveText'] = {
        'ok': True, 'actual': 'ENABLED', 'count': 1, 'element': {'hasValue': False}}
    result = bt.browser_assert(context(runtime), tab, bt.TextAssertion(
        assertion='text', locator=bt.Locator(role='textbox', name='Status'), expected='Enabled'))
    assert result['data']['passed']
    assert result['data']['expected'] == 'Enabled'
    assert result['data']['actual']['element']['hasValue'] is False


def test_old_password_verifier_requests_reload_instead_of_reporting_empty():
    bridge, runtime, tab = runtime_with_tab()
    bridge.responses['extension.info'] = {'methods':['expect.locator.toHaveValue'],'capabilities':{}}
    def legacy():
        raise bt.BrowserBridgeError('Value check timed out', data={'code':'LOCATOR_EXPECT_TIMEOUT',
            'diagnostic':{'actual':'','last':{'element':{'type':'password'}}}})
    bridge.responses['expect.locator.toHaveValue'] = legacy
    result = bt.browser_assert(context(runtime),tab,bt.ValueAssertion(assertion='value',locator=bt.Locator(label='Password'),expected='fixture-secret'))
    assert result['error']['code'] == 'BRIDGE_PROTOCOL_MISMATCH'
    assert 'Reload' in result['error']['message']
    assert 'fixture-secret' not in json.dumps(result)


@pytest.mark.parametrize('current', [False, True])
def test_password_check_uses_live_capabilities_without_revealing_value(current):
    bridge, runtime, tab = runtime_with_tab()
    methods = ['locator.count', 'expect.locator.toHaveValue']
    bridge.responses['extension.info'] = {'version': '1.1.0', 'methods': methods, 'capabilities': {}}
    runtime.refresh_capabilities()
    bridge.responses['extension.info']['capabilities'] = {'privateValueAssertions': current}
    bridge.responses['locator.count'] = {'count': 1, 'elements': [{'type': 'password'}]}
    bridge.responses['expect.locator.toHaveValue'] = {'ok': True, 'actual': '[REDACTED]'}
    result = bt.browser_assert(context(runtime), tab, bt.ValueAssertion(
        assertion='value', locator=bt.Locator(label='Password'), expected='fixture-secret'))
    if current:
        assert result['ok'] and result['data']['passed']
        assert not any(method == 'locator.count' for method, _ in bridge.calls)
    else:
        assert result['error']['code'] == 'BRIDGE_PROTOCOL_MISMATCH'
        assert 'fixture-secret' not in json.dumps(result)
        assert not any(method == 'expect.locator.toHaveValue' for method, _ in bridge.calls)


def test_capability_refresh_clears_warning_after_extension_reload():
    bridge, runtime, _ = runtime_with_tab()
    bridge.responses['extension.info'] = {'version': '1.1.0', 'capabilities': {}}
    runtime.refresh_capabilities()
    assert runtime.capability_warning
    bridge.responses['extension.info']['capabilities'] = dict.fromkeys(
        ['valuePropertyEvidence', 'attributeParameter', 'nativeDateFill', 'privateValueAssertions'], True)
    runtime.refresh_capabilities()
    assert runtime.capability_warning is None


@pytest.mark.parametrize('current', [False, True])
def test_real_password_diagnostic_shape_does_not_hide_genuine_mismatch(current):
    bridge, runtime, tab = runtime_with_tab()
    bridge.responses['extension.info'] = {'methods': ['expect.locator.toHaveValue'],
        'capabilities': {'privateValueAssertions': current}}
    def mismatch():
        raise bt.BrowserBridgeError('Value check timed out', data={'code': 'LOCATOR_EXPECT_TIMEOUT',
            'diagnostic': {'actual': '[REDACTED]', 'count': 1, 'candidates': [{'type': 'password'}]}})
    bridge.responses['expect.locator.toHaveValue'] = mismatch
    result = bt.browser_assert(context(runtime), tab, bt.ValueAssertion(
        assertion='value', locator=bt.Locator(label='Password'), expected='fixture-secret'))
    if current:
        assert result['ok'] and not result['data']['passed']
        assert 'Reload' not in result['data']['message']
    else:
        assert result['error']['code'] == 'BRIDGE_PROTOCOL_MISMATCH'
