"""Regressions from the PCF verification/replanning loop; no live browser calls."""
import json

import pytest
import browser_tools as bt
from axis.models import GoalCheck, PlanDecision, BrowserState, AxisConfig, RunConfig
from axis.attachments.models import DocumentRequest, WorkflowItem
from axis.attachments.runtime import DocumentSession
from test_attachments import documents, ingest, link
from test_attachment_workflows import instruction
from test_axis_agent import FakeBridge, Script, browse, done, make
from test_axis_reliability import ready, action
from axis.models import AttachmentUse


@pytest.fixture
def anyio_backend():
    return 'asyncio'


ROW = {'role': 'row', 'has_text': 'PCF', 'within': {'role': 'main'}}
CHECK = {'assertion': 'text', 'expected': 'PCF 2h 50s Edit Delete', 'target': 'PCF discovery validity row'}


def record(controller, operation='count', expected=1, locator=None, tab='tab_1'):
    item = action('browser_assert', operation, tab=tab,
        target=json.dumps({'locator': locator or ROW}), semantic_success=True,
        result_data={'expected': expected, 'passed': True, 'match': 'exact'})
    controller._register_action(item, False)
    controller.state.actions.append(item)
    return item


def setup_binding():
    controller, _, _ = ready()
    controller.state.current_verification = GoalCheck(**CHECK)
    command = bt.TextAssertion(assertion='text', locator=bt.Locator(**ROW), expected=CHECK['expected'])
    return controller, command


def test_logged_success_does_not_silently_bypass_target_contract():
    controller, command = setup_binding()
    record(controller, 'text', CHECK['expected'])
    assert not controller._current_goal_verified()
    record(controller)
    controller._bind_verification_target('tab_1', command, CHECK['target'])
    record(controller, 'text', CHECK['expected'])
    assert controller._current_goal_verified()
    assert controller.state.current_verification.expected == CHECK['expected']
    assert controller.state.current_verification.target == CHECK['target']
    assert controller.state.current_verification.tab == 'tab_1'


@pytest.mark.parametrize('invalid', ['no_count', 'multiple', 'stale', 'other_goal', 'other_tab',
    'different_locator', 'changed_expectation', 'contains', 'unscoped', 'explicit_target', 'uncertain', 'pending_scope'])
def test_binding_requires_fresh_unique_evidence_and_preserves_intent(invalid):
    controller, command = setup_binding()
    state = controller.state
    if invalid != 'no_count':
        item = record(controller, expected=2 if invalid == 'multiple' else 1,
            tab='tab_2' if invalid == 'other_tab' else 'tab_1',
            locator={**ROW, 'has_text': 'Other'} if invalid == 'different_locator' else ROW)
        if invalid == 'other_goal':
            state.assertions[-1].goal_id = 'old_goal'
    if invalid in {'stale', 'uncertain', 'pending_scope'}:
        mutation = action()
        controller._register_action(mutation, True)
        state.actions.append(mutation)
        pending = next(iter(state.pending_changes.values()))
        if invalid == 'uncertain':
            pending.uncertain = True
            record(controller)
        elif invalid == 'pending_scope':
            pending.verification_locator = {**ROW, 'within': {'role': 'dialog'}}
            record(controller)
    if invalid == 'changed_expectation':
        command.expected = 'Different'
    elif invalid == 'contains':
        command.contains = True
    elif invalid == 'unscoped':
        command.locator.within = None
    elif invalid == 'explicit_target':
        state.original_request = 'Check the exact target PCF discovery validity row.'
    before = state.current_verification.model_copy(deep=True)
    with pytest.raises(ValueError):
        controller._bind_verification_target('tab_1', command, CHECK['target'])
    assert state.current_verification == before


@pytest.mark.parametrize('invalid', ['scope', 'tab', 'expected', 'goal', 'later_edit', 'superseded'])
def test_bound_goal_rejects_wrong_or_stale_assertions(invalid):
    controller, command = setup_binding()
    record(controller)
    controller._bind_verification_target('tab_1', command, CHECK['target'])
    locator = {**ROW, 'within': {'role': 'dialog'}} if invalid == 'scope' else ROW
    record(controller, 'text', 'Other' if invalid == 'expected' else CHECK['expected'], locator,
        'tab_2' if invalid == 'tab' else 'tab_1')
    if invalid == 'goal':
        controller.state.assertions[-1].goal_id = 'other'
    elif invalid == 'superseded':
        controller.state.assertions[-1].superseded_by = 'later'
    elif invalid == 'later_edit':
        item = action()
        controller._register_action(item, True)
        controller.state.actions.append(item)
    assert not controller._current_goal_verified()
    if invalid == 'scope':
        command.locator = bt.Locator(**locator)
        with pytest.raises(ValueError, match='cannot be replaced'):
            controller._bind_verification_target('tab_1', command, CHECK['target'])


