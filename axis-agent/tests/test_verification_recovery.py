import json
from io import BytesIO

import pytest

import browser_tools as bt
from axis.attachments.models import DocumentRequest, WorkflowItem
from axis.attachments.runtime import DocumentSession
from axis.models import AttachmentUse, GoalCheck
from test_attachments import documents, ingest, link
from test_attachment_workflows import instruction
from test_axis_agent import FakeBridge, Script, browse, done, make
from test_axis_reliability import action, ready


@pytest.fixture
def anyio_backend():
    return 'asyncio'


FIELD = 'Service Priority Update Feature Status'
LOCATOR = {'selector': 'oj-select-one', 'has_text': FIELD, 'within': {'role': 'main'}}


@pytest.mark.parametrize('variant', ['valid', 'wrong_name', 'other_tab', 'other_goal', 'old', 'wrong_scope', 'contains'])
def test_scoped_custom_control_verifies_only_its_pending_change(variant):
    controller, _, _ = ready()
    state = controller.state
    state.current_verification = GoalCheck(assertion='value', expected='Enabled', target=FIELD)
    controller._register_action(action(), True)
    pending = next(iter(state.pending_changes.values()))
    pending.verification_locator = LOCATOR
    locator = {**LOCATOR}
    if variant == 'wrong_name':
        locator['has_text'] = 'Other ' + FIELD
    if variant == 'wrong_scope':
        locator['within'] = {'role': 'dialog'}
    record = action('browser_assert', 'value', target=json.dumps({'locator': locator}),
                    semantic_success=True, result_data={'expected': 'Enabled', 'passed': True,
                    'match': 'contains' if variant == 'contains' else 'exact'})
    if variant == 'other_tab':
        record.tab = 'tab_2'
    if variant == 'other_goal':
        state.current_goal_id = 'goal_2'
    if variant == 'old':
        pending.sequence = state.sequence + 2
    controller._register_action(record, False)
    assert bool(state.pending_changes) == (variant != 'valid')


@pytest.mark.parametrize('variant', ['valid', 'no_capability_error', 'wrong_control', 'changed_value',
                                    'explicit_property', 'uncertain', 'later_edit'])
def test_value_to_text_correction_requires_same_result_and_unsupported_property(variant):
    controller, _, _ = ready()
    state = controller.state
    state.original_request = 'Set Feature Status to Enabled.'
    if variant == 'explicit_property':
        state.original_request += ' Verify the value property.'
    state.current_verification = GoalCheck(assertion='value', expected='Enabled', target=FIELD)
    controller._register_action(action(), True)
    pending = next(iter(state.pending_changes.values()))
    pending.uncertain = variant == 'uncertain'
    target = json.dumps({'locator': {'role': 'textbox', 'name': FIELD, 'exact': True}})
    unsupported = action('browser_assert', 'value', target=target, code='UNSUPPORTED_ELEMENT_PROPERTY')
    unsupported.execution_success = False
    if variant == 'wrong_control':
        unsupported.target = json.dumps({'locator': {'name': 'Other status'}})
    controller._register_action(unsupported, False)
    if variant != 'no_capability_error':
        state.actions.append(unsupported)
    expected = 'Disabled' if variant == 'changed_value' else 'Enabled'
    check = action('browser_assert', 'text', target=target, semantic_success=True,
                   result_data={'expected': expected, 'passed': True})
    controller._register_action(check, False)
    if variant == 'later_edit':
        controller._register_action(action(), True)
    corrected = GoalCheck(assertion='text', expected=expected, target=FIELD)
    assert controller._refine_verification(corrected) == (variant == 'valid')
    assert bool(state.pending_changes) == (variant != 'valid')
    if variant == 'valid':
        assert unsupported.recovered
        assert state.assertions[-1].required


