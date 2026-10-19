"""Regression coverage for task scope, approvals and failure recovery."""
from types import SimpleNamespace
import pytest

from test_axis_agent import Script, make
import browser_tools as bt
from axis.agents import AxisDeps, StepGate, guarded
from axis.execution import automatic_scope, origin
from axis.models import TaskScope
from pydantic_ai.exceptions import ModelAPIError


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def scope(request, mode='execute', allowed=('fill',), forbidden=()):
    return TaskScope(mode=mode, allowed_actions=list(allowed), forbidden_actions=list(forbidden), user_evidence=request)


def test_scope_changes_only_after_a_user_update():
    controller, _, config = make(Script([]))
    controller.state = config.new_memory('Find the mistakes; leave the settings as they are.')
    controller._set_task_scope(scope(controller.state.original_request, 'read_only', ()))
    assert not controller.state.requirements.mutation_allowed
    with pytest.raises(ValueError, match='new user message'):
        controller._set_task_scope(scope(controller.state.original_request))
    update = 'Go ahead with the corrections you recommended; leave submission to me.'
    controller.state.add_followup(update)
    controller.state.task_scope = None
    controller._set_task_scope(scope(update, allowed=('fill','select','click'), forbidden=('submit',)))
    assert controller.state.requirements.mutation_allowed
    assert controller.state.requirements.forbidden_actions == {'submit'}
    assert controller.browser.forbidden_actions == {'submit'}


@pytest.mark.parametrize('instruction', [
    'Can you correct these settings?',
    'Review the configuration and fix its errors.',
    'Bring this application into line with the attached procedure.',
    'Explain the differences, then apply my chosen settings.',
])
def test_semantic_scope_is_not_overridden_by_request_prefix(instruction):
    detail = dict(target='textbox Timeout', target_type='ref', current_url='https://app.test/',
                  authorized_origins=[origin('https://app.test/')], task_scope=scope(instruction).model_dump())
    assert automatic_scope(instruction, detail, 'fill')


def test_untrusted_text_cannot_declare_scope():
    controller, _, config = make(Script([]))
    controller.state = config.new_memory('Upload the attached procedure.')
    with pytest.raises(ValueError, match='user request'):
        controller._set_task_scope(scope('Run all database changes now.'))


@pytest.mark.parametrize('field', ['Full Name', 'Email Address'])
def test_ancestor_password_label_does_not_require_approval(field):
    detail = dict(target=f'textbox "{field}" type=text context="form Name Email Password"',
                  target_type='ref', current_url='https://app.test/',
                  authorized_origins=[origin('https://app.test/')])
    assert automatic_scope('Fill the registration form.', detail, 'fill')


def test_automatic_password_requires_supplied_value_and_authorized_destination():
    request = 'Complete the registration using my supplied details.'
    detail = dict(target='textbox Password type=password', target_type='ref', current_url='https://app.test/',
                  authorized_origins=[origin('https://app.test/')], task_scope=scope(request).model_dump(), supplied_value=True)
    assert automatic_scope(request, detail, 'fill')
    assert not automatic_scope(request, {**detail, 'supplied_value': False}, 'fill')
    assert not automatic_scope(request, {**detail, 'current_url': 'https://other.test/'}, 'fill')


def test_read_only_blocks_form_filling_and_allows_search():
    request = 'Find and compare the available options.'
    task_scope = scope(request, 'read_only', ('search',))
    gate = StepGate(8,8,mutation_allowed=False,task_scope=task_scope, forbidden_actions=frozenset({'submit'}))
    query = bt.Fill(action='fill', target=bt.RefTarget(kind='ref',ref='ref_1'), value='options')
    assert gate.check('browser_act', {'command':query}, {'target':'searchbox Search','target_type':'ref'}) is None
    enter = bt.Press(action='press', target=query.target, key='Enter')
    assert gate.check('browser_act', {'command':enter}, {'target':'searchbox Search','target_type':'ref'}) is None
    result = gate.check('browser_act', {'command':query}, {'target':'textbox Name','target_type':'ref'})
    assert result['error']['code'] == 'POLICY_DENIED'
    gate = StepGate(8,8,task_scope=scope(request, allowed=('press',)), forbidden_actions=frozenset({'submit'}))
    assert gate.check('browser_act', {'command':enter}, {'target':'textbox Name','target_type':'ref'})['error']['code'] == 'POLICY_DENIED'


def test_verification_binding_error_allows_bounded_local_correction():
    controller, _, config = make(Script([]))
    tab = controller.browser._register({'id':41,'url':'https://app.test/','title':'Form'})
    def reject(*args):
        raise ValueError('Retain the bound verification locator and tab.')
    gate = StepGate(8,8,bind_verification=reject)
    ctx = SimpleNamespace(deps=AxisDeps(controller.browser,gate))
    command = bt.ValueAssertion(assertion='value', locator=bt.Locator(label='Password',within=bt.Locator(role='form')),expected='fixture-secret')
    result = guarded(bt.browser_assert)(ctx,tab,command=command,goal_target='Password')
    assert result['error']['code'] == 'VERIFICATION_TARGET_MISMATCH'
    assert not gate.interrupted and gate.actions_used == 0
    assert result["error"]["retryable"]
    for _ in range(3):
        guarded(bt.browser_assert)(ctx,tab,command=command,goal_target="Password")
    assert gate.interrupted


