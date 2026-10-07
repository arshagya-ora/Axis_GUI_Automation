"""Behavioral regressions for task scope, coherent operations and ordered progress."""
import json
from dataclasses import dataclass
import pytest
import browser_tools as bt
from axis.agents import AxisDeps, StepGate, browser_context, guarded
from axis.execution import automatic_scope, origin
from axis.models import AssertionRecord, BrowserStep, GoalCheck, NavigatorOutcome, PendingChange, TaskRunState
from axis.orchestrator import verify_completion
from test_axis_agent import FakeBridge, Script, make
from test_axis_reliability import ready, action, assertion


@dataclass
class Context:
    deps: object


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def test_task_scope_survives_title_change_but_not_origin_change():
    instruction = 'In the ACME tab, click Settings then Save.'
    detail = dict(target='button Save', target_type='ref', current_url='https://internal.test/settings',
                  authorized_origins=[origin('https://internal.test')], application_title='ACME')
    assert automatic_scope(instruction, detail, 'click')
    assert automatic_scope(instruction, {**detail, 'application_title': 'Settings'}, 'click')
    assert not automatic_scope(instruction, {**detail, 'current_url': 'https://other.test'}, 'click')
    assert not automatic_scope(instruction, {**detail, 'current_url': 'http://internal.test'}, 'click')
    assert not automatic_scope(instruction, {**detail, 'observed_target': {'href': 'https://other.test'}}, 'click')


def test_failed_diagnostic_does_not_become_a_task_requirement():
    controller, _, _ = ready()
    assertion(controller, target='#whole-menu', expected='Settings', passed=False)
    assertion(controller)
    assert not controller.state.assertions[0].required
    assert verify_completion(controller.state, None).valid


def test_outstanding_conditions_are_authoritative_even_if_counters_disagree():
    state = TaskRunState()
    state.counters.mutation_count = 100
    assert state.unverified_mutations == 0
    state.pending_changes['field'] = PendingChange(tab='tab_1', sequence=1, count=2)
    assert state.unverified_mutations == 2
    assert not verify_completion(state, None).valid


def test_pair_reservation_never_fills_second_field_without_capacity_to_check():
    controller, bridge, _ = ready()
    gate = StepGate(max_actions=3, remaining_total_actions=20)
    ctx = Context(AxisDeps(browser=controller.browser, gate=gate, documents=None))
    call = guarded(bt.browser_act)
    for name, value in [('First', 'Alice'), ('Last', 'Smith')]:
        result = call(ctx, tab='tab_1', command=bt.Fill(action='fill',
            target=bt.LocatorTarget(kind='locator', locator=bt.Locator(label=name)), value=value),
            postcondition=bt.Postcondition(assertion='value', locator=bt.Locator(label=name), expected=value))
    assert not result['ok']
    assert bridge.methods.count('locator.fill') == 1
    assert bridge.methods.count('expect.locator.toHaveValue') == 1
    assert gate.actions_used == 2


def test_step_results_cannot_skip_or_use_unverified_evidence():
    controller, _, _ = ready()
    controller.state.browser_steps = [BrowserStep(instruction='Open section'), BrowserStep(instruction='Inspect result')]
    proof = controller.state.add_evidence('observation', 'Section visible', tab='tab_1')
    skipped = NavigatorOutcome(status='continue', completed_steps=[dict(step=2, result='Done', evidence_ids=[proof.id])])
    assert controller._record_browser_steps(skipped)
    invented = NavigatorOutcome(status='continue', completed_steps=[dict(step=1, result='Done', evidence_ids=['invented'])])
    assert controller._record_browser_steps(invented)
    valid = NavigatorOutcome(status='continue', completed_steps=[dict(step=1, result='Section visible', evidence_ids=[proof.id])])
    assert controller._record_browser_steps(valid) is None
    assert controller.state.browser_steps[0].result == 'Section visible'
    assert not verify_completion(controller.state, None).valid


