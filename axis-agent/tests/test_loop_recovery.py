"""Regressions for the clear-field and multi-step form failures from real runs."""

import pytest
import browser_tools as bt
from axis.agents import AxisDeps, StepGate, guarded
from axis.models import BrowserStep, NavigatorOutcome
from axis.orchestrator import verify_completion
from test_autonomous_execution import Context
from test_axis_reliability import ready, page
from test_axis_agent import Script, make
from test_execution_continuation import FormBridge, field_pair


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_clear_and_continue_across_budget_windows_without_replaying_edits():
    clear = field_pair('User-Agent', '')
    edit = field_pair('Validity Period', '1h')
    script = Script([], [
        {'calls': [clear, edit], 'outcome': {'status': 'continue', 'completed_steps': [
            {'step': 1, 'result': 'Cleared in the editor.', 'evidence_ids': ['evidence_2']}] }},
        {'calls': [edit], 'outcome': {'status': 'task_complete', 'final_answer': 'Fields verified.',
            'completed_steps': [
                {'step': 1, 'result': 'User-Agent is empty.', 'evidence_ids': ['evidence_2']},
                {'step': 2, 'result': 'Validity is 1h.', 'evidence_ids': ['evidence_4']}] }},
    ])
    bridge = FormBridge()
    bridge.fields['User-Agent'] = 'NRF-Test-Agent'
    controller, _, config = make(script, direct=True, bridge=bridge)
    config.run.max_actions_per_step = 3
    result = await controller.start_task('1. Clear User-Agent\n2. Set Validity Period to 1h')
    assert result.status == 'completed', result.reason
    assert result.total_steps == 2 and result.browser_actions == 4
    assert result.planner_passes == 0
    assert bridge.edits == [('User-Agent', ''), ('Validity Period', '1h')]
    assert not controller.state.pending_changes


def field():
    return bt.Locator(role='textbox', name='User-Agent', exact=True,
                      within=bt.Locator(role='main'))


@pytest.mark.parametrize('code', ['TIMEOUT', 'BRIDGE_ERROR', 'BRIDGE_UNAVAILABLE'])
def test_unknown_fill_stays_blocked_until_matching_check_then_resumes_in_same_step(code):
    controller, _, _ = ready()
    controller.state.current_verification = None
    gate = StepGate(max_actions=20, remaining_total_actions=30,
                    pending_changes=controller.state.pending_changes,
                    on_record=controller._register_action)
    args = {'tab': 'tab_1', 'command': bt.Fill(action='fill',
        target=bt.LocatorTarget(kind='locator', locator=field()), value='')}
    gate.record('browser_act', args, {'ok': False, 'error': {'code': code}})
    assert gate.check('browser_act', args)['error']['code'] == 'OUTCOME_UNVERIFIED'
    for expected in ['original', '']:
        gate.record('browser_assert', {'tab': 'tab_1', 'command': bt.ValueAssertion(
            assertion='value', locator=field(), expected=expected)},
            {'ok': True, 'data': {'passed': True, 'expected': expected, 'match': 'exact'}})
        if expected:
            assert gate.unknown_outcome  # Unchanged is not proof of non-execution.
            assert gate.check('browser_act', args)['error']['code'] == 'OUTCOME_UNVERIFIED'
    assert not controller.state.pending_changes
    assert not gate.unknown_outcome
    assert gate.check('browser_act', args) is None
    assert all(r.success or r.recovered for r in gate.records)


@pytest.mark.parametrize('method', ['locator.fill', 'locator.fillRef'])
def test_old_extension_clear_rejection_does_not_create_unknown_mutation(method):
    controller, bridge, _ = ready()
    def reject():
        raise bt.BrowserBridgeError('text must be a non-empty string')
    bridge.responses[method] = reject
    with pytest.raises(bt.ToolFault) as caught:
        controller.browser._rpc(method, {'text': ''}, tab='tab_1')
    assert caught.value.code == 'BRIDGE_PROTOCOL_MISMATCH'
    gate = StepGate(max_actions=20, remaining_total_actions=30, on_record=controller._register_action)
    gate.record('browser_act', {'tab': 'tab_1', 'command': bt.Fill(action='fill',
        target=bt.LocatorTarget(kind='locator', locator=field()), value='')},
        {'ok': False, 'error': {'code': caught.value.code}})
    assert not controller.state.pending_changes
    assert not gate.unknown_outcome


def test_ref_field_postcondition_uses_observed_exact_name_before_dialog_closes():
    controller, bridge, _ = ready()
    controller.state.current_verification = None
    controller.browser.observe('tab_1')  # ref_1 is the observed textbox "Search".
    seen = []
    original_rpc = bridge.rpc
    def rpc(method, params=None, **kwargs):
        if method == 'expect.locator.toHaveValue':
            seen.append(params)
            if not params['locator'].get('exact'):
                raise bt.BrowserBridgeError('Search also matches Search Results',
                                           {'code': 'LOCATOR_STRICT_MODE_VIOLATION'})
            return {'passed': True}
        return original_rpc(method, params, **kwargs)
    bridge.rpc = rpc
    gate = StepGate(max_actions=20, remaining_total_actions=30,
                    pending_changes=controller.state.pending_changes,
                    on_record=controller._register_action)
    result = guarded(bt.browser_act)(Context(AxisDeps(controller.browser, gate)), tab='tab_1',
        command=bt.Fill(action='fill', target=bt.RefTarget(kind='ref', ref='ref_1'), value='1h'),
        postcondition=bt.Postcondition(assertion='value', locator=bt.Locator(role='textbox', name='Search'),
                                      expected='1h'))
    assert result['ok']
    assert len(seen) == 1
    assert seen[0]['locator']['exact'] is True
    assert not controller.state.pending_changes
    assert gate.actions_used == 2


def test_refusals_count_as_stalls_even_when_target_syntax_changes():
    controller, _, _ = ready()
    observed = page('Unchanged editor')
    controller._update_progress(observed, [])
    for i in range(1, 4):
        gate = StepGate(max_actions=20, remaining_total_actions=30)
        gate.reject('browser_act', {'tab': 'tab_1', 'command': bt.Fill(action='fill',
            target=bt.RefTarget(kind='ref', ref=f'ref_{i}'), value='')},
            'OUTCOME_UNVERIFIED', 'Verify the pending action')
        controller._update_progress(observed, gate.records)
        controller._update_progress(observed, [])
        assert controller.state.counters.no_progress_steps == i
    assert 'NO_PROGRESS' in controller._stop_reason().reason


def test_progress_reporting_does_not_duplicate_completion_guard():
    controller, _, _ = ready()
    controller.state.current_verification = None
    controller.state.browser_steps = [BrowserStep(instruction='Edit')]
    gate = StepGate(max_actions=20, remaining_total_actions=30, on_record=controller._register_action)
    gate.record('browser_act', {'tab': 'tab_1', 'command': bt.Fill(action='fill',
        target=bt.LocatorTarget(kind='locator', locator=field()), value='new')}, {'ok': True, 'data': {}})
    proof = controller.state.add_evidence('observation', 'Editor is open', tab='tab_1', sequence=10)
    report = NavigatorOutcome(status='continue', completed_steps=[
        dict(step=1, result='Edited; save pending.', evidence_ids=[proof.id])])
    assert controller._record_browser_steps(report) is None
    verdict = verify_completion(controller.state, None)
    assert not verdict.valid
    assert any('mutation(s) remain unverified' in reason for reason in verdict.reasons)
    report.completed_steps[0].evidence_ids = ['invented']
    assert controller._record_browser_steps(report)