def workbook_session(documents):
    from openpyxl import Workbook
    files, cid, task = documents
    book = Workbook()
    book.active.append(['Enable Service Priority Update and save.'])
    book.active.append(['Enable Retry and save.'])
    output = BytesIO()
    book.save(output)
    item = ingest(files, cid, 'changes.xlsx', output.getvalue())
    link(files, cid, task, item)
    prompt = 'Make the changes described in the attached workbook on the current page.'
    instruction(files, task, prompt)
    session = DocumentSession(files, task)
    session.configure_uses([AttachmentUse(attachment_id=item['id'], operations=['read', 'execute'],
        purpose='execute', coverage='all', user_evidence=prompt)], prompt)
    rows = session.execute(DocumentRequest(operation='read', attachment_id=item['id']))['items']
    instructions = [row for row in rows if row.get('row_number')]
    metadata = [row['id'] for row in rows if not row.get('row_number')]
    if metadata:
        session.execute(DocumentRequest(operation='skip', section_ids=metadata, reason='Workbook layout metadata.'))
    session.execute(DocumentRequest(operation='plan', steps=[WorkflowItem(section_id=row['id'],
        instruction=row['text'], expected_result='Enabled after reopening') for row in instructions]))
    return session, prompt


def test_corrected_workflow_check_survives_resume_without_rechecking(documents):
    session, _ = workbook_session(documents)
    controller, _, _ = ready()
    controller.documents = session
    old = controller.state.current_verification
    step = session.workflow()['next_steps'][0]
    session.begin(step['id'], 'goal_1', old)
    controller._register_action(action(), True)
    from test_axis_reliability import assertion
    assertion(controller, expected='Saved successfully')
    corrected = old.model_copy(update={'expected': 'Saved successfully'})
    assert controller._refine_verification(corrected)
    resumed = DocumentSession(session.store, session.task_id)
    resumed.begin(step['id'], 'goal_1', corrected)
    resumed.verified(controller.state, controller._matches_goal_check)
    assert resumed.workflow()['counts'] == {'verified': 1, 'pending': 1}


@pytest.mark.anyio
async def test_workbook_save_reopen_check_advances_to_next_instruction(documents):
    session, prompt = workbook_session(documents)
    steps = session.workflow()['next_steps']

    class SavedForm(FakeBridge):
        saved = False
        reopened = False

        def rpc(self, method, params=None, **kwargs):
            if method == 'locator.click':
                name = params['locator']['name']
                if name == 'Save':
                    self.saved = True
                if name == 'Edit':
                    assert self.saved
                    self.reopened = True
            if method == 'expect.locator.toHaveValue':
                assert self.saved and self.reopened
                assert params['locator']['hasText'] == FIELD
            return super().rpc(method, params, **kwargs)

    def click(name):
        return ('browser_act', {'tab': 'tab_1', 'command': {'action': 'click',
                'target': {'kind': 'locator', 'locator': {'role': 'button', 'name': name}}}})

    script = Script([
        browse('Enable Service Priority Update', workflow_step_id=steps[0]['id'],
               verification={'assertion': 'value', 'expected': 'Enabled', 'target': FIELD}),
        browse('Enable Retry', workflow_step_id=steps[1]['id'],
               verification={'assertion': 'text', 'expected': 'Enabled', 'target': 'Retry'}),
        done('Both saved changes verified.'),
    ], [
        {'calls': [click('Enable'), click('Save')]},
        {'calls': [click('Edit'), ('browser_assert', {'tab': 'tab_1', 'command': {
            'assertion': 'value', 'locator': LOCATOR, 'expected': 'Enabled'}})],
         'outcome': {'status': 'goal_reached'}},
        {'calls': [click('Enable Retry'), click('Save'), click('Edit'),
            ('browser_assert', {'tab': 'tab_1', 'command': {'assertion': 'text',
                'locator': {'role': 'textbox', 'name': 'Retry'}, 'expected': 'Enabled'}})],
         'outcome': {'status': 'goal_reached'}},
    ])
    controller, bridge, config = make(script, bridge=SavedForm())
    config.run.max_model_requests = 9
    controller.documents = session
    result = await controller.start_task(prompt, task_id=session.task_id)
    assert result.status == 'completed', (result.reason, session.workflow()['counts'],
        [(r.operation, r.code, r.target) for r in controller.state.actions], controller.state.pending_changes)
    assert session.workflow()['counts'] == {'verified': 2}
    assert script.planner_calls == 3
    assert result.model_requests == 9
    assert bridge.methods.count('expect.locator.toHaveValue') == 1
    assert not controller.state.pending_changes


