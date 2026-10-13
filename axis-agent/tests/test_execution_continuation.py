"""Cross-workflow execution regressions; no provider or live browser required."""
from types import SimpleNamespace

import pytest
import browser_tools as bt
from axis.agents import AxisDeps, StepGate, guarded
from axis.models import BrowserStep, NavigatorOutcome, PendingChange
from axis.orchestrator import explicit_browser_steps
from test_axis_agent import FakeBridge, Script, make, OBSERVE, CLICK, browse, done
from test_axis_reliability import ready, action, assertion


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
@pytest.mark.parametrize('direct', [True, False])
@pytest.mark.parametrize('reported', ['failed', 'blocked', 'needs_plan', 'ask_user'])
async def test_step_exhaustion_continues_without_restarting_or_replanning(direct, reported):
    script = Script([] if direct else [browse('Read the page'), done('Reviewed.')], [
        {'calls': [OBSERVE, OBSERVE, OBSERVE],
         'outcome': {'status': reported, 'reason': 'Call budget exhausted', 'final_answer': 'Incomplete'}},
        {'outcome': {'status': 'task_complete' if direct else 'goal_reached', 'final_answer': 'Reviewed.'}},
    ])
    controller, bridge, config = make(script, direct=direct)
    config.run.max_actions_per_step = 2
    result = await controller.start_task('Inspect the current page.')
    assert result.status == 'completed', result.reason
    assert result.total_steps == 2 and result.browser_actions == 2
    assert script.planner_calls == (0 if direct else 2)
    assert bridge.methods.count('page.accessibilityTree') == 4
    assert 'continue from the current state' in script.navigator_prompts[-1]


@pytest.mark.anyio
async def test_run_budget_is_resumable_and_not_reported_as_failure():
    script = Script([], [{'calls': [OBSERVE, OBSERVE, OBSERVE],
        'outcome': {'status': 'failed', 'reason': 'Cannot continue'}}])
    controller, _, config = make(script, direct=True)
    config.run.max_actions_per_step = 2
    config.run.max_browser_actions = 2
    result = await controller.start_task('Inspect the current page.')
    assert result.status == 'limit_reached' and result.recoverable
    assert result.browser_actions == 2 and result.total_steps == 1


@pytest.mark.parametrize('step,total,used,scope', [(2,20,2,'step'), (20,2,2,'run'), (2,2,2,'run')])
def test_budget_refusal_identifies_scope(step, total, used, scope):
    gate = StepGate(max_actions=step, remaining_total_actions=total, actions_used=used)
    result = gate.check('browser_observe', {'tab': 'tab_1'})
    assert result['error']['detail']['scope'] == scope
    assert gate.budget_scope == scope
    assert not gate.records[-1].executed


@pytest.mark.parametrize('step,total,scope', [(3,20,'step'), (20,3,'run')])
def test_pair_reservation_records_yield_and_never_dispatches_unverifiable_edit(step, total, scope):
    controller, bridge, _ = ready()
    gate = StepGate(max_actions=step, remaining_total_actions=total, actions_used=2)
    ctx = SimpleNamespace(deps=AxisDeps(browser=controller.browser, gate=gate))
    result = guarded(bt.browser_act)(ctx, tab='tab_1',
        command=bt.Fill(action='fill', target=bt.LocatorTarget(kind='locator', locator=bt.Locator(label='Name')), value='Alice'),
        postcondition=bt.Postcondition(assertion='value', locator=bt.Locator(label='Name'), expected='Alice'))
    assert result['error']['detail']['required_calls'] == 2
    assert gate.budget_scope == scope and not bridge.calls
    assert gate.records[-1].code == 'ACTION_BUDGET_EXHAUSTED'


@pytest.mark.anyio
@pytest.mark.parametrize('terminal', [True, False])
async def test_budget_does_not_hide_explicit_stop_or_environment_failure(terminal):
    outcome = {'status': 'failed', 'reason': 'Requested stop condition observed',
               'final_answer': 'Stopped as instructed.', 'stop_condition_met': terminal}
    if not terminal:
        outcome.update(blocker='environment', reason='Application unavailable')
    script = Script([], [{'calls': [OBSERVE, OBSERVE, OBSERVE], 'outcome': outcome}])
    controller, _, config = make(script, direct=True)
    config.run.max_actions_per_step = 2
    result = await controller.start_task('Inspect the page; stop if its service is unavailable.')
    assert result.status == 'failed' and result.reason == outcome['reason']
    assert result.total_steps == 1


