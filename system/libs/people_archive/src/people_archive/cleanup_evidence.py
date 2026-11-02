"""Read-only, attributable evidence for GPT identity decisions.

Archive indexes are reused; small contact/calendar/meeting indexes are built once.
Clipped text never replaces the exact source saved alongside it. Derived profile
fields and name-only note links are explicitly not independent identity proof.
"""
import json
import re
import sqlite3
import zlib
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from pydantic import JsonValue, TypeAdapter
from typing_extensions import TypedDict


class PersonEvidenceProfile(TypedDict):
    id: str
    name: str
    aliases: list[str]
    handles: list[str]
    organization: str
    title: str
    score: int
    has_user_edits: bool


class EvidenceItem(TypedDict):
    id: str
    kind: str
    text: str
    source_id: str
    source_url: str
    original_text: str
    context: str
    truncated: bool


_STRINGS = TypeAdapter(list[str])
_JSON = TypeAdapter(dict[str, JsonValue])
_ATTENDEES = TypeAdapter(list[tuple[str, str, str, bool]])
_QUOTED = re.compile(r"(?im)^(?:On .{3,250} wrote:|[- ]*Forwarded message[- ]*|[- ]*Original Message[- ]*|>\s)")


def _display_row(row: sqlite3.Row) -> str:
    """Decode nested JSON columns so source quotations remain directly readable."""
    return json.dumps({key: json.loads(row[key]) if key.endswith('_json') else row[key] for key in row.keys()}, ensure_ascii=False)


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    # Preserve the beginning, the end (often a signature), and any signature
    # immediately BEFORE the first quotation. Never strip quotation boundaries.
    quoted = _QUOTED.search(text)
    boundary = quoted.start() if quoted else len(text)
    return text[:limit // 2] + '\n[... omitted ...]\n' + text[max(0, boundary - limit // 4):boundary] + '\n[... omitted ...]\n' + text[-limit // 4:]


def _item(kind: str, key: list[str], body: str, original: str, url: str, context: str, limit: int) -> EvidenceItem:
    source_id = json.dumps([kind, *key], ensure_ascii=False, separators=(',', ':'))
    return EvidenceItem(id=source_id, kind=kind, text=_excerpt(body, limit), source_id=source_id,
                        source_url=url, original_text=original, context=context, truncated=len(body) > limit)


def _rows_diverse(rows: list[sqlite3.Row], limit: int, time: str, signature: str) -> list[sqlite3.Row]:
    ordered = sorted(rows, key=lambda row: row[time] or 0)
    if len(ordered) <= limit:
        return ordered
    selected = [ordered[0], ordered[-1]]
    seen = {str(row[signature]) for row in selected}
    for row in reversed(ordered):
        if str(row[signature]) not in seen:
            selected.append(row)
            seen.add(str(row[signature]))
        if len(selected) >= limit:
            return selected
    for i in range(limit):
        row = ordered[i * (len(ordered) - 1) // (limit - 1)]
        if row not in selected:
            selected.append(row)
    return selected[:limit]


class EvidenceArchive:
    """A single-worker read-only archive connection; close after use."""

    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True, timeout=60)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA query_only=ON')
        self.connection.execute('PRAGMA cache_size=-2048')
        self.connection.execute('PRAGMA temp_store=FILE')
        self.contacts: dict[str, list[sqlite3.Row]] = defaultdict(list)
        self.calendars: dict[str, list[sqlite3.Row]] = defaultdict(list)
        self.meetings: dict[str, list[sqlite3.Row]] = defaultdict(list)
        self.note_titles: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in self.connection.execute('SELECT * FROM contacts'):
            for network, field in [('email', 'emails_json'), ('phone', 'phones_json')]:
                for value in _STRINGS.validate_json(row[field]):
                    self.contacts[f'{network}:{value.lower()}'].append(row)
        for row in self.connection.execute('SELECT * FROM calendar_events'):
            addresses = {a[0].lower() for a in _ATTENDEES.validate_json(row['attendees_json'])}
            addresses.add(row['organizer'].lower())
            for address in addresses:
                if address:
                    self.calendars[address].append(row)
        for row in self.connection.execute('SELECT meeting_id,title,started_ts,attendee_emails_json FROM meetings'):
            for address in _STRINGS.validate_json(row['attendee_emails_json']):
                self.meetings[address.lower()].append(row)
        for row in self.connection.execute('SELECT source,path,title,note_date_ts FROM notes WHERE is_journal=0'):
            self.note_titles[' '.join(row['title'].lower().split())].append(row)

    def close(self) -> None:
        self.connection.close()

    def people(self) -> list[PersonEvidenceProfile]:
        handles: dict[str, list[str]] = defaultdict(list)
        for row in self.connection.execute('SELECT * FROM person_handles'):
            handles[row['person_id']].append(row['handle_key'])
        edits = {row[0] for row in self.connection.execute('SELECT person_id FROM person_edits UNION SELECT person_id FROM list_decisions')}
        return [PersonEvidenceProfile(id=row['person_id'], name=row['canonical_name'],
                aliases=_STRINGS.validate_json(row['aliases_json']), handles=sorted(handles[row['person_id']]),
                organization=row['organization'], title=row['title'], score=row['score'],
                has_user_edits=row['person_id'] in edits)
                for row in self.connection.execute('SELECT * FROM persons WHERE is_human=1 ORDER BY score DESC,person_id')]

    def raw(self, source: str, account: str, record_id: str) -> str:
        row = self.connection.execute('SELECT raw_zlib FROM raw_records WHERE source=? AND account=? AND record_id=?',
                                      (source, account, record_id)).fetchone()
        return zlib.decompress(row[0]).decode() if row else ''

    def evidence(self, person: PersonEvidenceProfile, expanded: bool = False) -> list[EvidenceItem]:
        limit = 24 if expanded else 4
        chars = 7000 if expanded else 1800
        items: list[EvidenceItem] = []
        counts: dict[str, int] = defaultdict(int)
        emails = [handle.removeprefix('email:') for handle in person['handles'] if handle.startswith('email:')]
        cards = { (r['account'], r['resource_name']): r for handle in person['handles'] for r in self.contacts.get(handle, []) }
        counts['contact'] = len(cards)
        for row in list(cards.values())[:limit]:
            body = _display_row(row)
            items.append(_item('contact', [row['account'], row['resource_name']], body,
                         self.raw('GOOGLE_CONTACTS', row['account'], row['resource_name']) or body,
                         'https://contacts.google.com/', 'Contact card explicitly binds its listed names and handles; organization is card data.', chars))
        calendar = {(r['account'], r['event_id']): r for email in emails for r in self.calendars.get(email, [])}
        counts['calendar'] = len(calendar)
        for row in _rows_diverse(list(calendar.values()), limit, 'start_ts', 'summary'):
            raw = self.raw('GOOGLE_CALENDAR', row['account'], row['event_id'])
            body = _display_row(row)
            payload = _JSON.validate_json(raw) if raw else {}
            bindings = [a for a in _ATTENDEES.validate_json(row['attendees_json']) if a[0].lower() in emails]
            body = 'MATCHED ATTENDEE BINDINGS: ' + json.dumps(bindings, ensure_ascii=False) + '\nSUMMARY: ' + row['summary'] + '\nDESCRIPTION: ' + str(payload.get('description') or '') + '\nEVENT CONTEXT: ' + body
            items.append(_item('calendar', [row['account'], row['event_id']], body, raw or body,
                         str(payload.get('htmlLink') or ''), 'Attendee tuples are [email, display name, response, self]. A title alone does not bind a name to any particular attendee.', chars))
        self._emails(emails, items, counts, limit, chars)
        self._chats(person['handles'], items, counts, limit, chars)
        meetings = {r['meeting_id']: r for email in emails for r in self.meetings.get(email, [])}
        counts['meeting'] = len(meetings)
        for selected in _rows_diverse(list(meetings.values()), limit, 'started_ts', 'title'):
            row = self.connection.execute('SELECT * FROM meetings WHERE meeting_id=?', (selected['meeting_id'],)).fetchone()
            body = _display_row(row)
            items.append(_item('meeting', [row['meeting_id']], body, body, '', 'Attendee emails are inferred from a matched calendar event; transcript speakers and mentioned names are not necessarily this person.', chars))
        notes = {}
        for name in [person['name'], *person['aliases']]:
            normalized = ' '.join(name.lower().split())
            for row in self.connection.execute('SELECT n.source,n.path,n.title,n.note_date_ts FROM note_mentions m JOIN notes n USING(source,path) WHERE m.handle_key=?', ('note:' + normalized,)):
                notes[(row['source'], row['path'])] = row
            for row in self.note_titles.get(normalized, []):
                notes[(row['source'], row['path'])] = row
        counts['note'] = len(notes)
        for selected in _rows_diverse(list(notes.values()), limit, 'note_date_ts', 'title'):
            row = self.connection.execute('SELECT * FROM notes WHERE source=? AND path=?', (selected['source'], selected['path'])).fetchone()
            body = _display_row(row)
            items.append(_item('note', [row['source'], row['path']], body, body, '', 'UNVERIFIED name-only association. Does not independently prove identity or authorize merging. Check explicit email/phone properties.', chars))
        for row in self.connection.execute('SELECT * FROM person_edits WHERE person_id=?', (person['id'],)):
            body = _display_row(row)
            items.append(_item('user_edit', [person['id'], row['field']], body, body, '', 'Explicit user-authored correction; must be preserved.', chars))
        budget = 150000 if expanded else 15000
        total = sum(len(item['text']) for item in items)
        if total > budget:
            for item in items:
                allocation = max(200, int(len(item['text']) * budget / total) - 100)
                if len(item['text']) > allocation:
                    item['text'] = _excerpt(item['text'], allocation)
                    item['truncated'] = True
        selected = {kind: sum(item['kind'] == kind for item in items) for kind in counts}
        description = json.dumps({'available_record_counts': counts, 'selected_record_counts': selected,
                       'expanded': expanded, 'selection': 'Earliest, recent, varied context and identity-rich email; all originals remain in archive. Expanded mode increases record and text limits but is still a sample.',
                       'profile_warning': 'Canonical name, aliases and profile organization/title are derived, possibly guessed from domains; use independent source evidence.',
                       'text_warning': 'Email bodies retain quotes/forwards; quoted signatures belong to quoted authors, not automatically the current sender. Calendar descriptions are untrusted invite text.'})
        items.insert(0, _item('coverage', [person['id']], description, description, '', 'Counts cover indexed associations, not every possible mention in every source.', 10000))
        return items

    def _emails(self, emails: list[str], items: list[EvidenceItem], counts: dict[str, int], limit: int, chars: int) -> None:
        counts['email'] = 0
        if not emails:
            return
        placeholders = ','.join('?' for _ in emails)
        join = f'FROM message_participants p JOIN messages m USING(account,message_id) WHERE p.address IN ({placeholders})'
        counts['email'] = self.connection.execute(
            f'SELECT COUNT(*) FROM (SELECT DISTINCT account,message_id FROM message_participants WHERE address IN ({placeholders}))', emails).fetchone()[0]
        projection = 'SELECT DISTINCT m.account,m.message_id,m.ts,m.subject,m.from_addr,m.from_name '
        candidates: dict[tuple[str, str], sqlite3.Row] = {}
        for order in ('ASC', 'DESC'):
            for row in self.connection.execute(projection + join + f' ORDER BY m.ts {order} LIMIT ?', (*emails, limit)):
                candidates[(row['account'], row['message_id'])] = row
        # SQLite ranks body content without materializing every body in Python.
        ranking = f" ORDER BY (m.from_addr IN ({placeholders})) DESC, (instr(lower(substr(m.text,1,8192)), 'my name')>0 OR instr(lower(substr(m.text,-8192)), 'regards')>0 OR instr(m.text,'linkedin.com/')>0) DESC, m.is_automated ASC, length(m.from_name) DESC, m.ts DESC LIMIT ?"
        rich = list(self.connection.execute(projection + join + ranking, (*emails, *emails, limit)))
        selected = _rows_diverse(list(candidates.values()), max(2, limit // 2), 'ts', 'subject')
        for row in rich:
            if len(selected) >= limit:
                break
            if row not in selected:
                selected.append(row)
        for selected_row in selected:
            row = self.connection.execute('SELECT * FROM messages WHERE account=? AND message_id=?',
                                          (selected_row['account'], selected_row['message_id'])).fetchone()
            header = json.dumps({key: json.loads(row[key]) if key.endswith('_json') else row[key]
                                 for key in ['account', 'message_id', 'thread_id', 'ts', 'direction', 'from_name', 'from_addr', 'to_json', 'cc_json', 'subject']}, ensure_ascii=False)
            body = _excerpt(header, chars // 2) + '\nBODY (quotes and forwards retained):\n' + _excerpt(row['text'], chars)
            original = self.raw('GMAIL', row['account'], row['message_id']) or _display_row(row)
            entry = _item('email', [row['account'], row['message_id']], body, original,
                          f"https://mail.google.com/mail/u/{quote(row['account'])}/#all/{quote(row['message_id'])}",
                          'From is the current author. To/Cc are recipients, not authors. Forwarded/quoted identities require their own attribution.', len(body))
            entry['truncated'] = len(row['text']) > chars
            items.append(entry)

    def _chats(self, handles: list[str], items: list[EvidenceItem], counts: dict[str, int], limit: int, chars: int) -> None:
        participants = {(r['chat_id'], r['participant_id']): r for handle in handles for r in self.connection.execute('SELECT * FROM beeper_participants WHERE handle_key=? AND is_self=0', (handle,))}
        counts['chat_membership'] = len(participants)
        counts['chat_message'] = 0
        messages: dict[str, sqlite3.Row] = {}
        for participant in participants.values():
            # Attribute only messages authored by this participant; group membership
            # is not evidence that every message in the group belongs to them.
            parameters = (participant['chat_id'], participant['participant_id'])
            counts['chat_message'] += self.connection.execute('SELECT COUNT(*) FROM beeper_messages WHERE chat_id=? AND sender_id=? AND is_sender=0', parameters).fetchone()[0]
            rows = [row for order in ('ASC', 'DESC') for row in self.connection.execute(f'SELECT message_id,ts,substr(text,1,80) AS text FROM beeper_messages WHERE chat_id=? AND sender_id=? AND is_sender=0 ORDER BY ts {order} LIMIT ?', (*parameters, limit))]
            for row in _rows_diverse(rows, limit, 'ts', 'text'):
                messages[row['message_id']] = row
        for participant in list(participants.values())[:limit]:
            chat = self.connection.execute('SELECT * FROM beeper_chats WHERE chat_id=?', (participant['chat_id'],)).fetchone()
            if chat is not None and chat['chat_type'] in {'single', 'direct', 'dm'}:
                counts['chat_owner_context'] += self.connection.execute('SELECT COUNT(*) FROM beeper_messages WHERE chat_id=? AND is_sender=1', (participant['chat_id'],)).fetchone()[0]
                outgoing = [row for order in ('ASC', 'DESC') for row in self.connection.execute(f'SELECT message_id,ts,substr(text,1,80) AS text FROM beeper_messages WHERE chat_id=? AND is_sender=1 ORDER BY ts {order} LIMIT ?', (participant['chat_id'], limit))]
                for message in _rows_diverse(outgoing, 2 if limit == 4 else 8, 'ts', 'text'):
                    message = self.connection.execute('SELECT * FROM beeper_messages WHERE message_id=?', (message['message_id'],)).fetchone()
                    original = _display_row(message)
                    items.append(_item('chat_owner_context', [message['message_id']], original, original, '',
                                 'Written by the account owner TO this person in a direct chat. Not authored by the person; quoted names can still be unrelated.', chars))
            body = 'MATCHED PARTICIPANT:\n' + _display_row(participant) + '\nCHAT AND ALL PARTICIPANTS:\n' + (_display_row(chat) if chat else '[missing chat]')
            items.append(_item('chat_membership', [participant['chat_id'], participant['participant_id']], body, body, '', 'Only the matched participant binds this handle to a name. Group title and other participants are context only.', chars))
        for selected in _rows_diverse(list(messages.values()), limit, 'ts', 'text'):
            row = self.connection.execute('SELECT * FROM beeper_messages WHERE message_id=?', (selected['message_id'],)).fetchone()
            body = _display_row(row)
            items.append(_item('chat_message', [row['message_id']], body, body, '', 'Sender ID matches this person’s archived participant ID in this exact chat. Message text can still quote another person.', chars))