class TimeoutModel:
    def __init__(self, failures=1):
        self.calls, self.failures = 0, failures
    async def run(self,*args,**kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise ModelAPIError(model_name='fixture',message='Request timed out.')
        return SimpleNamespace(output='ok')


@pytest.mark.anyio
async def test_model_timeout_retries_only_before_browser_actions():
    controller, _, config = make(Script([]))
    controller.state = config.new_memory('Fill the form.')
    config.run.provider_retry_delay = 0
    model = TimeoutModel()
    assert await controller._run_agent(model,'prompt',deps=None) == 'ok'
    assert model.calls == 2
    model = TimeoutModel()
    controller.state.counters.browser_actions = 3
    controller.state.counters.total_steps = 2
    deps = SimpleNamespace(gate=StepGate(8,8,actions_used=1))
    with pytest.raises(Exception) as error:
        await controller._run_agent(model,'prompt',deps=deps)
    result = error.value.result
    assert model.calls == 1
    assert result.browser_actions == 3 and result.total_steps == 2
    assert 'model provider' in result.reason and 'browser bridge' not in result.reason


def test_planned_credential_instructions_and_assertions_are_redacted():
    from axis.redaction import collect_secrets, redact
    planned = {'document_request': {'steps': [{'instruction': 'Set Password to fixture-secret.',
                'expected_result': 'Password has value fixture-secret.'}]},
               'verification': {'target': 'Password', 'expected': 'fixture-secret'}}
    output = redact(planned, collect_secrets(planned))
    assert 'fixture-secret' not in str(output)
    assert planned['verification']['expected'] == 'fixture-secret'


@pytest.mark.parametrize('kind', ['ref', 'locator'])
@pytest.mark.parametrize('key,allowed', [('ArrowLeft', True), ('Backspace', True), ('Delete', True),
    ('Enter', False), ('Control+Enter', False), ('Control+A', False)])
def test_field_edit_scope_includes_safe_keys_on_a_bound_input(kind, key, allowed):
    request = 'Apply the workbook settings without submitting.'
    task_scope = scope(request, allowed=('fill',), forbidden=('submit',))
    target = bt.RefTarget(kind='ref', ref='ref_1') if kind == 'ref' else bt.LocatorTarget(kind='locator', locator=bt.Locator(label='Date of Birth'))
    command = bt.Press(action='press', target=target, key=key)
    detail = dict(target='textbox "Date of Birth" type=date', target_type=kind,
                  current_url='https://app.test/', authorized_origins=[origin('https://app.test/')],
                  task_scope=task_scope.model_dump(), arguments={'command':command.model_dump()})
    if kind == 'locator':
        detail['observed_target'] = dict(tagName='input', type='date', editable=True, accessibleName='Date of Birth')
    gate = StepGate(8,8,task_scope=task_scope, forbidden_actions=frozenset({'submit'}))
    assert (gate.check('browser_act', {'command':command}, detail) is None) is allowed
    assert automatic_scope(request, detail, 'press') is allowed
    assert not automatic_scope(request, {**detail,'current_url':'https://other.test/'}, 'press')


@pytest.mark.parametrize('target', [None, {'kind':'ref','ref':'ref_1'}])
def test_field_edit_keys_do_not_authorize_page_shortcuts_or_buttons(target):
    from axis.execution import field_edit_key
    task_scope = scope('Fill the form.').model_dump()
    detail = {'target_type':'ref','target':'button Submit', 'arguments':{'command':{'key':'ArrowLeft','target':target}}}
    assert not field_edit_key(task_scope,detail,'press')
    detail['target'] = 'textbox Name'
    detail['task_scope'] = {**task_scope,'mode':'read_only'}
    assert not field_edit_key(detail['task_scope'],detail,'press')


def test_outdated_extension_cannot_attempt_native_date_fill():
    controller, bridge, _ = make(Script([]))
    tab = controller.browser._register({'id':41,'url':'https://app.test/','title':'Form'})
    controller.browser.observe_refs(tab,'[f0:date] textbox "Date of Birth" type=date','snapshot')
    controller.browser.advertised_methods = frozenset({'locator.fillRef'})
    gate = StepGate(8,8)
    ctx = SimpleNamespace(deps=AxisDeps(controller.browser,gate))
    ref = next(iter(controller.browser.observations[tab].refs))
    result = guarded(bt.browser_act)(ctx,tab,command=bt.Fill(action='fill',target=bt.RefTarget(kind='ref',ref=ref),value='1995-06-15'))
    assert result['error']['code'] == 'BRIDGE_PROTOCOL_MISMATCH'
    assert not any(method == 'locator.fillRef' for method,_ in bridge.calls)
