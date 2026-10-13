"""Attachment contracts: exact source reads, scoped retrieval, and durable progress."""
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from axis.attachments.models import DocumentRequest, WorkflowItem
from axis.attachments.parsing import parse
from axis.attachments.runtime import DocumentSession
from axis.attachments.search import fuse
from axis.attachments.store import AttachmentStore
from axis.models import AttachmentRef, AxisConfig, AssertionRecord, GoalCheck
from axis.ui_store import Store, ServiceError
from axis.orchestrator import AxisOrchestrator


class TinyEmbeddings:
    """Deterministic semantic synonym to prove independent vector retrieval."""
    def encode(self, texts, *, query=False):
        return np.asarray([[1, 0, 0] if any(word in text.lower() for word in ('logout', 'inactive')) else [0, 1, 0] for text in texts], dtype=np.float32)


class OfflineEmbeddings:
    def encode(self, *args, **kwargs):
        raise RuntimeError('offline')


@pytest.fixture
def documents(tmp_path):
    history = Store(tmp_path / 'history.sqlite3')
    files = AttachmentStore(history, tmp_path / 'attachments', embeddings=TinyEmbeddings())
    cid = history.create_conversation()['id']
    task = dict(id=uuid4().hex, conversation_id=cid, title='Fixture')
    with history.transaction():
        history.save_task(task)
    yield files, cid, task['id']
    files.close()
    history.close()


def ingest(files, cid, name, data):
    stage = files.directory / f'{uuid4().hex}.part'
    stage.write_bytes(data)
    item = files.accept(cid, name, stage, len(data))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        item = files.get(item['id'])
        if item['status'] != 'processing' and item['search_status'] != 'indexing':
            return item
        time.sleep(.01)
    raise AssertionError('Indexing did not finish')


def link(files, cid, task, item, role='reference'):
    files.link(task, cid, [AttachmentRef(attachment_id=item['id'], role=role)])
    if role == 'instructions':
        # Test execution is delegated by a user prompt, never by an old UI role.
        with files.history.transaction():
            saved = files.history.task(task)
            saved.setdefault('instruction', 'Follow all instructions in the attached files.')
            files.history.save_task(saved)


def test_hybrid_search_finds_synonym_without_keyword_and_keeps_task_scope(documents):
    files, cid, task = documents
    item = ingest(files, cid, 'guide.md', b'Terminate inactive sessions after 30 minutes.\nUse blue for the theme.')
    link(files, cid, task, item)
    other = ingest(files, cid, 'unattached.txt', b'logout hidden configuration')
    result = files.search(task, 'logout')
    assert result['mode'] == 'hybrid'
    assert result['items'][0]['attachment_id'] == item['id']
    assert 'inactive' in result['items'][0]['text']
    assert all(x['attachment_id'] != other['id'] for x in result['items'])
    with pytest.raises(ValueError, match='not available'):
        files.search(task, 'anything', other['id'])


def test_offline_keyword_search_and_fts_query_escaping(documents):
    files, cid, task = documents
    files.embeddings = OfflineEmbeddings()
    item = ingest(files, cid, 'config.txt', b'SESSION_TIMEOUT is 30 minutes.')
    link(files, cid, task, item)
    assert item['search_status'] == 'keyword'
    result = files.search(task, '"SESSION_TIMEOUT" OR (missing:*)')
    assert result['mode'] == 'keyword'
    assert result['items'][0]['text'] == 'SESSION_TIMEOUT is 30 minutes.'
    assert files.search(task, '"()') == {'items': [], 'mode': 'keyword', 'note': 'Matches locate sources; read the section before using values. Ranking is not confidence.'}


def test_upload_original_bytes_and_no_arbitrary_paths(documents):
    files, cid, task = documents
    original = b'original\r\nbytes\r\n'
    item = ingest(files, cid, '../../config.txt', original)
    link(files, cid, task, item, 'upload')
    session = DocumentSession(files, task)
    path = Path(session.resolve_uploads([item['id']])[0])
    assert path.read_bytes() == original
    assert path.name == 'config.txt'
    with pytest.raises(ValueError):
        session.resolve_uploads([str(path)])
    with pytest.raises(ServiceError, match='task history'):
        files.delete(item['id'])