def session_for_rows(documents):
    files, cid, task = documents
    item = ingest(files, cid, 'steps.txt', b'Confirm PCF validity 2h and empty-list validity 50s.\nEnable Extended Preferred Locality.')
    link(files, cid, task, item, 'instructions')
    prompt = 'Make the changes in the attached instructions.'
    instruction(files, task, prompt)
    session = DocumentSession(files, task)
    session.configure_uses([AttachmentUse(attachment_id=item['id'], operations=['read', 'execute'],
        purpose='execute', coverage='all', user_evidence=prompt)], prompt)
    rows = session.execute(DocumentRequest(operation='read', attachment_id=item['id']))['items']
    session.execute(DocumentRequest(operation='plan', steps=[WorkflowItem(section_id=r['id'],
        instruction=r['text'], expected_result='Saved and verified') for r in rows]))
    return session, prompt


def test_running_step_keeps_goal_identity_and_evidence_on_reword_and_resume(documents):
    session, _ = session_for_rows(documents)
    controller, command = setup_binding()
    controller.documents = session
    steps = session.workflow()['next_steps']
    session.begin(steps[0]['id'], 'goal_1', controller.state.current_verification)
    record(controller)
    controller._bind_verification_target('tab_1', command, CHECK['target'])
    record(controller, 'text', CHECK['expected'])
    before = controller.state.model_dump(exclude={'cancellation_token', 'message_history'})
    with pytest.raises(ValueError, match='first pending'):
        controller._apply_browse_plan(PlanDecision(**browse('Move to locality', workflow_step_id=steps[1]['id'], verification=CHECK)))
    assert controller.state.current_goal_id == before['current_goal_id']
    assert controller.state.current_goal == before['current_goal']
    controller.documents = DocumentSession(session.store, session.task_id)
    controller._apply_browse_plan(PlanDecision(**browse('Reword the PCF confirmation', workflow_step_id=steps[0]['id'], verification=CHECK)))
    assert controller.state.current_goal_id == 'goal_1'
    assert controller._current_goal_verified()
    controller.documents.verified(controller.state, controller._matches_goal_check)
    assert session.workflow()['counts'] == {'pending': 1, 'verified': 1}


def assert_call(binding=False):
    args = {'tab': 'tab_1', 'command': {'assertion': 'text', 'locator': ROW, 'expected': CHECK['expected']}}
    if binding:
        args['goal_target'] = CHECK['target']
    return ('browser_assert', args)


@pytest.mark.anyio
async def test_pcf_row_advances_to_next_source_step_without_bookkeeping_plans(documents):
    session, prompt = session_for_rows(documents)
    steps = session.workflow()['next_steps']
    script = Script([
        browse('Confirm PCF validity', workflow_step_id=steps[0]['id'], verification=CHECK),
        browse('Confirm locality', workflow_step_id=steps[1]['id'],
            verification={'assertion': 'text', 'expected': 'Enabled', 'target': 'Feature Status'}),
        done('Both changes verified.')], [
        {'calls': [('browser_assert', {'tab': 'tab_1', 'command': {'assertion': 'count', 'locator': ROW, 'count': 1}}),
            assert_call(True)], 'outcome': {'status': 'goal_reached'}},
        {'calls': [('browser_assert', {'tab': 'tab_1', 'command': {'assertion': 'text',
            'locator': {'role': 'textbox', 'name': 'Feature Status', 'within': {'role': 'main'}}, 'expected': 'Enabled'}})],
         'outcome': {'status': 'goal_reached'}}])
    controller, bridge, config = make(script)
    controller.documents = session
    result = await controller.start_task(prompt, task_id=session.task_id)
    assert result.status == 'completed', result.reason
    assert session.workflow()['counts'] == {'verified': 2}
    assert result.total_steps == 2 and script.planner_calls == 3
    assert result.model_requests == 7
    assert bridge.methods.count('expect.locator.toHaveText') == 2