def test_workbook_execution_has_no_research_reserve(documents):
    session, prompt = workbook_session(documents)
    controller, _, _ = ready()
    controller.documents = session
    controller.state.original_request = prompt
    assert controller._synthesis_reserve() == 0


@pytest.mark.anyio
async def test_unsupported_value_recovers_with_text_without_an_extra_planner_turn():
    locator = {'role': 'textbox', 'name': FIELD, 'exact': True}
    script = Script([browse('Enable Feature Status', verification={'assertion': 'value',
        'expected': 'Enabled', 'target': FIELD}), done('Enabled verified.')], [
        {'calls': [('browser_act', {'tab': 'tab_1', 'command': {'action': 'click',
                    'target': {'kind': 'ref', 'ref': 'ref_1'}}}),
                   ('browser_assert', {'tab': 'tab_1', 'command': {'assertion': 'value',
                    'locator': locator, 'expected': 'Enabled'}})]},
        {'calls': [('browser_assert', {'tab': 'tab_1', 'command': {'assertion': 'text',
                    'locator': locator, 'expected': 'Enabled'}})],
         'outcome': {'status': 'goal_reached'}},
    ])
    controller, bridge, _ = make(script)

    def unsupported():
        raise bt.BrowserBridgeError('No value property', {'code': 'UNSUPPORTED_ELEMENT_PROPERTY'})

    bridge.responses['expect.locator.toHaveValue'] = unsupported
    result = await controller.start_task('Set Feature Status to Enabled on the current page.')
    assert result.status == 'completed', result.reason
    assert script.planner_calls == 2
    assert controller.state.current_verification.assertion == 'text'
    assert bridge.methods.count('expect.locator.toHaveValue') == 1
    assert bridge.methods.count('expect.locator.toHaveText') == 1
    assert not controller.state.pending_changes


@pytest.mark.anyio
async def test_repeated_completion_mismatch_stops_after_one_recovery():
    script = Script([browse('Enable Feature', verification={'assertion': 'value',
        'expected': 'Enabled', 'target': FIELD})], [
        {'calls': [('browser_act', {'tab': 'tab_1', 'command': {'action': 'click',
                    'target': {'kind': 'ref', 'ref': 'ref_1'}}}),
                   ('browser_assert', {'tab': 'tab_1', 'command': {'assertion': 'value',
                    'locator': {'name': 'Other Field'}, 'expected': 'Enabled'}})],
         'outcome': {'status': 'goal_reached'}},
        {'outcome': {'status': 'goal_reached'}},
    ])
    controller, _, _ = make(script)
    result = await controller.start_task('Enable the feature on the current page.')
    assert result.status == 'failed'
    assert 'VERIFICATION_MISMATCH' in result.reason
    assert script.navigator_calls == 2
    assert controller.state.pending_changes


@pytest.mark.parametrize('gui', [True, False])
def test_synthesis_reserve_applies_to_research_not_gui(gui):
    controller, _, _ = ready()
    controller.config.run.max_model_requests = 60
    controller.config.run.synthesis_reserve_requests = 3
    controller.state.original_request = 'Enable Feature on the current page.' if gui else 'Research retry behavior.'
    assert controller._synthesis_reserve() == (0 if gui else 3)
    controller.state.usage.requests = 60
    assert controller._stop_reason().status == 'limit_reached'


