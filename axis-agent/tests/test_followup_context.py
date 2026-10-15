"""Chat handoffs retain the instructions originally supplied as Markdown."""
import pytest
from starlette.testclient import TestClient

from test_attachments import TinyEmbeddings, ingest
from test_axis_agent import Script, make
from test_ui_service import TOKEN, ORIGIN, HEADERS, conversation, submit, wait_for
from axis.models import AxisConfig, MAX_REQUEST
from axis.ui_runner import Runner
from axis.ui_service import create_app
from axis.ui_store import Store


@pytest.fixture
def chat(tmp_path):
    store = Store(tmp_path / 'history.sqlite3')
    scripts = []
    runner = Runner(store, AxisConfig.load(), factory=lambda cfg, on_event:
                    make(scripts.pop(0), config=cfg, direct=True, on_event=on_event)[0])
    with TestClient(create_app(store, runner, token=TOKEN, origin=ORIGIN),
                    base_url='http://127.0.0.1:8766', headers=HEADERS) as client:
        runner.attachments.embeddings = TinyEmbeddings()
        yield client, runner, store, scripts, conversation(client)


def run_turn(chat, text, *, script=None, **kwargs):
    client, runner, store, scripts, cid = chat
    # Stop after the real navigator prompt is built; this tests transport, not LLM judgment.
    script = script or Script([], [{'outcome': {'status': 'failed', 'reason': 'Fixture stopped.'}}])
    scripts.append(script)
    response = submit(client, cid, text=text, **kwargs)
    assert response.status_code == 202
    task_id = response.json()['task']['id']
    wait_for(lambda: runner.future.done())
    return store.task(task_id), script


def configuration_files(chat, padding=''):
    _, runner, _, _, cid = chat
    general = ingest(runner.attachments, cid, 'general.md', b'Maximum Hop Count: 6\n')
    discovery_text = (padding + '# Discovery\r\nThreshold: 0\r\nProfile count: 3\r\n'
                      'Service priority: Enabled\r\nALL_NF_TYPE: 1h / 30s\r\n'
                      'PCF: 2h / 50s\r\nCompression: false\r\n')
    discovery = ingest(runner.attachments, cid, 'discovery.md', discovery_text.encode())
    refs = [{'attachment_id': item['id']} for item in (general, discovery)]
    return general, discovery, discovery_text, refs


@pytest.mark.parametrize('padding', ['', 'Configuration notes.\n' * (MAX_REQUEST // 20 + 1)],
                         ids=['short', 'long'])
def test_failed_task_continuation_receives_original_attachment_instructions(chat, padding):
    _, runner, _, _, cid = chat
    general, discovery, discovery_text, refs = configuration_files(chat, padding)
    ingest(runner.attachments, cid, 'unselected.md', b'Do not include this unrelated file.')
    source, original = run_turn(chat, 'Update with the given configurations', attachments=refs)
    assert source['status'] == 'failed'
    assert source['prompt_attachment_ids'] == [general['id'], discovery['id']]
    assert 'Profile count: 3' in original.navigator_prompts[0]

    correction = 'Okay then restore it to 5 and continue the remaining work.'
    continued, script = run_turn(chat, correction, conversation_task_id=source['id'])
    context = continued['reference_context']
    assert continued['instruction'] == correction
    assert context['request'] == 'Update with the given configurations'
    assert context['status'] == 'failed'
    assert context['prompt_attachments'] == [
        {'id': general['id'], 'filename': 'general.md', 'text': 'Maximum Hop Count: 6\n'},
        {'id': discovery['id'], 'filename': 'discovery.md', 'text': discovery_text},
    ]
    prompt = script.navigator_prompts[0]
    assert correction in prompt and 'Maximum Hop Count: 6' in prompt
    assert 'Profile count: 3' in prompt and 'PCF: 2h / 50s' in prompt
    assert 'Do not include this unrelated file.' not in prompt
    assert not continued.get('attachments')
    assert runner.orchestrator.documents is None
    assert runner.orchestrator.state.original_request == correction
    assert script.navigator_calls == 1 and script.planner_calls == 0


def test_repeated_continuations_preserve_sources_and_ordered_corrections(chat):
    _, runner, _, _, cid = chat
    general, discovery, discovery_text, refs = configuration_files(chat)
    source, _ = run_turn(chat, 'Apply these settings.', attachments=refs)
    update = ingest(runner.attachments, cid, 'updated-discovery.md', b'Profile count: 4\n')
    correction = 'Use Hop Count 5 and the updated Discovery file.'
    continued, _ = run_turn(chat, correction, conversation_task_id=source['id'],
                            attachments=[{'attachment_id': update['id']}])
    again, _ = run_turn(chat, 'Continue the remaining work.', conversation_task_id=continued['id'])
    final, script = run_turn(chat, 'Finish the remaining work.', conversation_task_id=again['id'])

    context = final['reference_context']
    assert context['request'] == ('Apply these settings.\nFollow-up: ' + correction
                                  + '\nFollow-up: Continue the remaining work.')
    assert [item['id'] for item in context['prompt_attachments']] == [general['id'], discovery['id'], update['id']]
    assert context['prompt_attachments'][1]['text'] == discovery_text
    prompt = script.navigator_prompts[0]
    assert prompt.count('Maximum Hop Count: 6') == 1
    assert prompt.count('Profile count: 3') == 1
    assert prompt.count('Profile count: 4') == 1
    assert correction in prompt


def test_reusing_a_source_does_not_duplicate_it_in_later_context(chat):
    general, _, _, refs = configuration_files(chat)
    source, _ = run_turn(chat, 'Apply these settings.', attachments=refs)
    reused, _ = run_turn(chat, 'Use this file again.', conversation_task_id=source['id'],
                         attachments=[{'attachment_id': general['id']}])
    continued, script = run_turn(chat, 'Continue.', conversation_task_id=reused['id'])
    assert len(continued['reference_context']['prompt_attachments']) == 2
    assert script.navigator_prompts[0].count('Maximum Hop Count: 6') == 1


def test_attachment_context_does_not_reactivate_work_for_an_unrelated_request(chat):
    _, runner, _, _, cid = chat
    item = ingest(runner.attachments, cid, 'old-work.md',
                  b'Open https://old.test and download the report. Take a screenshot.\n')
    source, _ = run_turn(chat, 'Apply the attached instructions.', attachments=[{'attachment_id': item['id']}])
    greeting = Script([], [{'outcome': {'status': 'task_complete', 'final_answer': 'Hello.'}}])
    continued, script = run_turn(chat, 'Hi', script=greeting, conversation_task_id=source['id'])

    assert continued['status'] == 'completed'
    assert continued['result']['answer'] == 'Hello.'
    assert 'Open https://old.test' in script.navigator_prompts[0]
    state = runner.orchestrator.state
    assert state.original_request == state.current_goal == 'Hi'
    assert state.requirements.literal_urls == []
    assert not state.requirements.require_download
    assert not state.requirements.require_screenshot
    assert not state.actions and not state.pending_changes
    assert runner.orchestrator.documents is None
    assert continued['counters']['browser_actions'] == 0