@pytest.mark.anyio
async def test_repeated_success_with_target_mismatch_stops_without_pending_changes():
    script = Script([browse('Confirm PCF', verification=CHECK), browse('Record PCF completion', verification=CHECK)],
        [{'calls': [assert_call()], 'outcome': {'status': 'goal_reached'}}])
    controller, bridge, _ = make(script)
    result = await controller.run('Confirm the existing PCF row.')
    assert result.status == 'failed' and 'VERIFICATION_MISMATCH' in result.reason
    assert CHECK['target'] in result.reason and 'has_text' in result.reason
    assert not controller.state.pending_changes
    assert script.planner_calls == 2 and script.navigator_calls == 2
    assert result.model_requests == 6
    assert bridge.methods.count('expect.locator.toHaveText') == 2


def test_duplicate_success_does_not_reset_no_progress_counter():
    controller, _ = setup_binding()
    page = BrowserState(tab='tab_1', url='https://example.test', snapshot='PCF 2h 50s')
    item = record(controller, 'text', CHECK['expected'])
    controller._update_progress(page, [item])
    controller.state.made_progress = False
    controller.state.counters.no_progress_steps = 2
    controller._update_progress(page, [item])
    assert controller.state.counters.no_progress_steps == 2
    assert not controller.state.made_progress


def test_default_execution_window_is_consistent_and_total_budgets_unchanged():
    configured = AxisConfig.load().run
    defaults = RunConfig()
    for key, value in {'planner_interval_steps': 5, 'planner_max_interval_steps': 8, 'max_actions_per_step': 16}.items():
        assert getattr(configured, key) == getattr(defaults, key) == value
    assert configured.max_model_requests == 60
    assert configured.max_browser_actions == 90


def test_stale_evidence_stays_invalid_after_history_eviction_and_checkpoint(documents):
    session, _ = session_for_rows(documents)
    controller, command = setup_binding()
    controller.documents = session
    step = session.workflow()['next_steps'][0]
    session.begin(step['id'], 'goal_1', controller.state.current_verification)
    record(controller)
    controller._bind_verification_target('tab_1', command, CHECK['target'])
    record(controller, 'text', CHECK['expected'])
    mutation = action('browser_navigate', 'reload')
    controller._register_action(mutation, True)
    controller.state.actions.clear()  # Bounded history can evict the interaction.
    assert not controller._current_goal_verified()
    session.checkpoint(controller.state)
    saved = session.store.history.task(session.task_id)['document_checkpoint']
    assert saved['last_interaction_by_tab']['tab_1'] == mutation.sequence
    assert saved['current_verification']['locator'] == controller.state.current_verification.locator
    with pytest.raises(ValueError, match='passing browser_assert'):
        # An existing binding can be retained, but old evidence cannot complete it.
        controller.documents.verified(controller.state, controller._matches_goal_check)


class CadenceScript(Script):
    def _respond(self, messages, info):
        if not info.function_tools:
            self.planner = [done('Read 24 records.') if self.navigator_calls >= 24 else browse('Read 24 records')]
        return super()._respond(messages, info)


@pytest.mark.anyio
async def test_larger_cadence_reduces_planning_for_same_verified_work():
    results = []
    for base, maximum in [(3, 6), (5, 8)]:
        script = CadenceScript([browse()], [{'calls': [('browser_observe', {'tab': 'tab_1',
            'locator': {'selector': '#count'}, 'extract': 'count'})],
            'outcome': {'status': 'goal_reached' if i == 23 else 'continue'}} for i in range(24)])
        bridge = FakeBridge()
        bridge.responses['locator.count'] = lambda: {'count': bridge.methods.count('locator.count')}
        controller, _, config = make(script, bridge=bridge)
        config.run.planner_interval_steps = base
        config.run.planner_max_interval_steps = maximum
        result = await controller.run('Read 24 changing row counts.')
        assert result.status == 'completed'
        assert result.total_steps == result.browser_actions == 24
        results.append(result)
    old, new = results
    assert (old.planner_passes, new.planner_passes) == (5, 4)
    assert (old.model_requests, new.model_requests) == (53, 52)
    assert old.answer == new.answer
    print('cadence comparison:', json.dumps({'old_planner_passes': old.planner_passes,
        'new_planner_passes': new.planner_passes, 'old_model_requests': old.model_requests,
        'new_model_requests': new.model_requests, 'browser_calls_each': new.browser_actions}))