@pytest.mark.parametrize('variant', ['valid', 'native', 'missing', 'ambiguous', 'wrong_control',
    'wrong_tab', 'wrong_goal', 'stale', 'later_edit', 'contains', 'regex', 'explicit_property', 'uncertain'])
def test_property_metadata_refines_only_fresh_exact_same_control_checks(variant):
    controller, _, _ = ready()
    state = controller.state
    state.original_request = 'Set Feature Status to Enabled.'
    if variant == 'explicit_property':
        state.original_request += ' Verify the value property.'
    state.current_verification = GoalCheck(assertion='value', expected='Enabled', target=FIELD)
    target = json.dumps({'locator': {'role': 'textbox', 'name': FIELD, 'exact': True,
                                    'within': {'role': 'main'}}})

    def register(record, mutated=False):
        controller._register_action(record, mutated)
        state.actions.append(record)
        return record

    if variant != 'stale':
        register(action(), True)
    metadata_target = json.loads(target)
    if variant == 'wrong_control':
        metadata_target['locator']['name'] = 'Other status'
    element = {} if variant == 'missing' else {'hasValue': variant == 'native'}
    observed = register(action('browser_observe', 'compact', target=json.dumps(metadata_target),
        result_data={'result': {'count': 2 if variant == 'ambiguous' else 1, 'element': element}}))
    if variant == 'wrong_tab':
        observed.tab = 'tab_2'
    if variant == 'wrong_goal':
        observed.goal_id = 'goal_2'
    if variant == 'stale':
        register(action(), True)
    next(iter(state.pending_changes.values())).uncertain = variant == 'uncertain'
    register(action('browser_assert', 'text', target=target, semantic_success=True,
        result_data={'expected': 'Enabled', 'passed': True,
                     'match': variant if variant in {'contains', 'regex'} else 'exact'}))
    if variant == 'later_edit':
        register(action(), True)
    assert controller._refine_verification(GoalCheck(assertion='text', expected='Enabled', target=FIELD)) == (variant == 'valid')
    assert bool(state.pending_changes) == (variant != 'valid')


@pytest.mark.parametrize('pending', [True, False])
def test_text_assertion_itself_can_prove_capability_without_a_value_failure(pending):
    controller, _, _ = ready()
    state = controller.state
    state.original_request = 'Ensure Feature Status is Enabled.'
    state.current_verification = GoalCheck(assertion='value', expected='Enabled', target=FIELD)
    if pending:
        controller._register_action(action(), True)
    check = action('browser_assert', 'text', target=json.dumps({'locator': {
        'role': 'textbox', 'name': FIELD, 'exact': True, 'within': {'role': 'main'}}}),
        semantic_success=True, result_data={'expected': 'Enabled', 'passed': True, 'match': 'exact',
            'actual': {'count': 1, 'actual': 'ENABLED', 'element': {'hasValue': False}}})
    controller._register_action(check, False)
    state.actions.append(check)
    assert controller._refine_verification(GoalCheck(assertion='text', expected='Enabled', target=FIELD))
    assert controller._current_goal_verified()
    assert not state.pending_changes
    assert not any(r.operation == 'value' for r in state.actions)


def test_child_without_value_does_not_prove_parent_is_a_custom_control():
    controller, _, _ = ready()
    state = controller.state
    state.current_verification = GoalCheck(assertion='value', expected='Enabled', target=FIELD)
    controller._register_action(action(), True)
    parent = {'role': 'combobox', 'name': FIELD, 'exact': True}
    target = json.dumps({'locator': {'selector': '.selected', 'within': parent}})
    check = action('browser_assert', 'text', target=target, semantic_success=True,
        result_data={'expected': 'Enabled', 'passed': True,
                     'actual': {'count': 1, 'element': {'hasValue': False}}})
    controller._register_action(check, False)
    state.actions.append(check)
    assert not controller._refine_verification(GoalCheck(assertion='text', expected='Enabled', target=FIELD))
    assert state.pending_changes