def test_restart_preserves_verified_results_and_pending_changes_without_refs():
    controller, bridge, _ = ready()
    controller.state.browser_steps = [BrowserStep(instruction='Inspect error', result='Error 503', evidence_ids=['evidence_1']),
                                      BrowserStep(instruction='Save settings')]
    controller.state.authorized_origins['tab_1'] = [origin(bridge.url)]
    controller._register_action(action(postcondition=GoalCheck(assertion='text', expected='Saved', target='#status')), True)
    checkpoint = controller.checkpoint_browser()
    resumed, _, _ = ready()
    resumed._restore_browser_checkpoint(checkpoint)
    assert resumed.state.browser_steps[0].result == 'Error 503'
    assert resumed.state.pending_changes
    assert not resumed.browser.observations
    assert resumed.state.authorized_origins['tab_1'] == [origin(bridge.url)]
    assertion(resumed)
    assert not resumed.state.pending_changes


@pytest.mark.anyio
async def test_ordered_enterprise_workflow_is_autonomous_and_reports_actual_results():
    class EnterpriseBridge(FakeBridge):
        def __init__(self):
            super().__init__()
            self.title, self.url = 'ACME', 'https://internal.test/'
            self.position = 0
            self.saved = False
        def rpc(self, method, params=None, **kwargs):
            params = params or {}
            if method == 'locator.click':
                name = params.get('locator', params).get('name')
                expected = ['Services', 'Replication', 'Back', 'Configurations', 'Operations', 'Edit operation 17', 'Save'][self.position]
                assert name == expected, (name, expected)
                self.position += 1
                self.title = 'Settings'  # Authorization must survive this change.
                if name == 'Save': self.saved = True
                self.calls.append((method, params))
                return {}
            if method == 'expect.locator.toHaveText':
                actual = ['Services ready', 'Replication error 503', 'Services ready', 'Configurations ready',
                          '17 operations', 'Server rule', 'Saved'][self.position-1]
                self.calls.append((method, params))
                return {'passed': params.get('expected') == actual}
            return super().rpc(method, params, **kwargs)
    targets = ['Services', 'Replication', 'Back', 'Configurations', 'Operations', 'Edit operation 17', 'Save']
    results = ['Services ready', 'Replication error 503', 'Services ready', 'Configurations ready', '17 operations', 'Server rule', 'Saved']
    steps = []
    for i, (target, result) in enumerate(zip(targets, results)):
        steps.append({'calls': [('browser_act', {'tab': 'tab_1', 'command': {'action': 'click',
            'target': {'kind': 'locator', 'locator': {'role': 'button', 'name': target}}},
            'postcondition': {'assertion': 'text', 'locator': {'selector': '#status'}, 'expected': result}})],
            'outcome': {'status': 'continue', 'completed_steps': [{'step': i+1, 'result': result, 'evidence_ids': [f'evidence_{(i+1)*2}']}]}})
    steps.append({'outcome': {'status': 'task_complete', 'final_answer': 'Replication error 503; operation 17: Server rule; Saved.'}})
    script = Script([], steps)
    bridge = EnterpriseBridge()
    controller, _, _ = make(script, bridge=bridge, direct=True)
    request = 'In the ACME tab, execute in order:\n' + '\n'.join(f'{i+1}. Click {name} and verify {result}.' for i,(name,result) in enumerate(zip(targets, results)))
    result = await controller.start_task(request)
    assert result.status == 'completed', result.reason
    assert bridge.position == 7 and bridge.saved
    assert result.planner_passes == 0
    assert all(step.result for step in controller.state.browser_steps)
    assert '503' in result.answer and 'Server rule' in result.answer


@pytest.mark.anyio
async def test_targeting_uncertainty_recovers_once_without_asking_the_user():
    script = Script([], [{'outcome': {'status': 'ask_user', 'blocker': 'targeting', 'reason': 'Cannot locate control'}}] * 2)
    controller, _, config = make(script, direct=True)
    config.run.visual_mode = 'off'
    result = await controller.start_task('Click Continue on this page.')
    assert result.status == 'failed'
    assert script.navigator_calls == 2
    assert result.status != 'needs_user'