@pytest.mark.anyio
async def test_larger_tool_window_finishes_stable_form_with_fewer_model_requests():
    results = []
    for limit in (12, 16):
        steps = []
        for i in range(7):
            locator = {'role': 'textbox', 'name': 'Field ' + str(i), 'exact': True,
                'within': {'role': 'form', 'name': 'Settings', 'exact': True}}
            steps.append({'command': {'action': 'fill', 'target': {'kind': 'locator', 'locator': locator}, 'value': 'x'},
                'postcondition': {'assertion': 'value', 'locator': locator, 'expected': 'x'}})
        def batch(items):
            return ('browser_act', {'tab': 'tab_1', 'command': {'action': 'batch', 'steps': items}})
        script = Script([browse('Fill the seven settings', verification={'assertion': 'value', 'target': 'Field 6', 'expected': 'x'}),
            done('Seven fields verified.')], [
            {'calls': [batch(steps[:6]), batch(steps[-1:])], 'outcome': {'status': 'continue' if limit == 12 else 'goal_reached'}},
            {'calls': [batch(steps[-1:])], 'outcome': {'status': 'goal_reached'}}])
        controller, bridge, config = make(script)
        config.run.max_actions_per_step = limit
        result = await controller.run('Fill the seven settings fields with x and verify their values.')
        assert result.status == 'completed', result.reason
        assert result.browser_actions == 14
        assert bridge.methods.count('locator.fill') == 7
        assert bridge.methods.count('expect.locator.toHaveValue') == 7
        assert not controller.state.pending_changes
        results.append(result)
    old, new = results
    assert (old.total_steps, new.total_steps) == (2, 1)
    assert (old.model_requests, new.model_requests) == (6, 4)
    print('tool-window comparison:', json.dumps({'old_steps': old.total_steps, 'new_steps': new.total_steps,
        'old_model_requests': old.model_requests, 'new_model_requests': new.model_requests, 'browser_calls_each': 14}))


def test_planner_cannot_invent_bound_scope_or_drop_existing_binding():
    controller, command = setup_binding()
    invented = {**CHECK, 'locator': ROW, 'tab': 'tab_1'}
    with pytest.raises(ValueError, match='Only the navigator'):
        controller._apply_browse_plan(PlanDecision(**browse('PCF', verification=invented)))
    record(controller)
    controller._bind_verification_target('tab_1', command, CHECK['target'])
    bound = controller.state.current_verification.model_copy(deep=True)
    assert not controller._refine_verification(GoalCheck(**CHECK))
    assert controller.state.current_verification == bound


def test_browser_checkpoint_remaps_bound_verification_with_its_evidence():
    controller, command = setup_binding()
    controller.state.bound_tab = 'tab_1'
    record(controller)
    controller._bind_verification_target('tab_1', command, CHECK['target'])
    record(controller, 'text', CHECK['expected'])
    saved = controller.checkpoint_browser()
    restored, bridge, _ = ready()
    # Occupy the first alias, forcing the restored browser to use another one.
    saved['tabs']['tab_1']['id'] = bridge.base_id + 10
    restored._restore_browser_checkpoint(saved)
    assert restored.state.current_verification.tab == restored.state.bound_tab == 'tab_2'
    assert restored._current_goal_verified()


def test_saved_workflow_binding_is_accepted_after_checkpoint_lags_persistence(documents):
    session, _ = session_for_rows(documents)
    controller, command = setup_binding()
    controller.documents = session
    step = session.workflow()['next_steps'][0]
    old = controller.state.current_verification
    session.begin(step['id'], 'goal_1', old)
    record(controller)
    controller._bind_verification_target('tab_1', command, CHECK['target'])
    bound = controller.state.current_verification
    controller.state.current_verification = old
    controller._apply_browse_plan(PlanDecision(**browse('Recover PCF', workflow_step_id=step['id'],
        verification=bound.model_dump())))
    assert controller.state.current_verification == bound
    assert controller.state.current_goal_id == 'goal_1'