def test_cross_conversation_attachment_is_rejected(documents):
    files, cid, task = documents
    other = files.history.create_conversation()['id']
    item = ingest(files, other, 'other.txt', b'Other conversation')
    with pytest.raises(ServiceError, match='this conversation'):
        link(files, cid, task, item)


def test_read_pagination_covers_all_text_without_truncation(documents):
    files, cid, task = documents
    long_line = 'x' * 5000
    item = ingest(files, cid, 'long.txt', (long_line + '\n' + '\n'.join(f'step {i}' for i in range(15))).encode())
    link(files, cid, task, item, 'instructions')
    session = DocumentSession(files, task)
    pieces, offset = [], 0
    while True:
        result = session.execute(DocumentRequest(operation='read', attachment_id=item['id'], offset=offset))
        pieces.extend(result['items'])
        if not result['has_more']:
            break
        offset = result['next_offset']
    assert ''.join(p['text'] for p in pieces if p['location'].startswith('line 1 ')) == long_line
    assert len(pieces) == item['sections']
    assert session.completion_errors()  # read != planned or executed


def test_workflow_requires_reading_order_and_verified_browser_postcondition(documents):
    files, cid, task = documents
    item = ingest(files, cid, 'steps.md', b'Set timeout to 30.\nSet theme to blue.')
    link(files, cid, task, item, 'instructions')
    session = DocumentSession(files, task)
    rows = files.sections(task, item['id'])['items']
    request = DocumentRequest(operation='plan', steps=[WorkflowItem(section_id=r['id'], instruction=r['text'], expected_result='Value saved') for r in rows])
    with pytest.raises(ValueError, match='Read every'):
        session.execute(request)
    session.execute(DocumentRequest(operation='read', attachment_id=item['id']))
    session.execute(request)
    session.execute(request)  # retry cannot duplicate steps
    steps = session.workflow()['next_steps']
    assert len(steps) == 2
    with pytest.raises(ValueError, match='first pending'):
        session.begin(steps[1]['id'], 'goal_2')
    session.begin(steps[0]['id'], 'goal_1', GoalCheck(assertion='value', expected='30', target='Timeout'))
    assert session.context()['current_source']['items'][0]['text'] == 'Set timeout to 30.'
    state = AxisConfig.load().new_memory('Configure')
    state.current_goal_id = 'goal_1'
    state.current_verification = GoalCheck(assertion='value', expected='30', target='Timeout')
    with pytest.raises(ValueError, match='passing browser_assert'):
        session.verified(state, AxisOrchestrator._matches_goal_check)
    state.assertions.append(AssertionRecord(assertion='value', expected='30', target=json.dumps({'locator': {'label': 'Timeout'}}), passed=True, goal_id='goal_1'))
    session.verified(state, AxisOrchestrator._matches_goal_check)
    recovered = DocumentSession(files, task)
    assert recovered.workflow()['counts'] == {'pending': 1, 'verified': 1}
    assert recovered.workflow()['next_steps'][0]['id'] == steps[1]['id']
    assert recovered.completion_errors()


def test_spreadsheet_preserves_formula_cached_value_units_and_cell_locations(tmp_path):
    from openpyxl import Workbook
    path = tmp_path / 'mapd.xlsx'
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Authentication'
    sheet.append(['Setting', 'Value', 'Unit'])
    sheet.append(['Idle timeout', 30, 'minutes'])
    sheet.append(['Identifier', 7, 'code'])
    sheet['B3'].number_format = '0000'
    sheet.append(['Calculated', '=B2*2', 'minutes'])
    workbook.save(path)
    rows, warnings = parse(path)
    assert rows[1]['cells'][1]['address'] == 'B2'
    assert rows[1]['cells'][1]['value'] == 30
    assert rows[2]['cells'][1]['display'] == '0007'
    assert rows[3]['cells'][1]['formula'] == '=B2*2'
    assert rows[3]['cells'][1]['value'] is None
    assert warnings


