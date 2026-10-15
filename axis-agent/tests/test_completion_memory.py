"""Completion regressions from the two-section CNCC run; no live page writes."""
import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

import browser_tools as bt
from axis.agents import AxisDeps, StepGate, build_navigator, guarded
from axis.orchestrator import verify_completion
from test_axis_reliability import ready, action, assertion
from test_axis_agent import Script, make, OBSERVE, CLICK
from test_autonomous_execution import Context


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def saved_check():
    return bt.TextAssertion(assertion='text', locator=bt.Locator(selector='#saved-result'), expected='Saved')


def unbound_save():
    controller, bridge, _ = ready()
    controller.state.current_verification = None
    controller._register_action(action(target='checkbox "Save"'), True)
    pending = next(iter(controller.state.pending_changes.values()))
    return controller, bridge, pending


def test_missing_postcondition_is_bound_to_existing_action_without_replaying_it():
    controller, bridge, pending = unbound_save()
    assertion(controller, target='#unrelated', expected='Visible')
    assert controller.state.pending_changes  # Ordinary passing checks do not clear it.
    gate = StepGate(max_actions=20, remaining_total_actions=30,
                    on_record=controller._register_action,
                    bind_pending_verification=controller._bind_pending_verification)
    result = guarded(bt.browser_assert)(Context(AxisDeps(controller.browser, gate)),
        tab='tab_1', command=saved_check(), goal_target=f'action:{pending.sequence}')
    assert result['ok']
    assert not controller.state.pending_changes
    assert verify_completion(controller.state, None).valid
    assert bridge.methods == ['extension.info', 'native.sitePatterns', 'expect.locator.toHaveText']
    assert gate.actions_used == 1


@pytest.mark.parametrize('invalid', ['uncertain', 'other_page', 'other_tab', 'other_goal', 'missing_provenance', 'contains', 'declared'])
def test_missing_check_recovery_cannot_weaken_an_existing_contract(invalid):
    controller, bridge, pending = unbound_save()
    command, tab = saved_check(), 'tab_1'
    if invalid == 'uncertain':
        pending.uncertain = True
    elif invalid == 'other_page':
        controller.browser.tabs[tab].url = 'https://example.test/other'
    elif invalid == 'other_tab':
        tab = 'tab_2'
    elif invalid == 'other_goal':
        controller.state.current_goal_id = 'other'
    elif invalid == 'missing_provenance':
        pending.source_url = None
    elif invalid == 'contains':
        command.contains = True
    else:
        controller._bind_pending_verification(tab, command, pending.sequence)
        command = command.model_copy(update={'expected': 'Something else'})
    before = pending.model_dump()
    with pytest.raises(ValueError):
        controller._bind_pending_verification(tab, command, pending.sequence)
    assert pending.model_dump() == before
    assert not bridge.calls


def test_failed_recovery_assertion_keeps_its_contract_and_can_be_retried():
    controller, bridge, pending = unbound_save()
    controller._bind_pending_verification('tab_1', saved_check(), pending.sequence)
    assertion(controller, target='#saved-result', expected='Saved', passed=False)
    assert controller.state.pending_changes
    controller._bind_pending_verification('tab_1', saved_check(), pending.sequence)
    assertion(controller, target='#saved-result', expected='Saved')
    assert not controller.state.pending_changes


def test_bound_recovery_does_not_accept_matching_content_on_another_page():
    controller, _, pending = unbound_save()
    controller._bind_pending_verification('tab_1', saved_check(), pending.sequence)
    controller.browser.tabs['tab_1'].url = 'https://example.test/other'
    assertion(controller, target='#saved-result', expected='Saved')
    assert controller.state.pending_changes
    controller.browser.tabs['tab_1'].url = pending.source_url
    assertion(controller, target='#saved-result', expected='Saved')
    assert not controller.state.pending_changes


@pytest.mark.parametrize('command', [
    bt.ValueAssertion(assertion='value', locator=bt.Locator(label='User-Agent'), expected=''),
    bt.CountAssertion(assertion='count', locator=bt.Locator(role='row', name='310 14', exact=True), count=1),
    bt.StateAssertion(assertion='hidden', locator=bt.Locator(role='dialog', name='Edit', exact=True)),
])
def test_recovery_uses_the_existing_assertion_types(command):
    controller, _, pending = unbound_save()
    controller._bind_pending_verification('tab_1', command, pending.sequence)
    gate = StepGate(max_actions=5, remaining_total_actions=5, on_record=controller._register_action)
    gate.record('browser_assert', {'tab': 'tab_1', 'command': command},
                {'ok': True, 'data': {'passed': True, 'expected': pending.verification.expected, 'match': 'exact'}})
    assert not controller.state.pending_changes