@pytest.mark.anyio
async def test_rejected_partial_progress_does_not_overwrite_actual_failure():
    script = Script([], [{'calls': [CLICK, OBSERVE], 'outcome': {
        'status': 'failed', 'reason': 'The application reported an error.',
        'final_answer': 'Could not save.',
        'completed_steps': [{'step': 1, 'result': 'Located', 'evidence_ids': ['evidence_2']}]}}])
    controller, _, _ = make(script, direct=True)
    result = await controller.start_task('''1. Locate the form.
2. Save the update.''')
    assert result.status == 'failed'
    assert result.reason == 'The application reported an error.'
    assert controller.state.pending_changes


@pytest.mark.parametrize('control', [
    'a "Ancestor" context="navigation Workspace > group Items"',
    'a "Overview" context="group Items > navigation Workspace"',
    'menuitem "Preferences" context="menubar Application > group Items"',
    'tab "Details" context="tablist Views"',
])
def test_navigation_context_clears_navigation_without_claiming_configuration_saved(control):
    controller, _, _ = ready()
    controller._register_action(action(target='button "Apply"'), True)
    mutation = next(iter(controller.state.pending_changes))
    controller._register_action(action(target=control), True)
    assert len(controller.state.pending_changes) == 2
    controller._verify_navigation('tab_1', controller.browser.tabs['tab_1'].url, controller.state.next_sequence())
    assert list(controller.state.pending_changes) == [mutation]
    assert not controller._current_goal_verified()


@pytest.mark.parametrize('changed', [{'domChanged': True}, {'urlChanged': True}, {'navigated': True}, {}])
def test_rerender_or_redirect_does_not_turn_unclassified_save_into_navigation(changed):
    controller, _, _ = ready()
    controller.state.current_verification = None
    controller._register_action(action(target='button "Apply"', result_data={'whatChanged': changed}), True)
    controller._verify_navigation('tab_1', controller.browser.tabs['tab_1'].url, controller.state.next_sequence())
    assert len(controller.state.pending_changes) == 1
    assert not next(iter(controller.state.pending_changes.values())).navigation


@pytest.mark.parametrize('control', ['a "Action"', 'a "Action" context="form Settings"'])
def test_anchor_tag_alone_is_not_navigation(control):
    controller, _, _ = ready()
    controller.state.current_verification = None
    controller._register_action(action(target=control), True)
    controller._verify_navigation('tab_1', controller.browser.tabs['tab_1'].url, controller.state.next_sequence())
    assert controller.state.pending_changes


@pytest.mark.parametrize('operation', ['hover', 'scroll', 'focus'])
def test_inspection_preserves_goal_evidence_across_history_eviction_and_resume(operation):
    controller, _, _ = ready()
    assertion(controller)
    interaction = action(operation=operation, target='button "Help"')
    controller._register_action(interaction, False)
    controller.state.actions.append(interaction)
    assert controller._current_goal_verified()
    checkpoint = controller.checkpoint_browser()
    resumed, _, _ = ready()
    resumed._restore_browser_checkpoint(checkpoint)
    assert resumed._current_goal_verified()


@pytest.mark.parametrize('operation', ['click', 'fill', 'press', 'select'])
def test_edits_still_invalidate_goal_evidence(operation):
    controller, _, _ = ready()
    assertion(controller)
    controller._register_action(action(operation=operation), True)
    controller.state.actions.clear()
    assert not controller._current_goal_verified()


def test_other_goal_pending_change_does_not_block_independent_step_evidence():
    controller, _, _ = ready()
    controller.state.pending_changes['other'] = PendingChange(tab='tab_1', goal_id='other', sequence=0)
    controller.state.browser_steps = [BrowserStep(instruction='Inspect section')]
    proof = controller.state.add_evidence('observation', 'Section visible', tab='tab_1')
    outcome = NavigatorOutcome(status='continue', completed_steps=[
        dict(step=1, result='Visible', evidence_ids=[proof.id])])
    assert controller._record_browser_steps(outcome) is None
    assert controller.state.pending_changes  # No unrelated mutation was verified.


@pytest.mark.parametrize('instruction', [
    '''Feature A
1. Inspect A
2. Save A
Feature B
1. Inspect B
2. Save B''',
    '''1. Configure A
  1. Inspect
  2. Save
2. Configure B''',
    '''1. First
3. Third''',
])
def test_grouped_or_ambiguous_numbering_is_not_flattened(instruction):
    assert explicit_browser_steps(instruction) == []


def test_flat_explicit_order_and_multiline_values_are_preserved():
    request = '''Execute in order:
1. Inspect records
   Include archived records
2. Apply changes'''
    steps = explicit_browser_steps(request)
    assert len(steps) == 2
    assert 'Include archived records' in steps[0].instruction
    assert steps[1].instruction == 'Apply changes'