def test_word_powerpoint_pdf_csv_and_legacy_parsing(tmp_path):
    from docx import Document
    from pptx import Presentation
    from pypdf import PdfWriter
    word = Document()
    word.add_heading('Authentication', 1)
    word.add_paragraph('Set session timeout to 30 minutes.')
    word.add_table(rows=1, cols=2).cell(0, 0).text = 'Configuration table'
    path = tmp_path / 'guide.docx'
    word.save(path)
    sections, _ = parse(path)
    assert any('30 minutes' in x['text'] for x in sections)
    assert any('table row' in x['location'] for x in sections)
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = 'Install the product'
    slide.notes_slide.notes_text_frame.text = 'Then configure authentication'
    path = tmp_path / 'guide.pptx'
    deck.save(path)
    sections, _ = parse(path)
    assert any(x['location'] == 'slide 1' for x in sections)
    assert any('configure authentication' in x['text'] for x in sections)
    path = tmp_path / 'scan.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(path)
    sections, warnings = parse(path)
    assert not sections and any('OCR' in text for text in warnings)
    path = tmp_path / 'config.csv'
    path.write_text('setting,value\ntimeout,30\n', encoding='utf-8')
    sections, _ = parse(path)
    assert sections[1]['cells'][1]['value'] == '30'
    assert parse(tmp_path / 'legacy.doc')[1]


def test_corrupt_file_stays_available_for_website_upload(documents):
    files, cid, task = documents
    item = ingest(files, cid, 'broken.pdf', b'not actually a PDF')
    assert item['status'] == 'unreadable'
    assert files.path(item['id']).read_bytes() == b'not actually a PDF'
    link(files, cid, task, item)
    session = DocumentSession(files, task)
    with pytest.raises(ValueError, match='not readable'):
        session.execute(DocumentRequest(operation='read', attachment_id=item['id']))
    assert Path(session.resolve_uploads([item['id']])[0]).read_bytes() == b'not actually a PDF'


def test_fusion_deduplicates_and_combines_independent_rankings():
    assert fuse([1, 2, 2], [2, 3])[0] == 2
    assert len(fuse([1, 2], [2, 3])) == 3


def test_attachment_api_streams_over_chat_limit_and_preserves_retry_identity(tmp_path):
    from starlette.testclient import TestClient
    from axis.ui_service import create_app
    from axis.ui_runner import Runner
    from test_ui_service import ControlledOrchestrator, TOKEN, ORIGIN, HEADERS, conversation, submit
    history = Store(tmp_path / 'history.sqlite3')
    runner = Runner(history, AxisConfig.load(), factory=ControlledOrchestrator)
    app = create_app(history, runner, token=TOKEN, origin=ORIGIN)
    runner.attachments.embeddings = OfflineEmbeddings()
    with TestClient(app, base_url='http://127.0.0.1:8766', headers=HEADERS) as client:
        cid = conversation(client)
        data = b'Long instruction.\n' * 3000
        response = client.post(f'/api/conversations/{cid}/attachments?filename=steps.txt', content=data)
        assert response.status_code == 201
        identifier = response.json()['id']
        refs = [dict(attachment_id=identifier, role='upload')]
        request_id = uuid4().hex
        accepted = submit(client, cid, attachments=refs, client_request_id=request_id)
        assert accepted.status_code == 202
        duplicate = submit(client, cid, attachments=refs, client_request_id=request_id)
        assert duplicate.json()['duplicate'] is True
        changed = submit(client, cid, client_request_id=request_id)
        assert changed.status_code == 409
        snapshot = client.get(f'/api/conversations/{cid}').json()
        assert snapshot['messages'][0]['attachments'] == refs
        assert snapshot['tasks'][0]['attachments'][0]['filename'] == 'steps.txt'
        assert client.get(f'/api/attachments/{identifier}', headers={'Authorization': 'Bearer wrong'}).status_code == 401
        client.post('/api/tasks/' + accepted.json()['task']['id'] + '/control', json={'action': 'stop'})