@pytest.mark.anyio
async def test_authorization_wait_is_not_attributed_to_the_model():
    import time
    script = Script([], [{'calls': [('browser_act', {'tab': 'tab_1', 'command': {'action': 'fill',
        'target': {'kind': 'locator', 'locator': {'label': 'Name'}}, 'value': 'Alice'},
        'postcondition': {'assertion': 'value', 'locator': {'label': 'Name'}, 'expected': 'Alice'}})],
        'outcome': {'status': 'task_complete', 'final_answer': 'Entered Alice.'}}])
    controller, _, _ = make(script, direct=True)
    def authorize(*args):
        time.sleep(.03)
        return True
    controller.effect_authorizer = authorize
    result = await controller.start_task('Fill Name with Alice.')
    assert result.status == 'completed'
    assert result.authorization_ms >= 50  # action and check callbacks
    assert controller.state.tool_model_ms >= result.authorization_ms


def test_interrupted_submit_restores_pending_check_before_any_replay():
    controller, _, _ = ready()
    controller._active_gate = StepGate(max_actions=12, remaining_total_actions=20)
    controller._active_gate.in_flight = ('browser_act', {'tab': 'tab_1', 'command': bt.Click(action='click',
        target=bt.LocatorTarget(kind='locator', locator=bt.Locator(role='button', name='Save')))}, 0)
    checkpoint = controller.checkpoint_browser()
    assert checkpoint['uncertain_action']['operation'] == 'click'
    resumed, bridge, _ = ready()
    resumed._restore_browser_checkpoint(checkpoint)
    assert resumed.state.pending_changes['restart:uncertain'].verification.expected == 'Saved'
    assert not verify_completion(resumed.state, None).valid
    assertion(resumed)
    assert verify_completion(resumed.state, None).valid
    assert 'locator.click' not in bridge.methods


def test_partial_report_retains_verified_browser_step_results():
    controller, _, _ = ready()
    controller.state.browser_steps = [BrowserStep(instruction='Inspect failure', result='Exact error 503'), BrowserStep(instruction='Save')]
    result = controller._result('failed', reason='Control unavailable')
    assert 'Exact error 503' in result.answer
    assert 'Task incomplete' in result.answer


def test_unresolved_submission_refuses_further_changes_but_allows_verification():
    gate = StepGate(max_actions=12, remaining_total_actions=20, uncertain_tabs=frozenset({'tab_1'}))
    refused = gate.check('browser_act', {'tab': 'tab_1', 'command': bt.Click(action='click',
        target=bt.LocatorTarget(kind='locator', locator=bt.Locator(role='button', name='Save')))})
    assert refused['error']['code'] == 'OUTCOME_UNVERIFIED'
    assert gate.actions_used == 0
    fresh_gate = StepGate(max_actions=12, remaining_total_actions=20, uncertain_tabs=frozenset({'tab_1'}))
    assert fresh_gate.check('browser_assert', {'tab': 'tab_1', 'command': bt.TextAssertion(
        assertion='text', locator=bt.Locator(selector='#status'), expected='Saved')}) is None


def test_preflight_failure_does_not_create_an_uncertain_mutation():
    controller, bridge, _ = ready()
    controller.browser.observe('tab_1')
    gate = StepGate(max_actions=12, remaining_total_actions=20, effect_authorizer=lambda *args: True,
                    on_record=controller._register_action)
    def unavailable():
        raise bt.BrowserBridgeError('Bridge unavailable', {'code': 'BRIDGE_ERROR'})
    bridge.responses['locator.count'] = unavailable
    result = guarded(bt.browser_act)(Context(AxisDeps(controller.browser, gate)), tab='tab_1',
        command=bt.Click(action='click', target=bt.LocatorTarget(kind='locator', locator=bt.Locator(role='button', name='Save'))))
    controller._commit_gate(gate)
    assert not result['ok'] and not gate.records[0].executed
    assert not controller.state.pending_changes
    assert controller.state.counters.mutation_count == 0
    assert 'locator.click' not in bridge.methods