def field_pair(label, value):
    return ('browser_act', {'tab': 'tab_1', 'command': {'action': 'fill',
        'target': {'kind': 'locator', 'locator': {'label': label}}, 'value': value},
        'postcondition': {'assertion': 'value', 'locator': {'label': label}, 'expected': value}})


class FormBridge(FakeBridge):
    def __init__(self):
        super().__init__()
        self.fields = {}
        self.edits = []

    def rpc(self, method, params=None, **kwargs):
        params = params or {}
        if method == 'locator.fill':
            locator = params.get('locator', params)
            self.fields[locator['label']] = params['text']
            self.edits.append((locator['label'], params['text']))
        if method == 'expect.locator.toHaveValue':
            locator = params.get('locator', params)
            assert self.fields[locator['label']] == params['expected']
        return super().rpc(method, params, **kwargs)


@pytest.mark.anyio
async def test_long_form_crosses_many_windows_without_replaying_completed_edits():
    pairs = [field_pair('Setting ' + str(i), str(i)) for i in range(12)]
    entries = []
    for i, pair in enumerate(pairs):
        entries.append({'calls': [pair, pairs[i + 1]] if i + 1 < len(pairs) else [pair],
            'outcome': {'status': 'failed' if i + 1 < len(pairs) else 'task_complete',
                'reason': 'Step capacity reached' if i + 1 < len(pairs) else 'Verified all fields',
                'final_answer': 'All fields verified.',
                'completed_steps': [{'step': i + 1, 'result': 'Field value verified',
                    'evidence_ids': ['evidence_' + str((i + 1) * 2)]}]}})
    script = Script([], entries)
    bridge = FormBridge()
    controller, _, config = make(script, direct=True, bridge=bridge)
    config.run.max_actions_per_step = 3
    instruction = chr(10).join(str(i + 1) + '. Set Setting ' + str(i) + ' to ' + str(i) + '.' for i in range(12))
    result = await controller.start_task(instruction)
    assert result.status == 'completed', result.reason
    assert result.total_steps == 12 and result.browser_actions == 24
    assert result.planner_passes == 0
    assert bridge.edits == [('Setting ' + str(i), str(i)) for i in range(12)]
    assert all(step.result for step in controller.state.browser_steps)
    assert not controller.state.pending_changes


@pytest.mark.anyio
async def test_global_pair_boundary_resumes_after_extension_without_partial_edit():
    pair = field_pair('Display name', 'Alice')
    script = Script([], [
        {'calls': [OBSERVE, pair], 'outcome': {'status': 'failed', 'reason': 'Budget exhausted'}},
        {'calls': [pair], 'outcome': {'status': 'task_complete', 'final_answer': 'Name verified.'}},
    ])
    bridge = FormBridge()
    controller, _, config = make(script, direct=True, bridge=bridge)
    config.run.max_browser_actions = 2
    result = await controller.start_task('Set Display name to Alice on the current page.')
    assert result.status == 'limit_reached' and not bridge.edits
    result = await controller.extend_budget(10, actions=2)
    assert result.status == 'completed', result.reason
    assert bridge.edits == [('Display name', 'Alice')]
    assert result.browser_actions == 3


@pytest.mark.parametrize('stopped', ['paused', 'cancelled'])
def test_pause_and_cancel_win_over_pair_budget(stopped):
    controller, bridge, _ = ready()
    gate = StepGate(max_actions=2, remaining_total_actions=2, actions_used=2,
        is_paused=lambda: stopped == 'paused', is_cancelled=lambda: stopped == 'cancelled')
    result = gate.check_control('browser_act', {'tab': 'tab_1'}, required_calls=2)
    assert result['error']['code'] == ('RUN_PAUSED' if stopped == 'paused' else 'RUN_CANCELLED')
    assert gate.stopped == stopped and gate.budget_scope is None
    assert not bridge.calls


@pytest.mark.parametrize('operation', ['hover', 'scroll', 'focus'])
def test_inspection_that_actually_changes_the_page_requires_fresh_evidence(operation):
    controller, _, _ = ready()
    assertion(controller)
    controller._register_action(action(operation=operation,
        result_data={'whatChanged': {'domChanged': True}}), False)
    assert not controller._current_goal_verified()


@pytest.mark.anyio
async def test_planned_execution_honors_stop_condition_before_more_planning():
    script = Script([browse('Inspect the service')], [{'calls': [OBSERVE],
        'outcome': {'status': 'continue', 'stop_condition_met': True,
            'reason': 'Service is unavailable.', 'final_answer': 'Stopped at the requested condition.'}}])
    controller, _, _ = make(script)
    result = await controller.start_task('Inspect the service and stop if unavailable.')
    assert result.status == 'failed' and result.reason == 'Service is unavailable.'
    assert script.planner_calls == 1 and result.total_steps == 1