@pytest.mark.anyio
async def test_document_only_agent_reads_attachment_without_browser(documents):
    from test_axis_agent import Script, make, done
    files, cid, task = documents
    item = ingest(files, cid, 'guide.txt', b'The required timeout is 30 minutes.')
    link(files, cid, task, item)
    script = Script([dict(decision='document', document_request=dict(operation='read', attachment_id=item['id'])), done('The timeout is 30 minutes.')])
    orchestrator, bridge, _ = make(script)
    orchestrator.documents = DocumentSession(files, task)
    result = await orchestrator.start_task('What is the timeout in the attachment?', task_id=task)
    assert result.status == 'completed'
    assert bridge.calls == []
    assert '30 minutes' in script.planner_prompts[1]
    assert any(e.kind == 'document' and e.attachment_id == item['id'] for e in orchestrator.state.evidence)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_planned_browser_step_only_completes_after_exact_assertion(documents):
    from test_axis_agent import Script, make, browse, done
    files, cid, task = documents
    item = ingest(files, cid, 'steps.txt', b'Confirm the page title is Google.')
    link(files, cid, task, item, 'instructions')
    session = DocumentSession(files, task)
    section = session.execute(DocumentRequest(operation='read', attachment_id=item['id']))['items'][0]
    session.execute(DocumentRequest(operation='plan', steps=[WorkflowItem(section_id=section['id'], instruction=section['text'], expected_result='Title is Google')]))
    step_id = session.workflow()['next_steps'][0]['id']
    script = Script([browse('Confirm page title', workflow_step_id=step_id, verification={'assertion': 'title', 'expected': 'Google'}), done('Verified.')],
                    [{'calls': [('browser_assert', {'tab': 'tab_1', 'command': {'assertion': 'title', 'expected': 'Google'}})], 'outcome': {'status': 'goal_reached', 'summary': 'Title verified'}}])
    orchestrator, _, _ = make(script)
    orchestrator.documents = session
    result = await orchestrator.start_task('Follow the attached steps.', task_id=task)
    assert result.status == 'completed'
    assert session.workflow()['counts'] == {'verified': 1}
    assert not session.completion_errors()


@pytest.mark.anyio
async def test_browser_upload_resolves_attachment_id_to_original_file(documents):
    from test_axis_agent import Script, make, browse, done
    files, cid, task = documents
    item = ingest(files, cid, 'config.txt', b'Preserve this original file.')
    link(files, cid, task, item, 'upload')
    upload = ('browser_act', {'tab': 'tab_1', 'command': {'action': 'upload', 'locator': {'selector': 'input[type=file]'}, 'files': [item['id']]}})
    assertion = ('browser_assert', {'tab': 'tab_1', 'command': {'assertion': 'text', 'locator': {'selector': '.attachment-chip'}, 'expected': 'config.txt'}})
    script = Script([browse('Upload the attached file', verification={'assertion': 'text', 'expected': 'config.txt'}), done('Uploaded.')],
                    [{'calls': [upload, assertion], 'outcome': {'status': 'goal_reached', 'summary': 'Receipt checked'}}])
    orchestrator, bridge, _ = make(script, approval=lambda *_: True)
    orchestrator.documents = DocumentSession(files, task)
    orchestrator.browser.upload_roots = (files.path(item['id']).parent,)
    result = await orchestrator.start_task('Upload the attached file.', task_id=task)
    assert result.status == 'completed'
    dispatched = [params for method, params in bridge.calls if method == 'locator.setInputFiles']
    assert len(dispatched) == 1
    assert Path(dispatched[0]['files'][0]).read_bytes() == b'Preserve this original file.'


def test_recovery_cannot_weaken_saved_postcondition(documents):
    files, cid, task = documents
    item = ingest(files, cid, 'steps.txt', b'Set timeout to 30 minutes.')
    link(files, cid, task, item, 'instructions')
    session = DocumentSession(files, task)
    row = session.execute(DocumentRequest(operation='read', attachment_id=item['id']))['items'][0]
    session.execute(DocumentRequest(operation='plan', steps=[WorkflowItem(section_id=row['id'], instruction=row['text'], expected_result='30 minutes saved')]))
    step = session.workflow()['next_steps'][0]
    check = GoalCheck(assertion='value', expected='30', target='Timeout')
    session.begin(step['id'], 'goal_1', check)
    recovered = DocumentSession(files, task)
    assert recovered.workflow()['next_steps'][0]['status'] == 'running'
    with pytest.raises(ValueError, match='Retain the saved'):
        recovered.begin(step['id'], 'goal_1', GoalCheck(assertion='title', expected='Settings'))
    with pytest.raises(ValueError, match='goal identity'):
        recovered.begin(step['id'], 'goal_2', check)
    recovered.begin(step['id'], 'goal_1', check)


