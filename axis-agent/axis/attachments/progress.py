"""Scoped document coverage and bounded, source-linked findings in SQLite."""
import json

from .scope import requested_range, normalized


def matches_quote(quote, section):
    texts = [section['text']]
    for cell in section.get('cells') or []:
        texts.extend(value for key in ('value', 'display') if isinstance(value := cell.get(key), str))
    quote = normalized(quote)
    return bool(quote) and any(quote in normalized(text) for text in texts)


class DocumentProgress:
    def scope(self, identifier, sheet=None, cell_range=None):
        use = self._uses().get(identifier, {})
        requested = requested_range(self.store, self.task_id, identifier)
        scope = {'sheet': sheet or use.get('sheet'), 'cell_range': cell_range or use.get('cell_range')}
        if requested:
            if scope['sheet'] and scope['sheet'] != requested['sheet']:
                raise ValueError('Read the sheet requested by the user: ' + requested['sheet'])
            if scope['cell_range']:
                from openpyxl.utils.cell import range_boundaries
                a, b, c, d = range_boundaries(scope['cell_range'])
                x, y, z, w = range_boundaries(requested['cell_range'])
                if not (x <= a <= c <= z and y <= b <= d <= w):
                    raise ValueError('Read within the requested range: ' + requested['cell_range'])
            scope = {'sheet': requested['sheet'], 'cell_range': scope['cell_range'] or requested['cell_range']}
        return scope

    @staticmethod
    def scope_key(scope):
        return json.dumps(scope, sort_keys=True)

    def coverage(self):
        result = []
        with self.store.history.lock:
            db = self.store.history.db
            full_reads = {r[0] for r in db.execute('SELECT section_id FROM document_reads WHERE task_id=?', (self.task_id,))}
            scoped_reads = [(r['section_id'], r['scope'], json.loads(r['scope'])) for r in db.execute('SELECT section_id,scope FROM document_scope_reads WHERE task_id=?', (self.task_id,))]
            for item in self.store.catalog(self.task_id):
                scope = self.scope(item['id'])
                key = self.scope_key(scope)
                where, args, _ = self.store.section_filter(item['id'], **scope)
                rows = db.execute(f'SELECT id,ordinal,location FROM attachment_sections WHERE {where} ORDER BY ordinal', args).fetchall()
                read = set(full_reads)
                intervals = {}
                for section_id, recorded_key, recorded in scoped_reads:
                    if recorded_key == key:
                        read.add(section_id)
                    elif scope['cell_range'] and recorded.get('cell_range') and recorded.get('sheet') == scope['sheet']:
                        from openpyxl.utils.cell import range_boundaries
                        a, _, b, _ = range_boundaries(recorded['cell_range'])
                        intervals.setdefault(section_id, []).append((a, b))
                if scope['cell_range']:
                    from openpyxl.utils.cell import range_boundaries
                    x, _, y, _ = range_boundaries(scope['cell_range'])
                    for section_id, chunks in intervals.items():
                        next_column = x
                        for a, b in sorted(chunks):
                            if a > next_column:
                                break
                            next_column = max(next_column, b+1)
                        if next_column > y:
                            read.add(section_id)
                missing = [dict(row) for row in rows if row['id'] not in read]
                scanned = db.execute('SELECT 1 FROM document_scans WHERE task_id=? AND attachment_id=? AND scope=?', (self.task_id, item['id'], key)).fetchone()
                result.append(dict(attachment_id=item['id'], filename=item['filename'], **scope,
                    purpose=self._uses().get(item['id'], {}).get('purpose'), required_coverage=self._uses().get(item['id'], {}).get('coverage'),
                    total=len(rows), read=len(rows)-len(missing), complete=bool(rows or scanned) and not missing,
                    next_section_ids=[row['id'] for row in missing[:16]], remaining=len(missing)))
        return result

    def notes(self, offset=0, identifier=None, limit=24, max_chars=12000):
        with self.store.history.lock:
            where = 'n.task_id=?'
            args = [self.task_id]
            if identifier:
                self.store.require(self.task_id, identifier)
                where += ' AND s.attachment_id=?'
                args.append(identifier)
            rows = self.store.history.db.execute(f'''SELECT n.section_id,n.quote,s.attachment_id,s.location
                FROM document_notes n JOIN attachment_sections s ON s.id=n.section_id
                WHERE {where} ORDER BY s.attachment_id,s.ordinal,n.quote LIMIT ? OFFSET ?''', (*args, limit+1, offset)).fetchall()
        items, size, examined = [], 0, 0
        for row in rows[:limit]:
            item = dict(row)
            if items and size + len(json.dumps(item)) > max_chars:
                break
            examined += 1
            if self._reading_restricted(item['attachment_id']):
                continue
            scope = self.scope(item['attachment_id'])
            if scope.get('cell_range') or scope.get('sheet'):
                source = self.store.sections(self.task_id, item['attachment_id'], ids=[item['section_id']], **scope)
                if not source['items'] or not matches_quote(item['quote'], source['items'][0]):
                    continue
            items.append(item)
            size += len(json.dumps(item))
        return dict(items=items, has_more=len(rows)>examined, next_offset=offset+examined)

    def retain_notes(self, notes):
        rejected = []
        with self.store.history.transaction():
            for note in notes:
                reason = self._note_rejection(note)
                if reason:
                    rejected.append(dict(section_id=note.section_id, reason=reason))
                else:
                    self.store.history.db.execute('INSERT OR IGNORE INTO document_notes VALUES(?,?,?)', (self.task_id, note.section_id, note.quote))
            task = self.store.history.task(self.task_id)
            task['document_note_rejections'] = rejected
            self.store.history.save_task(task)
        return rejected

    def _note_rejection(self, note):
        row = self.store.history.db.execute('''SELECT s.attachment_id FROM attachment_sections s
            JOIN task_attachments t ON t.attachment_id=s.attachment_id
            WHERE t.task_id=? AND s.id=?''', (self.task_id, note.section_id)).fetchone()
        if not row:
            return 'unknown_section'
        identifier = row[0]
        if self._reading_restricted(identifier):
            return 'read_restricted'
        recorded = self.store.history.db.execute('SELECT scope FROM document_scope_reads WHERE task_id=? AND section_id=?',
                                                (self.task_id, note.section_id)).fetchall()
        if not recorded:
            return 'unread_source'
        result = self.store.sections(self.task_id, identifier, ids=[note.section_id], **self.scope(identifier))
        if not result['items']:
            return 'scope_mismatch'
        current_match = matches_quote(note.quote, result['items'][0])
        read_match = False
        for record in recorded:
            source = self.store.sections(self.task_id, identifier, ids=[note.section_id], **json.loads(record[0]))
            if source['items'] and matches_quote(note.quote, source['items'][0]):
                read_match = True
                break
        if current_match and read_match:
            return None
        return 'scope_mismatch' if current_match or read_match else 'quote_mismatch'

    def checkpoint(self, state, limits=None):
        """Save compact control state; raw source content already lives in storage."""
        with self.store.history.transaction():
            task = self.store.history.task(self.task_id)
            # Source text stays private, out of every public task/event payload.
            self.store.history.db.execute('INSERT INTO document_checkpoints VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET last_result=excluded.last_result',
                (self.task_id, json.dumps(self.last_result)))
            task['document_checkpoint'] = dict(
                base_limits=limits or task.get('document_checkpoint', {}).get('base_limits', task.get('limits', {})),
                remaining_work=state.remaining_work, total_steps=state.counters.total_steps,
                sequence=state.sequence, evidence_number=state.evidence_number, goal_number=state.goal_number,
                pending_uploads=state.pending_uploads,
                pending_changes={key:value.model_dump(mode='json') for key,value in state.pending_changes.items()},
                current_goal_id=state.current_goal_id, current_goal=state.current_goal,
                current_verification=state.current_verification.model_dump(mode='json') if state.current_verification else None,
                assertions=[value.model_dump(mode='json') for value in state.assertions],
                last_interaction_by_tab=state.last_interaction_by_tab,
                model_requests=int(state.usage.requests), browser_actions=state.counters.browser_actions,
                mutation_count=state.counters.mutation_count, verified_mutation_count=state.counters.verified_mutation_count,
                successful_actions=state.successful_actions,
                input_tokens=int(state.usage.input_tokens), output_tokens=int(state.usage.output_tokens),
                extra_steps=state.extra_steps, extra_model_requests=state.extra_model_requests,
                extra_browser_actions=state.extra_browser_actions,
                last_error=state.last_error,
                auto_steps=state.document_auto_steps,
                extension_progress=state.document_extension_progress)
            self.store.history.save_task(task)