@pytest.mark.parametrize('count,elements,code', [
    (0, [], 'TARGET_NOT_FOUND'),
    (2, [{'role': 'button', 'context': 'dialog A'}, {'role': 'button', 'context': 'dialog B'}], 'TARGET_AMBIGUOUS'),
    (1, [], 'TARGET_EVIDENCE_UNAVAILABLE'),
    (1, [{'role': 'button', 'disabled': True}], 'TARGET_DISABLED'),
])
def test_target_preflight_reports_actual_failure(count, elements, code):
    from axis.agents import _observed_effect_target
    controller, bridge, _ = ready()
    bridge.responses['locator.count'] = {'count': count, 'elements': elements}
    with pytest.raises(bt.ToolFault) as fault:
        _observed_effect_target(controller.browser, 'tab_1', bt.Locator(role='button', name='Save'))
    assert fault.value.code == code
    assert fault.value.detail['count'] == count
    assert len(fault.value.detail['candidates']) == len(elements)


def test_navigation_allows_read_then_fresh_action_in_same_step():
    gate = StepGate(max_actions=12, remaining_total_actions=30)
    kwargs = {'tab': 'tab_1', 'command': bt.Click(action='click', target=bt.RefTarget(kind='ref', ref='ref_1'))}
    gate.record('browser_act', kwargs, {'ok': True, 'data': {'refsInvalidated': True}})
    assert not gate.interrupted
    assert gate.check('browser_wait', {'tab': 'tab_1', 'command': bt.EventWait(condition='page_load')}) is None
    assert gate.check('browser_act', kwargs)['error']['code'] == 'OBSERVATION_REQUIRED'
    assert gate.check('browser_observe', {'tab': 'tab_1'}) is None
    gate.record('browser_observe', {'tab': 'tab_1'}, {'ok': True, 'data': {'snapshot': 'new page'}})
    assert gate.check('browser_act', kwargs) is None


def test_target_recovery_is_local_bounded_and_keeps_diagnostics():
    gate = StepGate(max_actions=12, remaining_total_actions=30)
    for _ in range(3):
        result = gate.reject('browser_act', {'tab': 'tab_1'}, 'TARGET_NOT_FOUND', 'Missing', {'count': 0})
        assert result['error']['detail']['count'] == 0
        assert not gate.interrupted
        assert gate.check('browser_observe', {'tab': 'tab_1'}) is None
    gate.reject('browser_act', {'tab': 'tab_1'}, 'TARGET_NOT_FOUND', 'Missing')
    assert gate.interrupted


def test_unknown_submission_can_be_inspected_but_not_replayed():
    gate = StepGate(max_actions=12, remaining_total_actions=30)
    kwargs = {'tab': 'tab_1', 'command': bt.Click(action='click', target=bt.RefTarget(kind='ref', ref='ref_1'))}
    gate.record('browser_act', kwargs, {'ok': False, 'error': {'code': 'TIMEOUT'}})
    assert gate.check('browser_observe', {'tab': 'tab_1'}) is None
    gate.record('browser_observe', {'tab': 'tab_1'}, {'ok': True, 'data': {'snapshot': 'Receipt'}})
    assert gate.check('browser_act', kwargs)['error']['code'] == 'OUTCOME_UNVERIFIED'
    assert not gate.interrupted


def test_cancel_and_authorization_refusal_still_stop_recovery_reads():
    cancelled = StepGate(max_actions=12, remaining_total_actions=30, is_cancelled=lambda: True)
    assert cancelled.check('browser_observe', {'tab': 'tab_1'})['error']['code'] == 'RUN_CANCELLED'
    denied = StepGate(max_actions=12, remaining_total_actions=30)
    denied.reject('browser_act', {'tab': 'tab_1'}, 'APPROVAL_DENIED', 'Denied')
    assert denied.check('browser_observe', {'tab': 'tab_1'})['error']['code'] == 'STEP_INTERRUPTED'


def test_successful_local_repair_does_not_count_as_unresolved_failure():
    gate = StepGate(max_actions=12, remaining_total_actions=30)
    kwargs = {'tab': 'tab_1', 'command': bt.Click(action='click', target=bt.RefTarget(kind='ref', ref='ref_1'))}
    gate.reject('browser_act', kwargs, 'TARGET_AMBIGUOUS', 'Scope it')
    gate.record('browser_observe', {'tab': 'tab_1'}, {'ok': True, 'data': {'snapshot': 'unique button'}})
    gate.record('browser_act', kwargs, {'ok': True, 'data': {}})
    assert gate.records[0].recovered
    assert not gate.interrupted