@pytest.mark.anyio
async def test_already_correct_custom_status_completes_without_edit_or_value_probe():
    locator = {'role': 'textbox', 'name': FIELD, 'exact': True, 'within': {'role': 'main'}}
    script = Script([browse('Ensure Feature Status is Enabled', verification={
        'assertion': 'value', 'expected': 'Enabled', 'target': FIELD}), done('Enabled verified.')], [
        {'calls': [('browser_assert', {'tab': 'tab_1', 'command': {
            'assertion': 'text', 'locator': locator, 'expected': 'Enabled'}})],
         'outcome': {'status': 'goal_reached'}},
    ])
    controller, bridge, _ = make(script)
    bridge.responses['expect.locator.toHaveText'] = {
        'actual': 'ENABLED', 'count': 1, 'element': {'hasValue': False}}
    result = await controller.start_task('Ensure Feature Status is Enabled on the current page.')
    assert result.status == 'completed', result.reason
    assert controller.state.current_verification.assertion == 'text'
    assert controller.state.counters.mutation_count == 0
    assert script.navigator_calls == 1
    assert 'expect.locator.toHaveValue' not in bridge.methods
    assert 'locator.click' not in bridge.methods


@pytest.mark.anyio
async def test_direct_already_correct_custom_status_accepts_exact_property_backed_check():
    script = Script([], [{'calls': [('browser_assert', {'tab': 'tab_1', 'command': {
        'assertion': 'text', 'locator': {'role': 'textbox', 'name': FIELD, 'exact': True},
        'expected': 'Enabled'}})], 'outcome': {'status': 'task_complete', 'final_answer': 'Enabled verified.'}}])
    controller, bridge, _ = make(script, direct=True)
    bridge.responses['expect.locator.toHaveText'] = {
        'actual': 'ENABLED', 'count': 1, 'element': {'hasValue': False}}
    result = await controller.run('Set Feature Status to Enabled on the current page.')
    assert result.status == 'completed', result.reason
    assert controller.state.counters.mutation_count == 0


def test_navigation_does_not_clear_saved_configuration_or_substitute_for_goal_check():
    controller, _, _ = ready()
    state = controller.state
    controller._register_action(action(target='button "Save"'), True)
    saved_key = next(iter(state.pending_changes))
    route = action(target=json.dumps({'locator': {'role': 'menuitem', 'name': 'Settings'}}),
                   result_data={'whatChanged': {'urlChanged': True, 'toUrl': 'https://example.test/settings'}})
    controller._register_action(route, True)
    state.actions.append(route)
    assert len(state.pending_changes) == 2
    controller._verify_navigation('tab_1', 'https://example.test/settings', state.next_sequence())
    assert list(state.pending_changes) == [saved_key]
    assert not controller._current_goal_verified()


@pytest.mark.anyio
async def test_legacy_value_timeout_stops_across_replanning_with_a_precise_diagnostic():
    command = ('browser_assert', {'tab': 'tab_1', 'command': {
        'assertion': 'value', 'locator': {'role': 'textbox', 'name': FIELD}, 'expected': 'Enabled'}})
    script = Script([browse('Ensure Enabled', verification={'assertion': 'value', 'expected': 'Enabled', 'target': FIELD})], [
        {'calls': [command], 'outcome': {'status': 'needs_plan', 'reason': 'Value check failed.'}},
    ])
    controller, bridge, _ = make(script)
    bridge.responses['extension.info'] = {'version': 'old'}

    def mismatch():
        raise bt.BrowserBridgeError('Value did not match', {'code': 'LOCATOR_EXPECT_TIMEOUT',
                                   'diagnostic': {'actual': '', 'count': 1}})

    bridge.responses['expect.locator.toHaveValue'] = mismatch
    result = await controller.start_task('Ensure Feature Status is Enabled on the current page.')
    assert result.status == 'failed'
    assert 'VERIFICATION_UNRESOLVED' in result.reason
    assert FIELD in result.reason and 'Enabled' in result.reason
    assert 'Reload or upgrade' in result.reason
    assert bridge.methods.count('expect.locator.toHaveValue') == 2
    assert script.planner_calls == 2
    assert controller.state.current_verification.assertion == 'value'


