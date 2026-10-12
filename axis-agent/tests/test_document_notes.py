"""Note validation uses returned source data without blocking document reads."""
import json
import sys
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axis.attachments.models import DocumentNote, DocumentRequest
from axis.attachments.runtime import DocumentSession, DocumentRestriction
from axis.models import AxisEvent
from axis.ui_runner import public_activity, public_phase
from test_attachments import documents, ingest, link
from test_attachment_workflows import instruction, followup
from test_axis_agent import Script, make, done


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def test_cell_quotes_use_decoded_values_and_respect_read_ranges(documents):
    files, cid, task = documents
    book = Workbook()
    sheet = book.active
    sheet.title = 'Instructions'
    value = 'Enable "Priority".\nKeep the path C:\\config unchanged.'
    sheet.append([value, 'A separate instruction.'])
    content = BytesIO()
    book.save(content)
    item = ingest(files, cid, 'instructions.xlsx', content.getvalue())
    link(files, cid, task, item)
    instruction(files, task, 'Read the workbook.')
    session = DocumentSession(files, task)
    result = session.execute(DocumentRequest(operation='read', attachment_id=item['id'],
                                             sheet='Instructions', cell_range='A1:A1'))
    section_id = result['items'][0]['id']
    valid = DocumentNote(section_id=section_id, quote=value)
    other_column = DocumentNote(section_id=section_id, quote='A separate instruction.')
    assert session.retain_notes([valid, other_column]) == [
        {'section_id': section_id, 'reason': 'scope_mismatch'}]
    assert value in [note['quote'] for note in session.notes()['items']]
    assert 'A separate instruction.' not in [note['quote'] for note in session.notes()['items']]

    session.execute(DocumentRequest(operation='read', attachment_id=item['id'],
                                    sheet='Instructions', cell_range='B1:B1'))
    assert not session.retain_notes([other_column])
    followup(files, cid, task, "Read only 'Instructions'!A1:A1")
    session.refresh_scope()
    recovered = DocumentSession(files, task)
    assert value in [note['quote'] for note in recovered.notes()['items']]
    assert 'A separate instruction.' not in [note['quote'] for note in recovered.notes()['items']]
    assert recovered.retain_notes([other_column]) == [
        {'section_id': section_id, 'reason': 'scope_mismatch'}]


def test_note_batch_keeps_valid_quotes_and_rejects_unread_or_unavailable_sources(documents):
    files, cid, task = documents
    item = ingest(files, cid, 'facts.txt', b'First recorded fact.\nSecond unread fact.')
    link(files, cid, task, item)
    instruction(files, task, 'Read the file.')
    sections = files.sections(task, item['id'])['items']
    first, second = sections[0]['id'], sections[1]['id']
    session = DocumentSession(files, task)
    session.execute(DocumentRequest(operation='read', attachment_id=item['id'], section_ids=[first]))
    rejected = session.retain_notes([
        DocumentNote(section_id=first, quote='recorded fact'),
        DocumentNote(section_id=first, quote='Invented fact'),
        DocumentNote(section_id=second, quote='Second unread fact.'),
        DocumentNote(section_id=second + 10000, quote='Unavailable source'),
        DocumentNote(section_id=first, quote='First recorded'),
    ])
    assert rejected == [
        {'section_id': first, 'reason': 'quote_mismatch'},
        {'section_id': second, 'reason': 'unread_source'},
        {'section_id': second + 10000, 'reason': 'unknown_section'},
    ]
    recovered = DocumentSession(files, task)
    quotes = [note['quote'] for note in recovered.notes()['items']]
    assert 'recorded fact' in quotes and 'First recorded' in quotes
    assert 'Invented fact' not in quotes and 'Second unread fact.' not in quotes
    assert recovered.context()['note_rejections'] == rejected
    followup(files, cid, task, 'Do not read this file.')
    assert recovered.retain_notes([DocumentNote(section_id=first, quote='recorded fact')]) == [
        {'section_id': first, 'reason': 'read_restricted'}]
    with pytest.raises(DocumentRestriction):
        recovered.execute(DocumentRequest(operation='read', attachment_id=item['id']))


@pytest.mark.anyio
@pytest.mark.parametrize('remaining_batches', [[4], [2, 1, 1]])
async def test_rejected_notes_do_not_block_remaining_four_sections(documents, remaining_batches):
    files, cid, task = documents
    item = ingest(files, cid, 'instructions.txt', '\n'.join(f'Entry {i}: a source fact.' for i in range(15)).encode())
    link(files, cid, task, item)
    prompt = 'Summarize every section in the attachment.'
    instruction(files, task, prompt)
    ids = [row['id'] for row in files.sections(task, item['id'], limit=16)['items']]
    plans = [dict(decision='document', document_request=dict(operation='read', attachment_id=item['id'], section_ids=ids[:11]))]
    offset = 11
    for count in remaining_batches:
        plans.append(dict(decision='document',
            document_notes=[dict(section_id=ids[0], quote='Invented instruction'),
                            dict(section_id=ids[0], quote='Entry 0')],
            document_request=dict(operation='read', attachment_id=item['id'], section_ids=ids[offset:offset + count])))
        offset += count
    plans.append(done('All 15 sections contain numbered source facts.'))
    script = Script(plans)
    events = []
    orchestrator, bridge, _ = make(script, on_event=events.append)
    orchestrator.documents = DocumentSession(files, task)
    result = await orchestrator.start_task(prompt, task_id=task)
    assert result.status == 'completed'
    assert result.model_requests == len(plans)
    assert result.total_steps == 1 + len(remaining_batches)
    assert not bridge.calls
    assert orchestrator.documents.coverage()[0]['read'] == 15
    assert orchestrator.documents.progress()['attempts'] == 0
    assert 'Entry 0' in [note['quote'] for note in orchestrator.documents.notes()['items']]
    rejected = [event for event in events if event.detail.get('phase') == 'document_notes_rejected']
    assert len(rejected) == len(remaining_batches)
    assert all(event.detail['rejections'] == [{'section_id': ids[0], 'reason': 'quote_mismatch'}] for event in rejected)
    assert all('Invented instruction' not in json.dumps(event.detail) for event in rejected)
    assert 'note_rejections' in script.planner_prompts[-1]


def test_public_note_diagnostics_include_only_section_ids_and_known_reasons():
    event = AxisEvent(kind='status', detail={'phase': 'document_notes_rejected',
        'error': 'private source text', 'rejections': [
            {'section_id': 42, 'reason': 'quote_mismatch', 'quote': 'private source text'},
            {'section_id': 43, 'reason': 'private source text'},
        ]})
    assert public_activity(event) == 'Skipped unverified document notes: section 42: quote_mismatch'
    assert public_phase(event) == 'Evidence'