def test_original_files_removed_when_conversation_is_deleted(tmp_path):
    from starlette.testclient import TestClient
    from axis.ui_service import create_app
    from axis.ui_runner import Runner
    from test_ui_service import ControlledOrchestrator, TOKEN, ORIGIN, HEADERS, conversation
    history = Store(tmp_path / 'history.sqlite3')
    runner = Runner(history, AxisConfig.load(), factory=ControlledOrchestrator)
    app = create_app(history, runner, token=TOKEN, origin=ORIGIN)
    runner.attachments.embeddings = OfflineEmbeddings()
    with TestClient(app, base_url='http://127.0.0.1:8766', headers=HEADERS) as client:
        cid = conversation(client)
        file = ingest(runner.attachments, cid, 'private.txt', b'Private fixture data')
        path = runner.attachments.path(file['id'])
        assert client.delete(f'/api/conversations/{cid}').status_code == 200
        assert not path.exists()
        assert client.get('/api/attachments/' + file['id']).status_code == 404


def test_index_reuses_embedding_cache(documents):
    files, cid, task = documents
    first = ingest(files, cid, 'one.txt', b'Cached text')
    assert first['search_status'] == 'hybrid'
    files.embeddings = OfflineEmbeddings()
    second = ingest(files, cid, 'two.txt', b'Cached text')
    assert second['search_status'] == 'hybrid'


def test_restart_recovers_running_step_without_replaying_verified_step(tmp_path):
    from starlette.testclient import TestClient
    from axis.ui_service import create_app
    from axis.ui_runner import Runner
    from test_ui_service import TOKEN, ORIGIN, HEADERS, wait_for
    from test_axis_agent import Script, make, browse, done
    path = tmp_path / 'history.sqlite3'
    history = Store(path)
    files = AttachmentStore(history, tmp_path / 'attachments', embeddings=TinyEmbeddings())
    cid, task_id = history.create_conversation()['id'], uuid4().hex
    config = AxisConfig.load()
    task = dict(id=task_id, conversation_id=cid, title='Configuration', instruction='Follow all steps in the attachment.',
                status='active', owns_runtime=True, created_at=time.time(), updated_at=time.time(),
                limits=config.run.model_dump(), counters={'browser_actions': 4, 'total_steps': 2, 'model_requests': 5})
    with history.transaction():
        history.save_task(task)
        history.message(cid, task_id, task['instruction'], uuid4().hex, 'new_task')
    item = ingest(files, cid, 'steps.txt', b'Confirm first setting.\nConfirm page title is Google.')
    link(files, cid, task_id, item, 'instructions')
    session = DocumentSession(files, task_id)
    rows = session.execute(DocumentRequest(operation='read', attachment_id=item['id']))['items']
    session.execute(DocumentRequest(operation='plan', steps=[WorkflowItem(section_id=row['id'], instruction=row['text'], expected_result='Configured') for row in rows]))
    first, second = session.workflow()['next_steps']
    with history.transaction():
        history.db.execute("UPDATE workflow_steps SET status='verified' WHERE id=?", (first['id'],))
    check = GoalCheck(assertion='title', expected='Google')
    session.begin(second['id'], 'old_goal', check)
    task.update(attachments=files.catalog(task_id), workflow=session.workflow())
    with history.transaction():
        history.save_task(task)
    files.close()
    history.close()

    history = Store(path)
    history.interrupt()
    script = Script([browse('Reconcile page title', workflow_step_id=second['id'], verification=check.model_dump()), done('Recovered and verified.')],
                    [{'calls': [('browser_assert', {'tab': 'tab_1', 'command': {'assertion': 'title', 'expected': 'Google'}})], 'outcome': {'status': 'goal_reached', 'summary': 'Existing result verified'}}])
    orchestrator, bridge, _ = make(script)
    def factory(config, on_event):
        orchestrator.on_event = on_event
        return orchestrator
    runner = Runner(history, config, factory=factory)
    with TestClient(create_app(history, runner, token=TOKEN, origin=ORIGIN), base_url='http://127.0.0.1:8766', headers=HEADERS) as client:
        response = client.post(f'/api/tasks/{task_id}/control', json={'action': 'resume'})
        assert response.status_code == 200
        wait_for(lambda: runner.future.done())
        saved = client.get(f'/api/tasks/{task_id}').json()
        assert saved['status'] == 'completed'
        assert saved['workflow']['counts'] == {'verified': 2}
        assert saved['counters']['browser_actions'] == 5
        assert saved['counters']['total_steps'] == 3
        assert saved['counters']['model_requests'] > 5
        assert 'locator.fillRef' not in bridge.methods