@pytest.mark.anyio
async def test_workbook_already_correct_status_advances_and_persists_grounded_check(documents):
    session, prompt = workbook_session(documents)
    steps = session.workflow()['next_steps']
    script = Script([
        browse('Ensure Service Priority Update', workflow_step_id=steps[0]['id'],
               verification={'assertion': 'value', 'expected': 'Enabled', 'target': FIELD}),
        browse('Ensure Retry', workflow_step_id=steps[1]['id'],
               verification={'assertion': 'text', 'expected': 'Enabled', 'target': 'Retry'}),
        done('Both saved settings verified.'),
    ], [
        {'calls': [('browser_assert', {'tab': 'tab_1', 'command': {'assertion': 'text',
            'locator': {'role': 'textbox', 'name': name, 'exact': True}, 'expected': 'Enabled'}})],
         'outcome': {'status': 'goal_reached'}} for name in (FIELD, 'Retry')
    ])
    controller, bridge, _ = make(script)
    controller.documents = session
    bridge.responses['expect.locator.toHaveText'] = {
        'actual': 'ENABLED', 'count': 1, 'element': {'hasValue': False}}
    result = await controller.start_task(prompt, task_id=session.task_id)
    assert result.status == 'completed', result.reason
    assert session.workflow()['counts'] == {'verified': 2}
    with session.store.history.transaction():
        saved = session.store.history.db.execute('SELECT verification FROM workflow_steps WHERE id=?',
                                                (steps[0]['id'],)).fetchone()
    assert json.loads(saved[0])['assertion'] == 'text'
    assert controller.state.counters.mutation_count == 0
    assert script.planner_calls == 3 and script.navigator_calls == 2
    assert 'expect.locator.toHaveValue' not in bridge.methods


@pytest.mark.anyio
async def test_planner_cannot_complete_an_unverified_non_mutating_goal():
    script = Script([
        browse('Ensure Enabled', verification={'assertion': 'value', 'expected': 'Enabled', 'target': FIELD}),
        done('Already enabled.'),
    ], [{'calls': [('browser_assert', {'tab': 'tab_1', 'command': {'assertion': 'text',
        'locator': {'role': 'textbox', 'name': FIELD}, 'expected': 'Enabled'}})],
        'outcome': {'status': 'goal_reached'}}])
    controller, bridge, config = make(script)
    config.run.max_model_requests = 7
    # A passing text assertion with no property evidence cannot replace value.
    result = await controller.start_task('Ensure Feature Status is Enabled on the current page.')
    assert result.status != 'completed'
    assert controller.state.current_verification.assertion == 'value'
    assert not controller._current_goal_verified()


def test_passing_check_before_navigation_is_not_fresh_goal_evidence():
    controller, _, _ = ready()
    state = controller.state
    record = action('browser_assert', 'text', target=json.dumps({'locator': {'selector': '#status'}}),
                    semantic_success=True, result_data={'expected': 'Saved', 'passed': True})
    controller._register_action(record, False)
    state.actions.append(record)
    assert controller._current_goal_verified()
    navigated = action('browser_navigate', 'open')
    controller._register_action(navigated, True)
    state.actions.append(navigated)
    controller._verify_navigation('tab_1', navigated.source_url, state.next_sequence())
    assert not controller._current_goal_verified()