def test_verified_results_survive_page_switches_and_checkpoint_but_not_later_edits():
    controller, _, _ = ready()
    controller.state.current_verification = None
    url_a = controller.browser.tabs['tab_1'].url
    a = assertion(controller, target='#a')
    controller.browser.tabs['tab_1'].url = 'https://example.test/b'
    controller._register_action(action(tool='browser_navigate', operation='open'), True)
    b = assertion(controller, target='#b')
    assert {c['evidence_id'] for c in controller._verified_checks()} == {a.evidence_id, b.evidence_id}
    controller.state.bound_tab = 'tab_1'
    saved = controller.checkpoint_browser()
    restored, _, _ = ready()
    restored._restore_browser_checkpoint(saved)
    assert {c['evidence_id'] for c in restored._verified_checks()} == {a.evidence_id, b.evidence_id}
    restored.browser.tabs['tab_1'].url = url_a
    restored._register_action(action(operation='fill', target='textbox "a"', expected_value='New'), True)
    restored.state.actions.clear()  # Eviction must not bring an obsolete proof back.
    assert [c['evidence_id'] for c in restored._verified_checks()] == [b.evidence_id]
    again, _, _ = ready()
    again._restore_browser_checkpoint(restored.checkpoint_browser())
    assert [c['evidence_id'] for c in again._verified_checks()] == [b.evidence_id]


def test_later_failed_check_removes_earlier_pass_from_continuation_memory():
    controller, _, _ = ready()
    assertion(controller)
    assert controller._verified_checks()
    assertion(controller, passed=False)
    assert not controller._verified_checks()


@pytest.mark.anyio
async def test_continuation_includes_verified_evidence_from_both_sections():
    controller, _, script = ready()
    a = assertion(controller, target='#general')
    controller.browser.tabs['tab_1'].url = 'https://example.test/discovery'
    b = assertion(controller, target='#discovery')
    state = await controller._browser_state()
    await controller._navigate(state)
    prompt = script.navigator_prompts[-1]
    assert '<verified_checks>' in prompt
    assert a.evidence_id in prompt and b.evidence_id in prompt
    assert '#general' in prompt and '#discovery' in prompt


@pytest.mark.anyio
async def test_unpaired_save_completes_after_budget_yield_without_another_click():
    script = Script([], [
        {'calls': [CLICK, OBSERVE, OBSERVE], 'outcome': {'status': 'continue'}},
        {'calls': [('browser_assert', {'tab': 'tab_1', 'command': saved_check().model_dump(),
                                      'goal_target': 'action:2'})],
         'outcome': {'status': 'task_complete', 'final_answer': 'Saved result verified.'}},
    ])
    controller, bridge, config = make(script, direct=True)
    config.run.max_actions_per_step = 2
    result = await controller.start_task('Save the current form and verify its result.')
    assert result.status == 'completed', result.reason
    assert result.total_steps == 2 and result.browser_actions == 3
    assert script.navigator_calls == 2 and script.planner_calls == 0
    assert bridge.methods.count('locator.clickRef') == 1
    assert not controller.state.pending_changes


@pytest.mark.anyio
async def test_model_that_ignores_budget_yield_gets_only_one_summary_request():
    controller, bridge, config = make(Script([], []), direct=True)
    config.run.max_actions_per_step = 2
    calls = 0
    def stubborn(messages, info):
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[ToolCallPart(OBSERVE[0], OBSERVE[1]) for _ in range(3)])
    controller.navigator = build_navigator(config, FunctionModel(stubborn))
    controller.state = config.new_memory('Inspect the page')
    controller.state.current_goal_id = 'direct'
    controller.state.current_goal = 'Inspect the page'
    controller.tab = controller.browser._register({'id': 41, 'url': bridge.url, 'title': bridge.title})
    outcome, gate = await controller._navigate(await controller._browser_state())
    assert outcome.status == 'continue'
    assert calls == 2  # Initial request and one summary opportunity, never repeated model retries.
    assert gate.actions_used == 2
    assert bridge.methods.count('page.accessibilityTree') == 3  # Initial observation plus two allowed calls.