def test_upload_size_limit_cleans_staging_files(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from axis.ui_service import create_app
    from axis.ui_runner import Runner
    from test_ui_service import ControlledOrchestrator, TOKEN, ORIGIN, HEADERS, conversation
    monkeypatch.setattr('axis.attachments.api.MAX_FILE_BYTES', 100)
    history = Store(tmp_path / 'history.sqlite3')
    runner = Runner(history, AxisConfig.load(), factory=ControlledOrchestrator)
    with TestClient(create_app(history, runner, token=TOKEN, origin=ORIGIN), base_url='http://127.0.0.1:8766', headers=HEADERS) as client:
        cid = conversation(client)
        response = client.post(f'/api/conversations/{cid}/attachments?filename=large.txt', content=b'a' * 101)
        assert response.status_code == 413
        assert not list(runner.attachments.directory.glob('*.part'))
        assert client.get(f'/api/conversations/{cid}/attachments').json()['items'] == []


def test_invalid_followup_attachments_are_rejected_before_acceptance(documents):
    files, cid, task = documents
    items = [ingest(files, cid, f'{i}.txt', b'Configuration') for i in range(9)]
    for item in items[:8]:
        link(files, cid, task, item)
    with pytest.raises(ServiceError, match='eight'):
        files.validate_refs(cid, [AttachmentRef(attachment_id=items[8]['id'])], task)
    duplicate = AttachmentRef(attachment_id=items[0]['id'])
    with pytest.raises(ServiceError, match='once'):
        files.validate_refs(cid, [duplicate, duplicate], task)
    assert len(files.catalog(task)) == 8


def test_merged_workbook_headers_retain_anchor_coordinates(tmp_path):
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.merge_cells('A1:C1')
    sheet['A1'] = 'Authentication module'
    sheet.append(['Setting', 'Value', 'Unit'])
    sheet.append(['Timeout', 30, 'minutes'])
    path = tmp_path / 'merged.xlsx'
    workbook.save(path)
    rows, _ = parse(path)
    metadata = json.loads(rows[0]['text'])
    assert metadata['merged_ranges'] == ['A1:C1']
    assert any('Authentication module' in row['text'] for row in rows)


def test_markdown_prompt_preserves_full_text_and_only_includes_markdown(documents):
    files, cid, task = documents
    text = '# Instructions\r\n\r\n' + ('  café \t\n' * 2500) + '\nLast instruction.\n'
    first = ingest(files, cid, 'WORKFLOW.MD', b'\xef\xbb\xbf' + text.encode('utf-8'))
    second = ingest(files, cid, 'notes.md', b'Additional instruction.\n')
    other = ingest(files, cid, 'data.txt', b'Do not inline this text file.')
    for item in (first, second, other):
        link(files, cid, task, item)
    session = DocumentSession(files, task)
    prompt = session.input_prompt('Apply these changes.')
    assert prompt.startswith('Apply these changes.\n\n')
    assert prompt == 'Apply these changes.\n\n' + text + '\n\nAdditional instruction.\n'
    assert 'Do not inline this text file.' not in prompt
    assert {item['id'] for item in session.context()['attachments']} == {other['id']}
    assert session.input_prompt('Apply these changes.') == prompt


def test_markdown_followup_includes_only_files_on_that_message(documents):
    files, cid, task = documents
    first = ingest(files, cid, 'first.md', b'First instruction.')
    second = ingest(files, cid, 'second.md', b'Second instruction.')
    for item in (first, second):
        link(files, cid, task, item)
    with files.history.transaction():
        files.history.message(cid, task, 'Also apply this.', uuid4().hex, 'follow_up',
                              attachments=[dict(attachment_id=second['id'], role='auto')])
    session = DocumentSession(files, task)
    assert session.input_prompt('Also apply this.', follow_up=True) == 'Also apply this.\n\nSecond instruction.'
    assert session.input_prompt('Continue.', follow_up=True) == 'Continue.'


def test_markdown_prompt_rejects_invalid_utf8(documents):
    files, cid, task = documents
    item = ingest(files, cid, 'invalid.md', b'\xff')
    link(files, cid, task, item)
    session = DocumentSession(files, task)
    with pytest.raises(ValueError, match='must be UTF-8 encoded'):
        session.input_prompt('Apply it.')
    assert session._prompt_attachment_ids() == set()


@pytest.mark.anyio
async def test_markdown_reaches_model_in_full_without_document_reads(documents):
    from test_axis_agent import Script, make, done
    files, cid, task = documents
    text = '# Request\n\n' + ('Preserve this line.\n' * 1200) + 'Tail value: café.\n'
    item = ingest(files, cid, 'request.md', text.encode())
    link(files, cid, task, item)
    script = Script([done('Read the complete prompt.')])
    orchestrator, bridge, _ = make(script)
    orchestrator.documents = DocumentSession(files, task)
    result = await orchestrator.start_task('Summarize the attached file.', task_id=task)
    expected = 'Summarize the attached file.\n\n' + text
    assert orchestrator.state.original_request == expected
    assert result.status == 'completed'
    assert orchestrator.documents is None
    assert not orchestrator.attachment_session.completion_errors()
    assert 'Tail value: café.' in str(script.prompts)
    script.planner.append(done('Read the follow-up.'))
    with files.history.transaction():
        files.history.message(cid, task, 'Use it again.', uuid4().hex, 'follow_up',
                              attachments=[dict(attachment_id=item['id'], role='auto')])
    await orchestrator.continue_task('Use it again.')
    assert orchestrator.state.follow_ups[-1] == 'Use it again.\n\n' + text


@pytest.mark.anyio
@pytest.mark.parametrize('direct', [True, False])
async def test_markdown_browser_request_uses_prompt_without_document_workflow(documents, direct):
    from test_axis_agent import Script, make, browse, done
    files, cid, task = documents
    text = '# Instructions\nOpen https://www.google.com/ and verify its title is Google.\n'
    item = ingest(files, cid, 'instructions.md', text.encode())
    link(files, cid, task, item)
    script = Script([browse('Verify the page title', verification={'assertion': 'title', 'expected': 'Google'}), done('Verified.')],
                    [{'calls': [('browser_assert', {'tab': 'tab_1', 'command': {'assertion': 'title', 'expected': 'Google'}})],
                      'outcome': {'status': 'task_complete' if direct else 'goal_reached', 'final_answer': 'Verified.'}}])
    orchestrator, bridge, _ = make(script, direct=direct)
    orchestrator.documents = DocumentSession(files, task)
    result = await orchestrator.start_task('Perform as directed', task_id=task)
    assert result.status == 'completed', result.reason
    assert orchestrator.state.original_request == 'Perform as directed\n\n' + text
    assert orchestrator.documents is None
    assert not orchestrator.attachment_session.workflow()['next_steps']
    assert script.planner_calls == (0 if direct else 2)
    assert 'source-linked' not in '\n'.join(script.planner_prompts + script.navigator_prompts)
    assert 'task_documents' not in '\n'.join(script.navigator_prompts)
    assert all('documents' not in prompt for prompt in script.planner_prompts)
    prompt = script.navigator_prompts[0] if direct else script.planner_prompts[0]
    assert 'Open https://www.google.com/ and verify its title is Google.' in prompt


@pytest.mark.anyio
async def test_markdown_prompt_and_other_document_use_separate_paths(documents):
    from test_axis_agent import Script, make, done
    files, cid, task = documents
    markdown = ingest(files, cid, 'request.md', b'Answer using the timeout in the text attachment.')
    reference = ingest(files, cid, 'config.txt', b'Timeout is 30 minutes.')
    for item in (markdown, reference):
        link(files, cid, task, item)
    script = Script([dict(decision='document', document_request=dict(operation='read', attachment_id=reference['id'])), done('30 minutes.')])
    orchestrator, _, _ = make(script)
    orchestrator.documents = DocumentSession(files, task)
    result = await orchestrator.start_task('Perform as directed', task_id=task)
    assert result.status == 'completed', result.reason
    assert orchestrator.state.original_request == 'Perform as directed\n\nAnswer using the timeout in the text attachment.'
    assert {item['id'] for item in orchestrator.documents.context()['attachments']} == {reference['id']}
    assert {item['attachment_id'] for item in orchestrator.documents.coverage()} == {reference['id']}
    assert markdown['id'] not in orchestrator.documents._uses()
    search = orchestrator.documents.execute(DocumentRequest(operation='search', query='timeout'))
    assert {item['attachment_id'] for item in search['items']} == {reference['id']}


def test_markdown_ui_submission_runs_as_full_prompt(tmp_path):
    from starlette.testclient import TestClient
    from axis.ui_service import create_app
    from axis.ui_runner import Runner
    from test_ui_service import TOKEN, ORIGIN, HEADERS, conversation, submit, wait_for
    from test_axis_agent import Script, make
    history = Store(tmp_path / 'history.sqlite3')
    script = Script([], [{'calls': [('browser_assert', {'tab': 'tab_1', 'command': {'assertion': 'title', 'expected': 'Google'}})],
                          'outcome': {'status': 'task_complete', 'final_answer': 'Verified.'}}])
    orchestrator, _, config = make(script, direct=True)
    def factory(config, on_event):
        orchestrator.on_event = on_event
        return orchestrator
    runner = Runner(history, config, factory=factory)
    app = create_app(history, runner, token=TOKEN, origin=ORIGIN)
    runner.attachments.embeddings = OfflineEmbeddings()
    text = '# Instructions\r\n' + ('Preserve the full prompt.\n' * 1200) + 'Open https://www.google.com/ and verify its title is Google.\n'
    with TestClient(app, base_url='http://127.0.0.1:8766', headers=HEADERS) as client:
        cid = conversation(client)
        response = client.post(f'/api/conversations/{cid}/attachments?filename=request.md', content=text.encode())
        assert response.status_code == 201
        identifier = response.json()['id']
        accepted = submit(client, cid, text='Perform as directed', attachments=[dict(attachment_id=identifier, role='auto')])
        assert accepted.status_code == 202
        wait_for(lambda: runner.future.done())
        saved = client.get('/api/tasks/' + accepted.json()['task']['id']).json()
        assert saved['status'] == 'completed', saved['current_activity']
        assert orchestrator.state.original_request == 'Perform as directed\n\n' + text
        assert orchestrator.documents is None
        assert script.planner_calls == 0
        assert 'Preserve the full prompt.' in script.navigator_prompts[0]
        assert 'Open https://www.google.com/ and verify its title is Google.' in script.navigator_prompts[0]
        assert 'task_documents' not in script.navigator_prompts[0]
        assert saved['browser_checkpoint']


def test_inline_markdown_does_not_reuse_saved_document_workflow(documents):
    files, cid, task = documents
    item = ingest(files, cid, 'instructions.md', b'Confirm the title is Google.')
    link(files, cid, task, item, 'instructions')
    session = DocumentSession(files, task)
    section = session.execute(DocumentRequest(operation='read', attachment_id=item['id']))['items'][0]
    session.execute(DocumentRequest(operation='plan', steps=[WorkflowItem(section_id=section['id'], instruction=section['text'], expected_result='Title is Google')]))
    assert session.workflow()['next_steps']
    assert session.input_prompt('Perform as directed') == 'Perform as directed\n\nConfirm the title is Google.'
    assert session.workflow()['counts'] == {}
    assert not session.workflow()['next_steps']
    assert session.context()['last_result'] is None
    assert not session.completion_errors()