@pytest.mark.anyio
async def test_protocol_mismatch_stops_once_and_preserves_pending_work():
    script = Script([browse('Inspect status')], [{'calls': [('browser_observe', {
        'tab': 'tab_1', 'locator': {'role': 'textbox', 'name': FIELD},
        'extract': 'attribute', 'attribute': 'title'})], 'outcome': {'status': 'needs_plan'}}])
    controller, bridge, _ = make(script)

    def legacy():
        raise bt.BrowserBridgeError('name must be a non-empty string')

    bridge.responses['locator.getAttribute'] = legacy
    result = await controller.start_task('Inspect the status on the current page.')
    assert result.status == 'paused'
    assert controller.state.status == 'paused'
    assert 'BRIDGE_PROTOCOL_MISMATCH' in result.reason
    assert 'Reload or upgrade' in result.reason
    assert result.recoverable
    assert bridge.methods.count('locator.getAttribute') == 1
    assert script.planner_calls == 1


def test_newer_native_property_evidence_vetoes_an_older_custom_control_observation():
    controller, _, _ = ready()
    state = controller.state
    state.current_verification = GoalCheck(assertion='value', expected='Enabled', target=FIELD)
    controller._register_action(action(), True)
    target = json.dumps({'locator': {'role': 'textbox', 'name': FIELD, 'exact': True}})
    observed = action('browser_observe', 'compact', target=target,
                      result_data={'result': {'count': 1, 'element': {'hasValue': False}}})
    controller._register_action(observed, False)
    state.actions.append(observed)
    check = action('browser_assert', 'text', target=target, semantic_success=True,
        result_data={'expected': 'Enabled', 'passed': True,
                     'actual': {'count': 1, 'element': {'hasValue': True}}})
    controller._register_action(check, False)
    state.actions.append(check)
    assert not controller._refine_verification(GoalCheck(assertion='text', expected='Enabled', target=FIELD))
    assert state.pending_changes


@pytest.mark.anyio
async def test_password_capability_block_preserves_fill_and_resume_refreshes_bridge(monkeypatch):
    locator = {'role': 'textbox', 'name': 'Password', 'exact': True, 'within': {'selector': 'form'}}
    script = Script([browse('Set password', verification={
        'assertion': 'value', 'expected': 'fixture-secret', 'target': 'Password'})],
        [{'calls': [('browser_act', {'tab': 'tab_1', 'command': {
            'action': 'fill', 'target': {'kind': 'locator', 'locator': locator}, 'value': 'fixture-secret'},
            'postcondition': {'assertion': 'value', 'locator': locator, 'expected': 'fixture-secret'}})],
          'outcome': {'status': 'blocked', 'stop_condition_met': True}}])
    controller, bridge, _ = make(script)
    bridge.responses['extension.info'] = {'version': '1.1.0', 'methods': [
        'tabs.list', 'page.accessibilityTree', 'locator.count', 'locator.fill', 'expect.locator.toHaveValue'],
        'capabilities': {}}
    bridge.responses['locator.count'] = {'count': 1, 'elements': [{'type': 'password'}]}
    result = await controller.start_task('Fill the form. Password: fixture-secret')
    assert result.status == 'paused', result.reason
    assert result.recoverable
    assert bridge.methods.count('locator.fill') == 1
    assert 'expect.locator.toHaveValue' not in bridge.methods
    assert script.planner_calls == 1
    state = controller.state
    pending = dict(state.pending_changes)
    assert pending
    calls = state.counters.browser_actions
    bridge.responses['extension.info']['capabilities'] = dict.fromkeys(
        ['valuePropertyEvidence', 'attributeParameter', 'nativeDateFill', 'privateValueAssertions'], True)
    async def resumed_loop():
        assert controller.state is state
        assert state.pending_changes == pending
        assert state.counters.browser_actions == calls
        assert state.status == 'active'
        assert controller.browser.capability_warning is None
        assert 'privateValueAssertions' in controller.browser.bridge_capabilities
        return controller._result('paused', reason='Fixture ends here.')
    monkeypatch.setattr(controller, '_loop', resumed_loop)
    await controller.resume_run()
    assert bridge.methods.count('locator.fill') == 1
