import pytest

from test_axis_agent import Script, make


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_prior_context_does_not_create_requirements_or_login_intent():
    script = Script([], [{'outcome': {'status': 'task_complete', 'final_answer': 'Hi!'}}])
    orchestrator, bridge, _ = make(script, direct=True)
    reference = {
        'request': '1. Login to http://old.test\nUser: old-user\nPassword: old-secret\n'
                   '2. Download a file, capture a screenshot, inspect browser console and network activity.\n'
                   'From Old GUI, do not modify anything.',
        'outcome': 'Saved to Downloads/browser-agent-bridge/current-page.png',
        'tab': {'url': 'http://old.test/index.html\\', 'title': 'Old'},
    }
    result = await orchestrator.start_task('Hi', reference_context=reference)
    assert result.status == 'completed'
    requirements = orchestrator.state.requirements
    assert requirements.literal_urls == []
    assert not requirements.require_screenshot
    assert not requirements.require_download
    assert not requirements.require_console_check
    assert not requirements.require_network_check
    assert requirements.mutation_allowed
    assert requirements.forbidden_actions == set()
    assert orchestrator.state.browser_steps == []
    assert orchestrator._login_intent(orchestrator.tab) is None
    assert 'page.navigate' not in bridge.methods
    assert '<prior_task_context>' in script.navigator_prompts[0]
    assert orchestrator.state.current_goal == 'Hi'


@pytest.mark.anyio
async def test_actual_followups_still_add_requirements_with_reference_context():
    script = Script([
        {'decision': 'ask_user', 'reason': 'Which report?'},
        {'decision': 'fail', 'reason': 'Fixture stopped.'},
    ])
    orchestrator, _, _ = make(script)
    await orchestrator.start_task('Inspect the report', reference_context={'request': 'Download http://old.test'})
    await orchestrator.continue_task('Take a screenshot and download the report. Do not modify it.')
    requirements = orchestrator.state.requirements
    assert requirements.require_screenshot and requirements.require_download
    assert not requirements.mutation_allowed
    assert requirements.literal_urls == []
    assert orchestrator.state.reference_context == {'request': 'Download http://old.test'}
    assert '<reference_context>' in script.planner_prompts[0]


@pytest.mark.anyio
async def test_recovery_recomputes_requirements_from_current_instructions():
    script = Script([], [{'outcome': {'status': 'task_complete', 'final_answer': 'Hi!'}}])
    orchestrator, _, config = make(script, direct=True)
    orchestrator.state = config.new_memory('Hi')
    checkpoint = orchestrator.checkpoint_browser()
    checkpoint['requirements'].update(literal_urls=['http://old.test/index.html\\'],
                                      require_screenshot=True, require_download=True)
    orchestrator.browser_checkpoint = checkpoint
    result = await orchestrator.start_task('Hi')
    assert result.status == 'completed'
    assert orchestrator.state.requirements.literal_urls == []
    assert not orchestrator.state.requirements.require_screenshot
    assert not orchestrator.state.requirements.require_download
